"""عمليات مشتركة لتعديل وحذف تذاكر الطابور (الحلاقة العادية)."""

from __future__ import annotations

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from accounts.models import UserRole
from core.models import Payment, Receipt, Ticket, TicketItem, TicketStatus
from core.receipt_utils import ticket_items_description


def can_modify_ticket(ticket: Ticket, user) -> tuple[bool, str]:
    if ticket.locked_by_close_id:
        return False, "هذه المعاملة مقفلة بإغلاق يومي ولا يمكن تعديلها أو حذفها."
    if user.role == UserRole.ADMIN or user.is_superuser:
        return True, ""
    if ticket.shift.is_closed:
        return False, "الشفت مغلق — لا يمكن تعديل أو حذف المعاملة."
    return True, ""


@transaction.atomic
def delete_ticket_record(ticket: Ticket) -> None:
    Receipt.objects.filter(ticket=ticket).delete()
    Payment.objects.filter(ticket=ticket).delete()
    ticket.items.all().delete()
    ticket.delete()


def _sync_ticket_items(ticket: Ticket, services: list) -> None:
    """مزامنة بنود الوصل مع قائمة الخدمات المختارة."""
    wanted_ids = {s.pk for s in services}
    ticket.items.exclude(service_id__in=wanted_ids).delete()
    existing = set(ticket.items.values_list("service_id", flat=True))
    for svc in services:
        if svc.pk not in existing:
            TicketItem.objects.create(
                ticket=ticket,
                service=svc,
                price=svc.base_price or Decimal("0"),
            )
        else:
            item = ticket.items.filter(service_id=svc.pk).first()
            if item and item.price != (svc.base_price or Decimal("0")):
                item.price = svc.base_price or Decimal("0")
                item.commission_pct = Decimal("0")
                item.save()


@transaction.atomic
def apply_ticket_edit(ticket: Ticket, cleaned: dict) -> Ticket:
    ticket.barber = cleaned["barber_id"]
    services = list(cleaned["service_ids"])
    _sync_ticket_items(ticket, services)

    ticket.service = services[0]
    ticket.description = " + ".join(s.name for s in services)[:255]
    ticket.payment_method = cleaned["payment_method"]

    new_status = cleaned["status"]
    if new_status != ticket.status:
        if new_status == TicketStatus.IN_PROGRESS and ticket.started_at is None:
            ticket.started_at = timezone.now()
        if new_status in (TicketStatus.COMPLETED, TicketStatus.CANCELLED) and ticket.completed_at is None:
            ticket.completed_at = timezone.now()
        ticket.status = new_status

    ticket.recalc_totals()

    for payment in ticket.payments.all():
        payment.amount = ticket.total
        payment.method = ticket.payment_method
        payment.save(update_fields=["amount", "method", "updated_at"])

    desc = ticket_items_description(ticket)
    for receipt in ticket.receipts.all():
        receipt.amount = ticket.total
        receipt.payment_method = ticket.payment_method
        receipt.items_description = desc
        receipt.barber_name = ticket.barber.display_name
        receipt.save(
            update_fields=[
                "amount",
                "payment_method",
                "items_description",
                "barber_name",
                "updated_at",
            ]
        )
    return ticket
