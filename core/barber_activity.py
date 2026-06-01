"""دمج واصلات POS وحجوزات VIP في سجل نشاط الحلاق."""

from __future__ import annotations

from decimal import Decimal

from django.db.models import Q, Sum

from accounts.models import BarberProfile
from core.models import Ticket, TicketStatus, VIPBooking


def _map_ticket_status_to_vip(status: str) -> str | None:
    if status == "COMPLETED":
        return "completed"
    if status == "CANCELLED":
        return "cancelled"
    if status in ("WAITING", "IN_PROGRESS"):
        return None
    return None


def _vip_activity_date_q(barber: BarberProfile, from_date, to_date) -> Q:
    """تاريخ النشاط VIP: موعد الحجز، أو إتمام الحجز، أو تاريخ توزيع مستحق الحلاق."""
    q = Q()
    if from_date:
        q &= Q(booking_date__gte=from_date)
    if to_date:
        q &= Q(booking_date__lte=to_date)
    booking_date_match = q

    completed_match = Q(status="completed")
    if from_date:
        completed_match &= Q(updated_at__date__gte=from_date)
    if to_date:
        completed_match &= Q(updated_at__date__lte=to_date)

    payout_match = Q(barber_payouts__barber=barber)
    if from_date:
        payout_match &= Q(barber_payouts__created_at__date__gte=from_date)
    if to_date:
        payout_match &= Q(barber_payouts__created_at__date__lte=to_date)

    return booking_date_match | completed_match | payout_match


def vip_bookings_for_barber(barber: BarberProfile, *, from_date=None, to_date=None, status=None):
    qs = (
        VIPBooking.objects.filter(
            Q(assigned_barbers=barber) | Q(barber_payouts__barber=barber)
        )
        .distinct()
        .select_related("customer")
        .prefetch_related("assigned_barbers", "receipts", "barber_payouts__barber")
    )
    if from_date or to_date:
        qs = qs.filter(_vip_activity_date_q(barber, from_date, to_date))
    if status:
        vip_st = _map_ticket_status_to_vip(status)
        if vip_st:
            qs = qs.filter(status=vip_st)
        elif status == "COMPLETED":
            qs = qs.filter(status="completed")
    return qs.order_by("-booking_date", "-id")


def barber_vip_share(booking: VIPBooking, barber: BarberProfile) -> tuple[Decimal, Decimal]:
    """حصة الحلاق من الحجز: (مبلغ معروض، عمولة/مستحق)."""
    payout = next(
        (p for p in booking.barber_payouts.all() if p.barber_id == barber.pk),
        None,
    )
    if payout:
        return payout.amount, payout.amount
    if booking.status != "completed":
        return Decimal("0"), Decimal("0")
    n = booking.assigned_barbers.count() or booking.barbers_count or 1
    share = (booking.final_price or Decimal("0")) / Decimal(n)
    pct = barber.default_commission_pct or Decimal("0")
    return share, share * pct / Decimal("100")


def barber_vip_stats(barber: BarberProfile, from_date=None, to_date=None) -> dict:
    qs = vip_bookings_for_barber(barber, from_date=from_date, to_date=to_date)
    completed = list(qs.filter(status="completed"))
    revenue = Decimal("0")
    commission = Decimal("0")
    for booking in completed:
        rev, comm = barber_vip_share(booking, barber)
        revenue += rev
        commission += comm
    return {
        "total": qs.count(),
        "completed": len(completed),
        "revenue": revenue,
        "commission": commission,
    }


def barber_ticket_stats(barber: BarberProfile, from_date=None, to_date=None) -> dict:
    qs = Ticket.objects.filter(barber=barber)
    if from_date:
        qs = qs.filter(completed_at__date__gte=from_date)
    if to_date:
        qs = qs.filter(completed_at__date__lte=to_date)
    completed = qs.filter(status=TicketStatus.COMPLETED)
    return {
        "total": qs.count(),
        "completed": completed.count(),
        "revenue": completed.aggregate(v=Sum("total"))["v"] or Decimal("0"),
        "commission": completed.aggregate(v=Sum("barber_commission_total"))["v"] or Decimal("0"),
    }


def barber_combined_stats(barber: BarberProfile, from_date=None, to_date=None) -> dict:
    pos = barber_ticket_stats(barber, from_date, to_date)
    vip = barber_vip_stats(barber, from_date, to_date)
    return {
        "total": pos["total"] + vip["total"],
        "completed": pos["completed"] + vip["completed"],
        "revenue": pos["revenue"] + vip["revenue"],
        "commission": pos["commission"] + vip["commission"],
        "pos_completed": pos["completed"],
        "vip_completed": vip["completed"],
        "pos_revenue": pos["revenue"],
        "vip_revenue": vip["revenue"],
    }
