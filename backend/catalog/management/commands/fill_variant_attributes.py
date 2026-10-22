"""
For variants without attributes — extracts the size/pack size from the variant name
and assigns the corresponding attribute. Skips Agrotextile.
Usage:
    python manage.py fill_variant_attributes
"""
import re
from django.core.management.base import BaseCommand
from catalog.models import Attribute, AttributeValue, ProductVariant, Category


# Language data: category / attribute names are the Ukrainian names stored in the shop's catalog DB
# ('Агротканина' = Agrotextile), so they stay in Ukrainian.
SKIP_CATS = ('Агротканина',)

# Patterns ordered by priority (most specific first).
# Language data: the patterns match the Ukrainian unit abbreviations typed into variant names
# (мл/л = ml/l, кг/г = kg/g, шт/штук = pcs), so they stay in Ukrainian. The attribute names are the
# ones stored in the catalog DB: "Volume" and "Weight" are English (the admin UI looks them up by
# name); 'Кількість в пакуванні' (= Quantity per pack) is a Ukrainian name and stays as it is.
PATTERNS = [
    # "500 мл" (500 ml), "1,2 л" (1.2 l)  → Volume
    (re.compile(r'([\d][,\.\d]*\s*(?:мл|л))', re.IGNORECASE), 'Volume'),
    # "20 г" (20 g), "1 кг" (1 kg), "0,5 г" (0.5 g)  → Weight
    (re.compile(r'([\d][,\.\d]*\s*(?:кг|г))', re.IGNORECASE), 'Weight'),
    # "100 штук" (100 pieces), "50 шт" (50 pcs)  → Quantity per pack
    (re.compile(r'([\d]+\s*(?:штук|шт\.?))', re.IGNORECASE), 'Кількість в пакуванні'),
]

# Normalize weight value: "1кг" (1 kg) -> "1000 г" (1000 g), "20г" -> "20 г" etc.
def normalize_weight(raw):
    s = str(raw).strip()
    m = re.match(r'^([\d,\.]+)\s*кг$', s, re.IGNORECASE)
    if m:
        try:
            from decimal import Decimal
            grams = int(Decimal(m.group(1).replace(',', '.')) * 1000)
            return f'{grams} г'
        except Exception:
            pass
    # Ensure space before unit
    s = re.sub(r'([\d,\.]+)\s*(г)$', r'\1 \2', s)
    return s

def normalize_volume(raw):
    s = str(raw).strip()
    # Ensure space before unit
    s = re.sub(r'([\d,\.]+)\s*(мл|л)$', r'\1 \2', s, flags=re.IGNORECASE)
    return s

def parse_size(name):
    """Return (attr_name, normalized_value) or (None, None)."""
    # Try after last comma first (most variants: "Name, 500 мл", i.e. 500 ml)
    for chunk in [name.rsplit(',', 1)[-1].strip(), name]:
        for pat, attr_name in PATTERNS:
            m = pat.search(chunk)
            if m:
                value = m.group(1).strip()
                if attr_name == 'Weight':
                    value = normalize_weight(value)
                elif attr_name == 'Volume':
                    value = normalize_volume(value)
                return attr_name, value
    return None, None


class Command(BaseCommand):
    help = 'Fill missing variant attributes by parsing size from variant name'

    def handle(self, *args, **options):
        # Build category skip set
        skip_ids = set()
        for s in SKIP_CATS:
            skip_ids |= set(
                Category.objects.filter(name__startswith=s).values_list('pk', flat=True)
            )

        # Attribute / value cache
        attr_cache = {}
        av_cache = {}

        def get_attr(name):
            if name not in attr_cache:
                obj, _ = Attribute.objects.get_or_create(name=name)
                attr_cache[name] = obj
            return attr_cache[name]

        def get_av(attr, value):
            key = (attr.pk, value)
            if key not in av_cache:
                obj, _ = AttributeValue.objects.get_or_create(attribute=attr, value=value)
                av_cache[key] = obj
            return av_cache[key]

        # Only process variants that currently have NO attributes
        no_attr_qs = (
            ProductVariant.objects
            .filter(attribute_values__isnull=True)
            .exclude(product__categories__in=skip_ids)
            .select_related('product')
            .distinct()
        )

        total = no_attr_qs.count()
        self.stdout.write(f'Variants without attributes (excl. agrotextile): {total}')

        assigned = skipped = 0
        for v in no_attr_qs:
            attr_name, value = parse_size(v.name)
            if not attr_name:
                skipped += 1
                continue
            attr = get_attr(attr_name)
            av = get_av(attr, value)
            v.attribute_values.add(av)
            assigned += 1

        self.stdout.write(f'Assigned: {assigned}, could not parse: {skipped}')
        self.stdout.write(self.style.SUCCESS('Done!'))
