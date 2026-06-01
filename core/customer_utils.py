"""إنشاء زبائن بأسماء تلقائية متسلسلة."""

from __future__ import annotations

from core.models import Customer

AUTO_CUSTOMER_PREFIX = "زبون"


def auto_customer_name(pk: int) -> str:
    return f"{AUTO_CUSTOMER_PREFIX} #{pk}"


def is_auto_customer_name(name: str) -> bool:
    n = (name or "").strip()
    if not n.startswith(AUTO_CUSTOMER_PREFIX):
        return False
    rest = n[len(AUTO_CUSTOMER_PREFIX) :].strip()
    return rest.startswith("#") and rest[1:].isdigit()


def resolve_customer_name(name: str | None, *, customer_pk: int | None = None) -> str:
    """اسم يدوي إن وُجد، وإلا الاسم التلقائي حسب رقم السجل."""
    manual = (name or "").strip()
    if manual:
        return manual
    if customer_pk:
        return auto_customer_name(customer_pk)
    last_id = Customer.objects.order_by("-id").values_list("id", flat=True).first() or 0
    return auto_customer_name(last_id + 1)


def get_or_create_customer(*, name: str | None = None, phone: str | None = None) -> tuple[Customer, bool]:
    """
    إنشاء زبون أو ربطه بالهاتف إن وُجد.
    بدون اسم يُعيَّن «زبون #<id>» بعد الحفظ.
    """
    phone = (phone or "").strip()
    manual_name = (name or "").strip()

    if phone:
        existing = Customer.objects.filter(phone=phone).order_by("id").first()
        if existing:
            if manual_name and existing.name != manual_name:
                existing.name = manual_name
                existing.save(update_fields=["name", "updated_at"])
            return existing, False

    if manual_name:
        return Customer.objects.create(name=manual_name, phone=phone), True

    customer = Customer.objects.create(name="-", phone=phone)
    final_name = auto_customer_name(customer.pk)
    if customer.name != final_name:
        Customer.objects.filter(pk=customer.pk).update(name=final_name)
        customer.name = final_name
    return customer, True
