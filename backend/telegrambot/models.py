from django.db import models


class CommandLog(models.Model):
    """A single user command in Telegram + what the agent did.

    Dedup by (chat_id, message_id): a bot restart must not process the same
    message twice (long-polling re-delivers unprocessed updates).
    """
    STATUS_CHOICES = [
        ('ok', 'Done'),
        ('error', 'Error'),
        ('denied', 'Rejected (whitelist)'),
    ]

    telegram_user_id = models.BigIntegerField(db_index=True, verbose_name='Telegram user id')
    telegram_username = models.CharField(max_length=64, blank=True, verbose_name='Username')
    chat_id = models.BigIntegerField(verbose_name='Chat id')
    message_id = models.BigIntegerField(null=True, blank=True, verbose_name='Message id')
    text = models.TextField(blank=True, verbose_name='Command text')
    had_photo = models.BooleanField(default=False, verbose_name='Had a photo')
    agent_summary = models.TextField(blank=True, verbose_name='Agent reply')
    tool_calls = models.JSONField(default=list, blank=True, verbose_name='Tool calls')
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default='ok', verbose_name='Status')
    error = models.TextField(blank=True, verbose_name='Error')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = 'Telegram command'
        verbose_name_plural = 'Telegram commands'
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['chat_id', 'message_id'],
                name='telegrambot_unique_chat_message',
                condition=models.Q(message_id__isnull=False),
            ),
        ]

    def __str__(self):
        return f'#{self.pk} @{self.telegram_username or self.telegram_user_id}: {self.text[:40]}'


class RollbackEntry(models.Model):
    """A single revertible write effect of a command.

    before/after are value snapshots. create: before=None; update: before={field: old value};
    Decimal is serialized as a string (json cannot handle Decimal).
    """
    command = models.ForeignKey(
        CommandLog, on_delete=models.CASCADE, related_name='rollbacks', verbose_name='Command'
    )
    action = models.CharField(max_length=40, verbose_name='Action')  # create_product|update_stock|update_price|update_field
    model_label = models.CharField(max_length=80, verbose_name='Model')  # 'catalog.ProductVariant'
    object_pk = models.CharField(max_length=40, blank=True, verbose_name='Object PK')
    before = models.JSONField(null=True, blank=True, verbose_name='Before')
    after = models.JSONField(null=True, blank=True, verbose_name='After')
    is_reverted = models.BooleanField(default=False, db_index=True, verbose_name='Rolled back')
    reverted_at = models.DateTimeField(null=True, blank=True, verbose_name='Rollback time')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = 'Rollback'
        verbose_name_plural = 'Rollbacks'
        ordering = ['-created_at']

    def __str__(self):
        return f'#{self.pk} {self.action} {self.model_label}#{self.object_pk}'
