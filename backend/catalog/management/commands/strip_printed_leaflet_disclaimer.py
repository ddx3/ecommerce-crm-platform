# -*- coding: utf-8 -*-
"""Remove from instructions the self-reference "до складу входить друкована інструкція" ("a printed
instruction is included").

An absurdity that sat in drafts from the site: the printed sheet IS THIS instruction, and saying
inside it "буде окрема друкована інструкція" ("a separate printed instruction will be included") is an
empty phrase that adds nothing for the customer. We remove the whole block (<p>/<li>/<h*>)
containing the phrase — measured (find_disclaimer.py), it is always its own separate paragraph and
never mixed with a dose or a treatment step.

We silently do NOT touch a block if it contains even one dose — better to do nothing than to delete
something meaningful along with the phrase.
"""
import re

from django.core.management.base import BaseCommand

from catalog import instruction_llm as il
from catalog.models import Bundle

# Language data: matches the Ukrainian phrase 'друкована інструкція' (printed instruction) in the
# instruction texts, so it stays in Ukrainian.
_TELL_RE = re.compile(r'друковану?\s+інструкц|друкована\s+інструкц', re.IGNORECASE)
_BLOCK_RE = re.compile(r'<(p|li|h[1-6])\b[^>]*>.*?</\1>', re.IGNORECASE | re.DOTALL)


def strip_disclaimer(html: str) -> tuple[str, int]:
    """Returns (new html, number of blocks removed)."""
    removed = 0

    def repl(m):
        nonlocal removed
        block = m.group(0)
        if not _TELL_RE.search(block):
            return block
        plain = re.sub(r'<[^>]+>', ' ', block).strip()
        if il.dose_set(block) or len(plain) >= 260:
            return block  # safer to leave it — there may be real content there too
        removed += 1
        return ''

    out = _BLOCK_RE.sub(repl, html or '')
    out = re.sub(r'(<p>\s*(?:&nbsp;)?\s*</p>\s*){2,}', '', out)
    return out, removed


class Command(BaseCommand):
    help = 'Remove the self-reference "a printed instruction will be included" from the texts'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **opts):
        dry = opts['dry_run']
        touched = 0
        for b in Bundle.objects.exclude(instruction_html='').exclude(instruction_html=None):
            new_html, n = strip_disclaimer(b.instruction_html)
            if not n:
                continue
            touched += 1
            self.stdout.write(f'bundle{b.pk}: removed {n} block(s)')
            if dry:
                continue
            b.instruction_html = new_html
            b.save(update_fields=['instruction_html', 'instruction_stamp',
                                  'instruction_updated_at'])
        self.stdout.write(self.style.SUCCESS(
            f'{"checked" if dry else "cleaned"}: {touched} instructions'))
