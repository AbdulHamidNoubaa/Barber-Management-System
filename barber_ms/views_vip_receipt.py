"""
Views لـ VIP Bookings و Receipts و Treasury Reports
"""

from decimal import Decimal
from datetime import datetime, timedelta

from django.db import transaction
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Sum, Q
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
from django.http import HttpResponse

from accounts.models import UserRole, BarberProfile
from barber_ms.forms import VIPBookingForm, ReceiptGenerationForm
from barber_ms.pagination import (
    RECEIPTS_PER_PAGE,
    VIP_COMPLETED_PER_PAGE,
    paginate_queryset,
    querystring_excluding_page,
)
from core.customer_utils import get_or_create_customer, resolve_customer_name
from core.models import (
    VIPBooking,
    VIPBookingType,
    VIPBarberPayout,
    Receipt,
    ReceiptType,
    TreasuryEntry,
    TreasuryEntryType,
    CloseLedger,
    CloseType,
    Payment,
    PaymentMethod,
    Ticket,
    Customer,
)


def _is_admin(user):
    return user.role == UserRole.ADMIN or user.is_superuser


def _is_cashier_or_admin(user):
    return user.role in (UserRole.ADMIN, UserRole.CASHIER) or user.is_superuser


# ─── VIP Booking Views ──────────────────────────────────────


@login_required
def vip_bookings_list(request):
    """قائمة حجوزات VIP"""
    if not _is_cashier_or_admin(request.user):
        messages.error(request, "ليس لديك صلاحية الوصول لهذه الصفحة.")
        return redirect("frontend:dashboard")
    
    # الحجوزات حسب الحالة
    pending = VIPBooking.objects.filter(status='pending').order_by('booking_date', 'booking_time')
    confirmed = VIPBooking.objects.filter(status='confirmed').order_by('booking_date', 'booking_time')
    in_progress = VIPBooking.objects.filter(status='in_progress').order_by('booking_date', 'booking_time')
    completed_qs = VIPBooking.objects.filter(status='completed').order_by('-booking_date', '-id')
    paginator, completed_page = paginate_queryset(
        request, completed_qs, per_page=VIP_COMPLETED_PER_PAGE, page_param='cpage'
    )
    
    context = {
        'pending': pending,
        'confirmed': confirmed,
        'in_progress': in_progress,
        'completed_page': completed_page,
        'completed_paginator': paginator,
        'completed_query_string': querystring_excluding_page(request, page_param='cpage'),
        'is_admin': _is_admin(request.user),
        'can_edit_vip': _is_cashier_or_admin(request.user),
    }
    return render(request, 'frontend/vip_bookings.html', context)


@login_required
def create_vip_booking(request):
    """إنشاء حجز VIP جديد"""
    if not _is_cashier_or_admin(request.user):
        messages.error(request, "ليس لديك صلاحية الوصول لهذه الصفحة.")
        return redirect("frontend:dashboard")
    
    if request.method == 'POST':
        form = VIPBookingForm(request.POST)
        if form.is_valid():
            try:
                customer, _ = get_or_create_customer(
                    name=form.cleaned_data.get("customer_name"),
                    phone=form.cleaned_data.get("customer_phone"),
                )
                
                # إنشاء الحجز
                booking = VIPBooking.objects.create(
                    customer=customer,
                    booking_type=form.cleaned_data['booking_type'],
                    booking_date=form.cleaned_data['booking_date'],
                    booking_time=form.cleaned_data['booking_time'],
                    barbers_count=form.cleaned_data['barbers_count'],
                    estimated_duration_hours=form.cleaned_data['estimated_duration_hours'],
                    base_price=form.cleaned_data['base_price'],
                    discount_pct=form.cleaned_data['discount_pct'],
                    final_price=form.cleaned_data['base_price'] - (
                        form.cleaned_data['base_price'] * form.cleaned_data['discount_pct'] / 100
                    ),
                    description=form.cleaned_data['description'],
                    special_requests=form.cleaned_data['special_requests'],
                    created_by=request.user,
                )
                assigned = form.cleaned_data.get("assigned_barber_ids")
                if assigned:
                    booking.assigned_barbers.set(assigned)
                
                # إنشاء وصل للحجز
                Receipt.objects.create(
                    receipt_type=ReceiptType.VIP_BOOKING,
                    receipt_number=Receipt.generate_receipt_number(),
                    vip_booking=booking,
                    customer_name=customer.name,
                    customer_phone=customer.phone,
                    amount=booking.final_price,
                    payment_method=PaymentMethod.CASH,
                    issued_by=request.user,
                    items_description=f"حجز VIP - {booking.get_booking_type_display()} - {booking.description}",
                )
                
                messages.success(request, f"تم إنشاء حجز VIP بنجاح: {booking}")
                return redirect('frontend:vip:vip_bookings_list')
            except Exception as e:
                messages.error(request, f"حدث خطأ: {str(e)}")
    else:
        form = VIPBookingForm()
    
    return render(request, 'frontend/create_vip_booking.html', {'form': form})


@login_required
def vip_booking_detail(request, booking_id):
    """تفاصيل حجز VIP"""
    booking = get_object_or_404(VIPBooking, id=booking_id)
    
    if request.method == 'POST':
        action = request.POST.get('action')
        
        if action == 'confirm' and _is_admin(request.user):
            booking.status = 'confirmed'
            booking.save()
            messages.success(request, "تم تأكيد الحجز.")
        
        elif action == 'start' and _is_admin(request.user):
            booking.status = 'in_progress'
            booking.save()
            messages.success(request, "تم بدء تنفيذ الحجز.")
        
        elif action == 'complete' and _is_admin(request.user):
            booking.status = 'completed'
            booking.save()
            messages.success(request, "تم إكمال الحجز.")
        
        elif action == 'cancel' and _is_admin(request.user):
            booking.status = 'cancelled'
            booking.save()
            messages.success(request, "تم إلغاء الحجز.")

        elif action == 'delete' and _is_admin(request.user):
            with transaction.atomic():
                Receipt.objects.filter(vip_booking=booking).delete()
                booking.delete()
            messages.success(request, "تم حذف حجز VIP نهائياً.")
            return redirect('frontend:vip:vip_bookings_list')
        
        elif action == 'pay':
            booking.paid_amount = booking.final_price
            booking.payment_method = request.POST.get('payment_method', PaymentMethod.CASH)
            booking.save()
            messages.success(request, "تم تسجيل الدفع.")

        elif action == 'split_payouts' and _is_admin(request.user):
            booking.barber_payouts.all().delete()
            barber_ids = request.POST.getlist('payout_barber_id')
            amounts = request.POST.getlist('payout_amount')
            created = 0
            for bid, amt in zip(barber_ids, amounts):
                if not bid or not str(amt).strip():
                    continue
                try:
                    amount = Decimal(str(amt).replace(',', '.'))
                except Exception:
                    continue
                if amount <= 0:
                    continue
                bp = BarberProfile.objects.filter(pk=bid).first()
                if bp:
                    VIPBarberPayout.objects.create(
                        vip_booking=booking,
                        barber=bp,
                        amount=amount,
                        recorded_by=request.user,
                    )
                    created += 1
            if created:
                messages.success(request, f"تم توزيع المدفوعات على {created} حلاق/حلاقين.")
            else:
                messages.warning(request, "لم يُسجَّل أي توزيع — تحقق من المبالغ.")

        return redirect('frontend:vip:vip_booking_detail', booking_id=booking_id)

    receipts = booking.receipts.all()
    assigned = booking.assigned_barbers.filter(is_active=True)
    payouts = booking.barber_payouts.select_related('barber').all()
    payout_total = payouts.aggregate(t=Sum('amount'))['t'] or Decimal('0')
    active_barbers = BarberProfile.objects.filter(is_active=True).order_by('name')
    context = {
        'booking': booking,
        'receipts': receipts,
        'assigned_barbers': assigned,
        'payouts': payouts,
        'payout_total': payout_total,
        'active_barbers': active_barbers,
        'is_admin': _is_admin(request.user),
        'can_edit_vip': _is_cashier_or_admin(request.user),
    }
    return render(request, 'frontend/vip_booking_detail.html', context)


@login_required
def edit_vip_booking(request, booking_id):
    """تعديل حجز VIP (كاشير أو مدير)."""
    booking = get_object_or_404(VIPBooking.objects.select_related("customer"), id=booking_id)
    if not _is_cashier_or_admin(request.user):
        messages.error(request, "ليس لديك صلاحية تعديل حجز VIP.")
        return redirect("frontend:dashboard")

    if request.method == "POST":
        form = VIPBookingForm(request.POST)
        if form.is_valid():
            try:
                customer = booking.customer
                customer.name = resolve_customer_name(
                    form.cleaned_data.get("customer_name"),
                    customer_pk=customer.pk,
                )
                customer.phone = (form.cleaned_data.get("customer_phone") or "").strip()
                customer.save(update_fields=["name", "phone", "updated_at"])

                booking.booking_type = form.cleaned_data["booking_type"]
                booking.booking_date = form.cleaned_data["booking_date"]
                booking.booking_time = form.cleaned_data["booking_time"]
                booking.barbers_count = form.cleaned_data["barbers_count"]
                booking.estimated_duration_hours = form.cleaned_data["estimated_duration_hours"]
                booking.base_price = form.cleaned_data["base_price"]
                booking.discount_pct = form.cleaned_data["discount_pct"]
                booking.description = form.cleaned_data.get("description") or ""
                booking.special_requests = form.cleaned_data.get("special_requests") or ""
                booking.save()
                assigned = form.cleaned_data.get("assigned_barber_ids")
                if assigned is not None:
                    booking.assigned_barbers.set(assigned)

                for receipt in booking.receipts.all():
                    receipt.amount = booking.final_price
                    receipt.customer_name = customer.name
                    receipt.customer_phone = customer.phone
                    receipt.items_description = (
                        f"حجز VIP - {booking.get_booking_type_display()} - {booking.description}"
                    )
                    receipt.save(
                        update_fields=[
                            "amount",
                            "customer_name",
                            "customer_phone",
                            "items_description",
                            "updated_at",
                        ]
                    )

                messages.success(request, "تم تحديث حجز VIP.")
                return redirect("frontend:vip:vip_booking_detail", booking_id=booking.id)
            except Exception as e:
                messages.error(request, f"حدث خطأ: {e}")
    else:
        form = VIPBookingForm(
            initial={
                "customer_name": booking.customer.name,
                "customer_phone": booking.customer.phone,
                "booking_type": booking.booking_type,
                "booking_date": booking.booking_date,
                "booking_time": booking.booking_time,
                "barbers_count": booking.barbers_count,
                "estimated_duration_hours": booking.estimated_duration_hours,
                "base_price": booking.base_price,
                "discount_pct": booking.discount_pct,
                "description": booking.description,
                "special_requests": booking.special_requests,
                "assigned_barber_ids": list(
                    booking.assigned_barbers.values_list("pk", flat=True)
                ),
            }
        )

    return render(
        request,
        "frontend/edit_vip_booking.html",
        {"form": form, "booking": booking, "is_admin": _is_admin(request.user)},
    )


# ─── Receipt Views ──────────────────────────────────────


@login_required
def generate_receipt(request, receipt_type=None, object_id=None):
    """توليد وصل للعملية"""
    if not _is_cashier_or_admin(request.user):
        messages.error(request, "ليس لديك صلاحية الوصول لهذه الصفحة.")
        return redirect("frontend:dashboard")
    
    if request.method == 'POST':
        form = ReceiptGenerationForm(request.POST)
        if form.is_valid():
            try:
                # الحصول على البيانات من الطلب
                receipt_type = form.cleaned_data.get('receipt_type', 'TICKET')
                note = form.cleaned_data.get('note', '')
                
                # البحث عن الكائن المطلوب (تذكرة، حجز VIP، إلخ)
                ticket = None
                customer_name = ''
                customer_phone = ''
                amount = Decimal('0')
                
                if request.POST.get('ticket_id'):
                    ticket = get_object_or_404(Ticket, id=request.POST.get('ticket_id'))
                    customer_name = ticket.customer.name
                    customer_phone = ticket.customer.phone
                    amount = ticket.total
                
                # إنشاء الوصل
                receipt = Receipt.objects.create(
                    receipt_type=receipt_type,
                    receipt_number=Receipt.generate_receipt_number(),
                    ticket=ticket,
                    customer_name=customer_name,
                    customer_phone=customer_phone,
                    amount=amount,
                    payment_method=PaymentMethod.CASH,
                    issued_by=request.user,
                    items_description=f"وصل {receipt_type}" + (f": {note}" if note else ""),
                )
                
                messages.success(request, f"تم إنشاء الوصل: {receipt.receipt_number}")
                return redirect('frontend:vip:receipt_print', receipt_id=receipt.id)
            except Exception as e:
                messages.error(request, f"حدث خطأ: {str(e)}")
    else:
        form = ReceiptGenerationForm()
    
    context = {
        'form': form,
        'receipt_type': receipt_type,
        'object_id': object_id,
    }
    return render(request, 'frontend/generate_receipt.html', context)


@login_required
def receipt_print(request, receipt_id):
    """طباعة الوصل"""
    receipt = get_object_or_404(Receipt, id=receipt_id)
    
    # يمكن إرجاع HTML للطباعة أو PDF
    auto_print = request.GET.get('auto_print') in ('1', 'true', 'yes')
    context = {
        'receipt': receipt,
        'print_mode': True,
        'auto_print': auto_print,
    }
    return render(request, 'frontend/receipt_print.html', context)


@login_required
def receipts_list(request):
    """قائمة الوصولات"""
    if not _is_cashier_or_admin(request.user):
        messages.error(request, "ليس لديك صلاحية الوصول لهذه الصفحة.")
        return redirect("frontend:dashboard")
    
    receipts_qs = Receipt.objects.select_related('issued_by').order_by('-created_at')
    receipt_type = request.GET.get('type')
    if receipt_type:
        receipts_qs = receipts_qs.filter(receipt_type=receipt_type)
    paginator, receipts_page = paginate_queryset(
        request, receipts_qs, per_page=RECEIPTS_PER_PAGE
    )
    
    context = {
        'receipts_page': receipts_page,
        'paginator': paginator,
        'query_string': querystring_excluding_page(request),
        'receipt_types': ReceiptType.choices,
        'filter_type': receipt_type or '',
    }
    return render(request, 'frontend/receipts_list.html', context)


# ─── Treasury Report Views ──────────────────────────────────


@login_required
def treasury_report(request):
    """تقرير شامل: ربط الخزنة بالإيراد"""
    if not _is_cashier_or_admin(request.user):
        messages.error(request, "ليس لديك صلاحية الوصول لهذه الصفحة.")
        return redirect("frontend:dashboard")
    
    # الفترة الزمنية (افتراضياً: آخر 30 يوم)
    start_date = request.GET.get('start_date')
    end_date = request.GET.get('end_date')
    
    if start_date:
        start_date = datetime.strptime(start_date, '%Y-%m-%d').date()
    else:
        start_date = timezone.now().date() - timedelta(days=30)
    
    if end_date:
        end_date = datetime.strptime(end_date, '%Y-%m-%d').date()
    else:
        end_date = timezone.now().date()
    
    # تجميع البيانات
    
    # 1. الإيرادات من CloseLedger
    closures = CloseLedger.objects.filter(
        closed_at__date__gte=start_date,
        closed_at__date__lte=end_date
    )
    total_revenue = closures.aggregate(Sum('total_revenue'))['total_revenue__sum'] or Decimal('0')
    total_cash_revenue = closures.aggregate(Sum('total_cash'))['total_cash__sum'] or Decimal('0')
    total_card_revenue = closures.aggregate(Sum('total_card'))['total_card__sum'] or Decimal('0')
    total_commission = closures.aggregate(Sum('total_barber_commission'))['total_barber_commission__sum'] or Decimal('0')

    # إيرادات VIP
    vip_bookings = VIPBooking.objects.filter(
        status='completed',
        updated_at__date__gte=start_date,
        updated_at__date__lte=end_date
    )
    vip_rev = vip_bookings.aggregate(Sum('paid_amount'))['paid_amount__sum'] or Decimal('0')
    total_revenue += vip_rev
    
    # توزيع كاش/بطاقة لـ VIP (تبسيط: نفترض أنها كاش إلا إذا حددنا غير ذلك، أو نجمعها حسب الحقل)
    vip_cash = vip_bookings.filter(payment_method=PaymentMethod.CASH).aggregate(Sum('paid_amount'))['paid_amount__sum'] or Decimal('0')
    vip_card = vip_bookings.filter(payment_method=PaymentMethod.CARD).aggregate(Sum('paid_amount'))['paid_amount__sum'] or Decimal('0')
    
    total_cash_revenue += vip_cash
    total_card_revenue += vip_card
    
    # 2. المصروفات من TreasuryEntry
    expenses = TreasuryEntry.objects.filter(
        entry_type=TreasuryEntryType.EXPENSE,
        is_voided=False,
        created_at__date__gte=start_date,
        created_at__date__lte=end_date
    )
    total_expenses = expenses.aggregate(Sum('amount'))['amount__sum'] or Decimal('0')
    
    # تقسيم المصروفات حسب الفئة
    expenses_by_category = expenses.values('category__name').annotate(
        total=Sum('amount')
    ).order_by('-total')
    
    # 3. الإيداعات
    deposits = TreasuryEntry.objects.filter(
        entry_type=TreasuryEntryType.DEPOSIT,
        is_voided=False,
        created_at__date__gte=start_date,
        created_at__date__lte=end_date
    )
    total_deposits = deposits.aggregate(Sum('amount'))['amount__sum'] or Decimal('0')
    
    # 4. الرصيد الحالي
    # الرصيد = الإيراد + الإيداعات - المصروفات
    current_balance = total_revenue + total_deposits - total_expenses
    
    # 5. توزيع الدفع (نقدي/بطاقة)
    cash_expenses = expenses.filter(payment_method=PaymentMethod.CASH).aggregate(Sum('amount'))['amount__sum'] or Decimal('0')
    card_expenses = expenses.filter(payment_method=PaymentMethod.CARD).aggregate(Sum('amount'))['amount__sum'] or Decimal('0')
    
    context = {
        'start_date': start_date,
        'end_date': end_date,
        'total_revenue': total_revenue,
        'total_cash_revenue': total_cash_revenue,
        'total_card_revenue': total_card_revenue,
        'total_commission': total_commission,
        'total_expenses': total_expenses,
        'total_deposits': total_deposits,
        'current_balance': current_balance,
        'cash_expenses': cash_expenses,
        'card_expenses': card_expenses,
        'expenses_by_category': expenses_by_category,
        'closures': closures[:30],  # آخر 30 إغلاق
    }
    
    return render(request, 'frontend/treasury_report.html', context)


@login_required
def treasury_summary(request):
    """ملخص الخزنة اليومي"""
    if not _is_cashier_or_admin(request.user):
        messages.error(request, "ليس لديك صلاحية الوصول لهذه الصفحة.")
        return redirect("frontend:dashboard")
    
    today = timezone.now().date()
    
    # الإيرادات اليومية
    today_closures = CloseLedger.objects.filter(closed_at__date=today)
    today_revenue = today_closures.aggregate(Sum('total_revenue'))['total_revenue__sum'] or Decimal('0')
    today_cash = today_closures.aggregate(Sum('total_cash'))['total_cash__sum'] or Decimal('0')
    today_card = today_closures.aggregate(Sum('total_card'))['total_card__sum'] or Decimal('0')
    
    # المصروفات اليومية
    today_expenses = TreasuryEntry.objects.filter(
        entry_type=TreasuryEntryType.EXPENSE,
        is_voided=False,
        created_at__date=today
    ).aggregate(Sum('amount'))['amount__sum'] or Decimal('0')
    
    # الإيداعات اليومية
    today_deposits = TreasuryEntry.objects.filter(
        entry_type=TreasuryEntryType.DEPOSIT,
        is_voided=False,
        created_at__date=today
    ).aggregate(Sum('amount'))['amount__sum'] or Decimal('0')
    
    # الرصيد
    balance = today_revenue + today_deposits - today_expenses
    
    # تفاصيل المصروفات
    expense_details = TreasuryEntry.objects.filter(
        entry_type=TreasuryEntryType.EXPENSE,
        is_voided=False,
        created_at__date=today
    ).select_related('category', 'recorded_by').order_by('-created_at')
    
    context = {
        'date': today,
        'revenue': today_revenue,
        'cash': today_cash,
        'card': today_card,
        'expenses': today_expenses,
        'deposits': today_deposits,
        'balance': balance,
        'expense_details': expense_details,
    }
    
    return render(request, 'frontend/treasury_summary.html', context)
