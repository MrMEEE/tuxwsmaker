from channels.auth import AuthMiddlewareStack
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.security.websocket import AllowedHostsOriginValidator

from apps.realtime.routing import websocket_urlpatterns


websocket_application = AllowedHostsOriginValidator(
    AuthMiddlewareStack(
        URLRouter(websocket_urlpatterns),
    )
)

application = ProtocolTypeRouter(
    {
        "websocket": websocket_application,
    }
)
