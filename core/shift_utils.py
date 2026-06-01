"""شفت واحد مفتوح في كل مرة — فتح وإغلاق يدوي (يمكن أكثر من شفت في اليوم بعد الإغلاق)."""

from __future__ import annotations

import datetime

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from core.models import Shift


def daily_shift_name(day: datetime.date | None = None) -> str:
    """اسم الشفت الجديد — إن وُجد أكثر من شفت في نفس اليوم يُضاف رقم تسلسلي."""
    day = day or timezone.localdate()
    base = f"شفت {day:%d/%m/%Y}"
    count_today = Shift.objects.filter(started_at__date=day).count()
    if count_today == 0:
        return base
    return f"{base} ({count_today + 1})"


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
        "can_close_shift": current.can_close(user) if current else False,
        "next_shift_label": daily_shift_name(),
    }


@transaction.atomic
def open_shift(*, opened_by=None) -> Shift:
    """
    فتح شفت جديد يدوياً.
    — مسموح شفت واحد مفتوح فقط في كل وقت.
    — بعد الإغلاق يمكن فتح شفت آخر (حتى في نفس اليوم، مثلاً بعد منتصف الليل ثم الصباح).
    """
    today = timezone.localdate()

    stale_open = (
        Shift.objects.select_for_update()
        .filter(is_closed=False, ended_at__isnull=True)
        .first()
    )
    if stale_open:
        if stale_open.started_at.date() < today:
            raise ValidationError(
                f"شفت {stale_open.started_at:%d/%m/%Y} ما زال مفتوحاً. أغلقه أولاً ثم افتح شفتاً جديداً."
            )
        raise ValidationError(
            f"يوجد شفت مفتوح: {stale_open.name}. أغلقه قبل فتح شفت جديد."
        )

    shift = Shift.objects.create(name=daily_shift_name(today))
    if opened_by is not None:
        shift.assigned_cashiers.add(opened_by)
    return shift
