from django.urls import path

from .views import LoginPageView, LogoutPageView

app_name = "users"

urlpatterns = [
    path("login/", LoginPageView.as_view(), name="login"),
    path("logout/", LogoutPageView.as_view(), name="logout"),
]
