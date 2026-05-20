from __future__ import annotations

from decimal import Decimal

from django import forms
from django.core.exceptions import ValidationError

from accounts.models import BarberProfile, User, UserRole
from core.models import ExpenseCategory, PaymentMethod, SystemSetting, TicketStatus, TreasuryEntryType


class QueueTicketForm(forms.Form):
    customer_name = forms.CharField(max_length=120)
    customer_phone = forms.CharField(max_length=30, required=False)
    barber_id = forms.ModelChoiceField(queryset=BarberProfile.objects.none())
    description = forms.CharField(max_length=255, required=False)
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
        self.fields["customer_name"].widget.attrs.update({
            "class": "field-input pos-input-name",
            "placeholder": "اسم الزبون",
            "autocomplete": "name",
            "id": "posCustomerName",
        })
        self.fields["customer_phone"].widget.attrs.update({
            "class": "field-input",
            "placeholder": "09xxxxxxxx",
            "inputmode": "tel",
            "id": "posCustomerPhone",
        })
        self.fields["description"].widget.attrs.update({
            "class": "field-input",
            "placeholder": "قص شعر، لحية، صبغة…",
            "id": "posDescription",
            "rows": 2,
        })
        self.fields["initial_amount"].widget.attrs.update({
            "class": "field-input pos-input-amount",
            "placeholder": "0",
            "inputmode": "decimal",
            "id": "posAmount",
            "min": "0",
            "step": "1",
        })
        self.fields["barber_id"].widget.attrs.update({"id": "posBarberSelect"})
        self.fields["payment_method"].widget.attrs.update({"id": "posPaymentSelect"})


class TicketEditForm(forms.Form):
    """تعديل تذكرة طابور (حلاقة عادية)."""

    customer_name = forms.CharField(max_length=120, label="اسم الزبون")
    customer_phone = forms.CharField(max_length=30, required=False, label="الجوال")
    barber_id = forms.ModelChoiceField(
        queryset=BarberProfile.objects.none(),
        label="الحلاق",
    )
    description = forms.CharField(max_length=255, required=False, label="الخدمة / ملاحظة")
    amount = forms.DecimalField(
        required=False,
        min_value=Decimal("0"),
        max_digits=12,
        decimal_places=2,
        label="المبلغ",
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
        for field in self.fields.values():
            field.widget.attrs.setdefault("class", "field-input")
        if ticket and not self.is_bound:
            self.fields["customer_name"].initial = ticket.customer.name
            self.fields["customer_phone"].initial = ticket.customer.phone or ""
            self.fields["barber_id"].initial = ticket.barber_id
            self.fields["description"].initial = ticket.description
            self.fields["amount"].initial = ticket.total if ticket.total > 0 else None
            self.fields["payment_method"].initial = ticket.payment_method or PaymentMethod.CASH
            self.fields["status"].initial = ticket.status


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
    default_commission_pct = forms.DecimalField(max_digits=5, decimal_places=2, min_value=0, max_value=100, initial=0)
    role_value = UserRole.BARBER

    def save(self, commit=True):
        user = super().save(commit=commit)
        if commit:
            nm = (user.get_full_name() or user.username or "")[:120]
            BarberProfile.objects.update_or_create(
                user=user,
                defaults={
                    "default_commission_pct": self.cleaned_data["default_commission_pct"],
                    "is_active": True,
                    "name": nm,
                },
            )
        return user


class BarberStandaloneForm(forms.Form):
    """حلاق بدون حساب دخول — يُعرض اسمه للكاشير فقط."""

    name = forms.CharField(max_length=120, label="اسم الحلاق")
    default_commission_pct = forms.DecimalField(
        max_digits=5, decimal_places=2, min_value=0, max_value=100, initial=0
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.update({"class": "field-input"})


class BarberCommissionForm(forms.ModelForm):
    class Meta:
        model = BarberProfile
        fields = ["name", "default_commission_pct", "is_active"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["name"].widget.attrs.update(
            {"class": "field-input", "maxlength": "120", "required": True}
        )
        self.fields["default_commission_pct"].widget.attrs.update(
            {"class": "field-input st-barber-pct-input", "step": "0.01", "min": "0", "max": "100"}
        )
        self.fields["is_active"].widget.attrs.update({"class": "field-checkbox"})


class UserEditForm(forms.ModelForm):
    """Update staff user details; optional password change when the password field is non-empty."""

    password = forms.CharField(widget=forms.PasswordInput, required=False)

    class Meta:
        model = User
        fields = ["username", "first_name", "last_name", "email", "role"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.update({"class": "field-input"})

    def save(self, commit=True):
        user = super().save(commit=False)
        pwd = self.cleaned_data.get("password") or ""
        if pwd:
            user.set_password(pwd)
        if commit:
            user.save()
        return user


class SystemSettingForm(forms.ModelForm):
    class Meta:
        model = SystemSetting
        fields = ["key", "value", "description"]


class TreasuryEntryForm(forms.Form):
    """تسجيل حركة خزنة: مصروف (يتطلب تصنيفاً) أو إيداع."""

    entry_type = forms.ChoiceField(choices=TreasuryEntryType.choices, label="نوع الحركة")
    amount = forms.DecimalField(
        min_value=Decimal("0.01"),
        max_digits=12,
        decimal_places=2,
        label="المبلغ",
    )
    payment_method = forms.ChoiceField(
        choices=[(PaymentMethod.CASH, "نقدي"), (PaymentMethod.CARD, "بطاقة")],
        initial=PaymentMethod.CASH,
        label="طريقة السداد",
    )
    category = forms.ModelChoiceField(
        queryset=ExpenseCategory.objects.none(),
        required=False,
        label="تصنيف المصروف",
    )
    description = forms.CharField(max_length=255, required=False, label="ملاحظة (اختياري)")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["category"].queryset = ExpenseCategory.objects.filter(is_active=True).order_by(
            "sort_order", "name"
        )
        self.fields["category"].empty_label = "— اختر التصنيف —"
        for field in self.fields.values():
            field.widget.attrs.update({"class": "field-input"})

    def clean(self):
        cleaned = super().clean()
        et = cleaned.get("entry_type")
        cat = cleaned.get("category")
        if et == TreasuryEntryType.EXPENSE and not cat:
            raise ValidationError("اختر تصنيفاً للمصروف.")
        if et == TreasuryEntryType.DEPOSIT and cat:
            self.add_error("category", "الإيداع لا يستخدم تصنيف مصروف — اترك الحقل فارغاً.")
        return cleaned


class ExpenseCategoryForm(forms.Form):
    name = forms.CharField(max_length=80, label="اسم التصنيف الجديد")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["name"].widget.attrs.update({"class": "field-input"})


class VIPBookingForm(forms.Form):
    """نموذج حجز VIP للعرسان والحفلات"""
    
    customer_name = forms.CharField(
        max_length=120,
        label="اسم المتلقي",
        widget=forms.TextInput(attrs={"class": "field-input"})
    )
    customer_phone = forms.CharField(
        max_length=30,
        label="رقم الهاتف",
        widget=forms.TextInput(attrs={"class": "field-input"})
    )
    booking_type = forms.ChoiceField(
        choices=[
            ('WEDDING', 'عرس'),
            ('EVENT', 'حفل'),
            ('GROUP', 'مجموعة'),
            ('CUSTOM', 'مخصص'),
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


class ReceiptGenerationForm(forms.Form):
    """نموذج توليد وصل للعملية"""
    
    RECEIPT_TYPE_CHOICES = [
        ('TICKET', 'وصل تذكرة'),
        ('PAYMENT', 'وصل دفع'),
    ]
    
    receipt_type = forms.ChoiceField(
        choices=RECEIPT_TYPE_CHOICES,
        label="نوع الوصل",
        widget=forms.Select(attrs={"class": "field-input"})
    )
    note = forms.CharField(
        required=False,
        label="ملاحظات إضافية",
        widget=forms.Textarea(attrs={"class": "field-input", "rows": 3})
    )
