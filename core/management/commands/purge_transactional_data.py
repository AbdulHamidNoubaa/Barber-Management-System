"""
حذف بيانات التشغيل (طلبات، شفتات، زبائن، خزنة، …) مع الإبقاء على:
- حسابات المستخدمين (accounts.User)
- ملفات الحلاقين (BarberProfile)
- إعدادات المحل (SystemSetting)
- الخدمات، قوالب الشفت، تصنيفات المصروف، تجاوزات العمولة
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from accounts.models import BarberProfile, User
from core.models import (
    AuditEvent,
    BarberCommissionOverride,
    BarberDailyClose,
    CloseLedger,
    Customer,
    ExpenseCategory,
    Payment,
    Receipt,
    Service,
    ServiceCategory,
    Shift,
    ShiftTemplate,
    SystemSetting,
    Ticket,
    TicketItem,
    TreasuryEntry,
    VIPBarberPayout,
    VIPBooking,
)


class Command(BaseCommand):
    help = "حذف كل بيانات الطلبات والمعاملات مع الإبقاء على المستخدمين والإعدادات الأساسية."

    def add_arguments(self, parser):
        parser.add_argument(
            "--confirm",
            action="store_true",
            help="تنفيذ الحذف فعلياً (بدون هذا الخيار: عرض المعاينة فقط).",
        )

    def handle(self, *args, **options):
        preview = {
            "وصولات": Receipt.objects.count(),
            "مدفوعات": Payment.objects.count(),
            "بنود التذاكر": TicketItem.objects.count(),
            "تذاكر/طلبات": Ticket.objects.count(),
            "توزيع VIP على الحلاقين": VIPBarberPayout.objects.count(),
            "حجوزات VIP": VIPBooking.objects.count(),
            "إغلاقات يومية للحلاقين": BarberDailyClose.objects.count(),
            "إغلاقات شفت": CloseLedger.objects.count(),
            "حركات الخزنة": TreasuryEntry.objects.count(),
            "شفتات": Shift.objects.count(),
            "زبائن": Customer.objects.count(),
            "سجل تدقيق": AuditEvent.objects.count(),
        }

        self.stdout.write("=== معاينة ما سيُحذف ===")
        for label, n in preview.items():
            self.stdout.write(f"  {label}: {n}")

        self.stdout.write("\n=== يُحفظ ===")
        self.stdout.write(f"  مستخدمون: {User.objects.count()}")
        self.stdout.write(f"  حلاقون (ملفات): {BarberProfile.objects.count()}")
        self.stdout.write(f"  إعدادات المحل: {SystemSetting.objects.count()}")
        self.stdout.write(f"  خدمات: {Service.objects.count()}")
        self.stdout.write(f"  تصنيفات خدمات: {ServiceCategory.objects.count()}")
        self.stdout.write(f"  قوالب شفت: {ShiftTemplate.objects.count()}")
        self.stdout.write(f"  تصنيفات مصروف: {ExpenseCategory.objects.count()}")
        self.stdout.write(f"  تجاوزات عمولة: {BarberCommissionOverride.objects.count()}")

        if not options["confirm"]:
            self.stdout.write(
                self.style.WARNING(
                    "\nلم يُحذف شيء. للتنفيذ أضف: --confirm"
                )
            )
            return

        if not any(preview.values()):
            self.stdout.write(self.style.WARNING("لا توجد بيانات تشغيلية لحذفها."))
            return

        with transaction.atomic():
            deleted = {}
            for model, key in [
                (Receipt, "وصولات"),
                (Payment, "مدفوعات"),
                (TicketItem, "بنود التذاكر"),
                (Ticket, "تذاكر/طلبات"),
                (VIPBarberPayout, "توزيع VIP على الحلاقين"),
                (VIPBooking, "حجوزات VIP"),
                (BarberDailyClose, "إغلاقات يومية للحلاقين"),
                (CloseLedger, "إغلاقات شفت"),
                (TreasuryEntry, "حركات الخزنة"),
                (Shift, "شفتات"),
                (Customer, "زبائن"),
                (AuditEvent, "سجل تدقيق"),
            ]:
                n, _ = model.objects.all().delete()
                deleted[key] = n

        self.stdout.write(self.style.SUCCESS("\nتم الحذف بنجاح:"))
        for label, n in deleted.items():
            if n:
                self.stdout.write(f"  {label}: {n} سجل")

        self.stdout.write(self.style.SUCCESS(f"\nالمستخدمون المتبقون: {User.objects.count()}"))
