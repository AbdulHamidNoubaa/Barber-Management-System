from __future__ import annotations

from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.db import models


class UserRole(models.TextChoices):
    ADMIN = "ADMIN", "Admin"
    CASHIER = "CASHIER", "Cashier"
    BARBER = "BARBER", "Barber"


class User(AbstractUser):
    role = models.CharField(max_length=20, choices=UserRole.choices, default=UserRole.CASHIER)

    def is_admin(self) -> bool:
        return self.role == UserRole.ADMIN or self.is_superuser


class BarberProfile(models.Model):
    """حلاق: إما مرتبط بحساب مستخدم (وضع قديم) أو اسم فقط يُدار من الكاشير."""

    user = models.OneToOneField(
        "accounts.User",
        on_delete=models.CASCADE,
        related_name="barber_profile",
        null=True,
        blank=True,
    )
    name = models.CharField(max_length=120, blank=True, default="", verbose_name="اسم الحلاق")
    default_commission_pct = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def clean(self) -> None:
        super().clean()
        if not self.user_id and not (self.name or "").strip():
            raise ValidationError({"name": "أدخل اسماً للحلاق أو اربط حساب مستخدم."})

    def save(self, *args, **kwargs):
        if self.user_id and not (self.name or "").strip():
            u = self.user
            self.name = (u.get_full_name() or u.username or "")[:120]
        super().save(*args, **kwargs)

    @property
    def display_name(self) -> str:
        n = (self.name or "").strip()
        if n:
            return n
        if self.user_id:
            return self.user.get_full_name() or self.user.username
        return "حلاق"

    @property
    def initial_letter(self) -> str:
        d = self.display_name
        return d[:1].upper() if d else "?"

    def __str__(self) -> str:
        return self.display_name
