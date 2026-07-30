from django.urls import path

from .views import ServerConfigurationView

app_name = "serverconfig"

urlpatterns = [
    path("", ServerConfigurationView.as_view(), name="server-config"),
]
