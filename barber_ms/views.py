from __future__ import annotations

import datetime
from decimal import Decimal

from django.contrib import messages
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
from barber_ms.forms import (
    AdminCreateForm,
    BarberCommissionForm,
    BarberCreateForm,
    BarberStandaloneForm,
    CashierCreateForm,
    ExpenseCategoryForm,
    QueueTicketForm,
    TreasuryEntryForm,
    UserEditForm,
)
from core.models import (
    CloseLedger,
    CloseType,
    Customer,
    ExpenseCategory,
    Payment,
    PaymentMethod,
    Service,
    Shift,
    ShiftTemplate,
    SystemSetting,
    Ticket,
    TicketStatus,
    TreasuryEntry,
    TreasuryEntryType,
    VIPBooking,
    get_or_create_open_shift,
)


def _is_admin(user):
    return user.role == UserRole.ADMIN or user.is_superuser


def _is_cashier_or_admin(user):
    return user.role in (UserRole.ADMIN, UserRole.CASHIER) or user.is_superuser


# _barber_login_enabled function removed.


def _time_in_range(start, end, current):
    """Check if current time falls within [start, end), handling midnight crossing."""
    if start <= end:
        return start <= current < end
    else:
        return current >= start or current < end


def _auto_manage_shifts():
    """Auto open/close shifts based on ShiftTemplate time ranges.

    Called at the start of dashboard/queue views. Returns the current open shift (or None).
    """
    from django.db import transaction
    
    now = timezone.localtime()
    current_time = now.time()
    today = now.date()

    templates = ShiftTemplate.objects.filter(
        is_active=True, start_time__isnull=False, end_time__isnull=False
    ).prefetch_related("default_cashiers")

    matching_tpl = None
    for tpl in templates:
        if _time_in_range(tpl.start_time, tpl.end_time, current_time):
            matching_tpl = tpl
            break

    # Use select_for_update to prevent race conditions
    current_shift = (
        Shift.objects.select_for_update()
        .filter(is_closed=False, ended_at__isnull=True)
        .order_by("-started_at")
        .first()
    )

    if current_shift:
        shift_tpl = templates.filter(name=current_shift.name).first()
        if shift_tpl and shift_tpl.end_time:
            if not _time_in_range(shift_tpl.start_time, shift_tpl.end_time, current_time):
                with transaction.atomic():
                    # Refresh to get latest state
                    current_shift.refresh_from_db()
                    if not current_shift.is_closed:
                        completed_tickets = Ticket.objects.filter(
                            shift=current_shift, status=TicketStatus.COMPLETED
                        )
                        total_revenue = completed_tickets.aggregate(v=Sum("total"))["v"] or Decimal("0")
                        total_commission = completed_tickets.aggregate(v=Sum("barber_commission_total"))["v"] or Decimal("0")
                        payments_qs = Payment.objects.filter(ticket__shift=current_shift)
                        total_cash = payments_qs.filter(method=PaymentMethod.CASH).aggregate(v=Sum("amount"))["v"] or Decimal("0")
                        total_card = payments_qs.filter(method=PaymentMethod.CARD).aggregate(v=Sum("amount"))["v"] or Decimal("0")
                        
                        # Fix: closed_by is now required, set to None for auto-close (or use a system user if available)
                        ledger = CloseLedger.objects.create(
                            close_type=CloseType.SHIFT,
                            shift=current_shift,
                            closed_by=None,  # سيتم تعيينه لاحقاً أو استخدام user نظام
                            total_revenue=total_revenue,
                            total_cash=total_cash,
                            total_card=total_card,
                            total_barber_commission=total_commission,
                            note="إغلاق تلقائي — انتهاء وقت الشفت",
                        )
                        current_shift.is_closed = True
                        current_shift.ended_at = now
                        current_shift.closed_by = None  # سيتم تعيينه لاحقاً أو استخدام user نظام
                        current_shift.save(update_fields=["is_closed", "ended_at", "closed_by", "updated_at"])
                        current_shift = None

    # Auto-open shift if matching template exists and no shift is open
    if not current_shift and matching_tpl:
        with transaction.atomic():
            # Double-check that no shift was created by another request
            already_exists = Shift.objects.filter(
                name=matching_tpl.name, started_at__date=today, is_closed=False, ended_at__isnull=True
            ).exists()
            if not already_exists:
                new_shift = Shift.objects.create(name=matching_tpl.name)
                if matching_tpl.default_cashiers.exists():
                    new_shift.assigned_cashiers.set(matching_tpl.default_cashiers.all())
                current_shift = new_shift

    return current_shift


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
        elif action == "open_shift" and _is_admin(request.user):
            from django.db import transaction
            
            try:
                with transaction.atomic():
                    # Double-check that no shift is already open (with lock)
                    existing = (
                        Shift.objects.select_for_update()
                        .filter(is_closed=False, ended_at__isnull=True)
                        .first()
                    )
                    if existing:
                        messages.error(request, f"يوجد شفت مفتوح بالفعل: {existing.name or 'شفت حالي'}")
                    else:
                        shift_name = request.POST.get("shift_name", "").strip()
                        new_shift = Shift.objects.create(name=shift_name)
                        tpl = ShiftTemplate.objects.filter(name=shift_name).first()
                        if tpl and tpl.default_cashiers.exists():
                            new_shift.assigned_cashiers.set(tpl.default_cashiers.all())
                        messages.success(request, f"تم فتح شفت جديد: {shift_name or 'شفت'}")
            except Exception as e:
                messages.error(request, f"حدث خطأ عند فتح الشفت: {str(e)}")
            return redirect("frontend:dashboard")

    today = timezone.localdate()
    current_shift = _auto_manage_shifts()

    if current_shift:
        shift_tickets = Ticket.objects.filter(shift=current_shift)
    else:
        shift_tickets = Ticket.objects.none()

    waiting = (
        shift_tickets.filter(status=TicketStatus.WAITING)
        .select_related("customer", "barber__user")
        .order_by("created_at")[:15]
    )
    in_progress = (
        shift_tickets.filter(status=TicketStatus.IN_PROGRESS)
        .select_related("customer", "barber__user")
        .order_by("started_at")[:15]
    )
    completed = (
        shift_tickets.filter(status=TicketStatus.COMPLETED)
        .select_related("customer", "barber__user")
        .order_by("-completed_at")[:15]
    )
    completed_shift = shift_tickets.filter(status=TicketStatus.COMPLETED)
    revenue = completed_shift.aggregate(v=Sum("total"))["v"] or Decimal("0")
    commission = (
        completed_shift.aggregate(v=Sum("barber_commission_total"))["v"]
        or Decimal("0")
    )
    customers_count = shift_tickets.values("customer_id").distinct().count()

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
    shift_templates = ShiftTemplate.objects.filter(is_active=True)
    can_close = False
    if current_shift:
        can_close = current_shift.can_close(request.user)

    next_shift_tpl = None
    if not current_shift:
        now_time = timezone.localtime().time()
        upcoming = shift_templates.filter(
            start_time__isnull=False, end_time__isnull=False, start_time__gt=now_time
        ).order_by("start_time").first()
        if not upcoming:
            upcoming = shift_templates.filter(
                start_time__isnull=False, end_time__isnull=False
            ).order_by("start_time").first()
        next_shift_tpl = upcoming

    all_completed = Ticket.objects.filter(status=TicketStatus.COMPLETED)
    all_revenue = all_completed.aggregate(v=Sum("total"))["v"] or Decimal("0")
    all_commission = all_completed.aggregate(v=Sum("barber_commission_total"))["v"] or Decimal("0")
    all_customers = Ticket.objects.values("customer_id").distinct().count()
    all_pay = Payment.objects.all()
    all_cash = all_pay.filter(method=PaymentMethod.CASH).aggregate(v=Sum("amount"))["v"] or Decimal("0")
    all_card = all_pay.filter(method=PaymentMethod.CARD).aggregate(v=Sum("amount"))["v"] or Decimal("0")

    context = {
        "waiting": waiting,
        "in_progress": in_progress,
        "completed": completed,
        "revenue": revenue,
        "customers_count": customers_count,
        "cash_total": cash_total,
        "card_total": card_total,
        "barbers_active": barbers_active,
        "commission": commission,
        "net_profit": revenue - commission,
        "current_shift": current_shift,
        "shift_templates": shift_templates,
        "can_close": can_close,
        "next_shift_tpl": next_shift_tpl,
        "all_revenue": all_revenue,
        "all_commission": all_commission,
        "all_net_profit": all_revenue - all_commission,
        "all_customers": all_customers,
        "all_cash": all_cash,
        "all_card": all_card,
    }
    return render(request, "frontend/dashboard.html", context)


# ─── Queue (Admin + Cashier) ──────────────────────────────


def _redirect_queue(request, anchor: str = ""):
    """إعادة التوجيه للطابور مع الحفاظ على معاملات التصفية."""
    url = reverse("frontend:queue")
    qs = (request.POST.get("_return_qs") or request.GET.urlencode()).strip()
    base = f"{url}?{qs}" if qs else url
    if anchor:
        base = f"{base}#{anchor}" if "#" not in base else base
    return redirect(base)


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

    _auto_manage_shifts()

    if request.method == "POST":
        action = request.POST.get("action")

        if action == "create_ticket":
            form = QueueTicketForm(request.POST)
            if form.is_valid():
                customer, _ = Customer.objects.get_or_create(
                    name=form.cleaned_data["customer_name"],
                    phone=form.cleaned_data["customer_phone"],
                )
                shift = get_or_create_open_shift()
                ticket = Ticket.objects.create(
                    customer=customer,
                    barber=form.cleaned_data["barber_id"],
                    shift=shift,
                    status=TicketStatus.WAITING,
                    description=form.cleaned_data.get("description", ""),
                    payment_method=form.cleaned_data.get("payment_method", PaymentMethod.CASH),
                )
                init_amt = form.cleaned_data.get("initial_amount")
                if init_amt is not None and init_amt > 0:
                    ticket.total = init_amt
                    ticket.subtotal = init_amt
                    pct = ticket.barber.default_commission_pct or Decimal("0")
                    ticket.barber_commission_total = init_amt * pct / Decimal("100")
                    ticket.save(
                        update_fields=["total", "subtotal", "barber_commission_total", "updated_at"]
                    )
                messages.success(request, "تم إضافة الزبون إلى الطابور.")
                return _redirect_queue(request, anchor="pos")
            for field, errors in form.errors.items():
                for err in errors:
                    messages.error(request, err)

        elif action == "update_status":
            ticket_id = request.POST.get("ticket_id")
            new_status = request.POST.get("status")
            try:
                ticket = Ticket.objects.get(pk=ticket_id)
                ticket.set_status(new_status, by_user=request.user)
                messages.success(request, "تم تحديث حالة التذكرة.")
            except Ticket.DoesNotExist:
                messages.error(request, "لم يتم العثور على التذكرة.")
            return _redirect_queue(request)

        elif action == "edit_price":
            ticket_id = request.POST.get("ticket_id")
            raw_amount = request.POST.get("amount", "").strip()
            try:
                ticket = Ticket.objects.get(pk=ticket_id)
                if not raw_amount:
                    messages.error(request, "يرجى إدخال السعر.")
                    return _redirect_queue(request)
                pay_amount = Decimal(raw_amount)
                if pay_amount < 0:
                    messages.error(request, "السعر لا يمكن أن يكون سالباً.")
                    return _redirect_queue(request)
                ticket.total = pay_amount
                ticket.subtotal = pay_amount
                pct = ticket.barber.default_commission_pct or Decimal("0")
                ticket.barber_commission_total = pay_amount * pct / Decimal("100")
                ticket.save(
                    update_fields=["total", "subtotal", "barber_commission_total", "updated_at"]
                )
                messages.success(request, f"تم تعديل السعر إلى {pay_amount}.")
            except (Ticket.DoesNotExist, Exception):
                messages.error(request, "خطأ في تعديل السعر.")
            return _redirect_queue(request)

        elif action == "reassign_barber":
            ticket_id = request.POST.get("ticket_id")
            new_barber_id = request.POST.get("new_barber_id")
            try:
                ticket = Ticket.objects.get(pk=ticket_id)
                if ticket.status in (TicketStatus.COMPLETED, TicketStatus.CANCELLED):
                    messages.error(request, "لا يمكن نقل تذكرة مكتملة أو ملغاة.")
                    return _redirect_queue(request)
                new_barber = BarberProfile.objects.get(pk=new_barber_id, is_active=True)
                old_name = ticket.barber.display_name
                ticket.barber = new_barber
                pct = new_barber.default_commission_pct or Decimal("0")
                if ticket.total > 0:
                    ticket.barber_commission_total = ticket.total * pct / Decimal("100")
                ticket.save(
                    update_fields=["barber", "barber_commission_total", "updated_at"]
                )
                new_name = new_barber.display_name
                messages.success(
                    request,
                    f"تم نقل التذكرة من {old_name} إلى {new_name}.",
                )
            except Ticket.DoesNotExist:
                messages.error(request, "لم يتم العثور على التذكرة.")
            except BarberProfile.DoesNotExist:
                messages.error(request, "الحلاق المحدد غير موجود أو غير نشط.")
            return _redirect_queue(request)

        elif action == "complete_with_payment":
            ticket_id = request.POST.get("ticket_id")
            method = request.POST.get("method")
            raw_amount = request.POST.get("amount", "").strip()
            try:
                ticket = Ticket.objects.get(pk=ticket_id)
                if not method or method not in (PaymentMethod.CASH, PaymentMethod.CARD):
                    method = ticket.payment_method or PaymentMethod.CASH
                if not raw_amount:
                    messages.error(request, "يرجى إدخال المبلغ لإتمام الدفع.")
                    return _redirect_queue(request)
                pay_amount = Decimal(raw_amount)
                if pay_amount <= 0:
                    messages.error(request, "المبلغ يجب أن يكون أكبر من صفر.")
                    return _redirect_queue(request)
                ticket.total = pay_amount
                ticket.subtotal = pay_amount
                pct = ticket.barber.default_commission_pct or Decimal("0")
                ticket.barber_commission_total = pay_amount * pct / Decimal("100")
                ticket.save(
                    update_fields=["total", "subtotal", "barber_commission_total", "updated_at"]
                )
                Payment.objects.create(
                    ticket=ticket,
                    method=method,
                    amount=pay_amount,
                    received_by=request.user,
                )
                ticket.set_status(TicketStatus.COMPLETED, by_user=request.user)
                messages.success(request, "تم إنهاء الخدمة وتسجيل الدفع.")
            except Ticket.DoesNotExist:
                messages.error(request, "لم يتم العثور على التذكرة.")
            except Exception:
                messages.error(request, "خطأ في البيانات المدخلة.")
            return _redirect_queue(request)

    form = QueueTicketForm()
    flt = _queue_filters(request)
    open_shift = (
        Shift.objects.filter(is_closed=False, ended_at__isnull=True)
        .order_by("-started_at")
        .first()
    )

    base_qs = Ticket.objects.select_related("customer", "barber__user").prefetch_related(
        "receipts"
    )
    filtered_qs = _apply_ticket_filters(base_qs, flt, open_shift=open_shift)

    op_flt = {**flt, "status": ""}
    waiting = _apply_ticket_filters(
        base_qs.filter(status=TicketStatus.WAITING), op_flt, open_shift=open_shift
    ).order_by("queue_position", "created_at")
    in_progress = _apply_ticket_filters(
        base_qs.filter(status=TicketStatus.IN_PROGRESS), op_flt, open_shift=open_shift
    ).order_by("started_at", "created_at")

    history_qs = filtered_qs.order_by("-created_at")
    paginator, recent_page = paginate_queryset(
        request, history_qs, per_page=QUEUE_PER_PAGE
    )

    active_barbers = BarberProfile.objects.filter(is_active=True).select_related("user").order_by(
        "name"
    )

    return render(
        request,
        "frontend/queue.html",
        {
            "form": form,
            "waiting": waiting,
            "in_progress": in_progress,
            "recent_page": recent_page,
            "paginator": paginator,
            "query_string": querystring_excluding_page(request),
            "filters": flt,
            "queue_stats": _queue_stats(filtered_qs),
            "active_barbers": active_barbers,
            "current_shift": open_shift,
            "ticket_status_choices": TicketStatus.choices,
            "ticket_status_labels": {
                TicketStatus.WAITING: "انتظار",
                TicketStatus.IN_PROGRESS: "قيد التنفيذ",
                TicketStatus.COMPLETED: "مكتمل",
                TicketStatus.CANCELLED: "ملغى",
            },
            "return_qs": request.GET.urlencode(),
            "quick_services": Service.objects.filter(is_active=True).order_by("name")[:16],
        },
    )


@login_required
def queue_barber_transactions(request):
    if not _is_cashier_or_admin(request.user):
        messages.error(request, "ليس لديك صلاحية الوصول.")
        return redirect("frontend:barber")

    raw_id = request.GET.get("barber")
    if not raw_id or not str(raw_id).isdigit():
        messages.error(request, "يرجى تحديد الحلاق.")
        return redirect("frontend:queue")

    barber = get_object_or_404(BarberProfile, pk=int(raw_id), is_active=True)
    tickets_qs = (
        Ticket.objects.filter(barber=barber)
        .select_related("customer", "barber__user")
        .order_by("-created_at")
    )
    status = (request.GET.get("status") or "").strip()
    if status in dict(TicketStatus.choices):
        tickets_qs = tickets_qs.filter(status=status)
    paginator, tickets_page = paginate_queryset(
        request, tickets_qs, per_page=BARBER_TX_PER_PAGE
    )

    return render(
        request,
        "frontend/queue_barber_transactions.html",
        {
            "barber": barber,
            "tickets_page": tickets_page,
            "paginator": paginator,
            "query_string": querystring_excluding_page(request),
            "filter_status": status,
            "ticket_status_choices": TicketStatus.choices,
        },
    )


# ─── Barber Screen (Barber only) ──────────────────────────


# Barber views removed as per user request.



# ─── Barber Activity Log (Admin only) ─────────────────────


@login_required
def barber_log_view(request):
    if not _is_admin(request.user):
        messages.error(request, "سجل الحلاقين متاح للمدير فقط.")
        return redirect("frontend:dashboard")

    barbers = BarberProfile.objects.select_related("user").filter(is_active=True).order_by(
        "user__first_name", "user__username"
    )

    selected_barber_id = request.GET.get("barber")
    from_date = request.GET.get("from")
    to_date = request.GET.get("to")
    today = timezone.localdate()

    qs = Ticket.objects.select_related("customer", "barber__user").order_by("-created_at")

    if selected_barber_id:
        qs = qs.filter(barber_id=selected_barber_id)
    if from_date:
        qs = qs.filter(created_at__date__gte=from_date)
    if to_date:
        qs = qs.filter(created_at__date__lte=to_date)
    if not from_date and not to_date:
        qs = qs.filter(created_at__date=today)

    paginator, tickets_page = paginate_queryset(request, qs, per_page=20)

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
        },
    )


# ─── Reports (Admin only) ─────────────────────────────────


@login_required
def reports_view(request):
    if not _is_admin(request.user):
        messages.error(request, "التقارير متاحة للمدير فقط.")
        if request.user.role == UserRole.BARBER:
            return redirect("frontend:barber")
        return redirect("frontend:dashboard")

    today = timezone.localdate()
    from_date = request.GET.get("from")
    to_date = request.GET.get("to")
    qs = Ticket.objects.filter(status=TicketStatus.COMPLETED)
    if from_date:
        qs = qs.filter(completed_at__date__gte=from_date)
    if to_date:
        qs = qs.filter(completed_at__date__lte=to_date)
    if not from_date and not to_date:
        qs = qs.filter(completed_at__date=today)

    summary = {
        "revenue": qs.aggregate(v=Sum("total"))["v"] or Decimal("0"),
        "barber_commission": qs.aggregate(v=Sum("barber_commission_total"))["v"] or Decimal("0"),
        "tickets": qs.count(),
    }
    top_barbers_raw = (
        qs.values("barber_id")
        .annotate(revenue=Sum("total"), count=Count("id"))
        .order_by("-revenue")[:10]
    )
    _tb_ids = [r["barber_id"] for r in top_barbers_raw if r.get("barber_id")]
    _tb_map = {b.pk: b for b in BarberProfile.objects.filter(pk__in=_tb_ids)}
    top_barbers = []
    for row in top_barbers_raw:
        bp = _tb_map.get(row["barber_id"])
        top_barbers.append(
            {
                "barber_name": bp.display_name if bp else "",
                "count": row["count"],
                "revenue": row["revenue"],
            }
        )
    payments = Payment.objects.filter(ticket__in=qs).values("method").annotate(v=Sum("amount"))

    return render(
        request,
        "frontend/reports.html",
        {
            "summary": summary,
            "top_barbers": top_barbers,
            "payments": payments,
            "filter_from": from_date or "",
            "filter_to": to_date or "",
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
                    category=entry_form.cleaned_data.get("category"),
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
            _close_current_shift(request)
            return redirect("frontend:settings")

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
    current_shift = (
        Shift.objects.filter(is_closed=False, ended_at__isnull=True)
        .order_by("-started_at")
        .first()
    )
    settings_cashiers = User.objects.filter(
        role__in=[UserRole.CASHIER, UserRole.ADMIN], is_active=True
    ).order_by("first_name", "username")

    can_close_shift = (
        current_shift.can_close(request.user) if current_shift else False
    )
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
            "current_shift": current_shift,
            "settings_cashiers": settings_cashiers,
            "can_close_shift": can_close_shift,
            "user_role_choices": UserRole.choices,
        },
    )
