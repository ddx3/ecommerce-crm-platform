from django.db import migrations


def seed_admin_roles(apps, schema_editor):
    User = apps.get_model('users', 'User')

    # Existing superusers get admin_role='superuser'.
    # Other staff (if any) get 'manager' by default.
    for u in User.objects.filter(is_staff=True):
        if u.is_superuser:
            u.admin_role = 'superuser'
        else:
            u.admin_role = 'manager'
        u.role = 'admin'
        u.save(update_fields=['admin_role', 'role'])

    # Create 3 test admins. The default password is 'admin12345'.
    # In prod it must be changed right after creation.
    test_admins = [
        ('viewer1@shop.com',  'Jane',  'Viewer',  'viewer'),
        ('viewer2@shop.com',  'John',   'Viewer',  'viewer'),
        ('manager@shop.com',  'Sam', 'Manager',     'manager'),
    ]
    for email, first_name, last_name, admin_role in test_admins:
        if User.objects.filter(email=email).exists():
            continue
        u = User(
            email=email,
            first_name=first_name,
            last_name=last_name,
            role='admin',
            admin_role=admin_role,
            is_staff=True,
            is_active=True,
        )
        # AbstractBaseUser.set_password is not available on the historical model, so we do it
        # manually.
        from django.contrib.auth.hashers import make_password
        u.password = make_password('admin12345')
        u.save()


def reverse_seed(apps, schema_editor):
    User = apps.get_model('users', 'User')
    User.objects.filter(email__in=[
        'viewer1@shop.com', 'viewer2@shop.com', 'manager@shop.com',
    ]).delete()


class Migration(migrations.Migration):
    dependencies = [
        ('users', '0006_user_admin_role'),
    ]
    operations = [
        migrations.RunPython(seed_admin_roles, reverse_seed),
    ]
