"""نطاق التقارير: حسب الشفت (زمني) أو حسب التاريخ التقويمي."""

from __future__ import annotations

import calendar
import datetime
from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.db.models import Q, QuerySet
from django.utils import timezone

if TYPE_CHECKING:
    from core.models import Shift


@dataclass
class ReportScope:
    mode: str  # "shift" | "date"
    shift: Shift | None = None
    from_date: datetime.date | None = None
    to_date: datetime.date | None = None

    @property
    def is_shift(self) -> bool:
        return self.mode == "shift" and self.shift is not None


def _month_bounds(year: int, month: int) -> tuple[datetime.datetime, datetime.datetime]:
    tz = timezone.get_current_timezone()
    start = timezone.make_aware(datetime.datetime(year, month, 1, 0, 0, 0), tz)
    last_day = calendar.monthrange(year, month)[1]
    end = timezone.make_aware(
        datetime.datetime(year, month, last_day, 23, 59, 59, 999999),
        tz,
    )
    return start, end


def shifts_overlapping_month(year: int, month: int) -> QuerySet:
    """كل الشفتات التي تتقاطع مع الشهر (بما فيها الممتدة عبر منتصف الليل)."""
    from core.models import Shift

    start, end = _month_bounds(year, month)
    return (
        Shift.objects.filter(started_at__lte=end)
        .filter(Q(ended_at__gte=start) | Q(ended_at__isnull=True))
        .order_by("-started_at")
    )


def parse_report_scope(
    request,
    *,
    prefer_open_shift: bool = True,
) -> ReportScope:
    """من GET/POST: scope=shift|date، shift_id، from، to."""
    from core.models import Shift
    from core.shift_utils import get_open_shift

    src = request.GET if request.method == "GET" else {**request.GET.dict(), **request.POST}
    mode = (src.get("scope") or "").strip().lower()
    shift_id = (src.get("shift_id") or "").strip()
    from_raw = (src.get("from") or "").strip()
    to_raw = (src.get("to") or "").strip()

    shift = None
    if shift_id.isdigit():
        shift = Shift.objects.filter(pk=int(shift_id)).first()

    from_date = to_date = None
    if from_raw:
        try:
            from_date = datetime.date.fromisoformat(from_raw[:10])
        except ValueError:
            pass
    if to_raw:
        try:
            to_date = datetime.date.fromisoformat(to_raw[:10])
        except ValueError:
            pass

    if mode == "shift":
        if shift:
            return ReportScope(mode="shift", shift=shift)
        open_s = get_open_shift()
        if open_s:
            return ReportScope(mode="shift", shift=open_s)

    if mode != "date" and prefer_open_shift and not from_date and not to_date:
        open_s = get_open_shift()
        if open_s:
            return ReportScope(mode="shift", shift=open_s)

    today = timezone.localdate()
    if not from_date and not to_date:
        from_date = to_date = today
    elif from_date and not to_date:
        to_date = from_date
    elif to_date and not from_date:
        from_date = to_date

    return ReportScope(mode="date", from_date=from_date, to_date=to_date)


def scope_querystring_params(scope: ReportScope) -> dict[str, str]:
    if scope.is_shift and scope.shift:
        return {"scope": "shift", "shift_id": str(scope.shift.pk)}
    q: dict[str, str] = {"scope": "date"}
    if scope.from_date:
        q["from"] = scope.from_date.isoformat()
    if scope.to_date:
        q["to"] = scope.to_date.isoformat()
    return q


def filter_tickets(qs: QuerySet, scope: ReportScope) -> QuerySet:
    if scope.is_shift:
        return qs.filter(shift=scope.shift)
    if scope.from_date:
        qs = qs.filter(completed_at__date__gte=scope.from_date)
    if scope.to_date:
        qs = qs.filter(completed_at__date__lte=scope.to_date)
    return qs


def filter_tickets_created(qs: QuerySet, scope: ReportScope) -> QuerySet:
    """للسجل قبل الإكمال — حسب created_at."""
    if scope.is_shift:
        return qs.filter(shift=scope.shift)
    if scope.from_date:
        qs = qs.filter(created_at__date__gte=scope.from_date)
    if scope.to_date:
        qs = qs.filter(created_at__date__lte=scope.to_date)
    return qs


def filter_vip_bookings(qs: QuerySet, scope: ReportScope) -> QuerySet:
    if scope.is_shift:
        return qs.filter(shift=scope.shift)
    if scope.from_date:
        qs = qs.filter(updated_at__date__gte=scope.from_date)
    if scope.to_date:
        qs = qs.filter(updated_at__date__lte=scope.to_date)
    return qs


def filter_treasury_entries(qs: QuerySet, scope: ReportScope) -> QuerySet:
    if scope.is_shift:
        return qs.filter(shift=scope.shift)
    if scope.from_date:
        qs = qs.filter(created_at__date__gte=scope.from_date)
    if scope.to_date:
        qs = qs.filter(created_at__date__lte=scope.to_date)
    return qs
