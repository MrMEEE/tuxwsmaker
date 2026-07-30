from django.contrib import admin
from django.conf import settings
from django.conf.urls.static import static
from django.urls import include, path
from django.views.generic import TemplateView

urlpatterns = [
    path("", TemplateView.as_view(template_name="dashboard.html"), name="dashboard"),
    path("admin/", admin.site.urls),
    path("users/", include("apps.users.urls")),
    path("os/", include("apps.catalog.urls")),
    path("partitions/", include("apps.layouts.urls")),
    path("packages/", include("apps.packages.urls")),
    path("playbooks/", include("apps.playbooks.urls")),
    path("builds/", include("apps.builds.urls")),
    path("configuration/", include("apps.serverconfig.urls")),
    path("api/", include("apps.api.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
