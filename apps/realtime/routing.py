from django.urls import re_path

from .consumers import LiveUpdatesConsumer


websocket_urlpatterns = [
    re_path(r"ws/updates/$", LiveUpdatesConsumer.as_asgi()),
]
