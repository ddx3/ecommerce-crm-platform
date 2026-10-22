"""
Reorganization of the category tree (one-off data operation).

What it does:
  1) renames categories properly (no extra commas, caps, abbreviations);
  2) creates a new root "Garden bed setup" and gathers the small root
     categories under it;
  3) lifts fertilizer subcategories up one level (removes the extra nesting);
  4) hides empty/irrelevant ones (video lessons, seasonal selection, empty bundles);
  5) sets a meaningful sort order.

Idempotent: re-running breaks nothing.
Rollback — from a full DB backup (pg_restore).

    python manage.py reorg_categories          # apply
    python manage.py reorg_categories --dry     # only show, change nothing
"""
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Count
from slugify import slugify

from catalog.models import Category


# id -> new name (slug is NOT touched — old links stay valid)
RENAMES = {
    664: 'Insecticides and acaricides',        # was "...and Acaricides" (caps)
    660: 'Stimulants and antistressants',      # was "Stimulants, antistressants"
    680: 'Tool care',                          # was "Tool care products"
    687: 'Baskets and mats',                   # was "Baskets, mats"
    688: 'Covers and baskets',                 # was "Tool covers, tool baskets"
    685: 'Headwear',                           # was "Panama hats"
    649: 'Fertilizers and stimulants',         # was "Fertilizers, stimulants"
}

NEW_ROOT_NAME = 'Garden bed setup'
NEW_ROOT_SLUG = 'oblashtuvannia-griadki'

# what to gather under the new root "Garden bed setup"
MOVE_TO_NEW_ROOT = [739, 653, 659, 658, 654, 662]
#                   agrofibre, agrotextile, pegs, netting, borders, irrigation

# lift the fertilizer subcategories from under "Fertilizers" (650) to under "Fertilizers and
# stimulants" (649)
LIFT_TO_FERTILIZERS = [665, 651, 679]  # biofertilizers, prolonged-action, super/family garden

# empty container "Agrotextile, pegs" (0 products, the children moved out) —
# delete
DELETE_EMPTY = [652]

# hide (is_active=False), products stay in the DB
HIDE = [
    744,                               # Video lessons (0)
    677,                               # Relevant in APRIL (seasonal, April)
    670, 674, 673, 671, 675, 676, 669, # empty bundles (we keep Conifers 672)
]

# order of root categories on the storefront
ROOT_ORDER = {
    646: 10,   # Plant protection products
    649: 20,   # Fertilizers and stimulants
    655: 30,   # Garden tools
    683: 50,   # Clothing and accessories
    668: 60,   # Bundles
    682: 70,   # Home lemons
    744: 900,  # Video lessons (hidden)
    677: 900,  # Current (hidden)
}

# plant protection products (ZZR in the code) subcategories — best sellers (more products) first
ZZR_ORDER = {663: 10, 664: 20, 647: 30, 666: 40, 661: 50, 689: 60, 690: 70}


class Command(BaseCommand):
    help = 'Reorganizes the category tree (proper names + new structure)'

    def add_arguments(self, parser):
        parser.add_argument('--dry', action='store_true', help='show without making changes')

    def handle(self, *args, **opts):
        dry = opts['dry']
        log = self.stdout.write

        def upd(pk, **fields):
            if dry:
                log(f'  [dry] Category({pk}).update({fields})')
                return
            Category.objects.filter(pk=pk).update(**fields)

        with transaction.atomic():
            log('1) Renaming...')
            for pk, name in RENAMES.items():
                upd(pk, name=name)

            log('2) New root "Garden bed setup"...')
            new_root = Category.objects.filter(slug=NEW_ROOT_SLUG).first()
            if new_root is None:
                if dry:
                    log(f'  [dry] create root {NEW_ROOT_NAME!r}')
                    new_root_id = None
                else:
                    new_root = Category.objects.create(
                        name=NEW_ROOT_NAME, slug=NEW_ROOT_SLUG,
                        parent=None, is_active=True, sort_order=40,
                    )
                    new_root_id = new_root.id
            else:
                new_root_id = new_root.id
                upd(new_root_id, parent=None, is_active=True, sort_order=40)

            log('3) Gathering the small roots under "Garden bed setup"...')
            for pk in MOVE_TO_NEW_ROOT:
                upd(pk, parent_id=new_root_id)

            log('4) Lift the fertilizer subcategories under "Fertilizers and stimulants" (649)...')
            for pk in LIFT_TO_FERTILIZERS:
                upd(pk, parent_id=649)

            log('5) Delete the empty container "Agrotextile, pegs" (652)...')
            for pk in DELETE_EMPTY:
                qs = Category.objects.filter(pk=pk)
                if qs.exists():
                    children = Category.objects.filter(parent_id=pk).count()
                    if children:
                        log(f'  ! {pk} has {children} children — skipping the deletion')
                    elif dry:
                        log(f'  [dry] delete Category({pk})')
                    else:
                        qs.delete()

            log('6) Hide the empty/irrelevant ones...')
            for pk in HIDE:
                upd(pk, is_active=False)

            log('7) Sort order...')
            for pk, order in ROOT_ORDER.items():
                upd(pk, sort_order=order)
            for pk, order in ZZR_ORDER.items():
                upd(pk, sort_order=order)

            if dry:
                log(self.style.WARNING('\nDRY RUN — nothing saved, rolling back the transaction'))
                transaction.set_rollback(True)

        log(self.style.SUCCESS('\nDone. New tree:'))
        self._print_tree()

    def _print_tree(self):
        cats = list(Category.objects.all())
        counts = {c.id: 0 for c in cats}
        for c in Category.objects.annotate(n=Count('products')):
            counts[c.id] = c.n
        by_parent = {}
        for c in cats:
            by_parent.setdefault(c.parent_id, []).append(c)

        def walk(pid, depth):
            for c in sorted(by_parent.get(pid, []), key=lambda x: (x.sort_order, x.name)):
                flag = '' if c.is_active else ' [hidden]'
                self.stdout.write(f"{'  ' * depth}- {c.name} (id={c.id}, products={counts[c.id]}){flag}")
                walk(c.id, depth + 1)

        walk(None, 0)
