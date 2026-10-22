"""Counter of the Analyst section for the inbox badge (catalog.notifications)."""
from __future__ import annotations

from .models import Finding


def new_count() -> int:
    """Active findings that no human has touched yet. A single COUNT over indexes:
    get_notification_counts() is called on every order post_save."""
    return Finding.objects.filter(resolved_at__isnull=True, status=Finding.NEW).count()
