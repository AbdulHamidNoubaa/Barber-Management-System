"""إغلاق الحساب اليومي والجرد الشهري للحلاقين."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.db.models import Count, Sum
from django.utils import timezone

from accounts.models import BarberProfile
from core.models import BarberDailyClose, Ticket, TicketStatus


def barber_day_ticket_qs(barber: BarberProfile, close_date: date):
    return Ticket.objects.filter(
        barber=barber,
        status=TicketStatus.COMPLETED,
        completed_at__date=close_date,
    )


def barber_day_summary(barber: BarberProfile, close_date: date) -> dict:
    qs = barber_day_ticket_qs(barber, close_date)
    agg = qs.aggregate(
        revenue=Sum("total"),
        commission=Sum("barber_commission_total"),
        cnt=Count("id"),
    )
    closed = BarberDailyClose.objects.filter(barber=barber, close_date=close_date).first()
    return {
        "barber": barber,
        "close_date": close_date,
        "total_revenue": agg["revenue"] or Decimal("0"),
        "total_commission": agg["commission"] or Decimal("0"),
        "ticket_count": agg["cnt"] or 0,
        "is_closed": closed is not None,
        "close_record": closed,
    }


def close_barber_account(barber: BarberProfile, close_date: date, user, *, note: str = "") -> BarberDailyClose:
    if BarberDailyClose.objects.filter(barber=barber, close_date=close_date).exists():
        raise ValueError(f"تم إغلاق حساب {barber.display_name} لهذا اليوم مسبقاً.")
    summary = barber_day_summary(barber, close_date)
    shift = (
        Ticket.objects.filter(barber=barber, completed_at__date=close_date)
        .order_by("-completed_at")
        .values_list("shift_id", flat=True)
        .first()
    )
    return BarberDailyClose.objects.create(
        barber=barber,
        close_date=close_date,
        shift_id=shift,
        total_revenue=summary["total_revenue"],
        total_commission=summary["total_commission"],
        ticket_count=summary["ticket_count"],
        closed_by=user,
        note=note,
    )


def month_barber_summary(year: int, month: int) -> list[dict]:
    barbers = BarberProfile.objects.filter(is_active=True).order_by("name")
    rows = []
    for bp in barbers:
        qs = Ticket.objects.filter(
            barber=bp,
            status=TicketStatus.COMPLETED,
            completed_at__year=year,
            completed_at__month=month,
        )
        agg = qs.aggregate(
            revenue=Sum("total"),
            commission=Sum("barber_commission_total"),
            cnt=Count("id"),
        )
        closes = BarberDailyClose.objects.filter(
            barber=bp, close_date__year=year, close_date__month=month
        ).count()
        rows.append(
            {
                "barber": bp,
                "revenue": agg["revenue"] or Decimal("0"),
                "commission": agg["commission"] or Decimal("0"),
                "ticket_count": agg["cnt"] or 0,
                "days_closed": closes,
            }
        )
    return rows


def month_grand_total(year: int, month: int) -> dict:
    qs = Ticket.objects.filter(
        status=TicketStatus.COMPLETED,
        completed_at__year=year,
        completed_at__month=month,
    )
    agg = qs.aggregate(
        revenue=Sum("total"),
        commission=Sum("barber_commission_total"),
        cnt=Count("id"),
    )
    return {
        "revenue": agg["revenue"] or Decimal("0"),
        "commission": agg["commission"] or Decimal("0"),
        "ticket_count": agg["cnt"] or 0,
    }
