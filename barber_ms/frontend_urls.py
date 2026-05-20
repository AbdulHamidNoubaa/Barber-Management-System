from __future__ import annotations

from django.urls import include, path

from barber_ms import views

app_name = 'frontend'

urlpatterns = [
    path("dashboard/", views.dashboard, name="dashboard"),
    path("queue/", views.queue_view, name="queue"),
    path(
        "queue/barber-transactions/",
        views.queue_barber_transactions,
        name="queue_barber_transactions",
    ),
    path("reports/", views.reports_view, name="reports"),
    path("treasury/", views.treasury_view, name="treasury"),
    path("settings/", views.settings_view, name="settings"),
    path("barber-log/", views.barber_log_view, name="barber_log"),
    path("", include("barber_ms.urls_vip_receipt")),
]

