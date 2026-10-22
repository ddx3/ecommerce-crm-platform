"""WebSocket consumer for the CRM notification counter.

Staff-only socket in the `admin_notifications` group. On connect it immediately
sends the current counters; afterwards it receives updates on any data change
(the broadcast is sent by model signals via broadcast_notification_counts).
"""

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer

from .notifications import GROUP_NOTIFICATIONS, get_notification_counts


class NotificationConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        user = self.scope.get('user')
        if not (getattr(user, 'is_authenticated', False) and user.is_staff):
            await self.close(code=4403)
            return
        await self.channel_layer.group_add(GROUP_NOTIFICATIONS, self.channel_name)
        await self.accept()
        # Initial state — so the badge doesn't wait for the first data change.
        counts = await database_sync_to_async(get_notification_counts)()
        await self.send_json({'type': 'counts', 'payload': counts})

    async def disconnect(self, code):
        await self.channel_layer.group_discard(GROUP_NOTIFICATIONS, self.channel_name)

    async def notif_counts(self, event):
        """Handler for the group_send event `notif.counts`."""
        await self.send_json({'type': 'counts', 'payload': event['payload']})
