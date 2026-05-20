from __future__ import annotations

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from accounts.models import BarberProfile, User, UserRole


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    list_display = ("username", "email", "first_name", "last_name", "role", "is_staff", "is_active")
    list_filter = ("role", "is_staff", "is_active")
    fieldsets = DjangoUserAdmin.fieldsets + (
        ("Business", {"fields": ("role",)}),
    )

    def has_module_permission(self, request):
        if request.user.is_superuser:
            return True
        return getattr(request.user, "role", None) == UserRole.ADMIN

    def has_view_permission(self, request, obj=None):
        return self.has_module_permission(request)

    def has_add_permission(self, request):
        return self.has_module_permission(request)

    def has_change_permission(self, request, obj=None):
        return self.has_module_permission(request)

    def has_delete_permission(self, request, obj=None):
        return self.has_module_permission(request)


@admin.register(BarberProfile)
class BarberProfileAdmin(admin.ModelAdmin):
    list_display = ("barber_display_name", "user", "default_commission_pct", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name", "user__username", "user__first_name", "user__last_name")

    @admin.display(description="الاسم")
    def barber_display_name(self, obj: BarberProfile) -> str:
        return obj.display_name

    def has_module_permission(self, request):
        if request.user.is_superuser:
            return True
        return getattr(request.user, "role", None) == UserRole.ADMIN
