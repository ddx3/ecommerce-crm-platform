"""
Import of store reviews from magiyaroslyn.com/store-reviews/ — together with the store's replies.

Logic:
1) Deletes all current "orphan" Reviews (without product/bundle) — these are store reviews.
2) Fetches the first page + all AJAX batches (via /_widget/ajax_comments/render/).
3) Parses the HTML into "review + replies" blocks. Each block:
   - <article class="p-review__body" itemtype=".../Review">  — the review itself
   - <article class="p-review__reply-body" itemtype=".../Comment"> — store replies
4) Creates a Review for the review, then Review.parent=<review> for each reply.

Run:
    python manage.py import_store_reviews                # everything: delete old + import
    python manage.py import_store_reviews --keep-old     # only add
    python manage.py import_store_reviews --dry-run      # preview what will happen
"""

import re
from datetime import datetime

import requests
from django.core.management.base import BaseCommand

from catalog.models import Review


BASE = 'https://magiyaroslyn.com'
URL = f'{BASE}/store-reviews/'
AJAX_URL = f'{BASE}/_widget/ajax_comments/render/'
TOKEN_RX = re.compile(r'/_widget/ajax_comments/render/\?skin=store&offset=20&token=([a-f0-9]+)')
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
      'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')

# Container of a single "review + replies" — <div class="p-review p-review--full" id="uid...">.
# We split the HTML on this marker, then take everything up to the next marker/end.
FULL_BLOCK_SPLIT = re.compile(r'(<div class="p-review p-review--full"\s+id="uid[a-f0-9]+">)')

# Review inside the block (top-level)
REVIEW_RX = re.compile(
    r'<article\s+class="p-review__body"\s+itemscope\s+itemtype="https://schema\.org/Review"\s*>(.*?)</article>',
    re.DOTALL,
)
# Store reply (Comment)
REPLY_RX = re.compile(
    r'<article\s+class="p-review__reply-body"\s+itemprop="comment"\s+itemscope\s+itemtype="https://schema\.org/Comment"\s*>(.*?)</article>',
    re.DOTALL,
)
NAME_RX = re.compile(
    r'<span\s+class="p-review-author__name"\s+itemprop="name"[^>]*>(.*?)</span>',
    re.DOTALL,
)
DATETIME_RX = re.compile(r'datetime="([^"]+)"')
RATING_RX = re.compile(r'itemprop="ratingValue"\s+content="(\d+)"')
REVIEW_BODY_RX = re.compile(
    r'<div\s+class="p-review__content"\s+itemprop="reviewBody"[^>]*>(.*?)</div>',
    re.DOTALL,
)
REPLY_BODY_RX = re.compile(
    r'<div\s+class="p-review__content"\s+itemprop="text"[^>]*>(.*?)</div>',
    re.DOTALL,
)


def clean_text(html: str) -> str:
    s = re.sub(r'<br\s*/?>', '\n', html)
    s = re.sub(r'<[^>]+>', '', s)
    s = re.sub(r'\n{3,}', '\n\n', s)
    # &quot; → ", &amp; → & etc.
    s = (s.replace('&quot;', '"').replace('&amp;', '&')
          .replace('&lt;', '<').replace('&gt;', '>')
          .replace('&nbsp;', ' '))
    return s.strip()


def clean_name(html: str) -> str:
    s = re.sub(r'<[^>]+>', '', html)
    return re.sub(r'\s+', ' ', s).strip()


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def parse_review(block: str) -> dict | None:
    """Parses a single review block. Returns a dict or None."""
    name_m = NAME_RX.search(block)
    body_m = REVIEW_BODY_RX.search(block)
    if not (name_m and body_m):
        return None
    rating_m = RATING_RX.search(block)
    dt_m = DATETIME_RX.search(block)
    return {
        'name': clean_name(name_m.group(1))[:100],
        'text': clean_text(body_m.group(1)),
        'rating': max(1, min(int(rating_m.group(1)) if rating_m else 5, 5)),
        'created_at': parse_dt(dt_m.group(1) if dt_m else None),
    }


def parse_reply(block: str) -> dict | None:
    """Parses a reply block. Returns a dict or None."""
    name_m = NAME_RX.search(block)
    body_m = REPLY_BODY_RX.search(block)
    if not (name_m and body_m):
        return None
    dt_m = DATETIME_RX.search(block)
    return {
        'name': clean_name(name_m.group(1))[:100],
        'text': clean_text(body_m.group(1)),
        'created_at': parse_dt(dt_m.group(1) if dt_m else None),
    }


def split_into_blocks(html: str) -> list[str]:
    """Splits the HTML into 'one review + replies' blocks, using REVIEW_RX as the boundary.
    Each block runs from the start of an <article Review> to the start of the next <article Review>
    (or to the end of the HTML for the last one). This includes replies that come below the review."""
    matches = list(REVIEW_RX.finditer(html))
    blocks = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(html)
        blocks.append(html[start:end])
    return blocks


class Command(BaseCommand):
    help = 'Import store reviews + admin replies from magiyaroslyn.com'

    def add_arguments(self, parser):
        parser.add_argument('--keep-old', action='store_true',
                            help='Do not delete existing orphan Review entries')
        parser.add_argument('--dry-run', action='store_true',
                            help='Print what would happen without writing')

    def handle(self, *args, **opts):
        keep = opts['keep_old']
        dry = opts['dry_run']

        # 1) Delete old ones
        orphan_qs = Review.objects.filter(product__isnull=True, bundle__isnull=True)
        n_old = orphan_qs.count()
        if not keep:
            self.stdout.write(f'Existing store reviews (orphan): {n_old}')
            if dry:
                self.stdout.write('  [dry-run] would delete them')
            else:
                orphan_qs.delete()
                self.stdout.write(self.style.WARNING(f'  deleted {n_old}'))
        else:
            self.stdout.write(f'Keeping {n_old} existing orphan reviews')

        # 2) Load the HTML — first page + AJAX batches
        self.stdout.write(f'\nFetching {URL}...')
        s = requests.Session()
        s.headers.update({'User-Agent': UA})
        r = s.get(URL, timeout=20)
        r.raise_for_status()
        all_html = r.text
        self.stdout.write(f'  first page: {len(all_html)} chars')

        token_m = TOKEN_RX.search(all_html)
        token = token_m.group(1) if token_m else None

        if token:
            offset = 20
            while True:
                self.stdout.write(f'  AJAX offset={offset}...')
                try:
                    ajax = s.get(AJAX_URL, params={
                        'skin': 'store', 'offset': offset, 'token': token,
                    }, headers={'X-Requested-With': 'XMLHttpRequest'}, timeout=20)
                    data = ajax.json()
                except Exception as e:
                    self.stdout.write(self.style.ERROR(f'  AJAX failed: {e}'))
                    break
                batch_html = (data.get('response') or {}).get('html', '') or ''
                if not batch_html.strip():
                    break
                batch_reviews_count = len(REVIEW_RX.findall(batch_html))
                self.stdout.write(f'    got {batch_reviews_count} reviews in batch')
                if batch_reviews_count == 0:
                    break
                all_html += batch_html
                offset += batch_reviews_count
                if batch_reviews_count < 20:
                    break
        else:
            self.stdout.write(self.style.WARNING('  no AJAX token — only first batch'))

        # 3) Split into blocks
        blocks = split_into_blocks(all_html)
        self.stdout.write(f'\nTotal blocks (review+replies): {len(blocks)}')

        created_reviews = created_replies = failed = 0

        for i, block in enumerate(blocks, 1):
            try:
                review_m = REVIEW_RX.search(block)
                if not review_m:
                    continue
                review_data = parse_review(review_m.group(1))
                if not review_data:
                    self.stdout.write(self.style.WARNING(f'  [{i}] cannot parse review — skip'))
                    failed += 1
                    continue

                replies = []
                for rep_m in REPLY_RX.finditer(block):
                    rep = parse_reply(rep_m.group(1))
                    if rep:
                        replies.append(rep)

                rd = review_data
                self.stdout.write(
                    f'  [{i}] {rd["name"]} | {rd["rating"]}★ | {rd["created_at"]} | '
                    f'replies: {len(replies)} | {rd["text"][:40]!r}'
                )

                if dry:
                    created_reviews += 1
                    created_replies += len(replies)
                    continue

                parent = Review.objects.create(
                    author_name=rd['name'],
                    author_email='',
                    rating=rd['rating'],
                    text=rd['text'],
                    is_approved=True,
                )
                if rd['created_at']:
                    Review.objects.filter(pk=parent.pk).update(created_at=rd['created_at'])
                created_reviews += 1

                for rep in replies:
                    # A reply is a Review with parent=<review>. The rating is not shown in a reply,
                    # but the field is required — default to 5.
                    reply_obj = Review.objects.create(
                        parent=parent,
                        author_name=rep['name'],
                        author_email='',
                        rating=5,
                        text=rep['text'],
                        is_approved=True,
                    )
                    if rep['created_at']:
                        Review.objects.filter(pk=reply_obj.pk).update(created_at=rep['created_at'])
                    created_replies += 1

            except Exception as e:
                self.stdout.write(self.style.ERROR(f'  [{i}] failed: {e}'))
                failed += 1

        self.stdout.write('\n' + '=' * 50)
        self.stdout.write(self.style.SUCCESS(f'Reviews:  {created_reviews}'))
        self.stdout.write(self.style.SUCCESS(f'Replies:  {created_replies}'))
        if failed:
            self.stdout.write(self.style.ERROR(f'Failed:   {failed}'))
