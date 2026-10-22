"""Initial indexing of variant names for semantic matching.

The free Gemini embeddings quota is a pool shared by all users of the model: sometimes it lets a
hundred names through, sometimes it refuses even at ten. So the command is patient (waits and keeps
trying) and resumable: it computes only what is missing or stale (the variant was renamed), and each
successful batch goes to the DB immediately. An interrupted run continues from the same place — just
run it again.

    python manage.py warm_embeddings            # catch up the index
    python manage.py warm_embeddings --status   # only show how many are left
    python manage.py warm_embeddings --limit 100
"""
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from core import embeddings


class Command(BaseCommand):
    help = 'Compute embeddings of variant names (with pauses for the Gemini quota)'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=None,
                            help='Maximum number of names to compute in this run')
        parser.add_argument('--status', action='store_true',
                            help='Only show the index state, compute nothing')
        parser.add_argument('--batch', type=int, default=25,
                            help='Batch size (smaller — better chance of fitting the quota)')
        parser.add_argument('--retries', type=int, default=30,
                            help='How many times to wait out a 429 on one batch')
        parser.add_argument('--wait', type=int, default=30,
                            help='Pause between attempts, seconds')

    def handle(self, *args, **opts):
        from catalog.models import ProductVariant

        total = ProductVariant.objects.count()
        stale = len(embeddings.stale_variant_ids())
        self.stdout.write(f'Variants: {total}, need indexing: {stale}')
        if opts['status']:
            return
        if not stale:
            self.stdout.write(self.style.SUCCESS('The index is complete — nothing to compute.'))
            return
        if not settings.GEMINI_API_KEY:
            raise CommandError('GEMINI_API_KEY is not configured')

        from google import genai
        client = genai.Client(api_key=settings.GEMINI_API_KEY)

        def progress(done, of):
            self.stdout.write(f'  {done}/{of}…')

        # The free quota is a shared pool, windows open unpredictably.
        # So the command is patient: it waits and keeps trying, and every successful batch is
        # already
        # in the DB — an interrupted run continues from the same place.
        done, left = embeddings.sync_embeddings(
            client, limit=opts['limit'], batch_size=opts['batch'],
            retries=opts['retries'], retry_wait=opts['wait'], progress=progress,
        )
        msg = f'Indexed {done}, {left} left'
        self.stdout.write(self.style.SUCCESS(msg) if not left else self.style.WARNING(msg))
