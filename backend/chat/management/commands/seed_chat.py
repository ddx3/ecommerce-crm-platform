"""Seed a few example chat conversations for local UI testing.

Usage: `python manage.py seed_chat`. Idempotent-ish: each run appends a
fresh batch — wipe with `python manage.py flush_chat` (not provided) or
delete manually if needed.
"""

import uuid
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from chat.models import Conversation, Message


SAMPLES = [
    {
        'guest_name': 'Jane Doe',
        'guest_email': 'jane.doe@example.com',
        'guest_phone': '+380000000001',
        'status': Conversation.STATUS_OPEN,
        'messages': [
            ('customer', 'Hello! Do you have the white ceramic plant pot, 12 cm, in stock?', -45),
            ('manager',  'Hello! Yes, we have 8 pcs in stock. Shall we place the order?', -42),
            ('customer', 'Yes, please. Nova Poshta, branch no. 3 in Lviv.', -40),
        ],
    },
    {
        'guest_name': 'John Smith',
        'guest_email': '',
        'guest_phone': '+380000000002',
        'status': Conversation.STATUS_OPEN,
        'messages': [
            ('customer', 'Could you please tell me how much the fertilizer costs with delivery?', -180),
            ('customer', 'Order for 350 UAH', -178),
        ],
    },
    {
        'guest_name': 'Mary Major',
        'guest_email': 'mary.major@example.com',
        'guest_phone': '',
        'status': Conversation.STATUS_OPEN,
        'messages': [
            ('customer', 'Hello! When will the mole nets be back in stock?', -1440),
            ('manager',  'Good afternoon! We expect a delivery next week.', -1430),
            ('customer', 'Thank you, I will wait!', -1425),
        ],
    },
    {
        'guest_name': 'Alex Taylor',
        'guest_email': 'alex.taylor@example.com',
        'guest_phone': '+380000000003',
        'status': Conversation.STATUS_CLOSED,
        'messages': [
            ('customer', 'Is it possible to return a product?', -7200),
            ('manager',  'Yes, within 14 days. Please submit a request through the site.', -7195),
            ('customer', 'Thank you, the question is solved.', -7190),
        ],
    },
]


class Command(BaseCommand):
    help = 'Seed example chat conversations for UI testing.'

    def add_arguments(self, parser):
        parser.add_argument('--clear', action='store_true', help='Delete all existing conversations first')

    def handle(self, *args, **opts):
        if opts.get('clear'):
            n = Conversation.objects.count()
            Conversation.objects.all().delete()
            self.stdout.write(self.style.WARNING(f'Cleared {n} existing conversations'))

        now = timezone.now()
        created = 0
        for sample in SAMPLES:
            conv = Conversation.objects.create(
                guest_id=uuid.uuid4().hex,
                guest_name=sample['guest_name'],
                guest_email=sample['guest_email'],
                guest_phone=sample['guest_phone'],
                status=sample['status'],
            )
            last_at = None
            for sender, text, mins_offset in sample['messages']:
                msg = Message.objects.create(
                    conversation=conv,
                    sender_type=sender,
                    text=text,
                )
                # Override created_at via update — auto_now_add doesn't accept manual values on create
                ts = now + timedelta(minutes=mins_offset)
                Message.objects.filter(pk=msg.pk).update(created_at=ts)
                last_at = ts
            if last_at:
                conv.last_message_at = last_at
                conv.save(update_fields=['last_message_at'])
            created += 1
            self.stdout.write(self.style.SUCCESS(f'  + conv #{conv.pk} ({sample["status"]}, {len(sample["messages"])} msgs)'))

        self.stdout.write(self.style.SUCCESS(f'Done — created {created} conversations'))
