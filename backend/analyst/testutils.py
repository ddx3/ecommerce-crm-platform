"""Shared helpers for analyst tests."""
from unittest import mock


class NoOrderThrottle:
    """Tests that place orders through the API must not run into the DRF request rate limit
    (it lives in the cache and is shared across the whole test run): otherwise other tests get 429.
    We do not clear the cache — when REDIS_URL is set it is shared with live sessions."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch('orders.views.OrderCreateThrottle.allow_request', return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)
