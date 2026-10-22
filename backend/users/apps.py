from django.apps import AppConfig


class UsersConfig(AppConfig):
    name = 'users'

    def ready(self):
        # Register signals (link_guest_orders_on_user_create, etc.)
        from . import signals  # noqa: F401
        # Role permissions in the admin (viewer = read-only, manager = no admin management).
        from .admin_perms import patch_admin_permissions
        patch_admin_permissions()
