"""One-off migration: chat HEIC/HEIF attachments uploaded BEFORE the conversion fix
(chat/views.py _convert_heic_to_jpeg) don't render in the CRM: Chrome/Firefox/Edge
can't handle <img src=".heic">, only Safari. This command converts the already saved
attachments to JPEG in place: the new attachment replaces the old one, and the old HEIC file
is deleted (django-cleanup would do it itself when attachment changes, but
we delete explicitly so as not to leave orphan files if cleanup is disabled/fails).

Usage: `python manage.py fix_heic_attachments [--dry-run]`
"""
import io

from django.core.management.base import BaseCommand
from django.db.models import Q

from chat.models import Message


class Command(BaseCommand):
    help = 'Converts already uploaded HEIC/HEIF chat attachments to JPEG (so they display in the CRM)'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', help='Only show what would be changed, touch nothing')

    def handle(self, *args, **options):
        import pillow_heif
        from PIL import Image, ImageOps
        pillow_heif.register_heif_opener()

        dry_run = options['dry_run']
        ids = list(
            Message.objects.filter(Q(attachment__iendswith='.heic') | Q(attachment__iendswith='.heif'))
            .order_by('id').values_list('id', flat=True)
        )
        self.stdout.write(f'Found {len(ids)} HEIC/HEIF attachments')

        ok, failed = 0, 0
        for msg_id in ids:
            msg = Message.objects.get(pk=msg_id)
            old_name = msg.attachment.name
            try:
                msg.attachment.open('rb')
                data_in = msg.attachment.read()
                msg.attachment.close()

                img = Image.open(io.BytesIO(data_in))
                img = ImageOps.exif_transpose(img)
                img = img.convert('RGB')
                buf = io.BytesIO()
                img.save(buf, format='JPEG', quality=90, optimize=True)
                data_out = buf.getvalue()

                new_name = old_name.rsplit('.', 1)[0] + '.jpg'

                if dry_run:
                    self.stdout.write(f'[dry-run] msg#{msg.id}: {old_name} -> {new_name} ({len(data_in)} -> {len(data_out)} bytes)')
                    continue

                storage = msg.attachment.storage
                saved_name = storage.save(new_name, io.BytesIO(data_out))
                msg.attachment.name = saved_name
                msg.save(update_fields=['attachment'])
                try:
                    if storage.exists(old_name):
                        storage.delete(old_name)
                except Exception:
                    self.stderr.write(f'msg#{msg.id}: could not delete the old file {old_name} (not critical)')

                self.stdout.write(f'msg#{msg.id}: {old_name} -> {saved_name}')
                ok += 1
            except Exception as e:
                failed += 1
                self.stderr.write(self.style.ERROR(f'msg#{msg.id}: ERROR ({old_name}): {e}'))

        if not dry_run:
            self.stdout.write(self.style.SUCCESS(f'Done: {ok} converted, {failed} errors'))
