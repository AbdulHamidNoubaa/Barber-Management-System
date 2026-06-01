"""إنشاء الوصولات وربطها بالتذاكر."""

from __future__ import annotations

from decimal import Decimal

from django.db import transaction

from core.models import Payment, PaymentMethod, Receipt, ReceiptType, Ticket, TicketStatus


def ticket_items_description(ticket: Ticket) -> str:
    """نص الخدمات على الوصل المطبوع (بدون عمولة الحلاق)."""
    items = list(ticket.items.select_related("service").order_by("id"))
    if items:
        return "\n".join(f"{i.service.name} — {int(i.price)}" for i in items)
    if ticket.service_id:
        svc = ticket.service
        if ticket.total > 0:
            return f"{svc.name} — {int(ticket.total)}"
        return svc.name
    if ticket.description:
        return ticket.description
    if ticket.total > 0:
        return f"خدمة حلاقة — {int(ticket.total)}"
    return "خدمة حلاقة"


@transaction.atomic
def create_receipt_for_ticket(ticket: Ticket, *, issued_by) -> Receipt:
    """إنشاء وصل للتذكرة مع رقم الشفت التسلسلي (بدون اسم الزبون على الطباعة)."""
    existing = ticket.receipts.filter(receipt_type=ReceiptType.TICKET).first()
    if existing:
        existing.amount = ticket.total
        existing.payment_method = ticket.payment_method or PaymentMethod.CASH
        existing.items_description = ticket_items_description(ticket)
        existing.shift_sequence_number = ticket.shift_sequence
        existing.barber_name = ticket.barber.display_name
        existing.shift = ticket.shift
        existing.save(
            update_fields=[
                "amount",
                "payment_method",
                "items_description",
                "shift_sequence_number",
                "barber_name",
                "shift",
                "updated_at",
            ]
        )
        return existing

    return Receipt.objects.create(
        receipt_type=ReceiptType.TICKET,
        receipt_number=Receipt.generate_receipt_number(),
        ticket=ticket,
        customer_name="",
        customer_phone="",
        shift_sequence_number=ticket.shift_sequence,
        barber_name=ticket.barber.display_name,
        amount=ticket.total or Decimal("0"),
        payment_method=ticket.payment_method or PaymentMethod.CASH,
        issued_by=issued_by,
        items_description=ticket_items_description(ticket),
        shift=ticket.shift,
    )


@transaction.atomic
def complete_ticket_sale(ticket: Ticket, *, amount, method: str, user) -> Receipt:
    """إتمام التذكرة: دفع، إغلاق، تسلسل شفت، وصل."""
    amount = Decimal(str(amount))
    ticket.total = amount
    ticket.subtotal = amount
    ticket.payment_method = method
    pct = ticket.barber.default_commission_pct or Decimal("0")
    ticket.barber_commission_total = amount * pct / Decimal("100")
    ticket.save(
        update_fields=[
            "total",
            "subtotal",
            "barber_commission_total",
            "payment_method",
            "updated_at",
        ]
    )
    Payment.objects.create(
        ticket=ticket,
        method=method,
        amount=amount,
        received_by=user,
    )
    ticket.set_status(TicketStatus.COMPLETED, by_user=user)
    ensure_ticket_shift_sequence(ticket)
    return create_receipt_for_ticket(ticket, issued_by=user)


def ensure_ticket_shift_sequence(ticket: Ticket) -> int:
    if ticket.shift_sequence:
        return ticket.shift_sequence
    from core.models import allocate_shift_sequence

    seq = allocate_shift_sequence(ticket.shift)
    ticket.shift_sequence = seq
    ticket.save(update_fields=["shift_sequence", "updated_at"])
    return seq
