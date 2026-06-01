from __future__ import annotations

from django.urls import include, path

from barber_ms import views

app_name = 'frontend'

urlpatterns = [
    path("dashboard/", views.dashboard, name="dashboard"),
    path("queue/", views.queue_view, name="queue"),
    path("transactions/", views.transactions_log_view, name="transactions_log"),
    path("barbers/", views.barbers_list_view, name="barbers_list"),
    path("barbers/<int:barber_id>/", views.barber_operations_view, name="barber_operations"),
    path(
        "queue/barber-transactions/",
        views.queue_barber_transactions,
        name="queue_barber_transactions",
    ),
    path("treasury/", views.treasury_view, name="treasury"),
    path("settings/", views.settings_view, name="settings"),
    path("daily-close/", views.daily_close_view, name="daily_close"),
    path("monthly-close/", views.monthly_close_view, name="monthly_close"),
    path("barber-log/", views.barber_log_view, name="barber_log"),
    path("", include("barber_ms.urls_vip_receipt")),
]

