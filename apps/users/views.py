from django.contrib.auth.views import LoginView, LogoutView


class LoginPageView(LoginView):
	template_name = "users/login.html"


class LogoutPageView(LogoutView):
	pass

# Create your views here.
