from __future__ import annotations

from decimal import Decimal

from django import forms
from django.core.exceptions import ValidationError

from accounts.models import BarberProfile, User, UserRole
from core.models import (
    ExpenseCategory,
    PaymentMethod,
    Service,
    SystemSetting,
    TicketStatus,
    TreasuryEntryType,
)


class QueueTicketForm(forms.Form):
    """إصدار وصل مباشر — حلاق + خدمات متعددة + دفع."""

    barber_id = forms.ModelChoiceField(queryset=BarberProfile.objects.none(), label="الحلاق")
    service_ids = forms.ModelMultipleChoiceField(
        queryset=Service.objects.none(),
        label="الخدمات",
        error_messages={"required": "اختر خدمة واحدة على الأقل."},
    )
    initial_amount = forms.DecimalField(
        required=False,
        min_value=Decimal("0"),
        max_digits=12,
        decimal_places=2,
    )
    payment_method = forms.ChoiceField(
        choices=[(PaymentMethod.CASH, "نقدي"), (PaymentMethod.CARD, "بطاقة")],
        initial=PaymentMethod.CASH,
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["barber_id"].queryset = BarberProfile.objects.filter(is_active=True).select_related("user")
        self.fields["barber_id"].label_from_instance = lambda obj: obj.display_name
        self.fields["service_ids"].queryset = Service.objects.filter(is_active=True).order_by("name")
        self.fields["initial_amount"].widget.attrs.update({
            "class": "field-input pos-input-amount",
            "id": "posAmount",
            "readonly": "readonly",
            "tabindex": "-1",
        })
        self.fields["barber_id"].widget.attrs.update({"id": "posBarberSelect"})
        self.fields["payment_method"].widget.attrs.update({"id": "posPaymentSelect"})

    def clean(self):
        cleaned = super().clean()
        services = list(cleaned.get("service_ids") or [])
        if not services:
            raise ValidationError({"service_ids": "اختر خدمة واحدة على الأقل."})
        total = sum((s.base_price or Decimal("0") for s in services), Decimal("0"))
        if total <= 0:
            raise ValidationError({"service_ids": "إحدى الخدمات بدون سعر — حدّث الأسعار من الإعدادات."})
        cleaned["initial_amount"] = total
        cleaned["service_ids"] = services
        return cleaned


class TicketEditForm(forms.Form):
    """تعديل وصل كامل: حلاق، خدمات متعددة، دفع، حالة."""

    barber_id = forms.ModelChoiceField(
        queryset=BarberProfile.objects.none(),
        label="الحلاق",
    )
    service_ids = forms.ModelMultipleChoiceField(
        queryset=Service.objects.none(),
        label="الخدمات",
        error_messages={"required": "اختر خدمة واحدة على الأقل."},
    )
    payment_method = forms.ChoiceField(
        choices=[(PaymentMethod.CASH, "نقدي"), (PaymentMethod.CARD, "بطاقة")],
        label="طريقة الدفع",
    )
    status = forms.ChoiceField(choices=TicketStatus.choices, label="الحالة")

    def __init__(self, ticket=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.ticket = ticket
        self.fields["barber_id"].queryset = BarberProfile.objects.filter(is_active=True).select_related(
            "user"
        )
        self.fields["service_ids"].queryset = Service.objects.filter(is_active=True).order_by("name")
        for field in self.fields.values():
            if hasattr(field.widget, "attrs"):
                field.widget.attrs.setdefault("class", "field-input")
        if ticket and not self.is_bound:
            self.fields["barber_id"].initial = ticket.barber_id
            item_ids = list(ticket.items.values_list("service_id", flat=True))
            if not item_ids and ticket.service_id:
                item_ids = [ticket.service_id]
            self.fields["service_ids"].initial = item_ids
            self.fields["payment_method"].initial = ticket.payment_method or PaymentMethod.CASH
            self.fields["status"].initial = ticket.status

    def clean_service_ids(self):
        services = self.cleaned_data.get("service_ids")
        if not services:
            raise ValidationError("اختر خدمة واحدة على الأقل.")
        for svc in services:
            if svc.base_price is None:
                raise ValidationError(f"الخدمة «{svc.name}» بدون سعر — حدّثها من الإعدادات.")
        return services


class _BaseUserCreateForm(forms.ModelForm):
    password = forms.CharField(widget=forms.PasswordInput, min_length=6)

    class Meta:
        model = User
        fields = ["username", "first_name", "last_name", "email", "password"]

    role_value: str | None = None

    def save(self, commit=True):
        user = super().save(commit=False)
        user.set_password(self.cleaned_data["password"])
        if self.role_value:
            user.role = self.role_value
        if commit:
            user.save()
        return user


class AdminCreateForm(_BaseUserCreateForm):
    role_value = UserRole.ADMIN


class CashierCreateForm(_BaseUserCreateForm):
    role_value = UserRole.CASHIER


class BarberCreateForm(_BaseUserCreateForm):
    role_value = UserRole.BARBER
    commission_pct = forms.DecimalField(
        min_value=Decimal("0"),
        max_value=Decimal("100"),
        initial=50,
        label="نسبة العمولة %",
    )

    def save(self, commit=True):
        user = super().save(commit=commit)
        if commit:
            BarberProfile.objects.create(
                user=user,
                name=user.get_full_name() or user.username,
                default_commission_pct=self.cleaned_data["commission_pct"],
            )
        return user


class BarberStandaloneForm(forms.Form):
    name = forms.CharField(max_length=120, label="اسم الحلاق")
    default_commission_pct = forms.DecimalField(
        min_value=Decimal("0"),
        max_value=Decimal("100"),
        initial=50,
        label="نسبة العمولة %",
    )


class BarberCommissionForm(forms.ModelForm):
    class Meta:
        model = BarberProfile
        fields = ["name", "default_commission_pct", "is_active"]
        labels = {
            "name": "الاسم",
            "default_commission_pct": "نسبة العمولة %",
            "is_active": "نشط",
        }


class UserEditForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ["first_name", "last_name", "email", "role"]
        labels = {
            "first_name": "الاسم الأول",
            "last_name": "الاسم الأخير",
            "email": "البريد",
            "role": "الدور",
        }


class TreasuryEntryForm(forms.Form):
    entry_type = forms.ChoiceField(
        choices=TreasuryEntryType.choices,
        label="نوع الحركة",
        widget=forms.Select(attrs={"class": "field-input", "id": "treasuryEntryType"}),
    )
    amount = forms.DecimalField(
        min_value=Decimal("0.01"),
        max_digits=12,
        decimal_places=2,
        label="المبلغ",
        widget=forms.NumberInput(attrs={"class": "field-input", "step": "0.01", "min": "0.01"}),
    )
    payment_method = forms.ChoiceField(
        choices=PaymentMethod.choices,
        label="طريقة السداد",
        initial=PaymentMethod.CASH,
        widget=forms.Select(attrs={"class": "field-input", "id": "treasuryPayMethod"}),
    )
    category_id = forms.ModelChoiceField(
        queryset=ExpenseCategory.objects.none(),
        required=False,
        label="تصنيف المصروف",
        empty_label="— اختر التصنيف —",
        widget=forms.Select(attrs={"class": "field-input", "id": "treasuryCategory"}),
    )
    description = forms.CharField(
        required=False,
        max_length=255,
        label="ملاحظة",
        widget=forms.TextInput(attrs={"class": "field-input", "placeholder": "اختياري"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["category_id"].queryset = ExpenseCategory.objects.filter(is_active=True).order_by(
            "sort_order", "name"
        )
        self.fields["entry_type"].choices = [
            (TreasuryEntryType.EXPENSE, "مصروف"),
            (TreasuryEntryType.DEPOSIT, "إيداع"),
        ]
        self.fields["payment_method"].choices = [
            (PaymentMethod.CASH, "نقدي"),
            (PaymentMethod.CARD, "بطاقة"),
        ]

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("entry_type") == TreasuryEntryType.EXPENSE and not cleaned.get("category_id"):
            raise forms.ValidationError("اختر تصنيف المصروف من القائمة.")
        if cleaned.get("entry_type") == TreasuryEntryType.DEPOSIT:
            cleaned["category_id"] = None
        return cleaned


class ExpenseCategoryForm(forms.ModelForm):
    class Meta:
        model = ExpenseCategory
        fields = ["name", "is_active"]
        widgets = {
            "name": forms.TextInput(attrs={"class": "field-input", "placeholder": "مثال: إيجار"}),
        }


class VIPBookingForm(forms.Form):
    """نموذج حجز VIP للعرسان والحفلات"""

    customer_name = forms.CharField(
        max_length=120,
        required=False,
        label="اسم العميل (اختياري)",
        widget=forms.TextInput(
            attrs={
                "class": "field-input",
                "placeholder": "يُولَّد تلقائياً: زبون #…",
            }
        ),
    )
    customer_phone = forms.CharField(
        max_length=30,
        required=False,
        label="رقم الهاتف (اختياري)",
        widget=forms.TextInput(attrs={"class": "field-input"}),
    )
    booking_type = forms.ChoiceField(
        choices=[
            ("VIP", "VIP"),
            ("WEDDING", "عرس"),
            ("EVENT", "حفل"),
            ("GROUP", "مجموعة"),
            ("CUSTOM", "مخصص"),
        ],
        label="نوع الحجز",
        widget=forms.Select(attrs={"class": "field-input"})
    )
    booking_date = forms.DateField(
        label="تاريخ الحجز",
        widget=forms.DateInput(attrs={"type": "date", "class": "field-input"})
    )
    booking_time = forms.TimeField(
        label="وقت الحجز",
        widget=forms.TimeInput(attrs={"type": "time", "class": "field-input"})
    )
    barbers_count = forms.IntegerField(
        min_value=1,
        initial=1,
        label="عدد الحلاقين",
        widget=forms.NumberInput(attrs={"class": "field-input"})
    )
    estimated_duration_hours = forms.DecimalField(
        min_value=Decimal("0.5"),
        initial=2,
        label="المدة المتوقعة (ساعات)",
        widget=forms.NumberInput(attrs={"step": "0.5", "class": "field-input"})
    )
    base_price = forms.DecimalField(
        min_value=Decimal("0"),
        max_digits=12,
        decimal_places=2,
        label="السعر الأساسي",
        widget=forms.NumberInput(attrs={"step": "0.01", "class": "field-input"})
    )
    discount_pct = forms.DecimalField(
        min_value=0,
        max_value=100,
        initial=0,
        label="نسبة الخصم %",
        widget=forms.NumberInput(attrs={"step": "0.01", "class": "field-input"})
    )
    description = forms.CharField(
        required=False,
        label="تفاصيل الخدمات",
        widget=forms.Textarea(attrs={"class": "field-input", "rows": 3})
    )
    special_requests = forms.CharField(
        required=False,
        label="طلبات خاصة",
        widget=forms.Textarea(attrs={"class": "field-input", "rows": 3})
    )
    assigned_barber_ids = forms.ModelMultipleChoiceField(
        queryset=BarberProfile.objects.none(),
        required=False,
        label="الحلاقون المشاركون",
        widget=forms.SelectMultiple(attrs={"class": "field-input", "size": 5}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["assigned_barber_ids"].queryset = BarberProfile.objects.filter(
            is_active=True
        ).order_by("name")
        self.fields["assigned_barber_ids"].label_from_instance = lambda obj: obj.display_name


class ReceiptGenerationForm(forms.Form):
    """نموذج توليد وصل للعملية"""

    RECEIPT_TYPE_CHOICES = [
        ('TICKET', 'وصل تذكرة'),
        ('PAYMENT', 'وصل دفع'),
    ]

    receipt_type = forms.ChoiceField(
        choices=RECEIPT_TYPE_CHOICES,
        label="نوع الوصل",
        widget=forms.Select(attrs={'class': 'field-input'})
    )
    note = forms.CharField(
        required=False,
        label="ملاحظة",
        widget=forms.Textarea(attrs={'class': 'field-input', 'rows': 2})
    )


class SystemSettingsForm(forms.Form):
    business_name = forms.CharField(required=False)
    business_phone = forms.CharField(required=False)
    business_address = forms.CharField(required=False, widget=forms.Textarea)
    currency = forms.CharField(required=False, initial="د.ل")
    theme = forms.ChoiceField(choices=[("light", "فاتح"), ("dark", "داكن")], required=False)

    @staticmethod
    def load_initial():
        keys = ("business_name", "business_phone", "business_address", "currency", "theme")
        data = {}
        for key in keys:
            row = SystemSetting.objects.filter(key=key).first()
            if row:
                data[key] = row.value
        return data
