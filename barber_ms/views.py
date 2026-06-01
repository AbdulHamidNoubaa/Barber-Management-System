from __future__ import annotations

import datetime
import json
from decimal import Decimal

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Max, Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from accounts.models import BarberProfile, User, UserRole
from barber_ms.pagination import (
    BARBER_TX_PER_PAGE,
    QUEUE_PER_PAGE,
    RECEIPTS_PER_PAGE,
    TREASURY_PER_PAGE,
    VIP_COMPLETED_PER_PAGE,
    paginate_queryset,
    querystring_excluding_page,
)
from barber_ms.ticket_actions import apply_ticket_edit, can_modify_ticket, delete_ticket_record
from barber_ms.forms import (
    AdminCreateForm,
    BarberCommissionForm,
    BarberCreateForm,
    BarberStandaloneForm,
    CashierCreateForm,
    ExpenseCategoryForm,
    QueueTicketForm,
    TicketEditForm,
    TreasuryEntryForm,
    UserEditForm,
)
from core.barber_activity import (
    barber_combined_stats,
    barber_vip_share,
    vip_bookings_for_barber,
)
from core.barber_close import (
    barber_day_summary,
    close_barber_account,
    month_barber_summary,
    month_grand_total,
)
from core.models import (
    CloseLedger,
    CloseType,
    Customer,
    ExpenseCategory,
    Payment,
    PaymentMethod,
    Service,
    ServiceCategory,
    Shift,
    ShiftTemplate,
    SystemSetting,
    Ticket,
    TicketItem,
    TicketStatus,
    TreasuryEntry,
    TreasuryEntryType,
    VIPBooking,
    VIPBarberPayout,
)
from core.customer_utils import get_or_create_customer
from core.receipt_utils import complete_ticket_sale
from core.shift_utils import get_open_shift, open_shift, require_open_shift, shift_ui_context


def _is_admin(user):
    return user.role == UserRole.ADMIN or user.is_superuser


def _is_cashier_or_admin(user):
    return user.role in (UserRole.ADMIN, UserRole.CASHIER) or user.is_superuser


# _barber_login_enabled function removed.


def _open_shift_request(request) -> bool:
    """فتح شفت اليوم (واحد فقط)."""
    if not _is_cashier_or_admin(request.user):
        messages.error(request, "ليس لديك صلاحية فتح الشفت.")
        return False
    try:
        shift = open_shift(opened_by=request.user)
        messages.success(request, f"تم فتح {shift.name}")
        return True
    except ValidationError as exc:
        messages.error(request, str(exc))
        return False


# ─── Auth ──────────────────────────────────────────────────


def root_redirect(request):
    if request.user.is_authenticated:
        return redirect("frontend:dashboard")
    return redirect("login")


def login_view(request):
    if request.user.is_authenticated:
        return redirect("frontend:dashboard")

    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        password = request.POST.get("password", "")
        user = authenticate(request, username=username, password=password)
        if user is not None:
            login(request, user)
            if user.role == UserRole.BARBER:
                logout(request)
                messages.warning(
                    request,
                    "دخول الحلاقين غير متاح. النظام مخصص للإدارة والكاشير فقط.",
                )
                return redirect("login")
            return redirect("frontend:dashboard")
        messages.error(request, "بيانات الدخول غير صحيحة.")
    return render(request, "frontend/login.html")


def logout_view(request):
    logout(request)
    return redirect("login")


# ─── Dashboard (Admin + Cashier) ──────────────────────────


def _close_current_shift(request):
    """Close current open shift with proper transaction handling.
    
    Returns:
        bool: True if shift was closed successfully, False otherwise
    """
    from django.db import transaction
    
    try:
        with transaction.atomic():
            # Use select_for_update to prevent race conditions
            shift = (
                Shift.objects.select_for_update()
                .filter(is_closed=False, ended_at__isnull=True)
                .order_by("-started_at")
                .first()
            )
            if not shift:
                messages.error(request, "لا يوجد شفت مفتوح حالياً.")
                return False
            
            now = timezone.now()
            
            # Calculate totals from completed tickets
            completed_tickets = Ticket.objects.filter(
                shift=shift, status=TicketStatus.COMPLETED
            )
            total_revenue = (
                completed_tickets.aggregate(v=Sum("total"))["v"] or Decimal("0")
            )
            total_commission = (
                completed_tickets.aggregate(v=Sum("barber_commission_total"))["v"]
                or Decimal("0")
            )
            
            # Calculate payment totals
            payments_qs = Payment.objects.filter(ticket__shift=shift)
            total_cash = (
                payments_qs.filter(method=PaymentMethod.CASH)
                .aggregate(v=Sum("amount"))["v"]
                or Decimal("0")
            )
            total_card = (
                payments_qs.filter(method=PaymentMethod.CARD)
                .aggregate(v=Sum("amount"))["v"]
                or Decimal("0")
            )
            
            # Create close ledger entry
            ledger = CloseLedger.objects.create(
                close_type=CloseType.SHIFT,
                shift=shift,
                closed_by=request.user,
                total_revenue=total_revenue,
                total_cash=total_cash,
                total_card=total_card,
                total_barber_commission=total_commission,
                note=request.POST.get("note", ""),
            )
            
            # Lock completed tickets with the close ledger
            completed_tickets.update(locked_by_close=ledger)
            
            # Close the shift
            shift.is_closed = True
            shift.ended_at = now
            shift.closed_by = request.user
            shift.save(update_fields=["is_closed", "ended_at", "closed_by", "updated_at"])
            
            shift_name = shift.name or "الشفت"
            messages.success(
                request,
                f"تم إغلاق {shift_name} بنجاح. الإيراد: {total_revenue}",
            )
            return True
            
    except Exception as e:
        messages.error(request, f"حدث خطأ عند إغلاق الشفت: {str(e)}")
        return False


@login_required
def dashboard(request):
    if request.user.role == UserRole.BARBER:
        logout(request)
        messages.warning(request, "النظام مخصص للإدارة والكاشير فقط.")
        return redirect("login")
    if not _is_cashier_or_admin(request.user):
        logout(request)
        return redirect("login")

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "close_shift":
            current = (
                Shift.objects.filter(is_closed=False, ended_at__isnull=True)
                .order_by("-started_at")
                .first()
            )
            if current and current.can_close(request.user):
                _close_current_shift(request)
            elif current:
                messages.error(request, "ليس لديك صلاحية إغلاق هذا الشفت.")
            else:
                messages.error(request, "لا يوجد شفت مفتوح حالياً.")
            return redirect("frontend:dashboard")
        elif action == "open_shift":
            _open_shift_request(request)
            return redirect("frontend:dashboard")

    today = timezone.localdate()
    shift_ctx = shift_ui_context(request.user)
    current_shift = shift_ctx["current_shift"]

    if current_shift:
        shift_tickets = Ticket.objects.filter(shift=current_shift)
    else:
        shift_tickets = Ticket.objects.none()

    completed_shift = shift_tickets.filter(status=TicketStatus.COMPLETED)
    recent_sales = (
        completed_shift.select_related("customer", "barber__user", "service")
        .prefetch_related("items__service", "receipts")
        .order_by("-completed_at", "-id")[:20]
    )
    revenue = completed_shift.aggregate(v=Sum("total"))["v"] or Decimal("0")
    commission = (
        completed_shift.aggregate(v=Sum("barber_commission_total"))["v"]
        or Decimal("0")
    )
    shift_receipts_count = completed_shift.count()
    customers_count = completed_shift.values("customer_id").distinct().count()
    avg_ticket = (
        revenue / Decimal(shift_receipts_count) if shift_receipts_count else Decimal("0")
    )

    barber_breakdown_raw = list(
        completed_shift.values("barber_id")
        .annotate(
            tickets=Count("id"),
            revenue=Sum("total"),
            commission=Sum("barber_commission_total"),
        )
        .order_by("-revenue")
    )
    _bb_ids = [r["barber_id"] for r in barber_breakdown_raw if r.get("barber_id")]
    _bb_map = {
        b.pk: b
        for b in BarberProfile.objects.filter(pk__in=_bb_ids).select_related("user")
    }
    barber_breakdown = []
    for row in barber_breakdown_raw:
        bp = _bb_map.get(row["barber_id"])
        if not bp:
            continue
        barber_breakdown.append(
            {
                "barber": bp,
                "tickets": row["tickets"] or 0,
                "revenue": row["revenue"] or Decimal("0"),
                "commission": row["commission"] or Decimal("0"),
            }
        )

    cash_from_tickets = (
        completed_shift.filter(payment_method=PaymentMethod.CASH)
        .aggregate(v=Sum("total"))["v"]
        or Decimal("0")
    )
    card_from_tickets = (
        completed_shift.filter(payment_method=PaymentMethod.CARD)
        .aggregate(v=Sum("total"))["v"]
        or Decimal("0")
    )
    if current_shift:
        pay_qs = Payment.objects.filter(ticket__shift=current_shift)
        cash_from_payments = (
            pay_qs.filter(method=PaymentMethod.CASH).aggregate(v=Sum("amount"))["v"]
            or Decimal("0")
        )
        card_from_payments = (
            pay_qs.filter(method=PaymentMethod.CARD).aggregate(v=Sum("amount"))["v"]
            or Decimal("0")
        )
        cash_total = max(cash_from_tickets, cash_from_payments)
        card_total = max(card_from_tickets, card_from_payments)
    else:
        cash_total = cash_from_tickets
        card_total = card_from_tickets

    barbers_active = BarberProfile.objects.filter(is_active=True).count()
    can_close = shift_ctx["can_close_shift"]

    all_completed = Ticket.objects.filter(status=TicketStatus.COMPLETED)
    all_revenue = all_completed.aggregate(v=Sum("total"))["v"] or Decimal("0")
    all_commission = all_completed.aggregate(v=Sum("barber_commission_total"))["v"] or Decimal("0")
    all_customers = Ticket.objects.values("customer_id").distinct().count()
    all_pay = Payment.objects.all()
    all_cash = all_pay.filter(method=PaymentMethod.CASH).aggregate(v=Sum("amount"))["v"] or Decimal("0")
    all_card = all_pay.filter(method=PaymentMethod.CARD).aggregate(v=Sum("amount"))["v"] or Decimal("0")

    context = {
        "recent_sales": recent_sales,
        "shift_receipts_count": shift_receipts_count,
        "avg_ticket": avg_ticket,
        "barber_breakdown": barber_breakdown,
        "revenue": revenue,
        "customers_count": customers_count,
        "cash_total": cash_total,
        "card_total": card_total,
        "barbers_active": barbers_active,
        "commission": commission,
        "net_profit": revenue - commission,
        "current_shift": current_shift,
        "can_close": can_close,
        "can_close_shift": can_close,
        "next_shift_label": shift_ctx["next_shift_label"],
        "all_revenue": all_revenue,
        "all_commission": all_commission,
        "all_net_profit": all_revenue - all_commission,
        "all_customers": all_customers,
        "all_cash": all_cash,
        "all_card": all_card,
    }
    return render(request, "frontend/dashboard.html", context)


# ─── Queue (Admin + Cashier) ──────────────────────────────


def _handle_ticket_edit_delete(request, redirect_to):
    """معالجة تعديل/حذف تذكرة طابور (POST)."""
    action = request.POST.get("action")
    if action not in ("edit_ticket", "delete_ticket"):
        return None
    ticket_id = request.POST.get("ticket_id")
    if not ticket_id:
        messages.error(request, "معرّف المعاملة غير صالح.")
        return redirect_to

    try:
        ticket = Ticket.objects.select_related("customer", "barber", "shift").get(pk=ticket_id)
    except Ticket.DoesNotExist:
        messages.error(request, "لم يتم العثور على المعاملة.")
        return redirect_to

    allowed, err = can_modify_ticket(ticket, request.user)
    if not allowed:
        messages.error(request, err)
        return redirect_to

    if action == "edit_ticket":
        form = TicketEditForm(ticket, request.POST)
        if form.is_valid():
            apply_ticket_edit(ticket, form.cleaned_data)
            messages.success(request, f"تم تحديث معاملة #{ticket_id}.")
        else:
            for field_errors in form.errors.values():
                for err_msg in field_errors:
                    messages.error(request, err_msg)
        return redirect_to

    if action == "delete_ticket":
        try:
            delete_ticket_record(ticket)
            messages.success(request, f"تم حذف المعاملة #{ticket_id}.")
        except Exception as exc:
            messages.error(request, f"تعذر الحذف: {exc}")
    return redirect_to


def _redirect_queue(request, anchor: str = ""):
    """إعادة التوجيه للطابور مع الحفاظ على معاملات التصفية."""
    url = reverse("frontend:queue")
    qs = (request.POST.get("_return_qs") or request.GET.urlencode()).strip()
    base = f"{url}?{qs}" if qs else url
    if anchor:
        base = f"{base}#{anchor}" if "#" not in base else base
    return redirect(base)


def _redirect_transactions_log(request):
    """إعادة التوجيه لصفحة سجل المعاملات مع معاملات التصفية."""
    url = reverse("frontend:transactions_log")
    qs = (request.POST.get("_return_qs") or request.GET.urlencode()).strip()
    return redirect(f"{url}?{qs}" if qs else url)


def _queue_filters(request) -> dict:
    return {
        "q": (request.GET.get("q") or "").strip(),
        "barber": (request.GET.get("barber") or "").strip(),
        "status": (request.GET.get("status") or "").strip(),
        "period": (request.GET.get("period") or "today").strip(),
        "from": (request.GET.get("from") or "").strip(),
        "to": (request.GET.get("to") or "").strip(),
        "pay": (request.GET.get("pay") or "").strip(),
        "shift_only": request.GET.get("shift_only") == "1",
    }


def _apply_ticket_filters(qs, flt: dict, *, open_shift=None):
    if flt["q"]:
        qs = qs.filter(
            Q(customer__name__icontains=flt["q"])
            | Q(customer__phone__icontains=flt["q"])
            | Q(description__icontains=flt["q"])
        )
    if flt["barber"].isdigit():
        qs = qs.filter(barber_id=int(flt["barber"]))
    if flt["status"] in dict(TicketStatus.choices):
        qs = qs.filter(status=flt["status"])
    if flt["pay"] in (PaymentMethod.CASH, PaymentMethod.CARD):
        qs = qs.filter(payment_method=flt["pay"])
    if flt["period"] == "today":
        qs = qs.filter(created_at__date=timezone.localdate())
    elif flt["period"] == "custom":
        if flt["from"]:
            qs = qs.filter(created_at__date__gte=flt["from"])
        if flt["to"]:
            qs = qs.filter(created_at__date__lte=flt["to"])
    if flt["shift_only"] and open_shift:
        qs = qs.filter(shift=open_shift)
    return qs


def _queue_stats(qs):
    completed = qs.filter(status=TicketStatus.COMPLETED)
    return {
        "total": qs.count(),
        "waiting": qs.filter(status=TicketStatus.WAITING).count(),
        "in_progress": qs.filter(status=TicketStatus.IN_PROGRESS).count(),
        "completed": completed.count(),
        "cancelled": qs.filter(status=TicketStatus.CANCELLED).count(),
        "revenue": completed.aggregate(v=Sum("total"))["v"] or Decimal("0"),
        "commission": completed.aggregate(v=Sum("barber_commission_total"))["v"] or Decimal("0"),
    }


@login_required
def queue_view(request):
    if not _is_cashier_or_admin(request.user):
        messages.error(request, "ليس لديك صلاحية الوصول لهذه الصفحة.")
    if request.user.role == UserRole.BARBER:
        logout(request)
        messages.warning(request, "الدخول متاح للإدارة والكاشير فقط.")
        return redirect("login")

    if request.method == "POST":
        action = request.POST.get("action")

        if action == "open_shift":
            _open_shift_request(request)
            return _redirect_queue(request)
        if action == "close_shift":
            current = get_open_shift()
            if current and current.can_close(request.user):
                _close_current_shift(request)
            elif current:
                messages.error(request, "ليس لديك صلاحية إغلاق هذا الشفت.")
            else:
                messages.error(request, "لا يوجد شفت مفتوح حالياً.")
            return _redirect_queue(request)

        if action == "create_ticket":
            form = QueueTicketForm(request.POST)
            if form.is_valid():
                services = form.cleaned_data["service_ids"]
                amount = form.cleaned_data["initial_amount"]
                method = form.cleaned_data.get("payment_method", PaymentMethod.CASH)
                label = " + ".join(s.name for s in services)[:120]
                customer, _ = get_or_create_customer()
                try:
                    shift = require_open_shift()
                except ValidationError as exc:
                    messages.error(request, str(exc))
                    return _redirect_queue(request, anchor="pos")
                ticket = Ticket.objects.create(
                    customer=customer,
                    barber=form.cleaned_data["barber_id"],
                    shift=shift,
                    service=services[0],
                    status=TicketStatus.WAITING,
                    description=label,
                    payment_method=method,
                )
                for svc in services:
                    TicketItem.objects.create(
                        ticket=ticket,
                        service=svc,
                        price=svc.base_price or Decimal("0"),
                    )
                ticket.recalc_totals()
                amount = ticket.total
                receipt = complete_ticket_sale(
                    ticket, amount=amount, method=method, user=request.user
                )
                messages.success(request, "تم إصدار الوصل — جاري الطباعة.")
                return redirect(
                    f"{reverse('frontend:vip:receipt_print', args=[receipt.id])}?auto_print=1"
                )
            for _field, errors in form.errors.items():
                for err in errors:
                    messages.error(request, err)
            return _redirect_queue(request, anchor="pos")

        elif action in ("edit_ticket", "delete_ticket"):
            return _handle_ticket_edit_delete(request, _redirect_queue(request))

    form = QueueTicketForm()
    flt = _queue_filters(request)
    shift_ctx = shift_ui_context(request.user)
    current_shift = shift_ctx["current_shift"]

    base_qs = Ticket.objects.select_related("customer", "barber__user").prefetch_related(
        "receipts"
    )
    filtered_qs = _apply_ticket_filters(base_qs, flt, open_shift=current_shift)

    recent_completed = (
        base_qs.filter(status=TicketStatus.COMPLETED)
        .select_related("service", "barber")
        .prefetch_related("items__service")
        .order_by("-completed_at")[:10]
    )

    active_barbers = BarberProfile.objects.filter(is_active=True).select_related("user").order_by(
        "name"
    )
    stats = _queue_stats(filtered_qs)

    return render(
        request,
        "frontend/queue.html",
        {
            "form": form,
            "recent_completed": recent_completed,
            "pos_services": Service.objects.filter(is_active=True).order_by("name"),
            "filters": flt,
            "queue_stats": stats,
            "history_count": stats["total"],
            "active_barbers": active_barbers,
            **shift_ctx,
            "ticket_status_choices": TicketStatus.choices,
            "ticket_status_labels": {
                TicketStatus.WAITING: "انتظار",
                TicketStatus.IN_PROGRESS: "قيد التنفيذ",
                TicketStatus.COMPLETED: "مكتمل",
                TicketStatus.CANCELLED: "ملغى",
            },
            "return_qs": request.GET.urlencode(),
            "services_json": json.dumps(
                [
                    {"id": s.id, "name": s.name, "price": str(s.base_price or "0")}
                    for s in Service.objects.filter(is_active=True).order_by("name")
                ],
                ensure_ascii=False,
            ),
            "is_admin": _is_admin(request.user),
        },
    )


@login_required
def transactions_log_view(request):
    """سجل المعاملات — صفحة مستقلة."""
    if not _is_cashier_or_admin(request.user):
        messages.error(request, "ليس لديك صلاحية الوصول لهذه الصفحة.")
        return redirect("frontend:dashboard")
    if request.user.role == UserRole.BARBER:
        logout(request)
        messages.warning(request, "الدخول متاح للإدارة والكاشير فقط.")
        return redirect("login")

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "open_shift":
            _open_shift_request(request)
            return _redirect_transactions_log(request)
        if action == "close_shift":
            current = get_open_shift()
            if current and current.can_close(request.user):
                _close_current_shift(request)
            elif current:
                messages.error(request, "ليس لديك صلاحية إغلاق هذا الشفت.")
            else:
                messages.error(request, "لا يوجد شفت مفتوح حالياً.")
            return _redirect_transactions_log(request)

        handled = _handle_ticket_edit_delete(request, _redirect_transactions_log(request))
        if handled is not None:
            return handled

    flt = _queue_filters(request)
    shift_ctx = shift_ui_context(request.user)
    current_shift = shift_ctx["current_shift"]
    base_qs = Ticket.objects.select_related("customer", "barber__user").prefetch_related(
        "receipts"
    )
    filtered_qs = _apply_ticket_filters(base_qs, flt, open_shift=current_shift)
    history_qs = filtered_qs.order_by("-created_at")
    paginator, recent_page = paginate_queryset(
        request, history_qs, per_page=QUEUE_PER_PAGE
    )
    active_barbers = BarberProfile.objects.filter(is_active=True).select_related("user").order_by(
        "name"
    )

    return render(
        request,
        "frontend/transactions_log.html",
        {
            "recent_page": recent_page,
            "paginator": paginator,
            "query_string": querystring_excluding_page(request),
            "filters": flt,
            "queue_stats": _queue_stats(filtered_qs),
            "active_barbers": active_barbers,
            **shift_ctx,
            "ticket_status_choices": TicketStatus.choices,
            "return_qs": request.GET.urlencode(),
            "pos_services": Service.objects.filter(is_active=True).order_by("name"),
            "ticket_edit_form": TicketEditForm(),
            "is_admin": _is_admin(request.user),
        },
    )


def _parse_filter_dates(request):
    today = timezone.localdate()
    from_raw = (request.GET.get("from") or "").strip()
    to_raw = (request.GET.get("to") or "").strip()
    period = (request.GET.get("period") or "today").strip()
    from_date = None
    to_date = None
    if period == "today":
        from_date = to_date = today
    elif period == "month":
        from_date = today.replace(day=1)
        to_date = today
    elif period == "all":
        pass
    elif period == "custom":
        if from_raw:
            from_date = datetime.date.fromisoformat(from_raw[:10])
        if to_raw:
            to_date = datetime.date.fromisoformat(to_raw[:10])
    return from_date, to_date, period


@login_required
def barbers_list_view(request):
    if not _is_cashier_or_admin(request.user):
        messages.error(request, "ليس لديك صلاحية الوصول.")
        return redirect("frontend:dashboard")

    from_date, to_date, period = _parse_filter_dates(request)
    barbers = BarberProfile.objects.select_related("user").order_by("-is_active", "name")
    rows = []
    for bp in barbers:
        rows.append({"barber": bp, "stats": barber_combined_stats(bp, from_date, to_date)})

    return render(
        request,
        "frontend/barbers_list.html",
        {
            "barber_rows": rows,
            "filter_from": from_date,
            "filter_to": to_date,
            "filter_period": period,
        },
    )


@login_required
def barber_operations_view(request, barber_id: int):
    if not _is_cashier_or_admin(request.user):
        messages.error(request, "ليس لديك صلاحية الوصول.")
        return redirect("frontend:dashboard")

    barber = get_object_or_404(BarberProfile.objects.select_related("user"), pk=barber_id)

    if request.method == "POST":
        action = request.POST.get("action")
        if action in ("edit_ticket", "delete_ticket"):
            qs = request.GET.urlencode()

            def _back(req):
                base = reverse("frontend:barber_operations", args=[barber_id])
                return redirect(f"{base}?{qs}" if qs else base)

            return _handle_ticket_edit_delete(request, _back(request))

    from_date, to_date, period = _parse_filter_dates(request)
    tickets_qs = (
        Ticket.objects.filter(barber=barber)
        .select_related("customer", "barber__user", "service")
        .prefetch_related("receipts", "items__service")
        .order_by("-completed_at", "-created_at")
    )
    if from_date:
        tickets_qs = tickets_qs.filter(completed_at__date__gte=from_date)
    if to_date:
        tickets_qs = tickets_qs.filter(completed_at__date__lte=to_date)
    status = (request.GET.get("status") or "COMPLETED").strip()
    if status == "ALL":
        pass
    elif status in dict(TicketStatus.choices):
        tickets_qs = tickets_qs.filter(status=status)
    else:
        tickets_qs = tickets_qs.filter(status=TicketStatus.COMPLETED)
        status = TicketStatus.COMPLETED

    summary = barber_combined_stats(barber, from_date, to_date)
    paginator, tickets_page = paginate_queryset(
        request, tickets_qs, per_page=BARBER_TX_PER_PAGE
    )
    vip_qs = vip_bookings_for_barber(
        barber, from_date=from_date, to_date=to_date, status=status if status != "ALL" else None
    )
    vip_paginator, vip_bookings_page = paginate_queryset(
        request, vip_qs, per_page=BARBER_TX_PER_PAGE, page_param="vpage"
    )
    vip_rows = []
    for booking in vip_bookings_page.object_list:
        rev, comm = barber_vip_share(booking, barber)
        vip_rows.append({"booking": booking, "share": rev, "commission": comm})

    return render(
        request,
        "frontend/barber_operations.html",
        {
            "barber": barber,
            "tickets_page": tickets_page,
            "paginator": paginator,
            "vip_rows": vip_rows,
            "vip_page": vip_bookings_page,
            "vip_paginator": vip_paginator,
            "vip_query_string": querystring_excluding_page(request, page_param="vpage"),
            "query_string": querystring_excluding_page(request),
            "filter_status": status or "COMPLETED",
            "filter_from": from_date,
            "filter_to": to_date,
            "filter_period": period,
            "summary": summary,
            "ticket_status_choices": TicketStatus.choices,
            "is_admin": _is_admin(request.user),
            "active_barbers": BarberProfile.objects.filter(is_active=True).order_by("name"),
            "pos_services": Service.objects.filter(is_active=True).order_by("name"),
        },
    )


@login_required
def queue_barber_transactions(request):
    """توافق قديم: يحوّل إلى صفحات الحلاقين الجديدة."""
    raw_id = request.GET.get("barber")
    if raw_id and str(raw_id).isdigit():
        qs = request.GET.urlencode()
        base = reverse("frontend:barber_operations", args=[int(raw_id)])
        return redirect(f"{base}?{qs}" if qs else base)
    return redirect("frontend:barbers_list")


# ─── Barber Screen (Barber only) ──────────────────────────


# Barber views removed as per user request.



# ─── Barber Activity Log (Admin only) ─────────────────────


@login_required
def barber_log_view(request):
    if not _is_cashier_or_admin(request.user):
        messages.error(request, "ليس لديك صلاحية الوصول.")
        return redirect("frontend:dashboard")

    if request.method == "POST":
        action = request.POST.get("action")
        if action in ("edit_ticket", "delete_ticket"):
            from django.urls import reverse
            from urllib.parse import urlencode

            q = {}
            for key in ("barber", "from", "to", "page"):
                v = request.POST.get(f"_ret_{key}") or request.GET.get(key)
                if v:
                    q[key] = v
            base = reverse("frontend:barber_log")
            dest = f"{base}?{urlencode(q)}" if q else base

            return _handle_ticket_edit_delete(request, redirect(dest))

    barbers = BarberProfile.objects.select_related("user").filter(is_active=True).order_by(
        "user__first_name", "user__username"
    )

    selected_barber_id = request.GET.get("barber")
    from_raw = request.GET.get("from")
    to_raw = request.GET.get("to")
    today = timezone.localdate()
    from_d, to_d = None, None
    if from_raw:
        try:
            from_d = datetime.date.fromisoformat(str(from_raw)[:10])
        except ValueError:
            pass
    if to_raw:
        try:
            to_d = datetime.date.fromisoformat(str(to_raw)[:10])
        except ValueError:
            pass
    if not from_d and not to_d:
        from_d = to_d = today

    qs = Ticket.objects.select_related("customer", "barber__user").order_by("-created_at")

    if selected_barber_id:
        qs = qs.filter(barber_id=selected_barber_id)
    if from_d:
        qs = qs.filter(created_at__date__gte=from_d)
    if to_d:
        qs = qs.filter(created_at__date__lte=to_d)

    paginator, tickets_page = paginate_queryset(request, qs, per_page=20)

    vip_paginator = None
    vip_log_page = None
    vip_log_rows = []
    if selected_barber_id:
        bp = BarberProfile.objects.filter(pk=selected_barber_id).first()
        if bp:
            vip_qs = vip_bookings_for_barber(bp, from_date=from_d, to_date=to_d)
            vip_paginator, vip_bookings_page = paginate_queryset(
                request, vip_qs, per_page=20, page_param="vpage"
            )
            for booking in vip_bookings_page.object_list:
                rev, comm = barber_vip_share(booking, bp)
                vip_log_rows.append({"booking": booking, "share": rev, "commission": comm})
            vip_log_page = vip_bookings_page

    barber_stats_raw = (
        qs.values("barber_id")
        .annotate(
            total_tickets=Count("id"),
            completed_tickets=Count("id", filter=Q(status=TicketStatus.COMPLETED)),
            cancelled_tickets=Count("id", filter=Q(status=TicketStatus.CANCELLED)),
            total_revenue=Sum("total", filter=Q(status=TicketStatus.COMPLETED)),
            total_commission=Sum(
                "barber_commission_total", filter=Q(status=TicketStatus.COMPLETED)
            ),
        )
        .order_by("-total_revenue")
    )
    _bp_ids = [r["barber_id"] for r in barber_stats_raw if r.get("barber_id")]
    _bp_map = {b.pk: b for b in BarberProfile.objects.filter(pk__in=_bp_ids)}
    barber_stats = []
    for row in barber_stats_raw:
        bp = _bp_map.get(row["barber_id"])
        barber_stats.append(
            {
                **row,
                "display_name": bp.display_name if bp else "",
                "initial_letter": bp.initial_letter if bp else "?",
            }
        )

    return render(
        request,
        "frontend/barber_log.html",
        {
            "barbers": barbers,
            "tickets_page": tickets_page,
            "paginator": paginator,
            "query_string": querystring_excluding_page(request),
            "barber_stats": barber_stats,
            "selected_barber_id": selected_barber_id,
            "vip_log_rows": vip_log_rows,
            "vip_log_page": vip_log_page,
            "vip_log_paginator": vip_paginator,
            "vip_log_query_string": querystring_excluding_page(request, page_param="vpage"),
            "filter_from": from_d,
            "filter_to": to_d,
            "ticket_edit_form": TicketEditForm(),
            "is_admin": True,
            "active_barbers": barbers,
        },
    )


def _treasury_filter_redirect(request) -> str:
    """بعد POST نعيد نفس نطاق التصفية (من/إلى/نوع)."""
    from urllib.parse import urlencode

    from django.urls import reverse

    src = request.POST if request.method == "POST" else request.GET
    q = {}
    if src.get("from"):
        q["from"] = src["from"].strip()
    if src.get("to"):
        q["to"] = src["to"].strip()
    if src.get("kind"):
        q["kind"] = src["kind"].strip()
    base = reverse("frontend:treasury")
    return f"{base}?{urlencode(q)}" if q else base


@login_required
def treasury_view(request):
    """خزنة المحل: مصروفات وإيداعات مع تصنيف ومراجعة."""
    if not _is_cashier_or_admin(request.user):
        messages.error(request, "الخزنة متاحة للكاشير والمدير فقط.")
        return redirect("frontend:dashboard")

    today = timezone.localdate()
    from_date = request.GET.get("from", "").strip()
    to_date = request.GET.get("to", "").strip()
    kind = request.GET.get("kind", "").strip()

    def apply_date_filters(qs):
        if from_date:
            qs = qs.filter(created_at__date__gte=from_date)
        if to_date:
            qs = qs.filter(created_at__date__lte=to_date)
        if not from_date and not to_date:
            qs = qs.filter(created_at__date=today)
        return qs

    barber_rev = (
        apply_date_filters(Ticket.objects.filter(status="COMPLETED")).aggregate(v=Sum("total"))["v"]
        or Decimal("0")
    )
    vip_rev = (
        apply_date_filters(VIPBooking.objects.filter(status="completed")).aggregate(v=Sum("paid_amount"))["v"]
        or Decimal("0")
    )
    total_revenue = barber_rev + vip_rev

    summary_base = apply_date_filters(TreasuryEntry.objects.filter(is_voided=False))
    total_expense = (
        summary_base.filter(entry_type=TreasuryEntryType.EXPENSE).aggregate(v=Sum("amount"))["v"]
        or Decimal("0")
    )
    total_deposit = (
        summary_base.filter(entry_type=TreasuryEntryType.DEPOSIT).aggregate(v=Sum("amount"))["v"]
        or Decimal("0")
    )
    net_movement = total_revenue + total_deposit - total_expense

    list_qs = apply_date_filters(
        TreasuryEntry.objects.select_related(
            "category", "recorded_by", "shift", "voided_by"
        )
    )
    if kind == "expense":
        list_qs = list_qs.filter(entry_type=TreasuryEntryType.EXPENSE)
    elif kind == "deposit":
        list_qs = list_qs.filter(entry_type=TreasuryEntryType.DEPOSIT)

    entries_qs = list_qs.order_by("-created_at")
    paginator, entries_page = paginate_queryset(
        request, entries_qs, per_page=TREASURY_PER_PAGE
    )

    entry_form = TreasuryEntryForm()
    category_form = ExpenseCategoryForm()

    if request.method == "POST":
        action = request.POST.get("action")

        if action == "add_entry":
            entry_form = TreasuryEntryForm(request.POST)
            if entry_form.is_valid():
                shift = (
                    Shift.objects.filter(is_closed=False, ended_at__isnull=True)
                    .order_by("-started_at")
                    .first()
                )
                entry = TreasuryEntry(
                    entry_type=entry_form.cleaned_data["entry_type"],
                    amount=entry_form.cleaned_data["amount"],
                    payment_method=entry_form.cleaned_data["payment_method"],
                    category=entry_form.cleaned_data.get("category_id"),
                    description=(entry_form.cleaned_data.get("description") or "").strip(),
                    shift=shift,
                    recorded_by=request.user,
                )
                entry.full_clean()
                entry.save()
                messages.success(request, "تم تسجيل الحركة في الخزنة.")
                return redirect(_treasury_filter_redirect(request))

        elif action == "void_entry" and _is_admin(request.user):
            eid = request.POST.get("entry_id")
            te = TreasuryEntry.objects.filter(pk=eid, is_voided=False).first()
            if te:
                te.is_voided = True
                te.voided_at = timezone.now()
                te.voided_by = request.user
                te.save(update_fields=["is_voided", "voided_at", "voided_by", "updated_at"])
                messages.success(request, "تم إلغاء تسجيل الحركة (لا تُحسب في الملخص).")
            else:
                messages.error(request, "تعذر إلغاء الحركة.")
            return redirect(_treasury_filter_redirect(request))

        elif action == "add_category" and _is_admin(request.user):
            category_form = ExpenseCategoryForm(request.POST)
            if category_form.is_valid():
                name = category_form.cleaned_data["name"].strip()
                if ExpenseCategory.objects.filter(name__iexact=name).exists():
                    messages.error(request, "يوجد تصنيف بنفس الاسم.")
                else:
                    mx = ExpenseCategory.objects.aggregate(v=Max("sort_order"))["v"]
                    next_order = (mx or 0) + 1
                    ExpenseCategory.objects.create(name=name[:80], sort_order=next_order, is_active=True)
                    messages.success(request, "تم إضافة التصنيف.")
                    category_form = ExpenseCategoryForm()
            return redirect(_treasury_filter_redirect(request))

        elif action == "edit_category" and _is_admin(request.user):
            cat = ExpenseCategory.objects.filter(pk=request.POST.get("category_id")).first()
            name = (request.POST.get("category_name") or "").strip()
            if not cat or not name:
                messages.error(request, "اسم التصنيف مطلوب.")
            elif ExpenseCategory.objects.filter(name__iexact=name).exclude(pk=cat.pk).exists():
                messages.error(request, "يوجد تصنيف بنفس الاسم.")
            else:
                cat.name = name[:80]
                cat.save(update_fields=["name", "updated_at"])
                messages.success(request, "تم تحديث التصنيف.")
            return redirect(_treasury_filter_redirect(request))

        elif action == "delete_category" and _is_admin(request.user):
            cat = ExpenseCategory.objects.filter(pk=request.POST.get("category_id")).first()
            if cat:
                label = cat.name
                if cat.treasury_entries.exists():
                    cat.is_active = False
                    cat.save(update_fields=["is_active", "updated_at"])
                    messages.success(
                        request,
                        f"تم إيقاف «{label}» (مستخدم في مصروفات سابقة — يبقى في السجل).",
                    )
                else:
                    cat.delete()
                    messages.success(request, f"تم حذف التصنيف: {label}")
            else:
                messages.error(request, "التصنيف غير موجود.")
            return redirect(_treasury_filter_redirect(request))

    categories = ExpenseCategory.objects.filter(is_active=True).order_by("sort_order", "name")

    return render(
        request,
        "frontend/treasury.html",
        {
            "entries_page": entries_page,
            "paginator": paginator,
            "query_string": querystring_excluding_page(request),
            "summary": {
                "total_revenue": total_revenue,
                "total_expense": total_expense,
                "total_deposit": total_deposit,
                "net_movement": net_movement,
            },
            "filter_from": from_date,
            "filter_to": to_date,
            "filter_kind": kind,
            "entry_form": entry_form,
            "category_form": category_form,
            "categories": categories,
        },
    )


# ─── Settings (Admin only) ────────────────────────────────


@login_required
def settings_view(request):
    if not _is_admin(request.user):
        messages.error(request, "الإعدادات للمدير فقط.")
        if request.user.role == UserRole.BARBER:
            return redirect("frontend:barber")
        return redirect("frontend:dashboard")

    admin_form = AdminCreateForm()
    cashier_form = CashierCreateForm()
    barber_form = BarberCreateForm()
    barber_standalone_form = BarberStandaloneForm()

    if request.method == "POST":
        action = request.POST.get("action")

        if action == "save_branding":
            import os
            from django.conf import settings as dj_settings

            for key in ("business_name", "business_phone", "business_address", "currency", "theme"):
                val = request.POST.get(key, "").strip()
                if val:
                    SystemSetting.objects.update_or_create(key=key, defaults={"value": val})
            bl_val = "1" if request.POST.get("barber_login_enabled") == "on" else "0"
            SystemSetting.objects.update_or_create(
                key="barber_login_enabled", defaults={"value": bl_val}
            )
            logo_file = request.FILES.get("logo")
            if logo_file:
                logos_dir = dj_settings.MEDIA_ROOT / "logos"
                os.makedirs(logos_dir, exist_ok=True)
                ext = os.path.splitext(logo_file.name)[1] or ".png"
                dest = logos_dir / f"logo{ext}"
                with open(dest, "wb+") as f:
                    for chunk in logo_file.chunks():
                        f.write(chunk)
                SystemSetting.objects.update_or_create(
                    key="logo", defaults={"value": f"logos/logo{ext}"}
                )
            messages.success(request, "تم حفظ إعدادات المحل.")
            return redirect("frontend:settings")

        elif action == "create_admin":
            admin_form = AdminCreateForm(request.POST)
            if admin_form.is_valid():
                admin_form.save()
                messages.success(request, "تم إنشاء مدير النظام بنجاح.")
                return redirect("frontend:settings")

        elif action == "create_cashier":
            cashier_form = CashierCreateForm(request.POST)
            if cashier_form.is_valid():
                cashier_form.save()
                messages.success(request, "تم إنشاء الكاشير بنجاح.")
                return redirect("frontend:settings")

        elif action == "create_barber":
            barber_form = BarberCreateForm(request.POST)
            if barber_form.is_valid():
                barber_form.save()
                messages.success(request, "تم إنشاء الحلاق بنجاح.")
                return redirect("frontend:settings")

        elif action == "create_barber_standalone":
            barber_standalone_form = BarberStandaloneForm(request.POST)
            if barber_standalone_form.is_valid():
                BarberProfile.objects.create(
                    user=None,
                    name=barber_standalone_form.cleaned_data["name"].strip()[:120],
                    default_commission_pct=barber_standalone_form.cleaned_data[
                        "default_commission_pct"
                    ],
                    is_active=True,
                )
                messages.success(request, "تم إضافة الحلاق (بدون حساب دخول).")
                return redirect("frontend:settings")
            messages.error(request, "تحقق من اسم الحلاق والعمولة.")

        elif action == "update_barber":
            barber_id = request.POST.get("barber_id")
            bp = BarberProfile.objects.filter(id=barber_id).first()
            if bp:
                b_form = BarberCommissionForm(request.POST, instance=bp)
                if b_form.is_valid():
                    b_form.save()
                    pct = b_form.cleaned_data["default_commission_pct"]
                    messages.success(
                        request,
                        f"تم تحديث {bp.display_name}: العمولة {pct}% — "
                        f"{'نشط' if b_form.cleaned_data['is_active'] else 'موقوف'}.",
                    )
                    return redirect(reverse("frontend:settings") + "#sec-barbers")
                messages.error(request, "بيانات الحلاق غير صحيحة — تحقق من الاسم والنسبة (0–100).")
            else:
                messages.error(request, "الحلاق غير موجود.")
            return redirect(reverse("frontend:settings") + "#sec-barbers")

        elif action == "toggle_user":
            user_id = request.POST.get("user_id")
            target = User.objects.filter(pk=user_id).first()
            if target and target.pk != request.user.pk:
                target.is_active = not target.is_active
                target.save(update_fields=["is_active"])
                status_text = "تفعيل" if target.is_active else "تعطيل"
                messages.success(request, f"تم {status_text} المستخدم {target.username}.")
            else:
                messages.error(request, "لا يمكن تعديل هذا المستخدم.")
            return redirect("frontend:settings")

        elif action == "edit_user":
            user_id = request.POST.get("user_id")
            target = User.objects.filter(pk=user_id).first()
            if target:
                edit_form = UserEditForm(request.POST, instance=target)
                if edit_form.is_valid():
                    edit_form.save()
                    messages.success(request, f"تم تحديث بيانات {target.username}.")
                else:
                    messages.error(request, "بيانات غير صحيحة.")
            else:
                messages.error(request, "المستخدم غير موجود.")
            return redirect("frontend:settings")

        elif action == "close_shift":
            current = get_open_shift()
            if current and current.can_close(request.user):
                _close_current_shift(request)
            elif current:
                messages.error(request, "ليس لديك صلاحية إغلاق هذا الشفت.")
            else:
                messages.error(request, "لا يوجد شفت مفتوح حالياً.")
            return redirect(reverse("frontend:settings") + "#panel-shift")

        elif action == "open_shift":
            _open_shift_request(request)
            return redirect(reverse("frontend:settings") + "#panel-shift")

        elif action == "add_shift_template":
            tpl_name = request.POST.get("tpl_name", "").strip()
            tpl_desc = request.POST.get("tpl_desc", "").strip()
            tpl_start = request.POST.get("tpl_start", "").strip()
            tpl_end = request.POST.get("tpl_end", "").strip()
            if tpl_name:
                tpl, created = ShiftTemplate.objects.get_or_create(
                    name=tpl_name,
                    defaults={"description": tpl_desc},
                )
                if tpl_start:
                    tpl.start_time = tpl_start
                if tpl_end:
                    tpl.end_time = tpl_end
                if tpl_desc and not created:
                    tpl.description = tpl_desc
                tpl.save()
                messages.success(request, f"تم إضافة نوع الشفت: {tpl_name}")
            else:
                messages.error(request, "اسم الشفت مطلوب.")
            return redirect("frontend:settings")

        elif action == "delete_shift_template":
            tpl_id = request.POST.get("tpl_id")
            ShiftTemplate.objects.filter(pk=tpl_id).delete()
            messages.success(request, "تم حذف نوع الشفت.")
            return redirect("frontend:settings")

        elif action == "toggle_shift_template":
            tpl_id = request.POST.get("tpl_id")
            tpl = ShiftTemplate.objects.filter(pk=tpl_id).first()
            if tpl:
                tpl.is_active = not tpl.is_active
                tpl.save(update_fields=["is_active", "updated_at"])
                messages.success(request, f"تم تحديث حالة الشفت: {tpl.name}")
            return redirect("frontend:settings")

        elif action == "update_shift_cashiers":
            tpl_id = request.POST.get("tpl_id")
            tpl = ShiftTemplate.objects.filter(pk=tpl_id).first()
            if tpl:
                cashier_ids = request.POST.getlist("cashier_ids")
                tpl.default_cashiers.set(cashier_ids)
                messages.success(request, f"تم تحديث موظفي شفت: {tpl.name}")
            return redirect("frontend:settings")

        elif action == "add_service_category":
            name = (request.POST.get("category_name") or "").strip()
            if name:
                ServiceCategory.objects.create(
                    name=name,
                    sort_order=ServiceCategory.objects.count(),
                    is_active=True,
                )
                messages.success(request, f"تمت إضافة التصنيف: {name}")
            else:
                messages.error(request, "أدخل اسم التصنيف.")
            return redirect(reverse("frontend:settings") + "#sec-services")

        elif action == "edit_service_category":
            cat = ServiceCategory.objects.filter(pk=request.POST.get("category_id")).first()
            name = (request.POST.get("category_name") or "").strip()
            if cat and name:
                cat.name = name
                cat.save(update_fields=["name", "updated_at"])
                messages.success(request, "تم تحديث التصنيف.")
            else:
                messages.error(request, "اسم التصنيف مطلوب.")
            return redirect(reverse("frontend:settings") + "#sec-services")

        elif action == "delete_service_category":
            cat = ServiceCategory.objects.filter(pk=request.POST.get("category_id")).first()
            if cat:
                label = cat.name
                cat.delete()
                messages.success(request, f"تم حذف التصنيف: {label}")
            return redirect(reverse("frontend:settings") + "#sec-services")

        elif action == "edit_service":
            svc = Service.objects.filter(pk=request.POST.get("service_id")).first()
            svc_name = (request.POST.get("service_name") or "").strip()
            raw_price = (request.POST.get("service_price") or "").strip()
            if not svc or not svc_name:
                messages.error(request, "بيانات الخدمة غير صحيحة.")
            else:
                try:
                    price = Decimal(raw_price.replace(",", ".")) if raw_price else svc.base_price
                except Exception:
                    price = svc.base_price
                svc.name = svc_name
                svc.base_price = price
                svc.save(update_fields=["name", "base_price", "updated_at"])
                messages.success(request, "تم تحديث الخدمة.")
            return redirect(reverse("frontend:settings") + "#sec-services")

        elif action == "delete_service":
            svc = Service.objects.filter(pk=request.POST.get("service_id")).first()
            if svc:
                label = svc.name
                svc.delete()
                messages.success(request, f"تم حذف الخدمة: {label}")
            return redirect(reverse("frontend:settings") + "#sec-services")

        elif action == "add_service":
            svc_name = (request.POST.get("service_name") or "").strip()
            raw_price = (request.POST.get("service_price") or "0").strip()
            if not svc_name:
                messages.error(request, "أدخل اسم الخدمة.")
            else:
                try:
                    price = Decimal(raw_price.replace(",", "."))
                except Exception:
                    price = Decimal("0")
                Service.objects.create(
                    name=svc_name,
                    base_price=price,
                    is_active=True,
                )
                messages.success(request, f"تمت إضافة الخدمة: {svc_name}")
            return redirect(reverse("frontend:settings") + "#sec-services")

        elif action == "toggle_service_category":
            cat = ServiceCategory.objects.filter(pk=request.POST.get("category_id")).first()
            if cat:
                cat.is_active = not cat.is_active
                cat.save(update_fields=["is_active", "updated_at"])
            return redirect(reverse("frontend:settings") + "#sec-services")

        elif action == "toggle_service":
            svc = Service.objects.filter(pk=request.POST.get("service_id")).first()
            if svc:
                svc.is_active = not svc.is_active
                svc.save(update_fields=["is_active", "updated_at"])
            return redirect(reverse("frontend:settings") + "#sec-services")

    open_form = ""
    if admin_form.errors:
        open_form = "admin"
    elif cashier_form.errors:
        open_form = "cashier"
    elif barber_form.errors:
        open_form = "barber"
    elif barber_standalone_form.errors:
        open_form = "barber_standalone"

    users = User.objects.order_by("-date_joined")
    barbers = BarberProfile.objects.select_related("user").order_by("name")
    shift_templates = ShiftTemplate.objects.prefetch_related("default_cashiers").all().order_by("start_time", "name")
    settings_cashiers = User.objects.filter(
        role__in=[UserRole.CASHIER, UserRole.ADMIN], is_active=True
    ).order_by("first_name", "username")

    shift_ctx = shift_ui_context(request.user)
    can_close_shift = shift_ctx["can_close_shift"]
    barbers_active_count = BarberProfile.objects.filter(is_active=True).count()
    users_active_count = users.filter(is_active=True).count()

    return render(
        request,
        "frontend/settings.html",
        {
            "admin_form": admin_form,
            "cashier_form": cashier_form,
            "barber_form": barber_form,
            "barber_standalone_form": barber_standalone_form,
            "users": users,
            "barbers": barbers,
            "barbers_active_count": barbers_active_count,
            "users_active_count": users_active_count,
            "open_form": open_form,
            "shift_templates": shift_templates,
            "current_shift": shift_ctx["current_shift"],
            "settings_cashiers": settings_cashiers,
            "can_close_shift": can_close_shift,
            "next_shift_label": shift_ctx["next_shift_label"],
            "user_role_choices": UserRole.choices,
            "all_services": Service.objects.order_by("name"),
        },
    )


# ─── إغلاق يومي / شهري ───────────────────────────────────


@login_required
def daily_close_view(request):
    if not _is_cashier_or_admin(request.user):
        messages.error(request, "ليس لديك صلاحية الوصول.")
        return redirect("frontend:dashboard")

    close_date = timezone.localdate()
    raw = request.GET.get("date") or request.POST.get("close_date")
    if raw:
        try:
            close_date = datetime.date.fromisoformat(str(raw)[:10])
        except ValueError:
            pass

    if request.method == "POST":
        action = request.POST.get("action")
        barber_id = request.POST.get("barber_id")
        if action == "close_one" and barber_id:
            bp = get_object_or_404(BarberProfile, pk=barber_id)
            try:
                close_barber_account(bp, close_date, request.user)
                messages.success(request, f"تم إغلاق حساب {bp.display_name} ليوم {close_date}.")
            except ValueError as e:
                messages.warning(request, str(e))
        elif action == "close_all" and _is_admin(request.user):
            closed = 0
            for bp in BarberProfile.objects.filter(is_active=True):
                try:
                    close_barber_account(bp, close_date, request.user)
                    closed += 1
                except ValueError:
                    pass
            messages.success(request, f"تم إغلاق {closed} حلاق/حلاقين ليوم {close_date}.")
        return redirect(f"{reverse('frontend:daily_close')}?date={close_date.isoformat()}")

    summaries = [barber_day_summary(bp, close_date) for bp in BarberProfile.objects.order_by("name")]
    totals = {
        "revenue": sum(s["total_revenue"] for s in summaries),
        "commission": sum(s["total_commission"] for s in summaries),
        "tickets": sum(s["ticket_count"] for s in summaries),
        "closed": sum(1 for s in summaries if s["is_closed"]),
    }

    return render(
        request,
        "frontend/daily_close.html",
        {
            "close_date": close_date,
            "summaries": summaries,
            "totals": totals,
            "is_admin": _is_admin(request.user),
        },
    )


@login_required
def monthly_close_view(request):
    if not _is_cashier_or_admin(request.user):
        messages.error(request, "ليس لديك صلاحية الوصول.")
        return redirect("frontend:dashboard")

    today = timezone.localdate()
    try:
        year = int(request.GET.get("year", today.year))
        month = int(request.GET.get("month", today.month))
    except (TypeError, ValueError):
        year, month = today.year, today.month

    rows = month_barber_summary(year, month)
    grand = month_grand_total(year, month)

    return render(
        request,
        "frontend/monthly_close.html",
        {
            "year": year,
            "month": month,
            "rows": rows,
            "grand": grand,
        },
    )
