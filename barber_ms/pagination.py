"""مساعد التصفح الموحّد لصفحات الواجهة."""

from __future__ import annotations

from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator

DEFAULT_PER_PAGE = 20
QUEUE_PER_PAGE = 15
TREASURY_PER_PAGE = 25
RECEIPTS_PER_PAGE = 20
BARBER_TX_PER_PAGE = 20
VIP_COMPLETED_PER_PAGE = 12
SETTINGS_USERS_PER_PAGE = 12


def paginate_queryset(request, queryset, *, per_page: int = DEFAULT_PER_PAGE, page_param: str = "page"):
    """
    يُرجع (paginator, page_obj).
    يحافظ على بقية معاملات GET عند بناء روابط الصفحات عبر القالب.
    """
    paginator = Paginator(queryset, per_page)
    raw = request.GET.get(page_param, 1)
    try:
        page_number = int(raw)
    except (TypeError, ValueError):
        page_number = 1
    try:
        page_obj = paginator.page(page_number)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages or 1)
    return paginator, page_obj


def querystring_excluding_page(request, page_param: str = "page") -> str:
    """سلسلة استعلام GET بدون رقم الصفحة (للروابط)."""
    q = request.GET.copy()
    if page_param in q:
        del q[page_param]
    return q.urlencode()
