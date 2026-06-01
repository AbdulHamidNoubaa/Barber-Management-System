"""إعادة تسمية الزبائن الذين ليس لهم اسم حقيقي إلى «زبون #id»."""

from django.core.management.base import BaseCommand

from core.customer_utils import AUTO_CUSTOMER_PREFIX, auto_customer_name, is_auto_customer_name
from core.models import Customer


class Command(BaseCommand):
    help = "تعيين أسماء تلقائية (زبون #رقم) للزبائن بدون اسم واضح أو بأسماء وهمية قديمة."

    def add_arguments(self, parser):
        parser.add_argument(
            "--all",
            action="store_true",
            help="إعادة تسمية كل الزبائن (يستبدل الأسماء الحالية).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="عرض التغييرات دون الحفظ.",
        )

    def handle(self, *args, **options):
        dry = options["dry_run"]
        rename_all = options["all"]
        updated = 0
        for c in Customer.objects.order_by("id"):
            target = auto_customer_name(c.pk)
            if rename_all:
                if c.name != target:
                    self.stdout.write(f"  {c.pk}: {c.name!r} -> {target!r}")
                    if not dry:
                        Customer.objects.filter(pk=c.pk).update(name=target)
                    updated += 1
                continue
            if is_auto_customer_name(c.name):
                continue
            name = (c.name or "").strip()
            if not name or name in ("واصل", "-") or name.startswith("خدمة"):
                self.stdout.write(f"  {c.pk}: {name!r} -> {target!r}")
                if not dry:
                    Customer.objects.filter(pk=c.pk).update(name=target)
                updated += 1
        verb = "سيُحدَّث" if dry else "تم تحديث"
        self.stdout.write(self.style.SUCCESS(f"{verb} {updated} زبون/زبائن ({AUTO_CUSTOMER_PREFIX} #…)."))
