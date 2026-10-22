from django.conf import settings
from django.db import models
from django.db.models import Q


class Conversation(models.Model):
    """A single support thread between a customer (user or guest) and managers."""

    STATUS_OPEN = 'open'
    STATUS_CLOSED = 'closed'
    STATUS_CHOICES = [
        (STATUS_OPEN, 'Open'),
        (STATUS_CLOSED, 'Closed'),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='chat_conversations',
        verbose_name='User',
    )
    guest_id = models.CharField(max_length=64, blank=True, default='', db_index=True, verbose_name='Guest ID')
    guest_name = models.CharField(max_length=255, blank=True, default='', verbose_name="Guest name")
    guest_email = models.EmailField(blank=True, default='', verbose_name='Guest email')
    guest_phone = models.CharField(max_length=32, blank=True, default='', verbose_name='Guest phone')

    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_OPEN, db_index=True, verbose_name='Status')
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='assigned_chats',
        verbose_name='Manager',
    )

    created_at = models.DateTimeField(auto_now_add=True, verbose_name='Created')
    last_message_at = models.DateTimeField(null=True, blank=True, db_index=True, verbose_name='Last message')

    customer_seen_at = models.DateTimeField(null=True, blank=True, verbose_name='Seen by customer')
    manager_seen_at = models.DateTimeField(null=True, blank=True, verbose_name='Seen by manager')

    class Meta:
        verbose_name = 'Conversation'
        verbose_name_plural = 'Conversations'
        ordering = ['-last_message_at', '-created_at']
        constraints = [
            models.CheckConstraint(
                condition=Q(user__isnull=False) | ~Q(guest_id=''),
                name='chat_conv_has_owner',
            ),
        ]

    def __str__(self) -> str:
        if self.user_id:
            return f'#{self.pk} · {self.user}'
        label = self.guest_name or self.guest_email or self.guest_phone or self.guest_id[:8]
        return f'#{self.pk} · guest {label}'

    def display_name(self) -> str:
        from orders.models import format_phone
        if self.user_id and self.user:
            full = f'{self.user.first_name} {self.user.last_name}'.strip()
            return full or self.user.email or format_phone(self.user.phone) or f'#{self.user_id}'
        return self.guest_name or self.guest_email or format_phone(self.guest_phone) or 'Guest'

    def unread_for_manager(self) -> int:
        qs = self.messages.filter(sender_type=Message.SENDER_CUSTOMER)
        if self.manager_seen_at:
            qs = qs.filter(created_at__gt=self.manager_seen_at)
        return qs.count()

    def unread_for_customer(self) -> int:
        qs = self.messages.exclude(sender_type=Message.SENDER_CUSTOMER)
        if self.customer_seen_at:
            qs = qs.filter(created_at__gt=self.customer_seen_at)
        return qs.count()


class Message(models.Model):
    SENDER_CUSTOMER = 'customer'
    SENDER_MANAGER = 'manager'
    SENDER_SYSTEM = 'system'
    SENDER_BOT = 'bot'
    SENDER_CHOICES = [
        (SENDER_CUSTOMER, 'Customer'),
        (SENDER_MANAGER, 'Manager'),
        (SENDER_SYSTEM, 'System'),
        (SENDER_BOT, 'Bot'),
    ]

    conversation = models.ForeignKey(
        Conversation, on_delete=models.CASCADE, related_name='messages', verbose_name='Conversation',
    )
    sender_type = models.CharField(max_length=12, choices=SENDER_CHOICES, default=SENDER_CUSTOMER, verbose_name='Sender type')
    sender_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='+',
        verbose_name='Sender (user)',
    )
    text = models.TextField(blank=True, default='', verbose_name='Text')
    attachment = models.FileField(upload_to='chat/%Y/%m/', null=True, blank=True, verbose_name='Attachment')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True, verbose_name='Created')
    edited_at = models.DateTimeField(null=True, blank=True, verbose_name='Edited')

    IMAGE_EXTS = {'jpg', 'jpeg', 'png', 'gif', 'webp', 'heic', 'bmp', 'avif', 'svg'}

    class Meta:
        verbose_name = 'Message'
        verbose_name_plural = 'Message'
        ordering = ['created_at']

    def sender_display(self) -> str:
        if self.sender_type == self.SENDER_MANAGER and self.sender_user_id:
            full = f'{self.sender_user.first_name} {self.sender_user.last_name}'.strip()
            return full or self.sender_user.email or 'Manager'
        return dict(self.SENDER_CHOICES).get(self.sender_type, self.sender_type)

    def attachment_info(self) -> dict | None:
        if not self.attachment:
            return None
        try:
            url = self.attachment.url
        except Exception:
            return None
        name = self.attachment.name.split('/')[-1]
        ext = name.rsplit('.', 1)[-1].lower() if '.' in name else ''
        kind = 'image' if ext in self.IMAGE_EXTS else 'file'
        try:
            size = self.attachment.size
        except (FileNotFoundError, OSError, ValueError):
            size = None
        return {'url': url, 'name': name, 'kind': kind, 'ext': ext, 'size': size}
