"""WebSocket consumers for chat.

ChatConsumer: per-conversation socket. Joined by both customer and any manager
viewing the dialog. Messages broadcast through group `chat_conv_<id>`.

DashboardConsumer: staff-only socket joined into `chat_dashboard`. Receives
notifications about every new message / new conversation so manager UI can
update unread counters and conversation list in real time.
"""

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.utils import timezone

from .models import Conversation, Message
from .services import (
    GROUP_DASHBOARD,
    conversation_group,
    serialize_message,
    _invalidate_sidebar_cache,
)


class _ChatRelayMixin:
    """Shared relay handlers: forward a group event to the client as JSON.
    Mixed into both ChatConsumer and DashboardConsumer."""

    async def chat_message_updated(self, event):
        await self.send_json({'type': 'message_updated', 'payload': event['payload']})

    async def chat_message_deleted(self, event):
        await self.send_json({'type': 'message_deleted', 'payload': event['payload']})

    async def chat_conversation_deleted(self, event):
        await self.send_json({'type': 'conversation_deleted', 'payload': event['payload']})


class ChatConsumer(_ChatRelayMixin, AsyncJsonWebsocketConsumer):
    async def connect(self):
        self.conversation_id = int(self.scope['url_route']['kwargs']['conversation_id'])
        ok = await self._can_access()
        if not ok:
            await self.close(code=4403)
            return
        self.group = conversation_group(self.conversation_id)
        await self.channel_layer.group_add(self.group, self.channel_name)
        await self.accept()

    async def disconnect(self, code):
        if hasattr(self, 'group'):
            await self.channel_layer.group_discard(self.group, self.channel_name)

    async def receive_json(self, content, **kwargs):
        action = content.get('type') or content.get('action')
        if action == 'message':
            text = (content.get('text') or '').strip()
            if not text:
                return
            text = text[:4000]
            msg = await self._create_message(text)
            if msg is None:
                return
            payload = serialize_message(msg)
            await self.channel_layer.group_send(self.group, {
                'type': 'chat.message', 'payload': payload,
            })
            await self.channel_layer.group_send(GROUP_DASHBOARD, {
                'type': 'chat.message', 'payload': payload,
            })
        elif action == 'read':
            await self._mark_seen()

    async def chat_message(self, event):
        await self.send_json({'type': 'message', 'payload': event['payload']})

    async def chat_conversation(self, event):
        await self.send_json({'type': 'conversation', 'payload': event['payload']})

    @database_sync_to_async
    def _can_access(self) -> bool:
        try:
            conv = Conversation.objects.get(pk=self.conversation_id)
        except Conversation.DoesNotExist:
            return False
        user = self.scope.get('user')
        guest_id = self.scope.get('guest_id') or ''
        if getattr(user, 'is_authenticated', False) and user.is_staff:
            return True
        if conv.user_id and getattr(user, 'is_authenticated', False) and conv.user_id == user.id:
            return True
        if conv.guest_id and guest_id and conv.guest_id == guest_id:
            return True
        return False

    @database_sync_to_async
    def _create_message(self, text: str):
        try:
            conv = Conversation.objects.get(pk=self.conversation_id)
        except Conversation.DoesNotExist:
            return None
        if conv.status == Conversation.STATUS_CLOSED:
            return None
        user = self.scope.get('user')
        is_manager = getattr(user, 'is_authenticated', False) and user.is_staff
        if is_manager:
            sender_type = Message.SENDER_MANAGER
            sender_user = user
        else:
            sender_type = Message.SENDER_CUSTOMER
            sender_user = user if getattr(user, 'is_authenticated', False) else None

        msg = Message.objects.create(
            conversation=conv,
            sender_type=sender_type,
            sender_user=sender_user,
            text=text,
        )
        now = timezone.now()
        conv.last_message_at = now
        fields = ['last_message_at']
        if is_manager:
            conv.manager_seen_at = now
            fields.append('manager_seen_at')
            if conv.assigned_to_id is None:
                conv.assigned_to = user
                fields.append('assigned_to')
        else:
            conv.customer_seen_at = now
            fields.append('customer_seen_at')
        conv.save(update_fields=fields)
        # Realtime: the manager's unread count changed (new client message → +1,
        # or the manager wrote → manager_seen_at reset), so we push a fresh chat to the sidebar.
        _invalidate_sidebar_cache()
        return msg

    @database_sync_to_async
    def _mark_seen(self):
        try:
            conv = Conversation.objects.get(pk=self.conversation_id)
        except Conversation.DoesNotExist:
            return
        user = self.scope.get('user')
        now = timezone.now()
        if getattr(user, 'is_authenticated', False) and user.is_staff:
            conv.manager_seen_at = now
            conv.save(update_fields=['manager_seen_at'])
            # Manager read → their unread count dropped → update the sidebar badge in realtime.
            _invalidate_sidebar_cache()
        else:
            conv.customer_seen_at = now
            conv.save(update_fields=['customer_seen_at'])


class DashboardConsumer(_ChatRelayMixin, AsyncJsonWebsocketConsumer):
    async def connect(self):
        user = self.scope.get('user')
        if not (getattr(user, 'is_authenticated', False) and user.is_staff):
            await self.close(code=4403)
            return
        await self.channel_layer.group_add(GROUP_DASHBOARD, self.channel_name)
        await self.accept()

    async def disconnect(self, code):
        await self.channel_layer.group_discard(GROUP_DASHBOARD, self.channel_name)

    async def chat_message(self, event):
        await self.send_json({'type': 'message', 'payload': event['payload']})

    async def chat_conversation(self, event):
        await self.send_json({'type': 'conversation', 'payload': event['payload']})
