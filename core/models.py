from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import Q
from django.utils import timezone


class TimestampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        abstract = True


class PaymentMethod(models.TextChoices):
    CASH = "CASH", "Cash"
    CARD = "CARD", "Card"


class TicketStatus(models.TextChoices):
    WAITING = "WAITING", "Waiting"
    IN_PROGRESS = "IN_PROGRESS", "In progress"
    COMPLETED = "COMPLETED", "Completed"
    CANCELLED = "CANCELLED", "Cancelled"


class CloseType(models.TextChoices):
    SHIFT = "SHIFT", "Shift close"
    DAY = "DAY", "Day close"


class TreasuryEntryType(models.TextChoices):
    """حركات الخزنة: صرف (مصروف) أو إيداع نقد للخزنة (مثلاً من المالك)."""

    EXPENSE = "EXPENSE", "مصروف"
    DEPOSIT = "DEPOSIT", "إيداع"


class Customer(TimestampedModel):
    name = models.CharField(max_length=120)
    phone = models.CharField(max_length=30, blank=True, default="")
    notes = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True)

    def __str__(self) -> str:
        return self.name


class Service(TimestampedModel):
    name = models.CharField(max_length=120, unique=True)
    base_price = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    is_active = models.BooleanField(default=True)

    def __str__(self) -> str:
        return self.name


class BarberCommissionOverride(TimestampedModel):
    barber = models.ForeignKey(
        "accounts.BarberProfile",
        on_delete=models.CASCADE,
        related_name="commission_overrides",
    )
    service = models.ForeignKey("core.Service", on_delete=models.CASCADE, related_name="commission_overrides")
    commission_pct = models.DecimalField(max_digits=5, decimal_places=2)
    is_active = models.BooleanField(default=True)

    class Meta:
        unique_together = [("barber", "service")]

    def clean(self) -> None:
        super().clean()
        if self.commission_pct < 0 or self.commission_pct > 100:
            raise ValidationError({"commission_pct": "Must be between 0 and 100."})

    def __str__(self) -> str:
        return f"{self.barber} - {self.service}: {self.commission_pct}%"


class ShiftTemplate(TimestampedModel):
    name = models.CharField(max_length=80, unique=True)
    description = models.CharField(max_length=255, blank=True, default="")
    start_time = models.TimeField(null=True, blank=True)
    end_time = models.TimeField(null=True, blank=True)
    default_cashiers = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        blank=True,
        related_name="shift_templates",
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["start_time", "name"]

    def time_range_display(self) -> str:
        if self.start_time and self.end_time:
            return f"{self.start_time.strftime('%H:%M')} — {self.end_time.strftime('%H:%M')}"
        return ""

    def __str__(self) -> str:
        tr = self.time_range_display()
        return f"{self.name} ({tr})" if tr else self.name


class Shift(TimestampedModel):
    name = models.CharField(max_length=120, blank=True, default="")
    started_at = models.DateTimeField(default=timezone.now, db_index=True)
    ended_at = models.DateTimeField(null=True, blank=True, db_index=True)
    is_closed = models.BooleanField(default=False, db_index=True)
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="closed_shifts",
    )
    assigned_cashiers = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        blank=True,
        related_name="assigned_shifts",
    )

    def can_close(self, user) -> bool:
        if user.is_superuser or user.role == "ADMIN":
            return True
        return self.assigned_cashiers.filter(pk=user.pk).exists()

    def __str__(self) -> str:
        label = self.name or "شفت"
        end = self.ended_at.isoformat(sep=" ", timespec="minutes") if self.ended_at else "مفتوح"
        return f"{label} - {self.started_at.date()} ({end})"


class CloseLedger(TimestampedModel):
    close_type = models.CharField(max_length=10, choices=CloseType.choices)
    shift = models.ForeignKey("core.Shift", on_delete=models.PROTECT, null=True, blank=True, related_name="closures")
    closed_at = models.DateTimeField(default=timezone.now, db_index=True)
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, 
        on_delete=models.PROTECT, 
        related_name="closures",
        null=True,  # Allow NULL for auto-close operations
        blank=True,
    )

    total_revenue = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total_cash = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total_card = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total_barber_commission = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    note = models.TextField(blank=True, default="")

    class Meta:
        indexes = [
            models.Index(fields=["close_type", "closed_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.get_close_type_display()} @ {self.closed_at:%Y-%m-%d %H:%M}"


class Ticket(TimestampedModel):
    customer = models.ForeignKey("core.Customer", on_delete=models.PROTECT, related_name="tickets")
    barber = models.ForeignKey("accounts.BarberProfile", on_delete=models.PROTECT, related_name="tickets")
    shift = models.ForeignKey("core.Shift", on_delete=models.PROTECT, related_name="tickets")

    status = models.CharField(max_length=20, choices=TicketStatus.choices, default=TicketStatus.WAITING, db_index=True)
    queue_position = models.PositiveIntegerField(default=0, db_index=True)

    started_at = models.DateTimeField(null=True, blank=True, db_index=True)
    completed_at = models.DateTimeField(null=True, blank=True, db_index=True)

    # Money
    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    discount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    barber_commission_total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    description = models.CharField(max_length=255, blank=True, default="")
    payment_method = models.CharField(
        max_length=10, choices=PaymentMethod.choices, default=PaymentMethod.CASH, blank=True,
    )

    locked_by_close = models.ForeignKey(
        "core.CloseLedger",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="locked_tickets",
    )

    class Meta:
        indexes = [
            models.Index(fields=["barber", "status", "queue_position"]),
            models.Index(fields=["shift", "status"]),
        ]
        constraints = [
            models.CheckConstraint(check=Q(total__gte=0), name="ticket_total_gte_0"),
        ]

    def __str__(self) -> str:
        return f"Ticket #{self.id} - {self.customer} ({self.get_status_display()})"

    def clean(self) -> None:
        super().clean()
        if self.discount < 0:
            raise ValidationError({"discount": "Discount cannot be negative."})

    def assert_mutable(self) -> None:
        if self.locked_by_close_id is not None:
            raise ValidationError("This ticket is locked by a close and cannot be modified.")
        if self.shift.is_closed:
            raise ValidationError("Shift is closed; ticket cannot be modified.")

    @transaction.atomic
    def recalc_totals(self) -> None:
        self.assert_mutable()
        items = list(self.items.select_related("service").all())
        subtotal = sum((i.price for i in items), Decimal("0"))
        total = subtotal - (self.discount or Decimal("0"))
        if total < 0:
            total = Decimal("0")

        commission_total = sum((i.barber_commission_amount for i in items), Decimal("0"))

        self.subtotal = subtotal
        self.total = total
        self.barber_commission_total = commission_total
        self.save(update_fields=["subtotal", "total", "barber_commission_total", "updated_at"])

    @transaction.atomic
    def set_status(self, new_status: str, by_user: settings.AUTH_USER_MODEL | None = None) -> None:
        self.assert_mutable()
        now = timezone.now()
        if new_status == TicketStatus.IN_PROGRESS and self.started_at is None:
            self.started_at = now
        if new_status in (TicketStatus.COMPLETED, TicketStatus.CANCELLED) and self.completed_at is None:
            self.completed_at = now
        self.status = new_status
        self.save(update_fields=["status", "started_at", "completed_at", "updated_at"])


class TicketItem(TimestampedModel):
    ticket = models.ForeignKey("core.Ticket", on_delete=models.CASCADE, related_name="items")
    service = models.ForeignKey("core.Service", on_delete=models.PROTECT, related_name="ticket_items")
    price = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    commission_pct = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    barber_commission_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    class Meta:
        unique_together = [("ticket", "service")]

    def clean(self) -> None:
        super().clean()
        if self.commission_pct < 0 or self.commission_pct > 100:
            raise ValidationError({"commission_pct": "Must be between 0 and 100."})
        if self.price < 0:
            raise ValidationError({"price": "Price cannot be negative."})

    def __str__(self) -> str:
        return f"{self.service} ({self.price})"

    def compute_commission(self) -> None:
        pct = self.commission_pct or Decimal("0")
        self.barber_commission_amount = (self.price or Decimal("0")) * pct / Decimal("100")

    def save(self, *args, **kwargs):
        if self.pk:
            self.ticket.assert_mutable()
        self.compute_commission()
        if not self.commission_pct:
            # default commission: per-service override if active, else barber default
            override = (
                BarberCommissionOverride.objects.filter(
                    barber=self.ticket.barber,
                    service=self.service,
                    is_active=True,
                )
                .only("commission_pct")
                .first()
            )
            self.commission_pct = (
                override.commission_pct
                if override is not None
                else (self.ticket.barber.default_commission_pct or Decimal("0"))
            )
            self.compute_commission()
        super().save(*args, **kwargs)


class Payment(TimestampedModel):
    ticket = models.ForeignKey("core.Ticket", on_delete=models.PROTECT, related_name="payments")
    method = models.CharField(max_length=10, choices=PaymentMethod.choices)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    paid_at = models.DateTimeField(default=timezone.now, db_index=True)
    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="received_payments",
    )

    def clean(self) -> None:
        super().clean()
        if self.amount <= 0:
            raise ValidationError({"amount": "Amount must be > 0."})

    def save(self, *args, **kwargs):
        if self.pk:
            self.ticket.assert_mutable()
        else:
            self.ticket.assert_mutable()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.get_method_display()} {self.amount}"


class AuditEvent(TimestampedModel):
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    action = models.CharField(max_length=80)
    entity = models.CharField(max_length=80)
    entity_id = models.CharField(max_length=80, blank=True, default="")
    message = models.TextField(blank=True, default="")
    ip_address = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["created_at", "action"]),
            models.Index(fields=["entity", "entity_id"]),
        ]

    def __str__(self) -> str:
        return f"{self.action} {self.entity} {self.entity_id}"


class SystemSetting(TimestampedModel):
    key = models.CharField(max_length=80, unique=True)
    value = models.CharField(max_length=255, blank=True, default="")
    description = models.CharField(max_length=255, blank=True, default="")

    def __str__(self) -> str:
        return f"{self.key}={self.value}"


class ExpenseCategory(TimestampedModel):
    """تصنيف مصروف (إيجار، مستلزمات، …) — يُستخدم مع نوع «مصروف» فقط."""

    name = models.CharField(max_length=80, unique=True)
    sort_order = models.PositiveSmallIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["sort_order", "name"]

    def __str__(self) -> str:
        return self.name


class TreasuryEntry(TimestampedModel):
    """
    سجل خزنة المحل: مصروفات نقدية/بطاقة أو إيداعات للخزنة.
    السجلات الملغاة تبقى للمراجعة ولا تُحسب في الملخص.
    """

    entry_type = models.CharField(max_length=10, choices=TreasuryEntryType.choices, db_index=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    payment_method = models.CharField(
        max_length=10,
        choices=PaymentMethod.choices,
        default=PaymentMethod.CASH,
        db_index=True,
    )
    category = models.ForeignKey(
        ExpenseCategory,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="treasury_entries",
    )
    description = models.CharField(max_length=255, blank=True, default="")
    shift = models.ForeignKey(
        "core.Shift",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="treasury_entries",
    )
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="treasury_entries",
    )
    is_voided = models.BooleanField(default=False, db_index=True)
    voided_at = models.DateTimeField(null=True, blank=True)
    voided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="voided_treasury_entries",
    )

    class Meta:
        indexes = [
            models.Index(fields=["entry_type", "created_at"]),
            models.Index(fields=["is_voided", "created_at"]),
        ]
        constraints = [
            models.CheckConstraint(check=Q(amount__gt=0), name="treasury_amount_gt_0"),
        ]

    def __str__(self) -> str:
        return f"{self.get_entry_type_display()} {self.amount}"

    def clean(self) -> None:
        super().clean()
        if self.entry_type == TreasuryEntryType.EXPENSE and self.category_id is None:
            raise ValidationError({"category": "اختر تصنيفاً للمصروف."})
        if self.entry_type == TreasuryEntryType.DEPOSIT and self.category_id is not None:
            raise ValidationError({"category": "الإيداع لا يستخدم تصنيف مصروف."})


class VIPBookingType(models.TextChoices):
    """أنواع حجوزات VIP"""
    WEDDING = "WEDDING", "عرس"
    EVENT = "EVENT", "حفل"
    GROUP = "GROUP", "مجموعة"
    CUSTOM = "CUSTOM", "مخصص"


class VIPBooking(TimestampedModel):
    """حجز VIP خاص للعرسان والحفلات"""
    
    customer = models.ForeignKey("core.Customer", on_delete=models.PROTECT, related_name="vip_bookings")
    booking_type = models.CharField(max_length=20, choices=VIPBookingType.choices)
    
    # تاريخ ووقت الحجز
    booking_date = models.DateField(db_index=True)
    booking_time = models.TimeField()
    
    # الخدمات والأسعار
    barbers_count = models.PositiveIntegerField(default=1, help_text="عدد الحلاقين")
    estimated_duration_hours = models.DecimalField(max_digits=5, decimal_places=2, default=2)
    
    # الأسعار المخصصة
    base_price = models.DecimalField(max_digits=12, decimal_places=2, help_text="السعر الأساسي")
    discount_pct = models.DecimalField(max_digits=5, decimal_places=2, default=0, help_text="نسبة الخصم %")
    final_price = models.DecimalField(max_digits=12, decimal_places=2)
    
    # التفاصيل
    description = models.TextField(blank=True, help_text="تفاصيل الخدمات المطلوبة")
    special_requests = models.TextField(blank=True, help_text="طلبات خاصة")
    
    # حالة الحجز
    STATUS_CHOICES = [
        ('pending', 'قيد الانتظار'),
        ('confirmed', 'مؤكد'),
        ('in_progress', 'قيد التنفيذ'),
        ('completed', 'مكتمل'),
        ('cancelled', 'ملغي'),
    ]
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending', db_index=True)
    
    # الحلاقون المسندون
    assigned_barbers = models.ManyToManyField(
        "accounts.BarberProfile",
        blank=True,
        related_name="vip_bookings"
    )
    
    # الدفع
    paid_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    payment_method = models.CharField(
        max_length=10,
        choices=PaymentMethod.choices,
        default=PaymentMethod.CASH,
        blank=True
    )
    
    # من وضع الحجز
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_vip_bookings"
    )
    
    # شفت التنفيذ (اختياري)
    shift = models.ForeignKey(
        "core.Shift",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="vip_bookings"
    )
    
    class Meta:
        ordering = ["booking_date", "booking_time"]
        indexes = [
            models.Index(fields=["booking_date", "status"]),
            models.Index(fields=["customer", "status"]),
        ]
    
    def __str__(self) -> str:
        return f"حجز VIP - {self.customer} ({self.get_booking_type_display()}) - {self.booking_date}"
    
    def save(self, *args, **kwargs):
        # حساب السعر النهائي تلقائياً
        self.final_price = self.base_price - (self.base_price * self.discount_pct / 100)
        super().save(*args, **kwargs)


class ReceiptType(models.TextChoices):
    """أنواع الوصولات"""
    TICKET = "TICKET", "وصل تذكرة"
    VIP_BOOKING = "VIP_BOOKING", "وصل حجز VIP"
    PAYMENT = "PAYMENT", "وصل دفع"
    TREASURY = "TREASURY", "وصل خزنة"


class Receipt(TimestampedModel):
    """وصل شامل لجميع العمليات"""
    
    receipt_type = models.CharField(max_length=20, choices=ReceiptType.choices, db_index=True)
    receipt_number = models.CharField(max_length=50, unique=True, db_index=True)
    
    # الارتباطات المختلفة
    ticket = models.ForeignKey(
        "core.Ticket",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="receipts"
    )
    vip_booking = models.ForeignKey(
        "core.VIPBooking",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="receipts"
    )
    payment = models.ForeignKey(
        "core.Payment",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="receipts"
    )
    treasury_entry = models.ForeignKey(
        "core.TreasuryEntry",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="receipts"
    )
    
    # البيانات الأساسية
    customer_name = models.CharField(max_length=120)
    customer_phone = models.CharField(max_length=30, blank=True)
    
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    payment_method = models.CharField(
        max_length=10,
        choices=PaymentMethod.choices,
        default=PaymentMethod.CASH
    )
    
    # من استخرج الوصل
    issued_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="issued_receipts"
    )
    
    # التفاصيل
    items_description = models.TextField(blank=True, help_text="وصف العناصر/الخدمات")
    note = models.TextField(blank=True, help_text="ملاحظات إضافية")
    
    # الشفت (اختياري)
    shift = models.ForeignKey(
        "core.Shift",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="receipts"
    )
    
    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["receipt_type", "created_at"]),
            models.Index(fields=["customer_name"]),
        ]
    
    def __str__(self) -> str:
        return f"وصل #{self.receipt_number} - {self.customer_name} ({self.amount})"
    
    @staticmethod
    def generate_receipt_number() -> str:
        """توليد رقم وصل فريد"""
        from datetime import datetime
        last = Receipt.objects.order_by('-id').first()
        next_num = (last.id + 1) if last else 1
        return f"R-{datetime.now().strftime('%Y%m%d')}-{next_num:05d}"


def get_or_create_open_shift(name: str = "") -> Shift:
    """Get an open shift or create a new one if none exists.
    
    Uses select_for_update to prevent race conditions in concurrent requests.
    
    Args:
        name: Optional name for the new shift if it needs to be created
        
    Returns:
        Shift: An open (not closed) shift
    """
    # Try to get existing open shift first
    shift = (
        Shift.objects.select_for_update()
        .filter(is_closed=False, ended_at__isnull=True)
        .order_by("-started_at")
        .first()
    )
    if shift:
        return shift
    
    # Create new shift if none exists
    return Shift.objects.create(name=name)
