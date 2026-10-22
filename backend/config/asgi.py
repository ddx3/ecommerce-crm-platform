"""
ASGI config for config project.

Routes HTTP through Django, WebSockets through Channels with custom JWT/guest auth.
"""

import os

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

# Initialise Django ASGI early so models/apps are ready before importing routing.
from django.core.asgi import get_asgi_application
django_asgi_app = get_asgi_application()

from channels.routing import ProtocolTypeRouter, URLRouter
from channels.security.websocket import AllowedHostsOriginValidator

from chat.middleware import ChatAuthMiddleware
import chat.routing
import catalog.routing


application = ProtocolTypeRouter({
    'http': django_asgi_app,
    'websocket': AllowedHostsOriginValidator(
        ChatAuthMiddleware(
            URLRouter(
                chat.routing.websocket_urlpatterns
                + catalog.routing.websocket_urlpatterns
            )
        )
    ),
})
