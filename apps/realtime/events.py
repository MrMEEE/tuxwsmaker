from __future__ import annotations

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.utils import timezone


def publish_event(scope: str, action: str, payload: dict | None = None) -> None:
    channel_layer = get_channel_layer()
    if channel_layer is None:
        return

    event = {
        "scope": scope,
        "action": action,
        "payload": payload or {},
        "timestamp": timezone.now().isoformat(),
    }
    async_to_sync(channel_layer.group_send)(
        "live_updates",
        {
            "type": "event.message",
            "event": event,
        },
    )
