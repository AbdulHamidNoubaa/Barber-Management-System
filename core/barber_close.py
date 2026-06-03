"""إغلاق حساب الحلاقين حسب الشفت (نطاق زمني واحد — لا تقسيم يوم تقويمي)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.db.models import Count, Sum
from django.utils import timezone

from accounts.models import BarberProfile
from core.models import BarberDailyClose, Shift, Ticket, TicketStatus
from core.shift_scope import shifts_overlapping_month
from core.shift_utils import shift_bounds, shift_display_range


def barber_shift_ticket_qs(barber: BarberProfile, shift: Shift):
    """كل واصلات الحلاق المكتملة ضمن هذا الشفت (بغض النظر عن تاريخ التقويم)."""
    return Ticket.objects.filter(
        barber=barber,
        shift=shift,
        status=TicketStatus.COMPLETED,
    )


def barber_shift_summary(barber: BarberProfile, shift: Shift) -> dict:
    qs = barber_shift_ticket_qs(barber, shift)
    agg = qs.aggregate(
        revenue=Sum("total"),
        commission=Sum("barber_commission_total"),
        cnt=Count("id"),
    )
    closed = BarberDailyClose.objects.filter(barber=barber, shift=shift).first()
    start, end = shift_bounds(shift)
    return {
        "barber": barber,
        "shift": shift,
        "shift_range": shift_display_range(shift),
        "range_start": start,
        "range_end": end,
        "close_date": timezone.localdate(start),
        "total_revenue": agg["revenue"] or Decimal("0"),
        "total_commission": agg["commission"] or Decimal("0"),
        "ticket_count": agg["cnt"] or 0,
        "is_closed": closed is not None,
        "close_record": closed,
    }


def close_barber_account(
    barber: BarberProfile, shift: Shift, user, *, note: str = ""
) -> BarberDailyClose:
    if BarberDailyClose.objects.filter(barber=barber, shift=shift).exists():
        raise ValueError(f"تم إغلاق حساب {barber.display_name} لهذا الشفت مسبقاً.")
    summary = barber_shift_summary(barber, shift)
    start, _ = shift_bounds(shift)
    return BarberDailyClose.objects.create(
        barber=barber,
        shift=shift,
        close_date=timezone.localdate(start),
        total_revenue=summary["total_revenue"],
        total_commission=summary["total_commission"],
        ticket_count=summary["ticket_count"],
        closed_by=user,
        note=note,
    )


# ─── إبقاء للتوافق مع استدعاءات قديمة (تجميع تقويمي اختياري) ───


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


def month_barber_summary_by_shifts(year: int, month: int) -> list[dict]:
    """جرد شهري حسب الشفتات التي تقاطع الشهر (لا تقسيم عند منتصف الليل)."""
    shift_ids = list(shifts_overlapping_month(year, month).values_list("pk", flat=True))
    barbers = BarberProfile.objects.filter(is_active=True).order_by("name")
    rows = []
    for bp in barbers:
        if shift_ids:
            qs = Ticket.objects.filter(
                barber=bp,
                status=TicketStatus.COMPLETED,
                shift_id__in=shift_ids,
            )
        else:
            qs = Ticket.objects.none()
        agg = qs.aggregate(
            revenue=Sum("total"),
            commission=Sum("barber_commission_total"),
            cnt=Count("id"),
        )
        closes = BarberDailyClose.objects.filter(
            barber=bp, shift_id__in=shift_ids
        ).count()
        rows.append(
            {
                "barber": bp,
                "revenue": agg["revenue"] or Decimal("0"),
                "commission": agg["commission"] or Decimal("0"),
                "ticket_count": agg["cnt"] or 0,
                "days_closed": closes,
                "shifts_closed": closes,
            }
        )
    return rows


def month_grand_total_by_shifts(year: int, month: int) -> dict:
    shift_ids = list(shifts_overlapping_month(year, month).values_list("pk", flat=True))
    if not shift_ids:
        return {"revenue": Decimal("0"), "commission": Decimal("0"), "ticket_count": 0}
    qs = Ticket.objects.filter(
        status=TicketStatus.COMPLETED,
        shift_id__in=shift_ids,
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
