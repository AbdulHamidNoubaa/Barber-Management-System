from __future__ import annotations

from decimal import Decimal

from django.contrib import admin, messages
from django.db import transaction
from django.db.models import F, Sum
from django.utils import timezone

from accounts.models import UserRole
from core.models import (
    BarberCommissionOverride,
    CloseLedger,
    Customer,
    ExpenseCategory,
    Payment,
    Receipt,
    Service,
    Shift,
    Ticket,
    TicketItem,
    TicketStatus,
    TreasuryEntry,
    VIPBooking,
)
from core.shift_utils import get_open_shift


def _role(user) -> str | None:
    return getattr(user, "role", None)


class RoleScopedAdmin(admin.ModelAdmin):
    def is_admin(self, request) -> bool:
        return request.user.is_superuser or _role(request.user) == UserRole.ADMIN

    def is_cashier(self, request) -> bool:
        return _role(request.user) == UserRole.CASHIER

    def is_barber(self, request) -> bool:
        return _role(request.user) == UserRole.BARBER


@admin.register(ExpenseCategory)
class ExpenseCategoryAdmin(RoleScopedAdmin):
    list_display = ("name", "sort_order", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name",)
    ordering = ("sort_order", "name")

    def has_module_permission(self, request):
        return self.is_admin(request)


@admin.register(TreasuryEntry)
class TreasuryEntryAdmin(RoleScopedAdmin):
    list_display = (
        "id",
        "created_at",
        "entry_type",
        "amount",
        "payment_method",
        "category",
        "is_voided",
        "recorded_by",
    )
    list_filter = ("entry_type", "payment_method", "is_voided", "created_at")
    search_fields = ("description", "recorded_by__username")
    readonly_fields = ("voided_at", "voided_by")
    autocomplete_fields = ("category", "shift", "recorded_by", "voided_by")
    date_hierarchy = "created_at"

    def has_module_permission(self, request):
        return self.is_admin(request)


@admin.register(Service)
class ServiceAdmin(RoleScopedAdmin):
    list_display = ("name", "base_price", "is_active", "created_at")
    list_filter = ("is_active",)
    search_fields = ("name",)

    def has_module_permission(self, request):
        return self.is_admin(request)


@admin.register(BarberCommissionOverride)
class BarberCommissionOverrideAdmin(RoleScopedAdmin):
    list_display = ("barber", "service", "commission_pct", "is_active")
    list_filter = ("is_active", "service")
    search_fields = ("barber__name", "barber__user__username", "service__name")

    def has_module_permission(self, request):
        return self.is_admin(request)


@admin.register(Customer)
class CustomerAdmin(RoleScopedAdmin):
    list_display = ("name", "phone", "is_active", "created_at")
    list_filter = ("is_active",)
    search_fields = ("name", "phone")

    def has_module_permission(self, request):
        return self.is_admin(request) or self.is_cashier(request) or self.is_barber(request)

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if self.is_barber(request):
            return qs.filter(tickets__barber__user=request.user).distinct()
        return qs


class TicketItemInline(admin.TabularInline):
    model = TicketItem
    extra = 0
    autocomplete_fields = ("service",)
    fields = ("service", "price", "commission_pct", "barber_commission_amount", "created_at")
    readonly_fields = ("barber_commission_amount", "created_at")


class PaymentInline(admin.TabularInline):
    model = Payment
    extra = 0
    fields = ("method", "amount", "paid_at", "received_by")
    readonly_fields = ("paid_at",)

    def has_add_permission(self, request, obj=None):
        if obj and obj.locked_by_close_id:
            return False
        return super().has_add_permission(request, obj)


@admin.register(Ticket)
class TicketAdmin(RoleScopedAdmin):
    list_display = (
        "id",
        "customer",
        "barber",
        "shift",
        "status",
        "queue_position",
        "total",
        "barber_commission_total",
        "created_at",
    )
    list_filter = ("status", "barber", "shift")
    search_fields = ("id", "customer__name", "customer__phone", "barber__name", "barber__user__username")
    autocomplete_fields = ("customer", "barber", "shift")
    inlines = (TicketItemInline, PaymentInline)
    actions = ("action_start", "action_complete", "action_cancel", "action_recalc", "action_move_to_top")

    def has_module_permission(self, request):
        return self.is_admin(request) or self.is_cashier(request) or self.is_barber(request)

    def get_queryset(self, request):
        qs = super().get_queryset(request).select_related("customer", "barber__user", "shift")
        if self.is_barber(request):
            return qs.filter(barber__user=request.user)
        return qs

    def get_readonly_fields(self, request, obj=None):
        ro = ["subtotal", "total", "barber_commission_total", "created_at", "updated_at"]
        if obj and obj.locked_by_close_id:
            return ro + [f.name for f in self.model._meta.fields]
        if self.is_barber(request):
            return ro + ["customer", "barber", "shift", "discount", "queue_position", "locked_by_close"]
        return ro

    def save_model(self, request, obj: Ticket, form, change):
        if not change:
            if not obj.shift_id:
                shift = get_open_shift()
                if shift:
                    obj.shift = shift
        super().save_model(request, obj, form, change)

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        ticket: Ticket = form.instance
        try:
            ticket.recalc_totals()
        except Exception:
            # avoid breaking admin save; show message instead
            messages.warning(request, "Ticket totals could not be recalculated. Please review items/discount.")

    @admin.action(description="Start selected tickets (IN_PROGRESS)")
    def action_start(self, request, queryset):
        for t in queryset:
            t.set_status(TicketStatus.IN_PROGRESS, by_user=request.user)
        self.message_user(request, "Selected tickets started.", level=messages.SUCCESS)

    @admin.action(description="Complete selected tickets (COMPLETED)")
    def action_complete(self, request, queryset):
        for t in queryset:
            t.set_status(TicketStatus.COMPLETED, by_user=request.user)
        self.message_user(request, "Selected tickets completed.", level=messages.SUCCESS)

    @admin.action(description="Cancel selected tickets (CANCELLED)")
    def action_cancel(self, request, queryset):
        for t in queryset:
            t.set_status(TicketStatus.CANCELLED, by_user=request.user)
        self.message_user(request, "Selected tickets cancelled.", level=messages.SUCCESS)

    @admin.action(description="Recalculate totals for selected tickets")
    def action_recalc(self, request, queryset):
        for t in queryset:
            t.recalc_totals()
        self.message_user(request, "Selected tickets recalculated.", level=messages.SUCCESS)

    @admin.action(description="Move selected tickets to top of their barber queue")
    def action_move_to_top(self, request, queryset):
        with transaction.atomic():
            for t in queryset.select_related("barber"):
                if t.locked_by_close_id:
                    continue
                min_pos = (
                    Ticket.objects.filter(barber=t.barber, status=TicketStatus.WAITING)
                    .exclude(id=t.id)
                    .aggregate(v=Sum("queue_position"))  # cheap placeholder; recompute below
                )
                # set to 1, shift others down
                Ticket.objects.filter(barber=t.barber, status=TicketStatus.WAITING).exclude(id=t.id).update(
                    queue_position=F("queue_position") + 1
                )
                t.queue_position = 1
                t.save(update_fields=["queue_position", "updated_at"])
        self.message_user(request, "Queue updated.", level=messages.SUCCESS)


@admin.register(Payment)
class PaymentAdmin(RoleScopedAdmin):
    list_display = ("id", "ticket", "method", "amount", "paid_at", "received_by")
    list_filter = ("method",)
    search_fields = ("ticket__id", "ticket__customer__name", "received_by__username")
    autocomplete_fields = ("ticket", "received_by")

    def has_module_permission(self, request):
        return self.is_admin(request) or self.is_cashier(request)

    def has_change_permission(self, request, obj=None):
        if obj and obj.ticket.locked_by_close_id:
            return False
        return super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        if obj and obj.ticket.locked_by_close_id:
            return False
        return super().has_delete_permission(request, obj)


@admin.register(Shift)
class ShiftAdmin(RoleScopedAdmin):
    list_display = ("id", "started_at", "ended_at", "is_closed", "closed_by")
    list_filter = ("is_closed",)
    search_fields = ("id",)
    actions = ("action_close_shift",)

    def has_module_permission(self, request):
        return self.is_admin(request) or self.is_cashier(request)

    def has_add_permission(self, request):
        return self.is_admin(request)

    @admin.action(description="Close selected shifts (create close ledger + lock completed tickets)")
    def action_close_shift(self, request, queryset):
        if not (self.is_admin(request) or self.is_cashier(request)):
            self.message_user(request, "Not allowed.", level=messages.ERROR)
            return
        now = timezone.now()
        for shift in queryset.select_for_update():
            if shift.is_closed:
                continue
            with transaction.atomic():
                completed = shift.tickets.filter(status=TicketStatus.COMPLETED, locked_by_close__isnull=True)
                total_revenue = completed.aggregate(v=Sum("total"))["v"] or Decimal("0")
                total_comm = completed.aggregate(v=Sum("barber_commission_total"))["v"] or Decimal("0")

                payments = Payment.objects.filter(ticket__shift=shift, ticket__status=TicketStatus.COMPLETED).values("method").annotate(
                    v=Sum("amount")
                )
                by_method = {p["method"]: (p["v"] or Decimal("0")) for p in payments}
                cash_total = by_method.get("CASH", Decimal("0"))
                card_total = by_method.get("CARD", Decimal("0"))

                ledger = CloseLedger.objects.create(
                    close_type="SHIFT",
                    shift=shift,
                    closed_at=now,
                    closed_by=request.user,
                    total_revenue=total_revenue,
                    total_cash=cash_total,
                    total_card=card_total,
                    total_barber_commission=total_comm,
                )
                completed.update(locked_by_close=ledger)
                shift.is_closed = True
                shift.ended_at = now
                shift.closed_by = request.user
                shift.save(update_fields=["is_closed", "ended_at", "closed_by", "updated_at"])
        self.message_user(request, "Selected shifts closed.", level=messages.SUCCESS)


@admin.register(CloseLedger)
class CloseLedgerAdmin(RoleScopedAdmin):
    list_display = ("id", "close_type", "shift", "closed_at", "closed_by", "total_revenue", "total_cash", "total_card")
    list_filter = ("close_type",)

    def has_module_permission(self, request):
        return self.is_admin(request)


@admin.register(VIPBooking)
class VIPBookingAdmin(RoleScopedAdmin):
    list_display = (
        "id",
        "customer",
        "booking_type",
        "booking_date",
        "booking_time",
        "status",
        "final_price",
        "barbers_count",
    )
    list_filter = ("booking_type", "status", "booking_date")
    search_fields = ("customer__name", "customer__phone", "description")
    filter_horizontal = ("assigned_barbers",)
    readonly_fields = ("created_by", "created_at")
    
    fieldsets = (
        ("معلومات الحجز", {
            "fields": ("customer", "booking_type", "status", "shift")
        }),
        ("التاريخ والوقت", {
            "fields": ("booking_date", "booking_time", "estimated_duration_hours")
        }),
        ("الخدمات والأسعار", {
            "fields": ("barbers_count", "base_price", "discount_pct", "final_price")
        }),
        ("الحلاقون", {
            "fields": ("assigned_barbers",)
        }),
        ("الدفع", {
            "fields": ("paid_amount", "payment_method")
        }),
        ("التفاصيل", {
            "fields": ("description", "special_requests")
        }),
        ("بيانات النظام", {
            "fields": ("created_by", "created_at")
        }),
    )
    
    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)
    
    def has_module_permission(self, request):
        return self.is_admin(request) or self.is_cashier(request)


@admin.register(Receipt)
class ReceiptAdmin(RoleScopedAdmin):
    list_display = (
        "receipt_number",
        "receipt_type",
        "customer_name",
        "amount",
        "payment_method",
        "issued_by",
        "created_at",
    )
    list_filter = ("receipt_type", "payment_method", "created_at")
    search_fields = ("receipt_number", "customer_name", "customer_phone")
    readonly_fields = (
        "receipt_number",
        "issued_by",
        "created_at",
        "ticket",
        "vip_booking",
        "payment",
        "treasury_entry",
    )
    
    fieldsets = (
        ("رقم الوصل", {
            "fields": ("receipt_number", "receipt_type")
        }),
        ("بيانات العميل", {
            "fields": ("customer_name", "customer_phone")
        }),
        ("التفاصيل المالية", {
            "fields": ("amount", "payment_method")
        }),
        ("الارتباطات", {
            "fields": ("ticket", "vip_booking", "payment", "treasury_entry", "shift")
        }),
        ("ملاحظات", {
            "fields": ("items_description", "note")
        }),
        ("بيانات النظام", {
            "fields": ("issued_by", "created_at")
        }),
    )
    
    def save_model(self, request, obj, form, change):
        if not obj.receipt_number:
            obj.receipt_number = Receipt.generate_receipt_number()
        if not change:
            obj.issued_by = request.user
        super().save_model(request, obj, form, change)
    
    def has_module_permission(self, request):
        return self.is_admin(request) or self.is_cashier(request)
