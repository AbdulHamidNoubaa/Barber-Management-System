"""عمليات مشتركة لتعديل وحذف تذاكر الطابور (الحلاقة العادية)."""

from __future__ import annotations

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction

from accounts.models import UserRole
from core.models import Payment, Receipt, Ticket, TicketStatus


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


@transaction.atomic
def apply_ticket_edit(ticket: Ticket, cleaned: dict) -> Ticket:
    ticket.barber = cleaned["barber_id"]
    service = cleaned.get("service_id")
    if service:
        ticket.service = service
        ticket.description = service.name
        ticket.customer.name = service.name
        ticket.customer.save(update_fields=["name", "updated_at"])
    ticket.payment_method = cleaned["payment_method"]

    new_status = cleaned["status"]
    if new_status != ticket.status:
        if new_status == TicketStatus.IN_PROGRESS and ticket.started_at is None:
            from django.utils import timezone

            ticket.started_at = timezone.now()
        if new_status in (TicketStatus.COMPLETED, TicketStatus.CANCELLED) and ticket.completed_at is None:
            from django.utils import timezone

            ticket.completed_at = timezone.now()
        ticket.status = new_status

    amt = cleaned.get("amount")
    if amt is not None and amt > 0:
        ticket.total = amt
        ticket.subtotal = amt
        pct = ticket.barber.default_commission_pct or Decimal("0")
        ticket.barber_commission_total = amt * pct / Decimal("100")
    elif amt is not None and amt == 0:
        ticket.total = Decimal("0")
        ticket.subtotal = Decimal("0")
        ticket.barber_commission_total = Decimal("0")

    ticket.save()
    for receipt in ticket.receipts.all():
        from core.receipt_utils import create_receipt_for_ticket, ticket_items_description

        receipt.amount = ticket.total
        receipt.payment_method = ticket.payment_method
        receipt.items_description = ticket_items_description(ticket)
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
