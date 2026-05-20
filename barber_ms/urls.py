from django.conf import settings
from django.conf.urls.static import static
from django.urls import include, path

from barber_ms import views

urlpatterns = [
    path("", views.root_redirect, name="root"),
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("app/", include("barber_ms.frontend_urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
