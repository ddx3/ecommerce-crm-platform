"""
Deletes expired VariantLabel records.
Run daily via cron or the task scheduler.
"""
from django.core.management.base import BaseCommand
from django.utils import timezone

from catalog.models import VariantLabel


class Command(BaseCommand):
    help = 'Delete expired variant labels'

    def handle(self, *args, **options):
        expired = VariantLabel.objects.filter(
            expires_at__isnull=False,
            expires_at__lt=timezone.now(),
        )
        count = expired.count()
        expired.delete()
        self.stdout.write(self.style.SUCCESS(f'Deleted {count} expired labels'))
