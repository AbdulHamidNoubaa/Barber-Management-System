"""شفت واحد مفتوح — يمتد من وقت الفتح حتى الإغلاق الفعلي (بلا تقسيم تقويمي عند منتصف الليل)."""

from __future__ import annotations

import datetime

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from core.models import Shift


def shift_bounds(shift: Shift) -> tuple[datetime.datetime, datetime.datetime]:
    """نطاق الشفت الزمني: من started_at حتى ended_at أو الآن."""
    end = shift.ended_at or timezone.now()
    return shift.started_at, end


def shift_display_range(shift: Shift) -> str:
    """نص عرض مدة الشفت (قد يمتد لأكثر من يوم تقويمي)."""
    start = timezone.localtime(shift.started_at)
    if shift.ended_at:
        end = timezone.localtime(shift.ended_at)
        if start.date() == end.date():
            return f"{start:%d/%m/%Y} · {start:%H:%M} – {end:%H:%M}"
        return f"{start:%d/%m/%Y %H:%M} → {end:%d/%m/%Y %H:%M}"
    return f"{start:%d/%m/%Y %H:%M} — مفتوح"


def new_shift_name(at: datetime.datetime | None = None) -> str:
    """اسم شفت جديد حسب وقت الفعلي (وليس يوم التقويم فقط)."""
    at = at or timezone.now()
    local = timezone.localtime(at)
    base = f"شفت {local:%d/%m/%Y %H:%M}"
    n = Shift.objects.filter(name__startswith=base).count()
    if n == 0:
        return base
    return f"{base} ({n + 1})"


def daily_shift_name(day: datetime.date | None = None) -> str:
    """توافق مع الاستدعاءات القديمة — يُفضّل new_shift_name()."""
    return new_shift_name()


def get_open_shift() -> Shift | None:
    return (
        Shift.objects.filter(is_closed=False, ended_at__isnull=True)
        .order_by("-started_at")
        .first()
    )


def require_open_shift() -> Shift:
    shift = get_open_shift()
    if not shift:
        raise ValidationError(
            "لا يوجد شفت مفتوح. اضغط «فتح شفت» من لوحة التحكم أو نقطة البيع."
        )
    return shift


def shift_ui_context(user) -> dict:
    current = get_open_shift()
    return {
        "current_shift": current,
        "current_shift_range": shift_display_range(current) if current else "",
        "can_close_shift": current.can_close(user) if current else False,
        "next_shift_label": new_shift_name(),
    }


def recent_shifts_for_close(*, limit: int = 30):
    """شفتات للاختيار في الإغلاق اليومي: المفتوح ثم المغلقة حديثاً."""
    open_s = get_open_shift()
    closed = Shift.objects.filter(is_closed=True).order_by("-ended_at", "-id")[:limit]
    if open_s:
        return [open_s] + [s for s in closed if s.pk != open_s.pk]
    return list(closed)


@transaction.atomic
def open_shift(*, opened_by=None) -> Shift:
    """
    فتح شفت جديد يدوياً.
    — مسموح شفت واحد مفتوح فقط.
    — يمكن أن يمتد الشفت لأكثر من يوم تقويمي حتى الإغلاق اليدوي.
  """
    stale_open = (
        Shift.objects.select_for_update()
        .filter(is_closed=False, ended_at__isnull=True)
        .first()
    )
    if stale_open:
        raise ValidationError(
            f"يوجد شفت مفتوح: {stale_open.name}. أغلقه قبل فتح شفت جديد."
        )

    shift = Shift.objects.create(name=new_shift_name())
    if opened_by is not None:
        shift.assigned_cashiers.add(opened_by)
    return shift
