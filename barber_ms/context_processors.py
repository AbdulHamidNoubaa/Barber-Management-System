from __future__ import annotations

from django.utils import timezone

from core.models import SystemSetting


_AR_WEEKDAYS = (
    "الإثنين",
    "الثلاثاء",
    "الأربعاء",
    "الخميس",
    "الجمعة",
    "السبت",
    "الأحد",
)


def _time_greeting_ar() -> str:
    """Arabic greeting based on local time (TIME_ZONE, e.g. Africa/Tripoli UTC+2)."""
    h = timezone.localtime().hour
    if 5 <= h < 12:
        return "صباح الخير"
    if 12 <= h < 17:
        return "طاب نهارك"
    if 17 <= h < 22:
        return "مساء الخير"
    return "أهلاً بك"

_DEFAULTS = {
    "business_name": "Barber Pro",
    "business_phone": "",
    "business_address": "",
    "currency": "د",
    "logo": "",
    "theme": "ocean",
    # "0" = الكاشير يخصّص الحلاق والسعر (بدون دخول حلاقين) | "1" = دخول شاشة الحلاق
    "barber_login_enabled": "0",
}

THEMES = {
    "ocean": {
        "label": "أزرق محيطي",
        "primary": "#0c4a6e",
        "primary_hover": "#083554",
        "accent": "#0284c7",
        "accent_hover": "#0369a1",
        "gradient": "linear-gradient(135deg,#0c4a6e 0%,#155e75 40%,#164e63 100%)",
        "gradient_accent": "linear-gradient(135deg,#0284c7 0%,#0ea5e9 100%)",
    },
    "emerald": {
        "label": "أخضر زمردي",
        "primary": "#065f46",
        "primary_hover": "#064e3b",
        "accent": "#059669",
        "accent_hover": "#047857",
        "gradient": "linear-gradient(135deg,#065f46 0%,#047857 40%,#059669 100%)",
        "gradient_accent": "linear-gradient(135deg,#059669 0%,#34d399 100%)",
    },
    "royal": {
        "label": "بنفسجي ملكي",
        "primary": "#4c1d95",
        "primary_hover": "#3b0764",
        "accent": "#7c3aed",
        "accent_hover": "#6d28d9",
        "gradient": "linear-gradient(135deg,#4c1d95 0%,#5b21b6 40%,#6d28d9 100%)",
        "gradient_accent": "linear-gradient(135deg,#7c3aed 0%,#a78bfa 100%)",
    },
    "charcoal": {
        "label": "رمادي فحمي",
        "primary": "#1e293b",
        "primary_hover": "#0f172a",
        "accent": "#475569",
        "accent_hover": "#334155",
        "gradient": "linear-gradient(135deg,#1e293b 0%,#334155 40%,#475569 100%)",
        "gradient_accent": "linear-gradient(135deg,#475569 0%,#64748b 100%)",
    },
    "crimson": {
        "label": "أحمر داكن",
        "primary": "#7f1d1d",
        "primary_hover": "#661717",
        "accent": "#b91c1c",
        "accent_hover": "#991b1b",
        "gradient": "linear-gradient(135deg,#7f1d1d 0%,#991b1b 40%,#b91c1c 100%)",
        "gradient_accent": "linear-gradient(135deg,#b91c1c 0%,#ef4444 100%)",
    },
    "gold": {
        "label": "ذهبي فاخر",
        "primary": "#78350f",
        "primary_hover": "#633112",
        "accent": "#b45309",
        "accent_hover": "#92400e",
        "gradient": "linear-gradient(135deg,#78350f 0%,#92400e 40%,#b45309 100%)",
        "gradient_accent": "linear-gradient(135deg,#b45309 0%,#d97706 100%)",
    },
}


def system_settings(request):
    try:
        qs = SystemSetting.objects.filter(key__in=_DEFAULTS.keys()).values_list("key", "value")
        stored = dict(qs)
    except Exception:
        stored = {}
    merged = {k: stored.get(k) or v for k, v in _DEFAULTS.items()}
    theme_key = merged.get("theme", "ocean")
    theme = THEMES.get(theme_key, THEMES["ocean"])
    now_local = timezone.localtime()
    return {
        "sys": merged,
        "theme": theme,
        "themes": THEMES,
        "time_greeting": _time_greeting_ar(),
        "now_local": now_local,
        "weekday_ar": _AR_WEEKDAYS[now_local.weekday()],
        "barber_login_enabled": merged.get("barber_login_enabled", "0") == "1",
    }
