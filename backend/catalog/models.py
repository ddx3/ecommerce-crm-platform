from django.conf import settings
import os

from django.db import models
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator
from django_cleanup import cleanup
from slugify import slugify


def _site_url(path: str) -> str:
    """Absolute URL to the frontend site (takes SITE_URL from settings)."""
    base = getattr(settings, 'SITE_URL', '').rstrip('/')
    return f"{base}{path}"


def unique_slug(model_class, base_slug, exclude_pk=None):
    slug = base_slug
    n = 1
    qs = model_class.objects.filter(slug=slug)
    if exclude_pk:
        qs = qs.exclude(pk=exclude_pk)
    while qs.exists():
        n += 1
        slug = f'{base_slug}-{n}'
        qs = model_class.objects.filter(slug=slug)
        if exclude_pk:
            qs = qs.exclude(pk=exclude_pk)
    return slug


class Label(models.Model):
    SLUG_NOVYNKA        = 'novynka'
    SLUG_PID_ZAMOVLENNYA = 'pid-zamovlennya'
    SLUG_AKTSIA         = 'aktsia'
    SLUG_KHIT           = 'khit'

    name      = models.CharField(max_length=50, verbose_name='Name')
    color     = models.CharField(max_length=20, default='#ff0000', verbose_name='Color')
    slug      = models.CharField(max_length=50, unique=True, null=True, blank=True, verbose_name='Slug')
    is_system = models.BooleanField(default=False, verbose_name='System')
    auto_days = models.PositiveIntegerField(null=True, blank=True, verbose_name='Days (auto)', help_text='How many days an auto label stays active once set. Empty = indefinite.')

    class Meta:
        verbose_name = 'Label'
        verbose_name_plural = 'Labels'

    def __str__(self):
        return self.name

    @classmethod
    def display_rank(cls, slug):
        """Fixed order of labels in the output (lower = earlier).
        Hit / Sale / New first (selling ones), custom ones in the middle,
        "Made to order" always last (it is an availability restriction, not an
        advantage)."""
        return {
            cls.SLUG_KHIT: 10,
            cls.SLUG_AKTSIA: 20,
            cls.SLUG_NOVYNKA: 30,
            cls.SLUG_PID_ZAMOVLENNYA: 1000,
        }.get(slug, 100)


class Category(models.Model):
    name = models.CharField(max_length=200, verbose_name='Name')
    slug = models.SlugField(max_length=200, unique=True, verbose_name='Slug')
    parent = models.ForeignKey(
        'self', null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='children',
        verbose_name='Parent category'
    )
    image = models.ImageField(upload_to='categories/', blank=True, null=True, verbose_name='Image')
    is_active = models.BooleanField(default=True, verbose_name='Visible')
    sort_order = models.PositiveIntegerField(default=0, verbose_name='Sort order')
    meta_title = models.CharField(max_length=200, blank=True, verbose_name='Meta Title')
    meta_description = models.TextField(blank=True, verbose_name='Meta Description')
    discount_percent = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True, verbose_name='Discount %')
    discount_start = models.DateTimeField(null=True, blank=True, verbose_name='Discount start')
    discount_end = models.DateTimeField(null=True, blank=True, verbose_name='Discount end')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Category'
        verbose_name_plural = 'Categories'
        ordering = ['sort_order', 'name']

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = unique_slug(self.__class__, slugify(self.name), self.pk)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return _site_url(f"/catalog?category={self.slug}")

    @classmethod
    def get_subtree_ids(cls, root_ids, include_self=True):
        """IDs of a category plus all its descendants at any depth.

        Used everywhere a category filter must cascade into subcategories
        (products, bundles, courses).
        """
        if isinstance(root_ids, int):
            root_ids = [root_ids]
        root_ids = {pk for pk in root_ids if pk is not None}
        if not root_ids:
            return []
        collected = set(root_ids) if include_self else set()
        frontier = root_ids
        while frontier:
            children = set(
                cls.objects.filter(parent_id__in=frontier).values_list('id', flat=True)
            )
            new = children - collected
            if not new:
                break
            collected |= new
            frontier = new
        return list(collected)

    def get_subtree_ids_self(self, include_self=True):
        return self.__class__.get_subtree_ids([self.pk], include_self=include_self)


class Attribute(models.Model):

    name = models.CharField(max_length=100, unique=True, verbose_name='Name')
    is_color = models.BooleanField(default=False, verbose_name='Color')

    class Meta:
        verbose_name = 'Attribute'
        verbose_name_plural = 'Attributes'
        constraints = [
            models.UniqueConstraint(
                fields=['is_color'],
                condition=models.Q(is_color=True),
                name='catalog_attribute_single_color',
            ),
        ]

    def __str__(self):
        return self.name


class AttributeValue(models.Model):
    attribute = models.ForeignKey(Attribute, on_delete=models.CASCADE, related_name='values', verbose_name='Attribute')
    value = models.CharField(max_length=100, verbose_name='Value')
    color_hex = models.CharField(max_length=7, blank=True, verbose_name='HEX')

    class Meta:
        verbose_name = 'Attribute value'
        verbose_name_plural = 'Attribute values'
        unique_together = ['attribute', 'value']

    def __str__(self):
        return f'{self.attribute.name}: {self.value}'


# @cleanup.ignore: the logo file may be shared with BrandImage (the photo manager creates
# a BrandImage at the same path). django-cleanup without ref-counting would delete the file,
# breaking the "neighboring" image. Files are deleted manually in the endpoints in views.py.
@cleanup.ignore
class Brand(models.Model):
    name = models.CharField(max_length=200, verbose_name='Name')
    slug = models.SlugField(max_length=200, unique=True, blank=True, verbose_name='Slug')
    logo = models.ImageField(upload_to='brands/', blank=True, null=True, verbose_name='Logo')
    discount_percent = models.PositiveSmallIntegerField(null=True, blank=True, verbose_name='Discount %', validators=[MaxValueValidator(100)])
    is_active = models.BooleanField(default=True, verbose_name='Visible')
    meta_title = models.CharField(max_length=200, blank=True, verbose_name='Meta Title')
    meta_description = models.TextField(blank=True, verbose_name='Meta Description')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Brand'
        verbose_name_plural = 'Brands'
        ordering = ['name']

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = unique_slug(self.__class__, slugify(self.name), self.pk)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return _site_url(f"/catalog?brand={self.slug}")


@cleanup.ignore  # shares the file with Brand.logo — see the comment above
class BrandImage(models.Model):
    brand = models.ForeignKey(Brand, on_delete=models.CASCADE, related_name='images', null=True, blank=True, verbose_name='Brand')
    image = models.ImageField(upload_to='brands/', verbose_name='Image')
    sort_order = models.IntegerField(default=0)
    temp_key = models.CharField(max_length=100, blank=True, db_index=True)

    class Meta:
        verbose_name = 'Brand photo'
        verbose_name_plural = 'Brand photos'
        ordering = ['sort_order', 'pk']


class Product(models.Model):
    PACKAGING_FACTORY = 'factory'
    PACKAGING_OWN = 'own'
    PACKAGING_CHOICES = [
        (PACKAGING_FACTORY, 'Factory'),
        (PACKAGING_OWN, 'Own'),
    ]

    name = models.CharField(max_length=300, verbose_name='Name')
    slug = models.SlugField(max_length=300, unique=True, verbose_name='Slug')
    description = models.TextField(blank=True, verbose_name='Description')
    application_norms = models.TextField(blank=True, verbose_name='Application norms')
    raw_text = models.TextField(blank=True, default='', verbose_name='Raw text (draft for AI)')
    raw_characteristics = models.TextField(blank=True, default='', verbose_name='Raw characteristics text')
    raw_composition = models.TextField(blank=True, default='', verbose_name='Raw composition text')
    llm_processed = models.BooleanField(default=False, verbose_name='Processed by AI',
                                        help_text='The product content has already been run through the AI magic buttons')
    categories = models.ManyToManyField(Category, related_name='products', verbose_name='Categories')
    labels = models.ManyToManyField(Label, blank=True, related_name='products', verbose_name='Common labels')
    brand = models.ForeignKey(Brand, null=True, blank=True, on_delete=models.SET_NULL, related_name='products', verbose_name='Brand')
    is_active = models.BooleanField(default=True, verbose_name='Visible')
    packaging_kind = models.CharField(
        max_length=10,
        choices=PACKAGING_CHOICES,
        default=PACKAGING_FACTORY,
        verbose_name='Packaging type',
    )
    paired_product = models.OneToOneField(
        'self',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='+',
        verbose_name='Paired product',
    )
    meta_title = models.CharField(max_length=200, blank=True, verbose_name='Meta Title')
    meta_description = models.TextField(blank=True, verbose_name='Meta Description')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'product'
        verbose_name_plural = 'Products'
        indexes = [
            models.Index(fields=['is_active']),
        ]

    def clean(self):
        super().clean()
        if self.paired_product_id:
            partner = self.paired_product
            if partner.packaging_kind == self.packaging_kind:
                raise ValidationError({'paired_product': 'The pair must be factory ↔ own.'})

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = unique_slug(self.__class__, slugify(self.name), self.pk)
        super().save(*args, **kwargs)
        self._mirror_pair()

    def _mirror_pair(self):
        if self.paired_product_id:
            Product.objects.filter(pk=self.paired_product_id)\
                .exclude(paired_product_id=self.pk)\
                .update(paired_product_id=self.pk)
        Product.objects.filter(paired_product_id=self.pk)\
            .exclude(pk=self.paired_product_id or 0)\
            .update(paired_product=None)

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return _site_url(f"/product/{self.slug}")


class BulkSource(models.Model):
    UNIT_CHOICES = [('g', 'g'), ('ml', 'ml'), ('pcs', 'pcs')]
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='bulk_sources', verbose_name='Product')
    name = models.CharField(max_length=100, verbose_name='Bag name')
    quantity = models.DecimalField(max_digits=10, decimal_places=3, verbose_name='Stock')
    unit = models.CharField(max_length=10, choices=UNIT_CHOICES, default='g', verbose_name='Unit')
    waste_percent = models.DecimalField(
        max_digits=5, decimal_places=2, default=1, verbose_name='Packaging tolerance, %',
        help_text='Repacking/spillage/residue left in the machine: for 1 packaging, '
                  'unit_size × (1 + %/100) is actually written off from stock. Ignored for piece-counted bags (pcs).')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Bag (bulk stock)'
        verbose_name_plural = 'Bags (bulk stock)'

    def __str__(self):
        return f'{self.name} ({self.quantity} {self.unit})'

    @staticmethod
    def _dec(value, default='0'):
        """Numeric field → Decimal. CRM endpoints put raw JSON values ('15000')
        straight into the field: Django coerces them only when writing to the DB, so the post_save
        signal
        saw a str and crashed on `str // Decimal` — the bag was saved, but the packagings' stock was
        not."""
        from decimal import Decimal, InvalidOperation
        if isinstance(value, Decimal):
            return value
        try:
            return Decimal(str(value if value is not None else default))
        except (InvalidOperation, ValueError, TypeError):
            return Decimal(default)

    def save(self, *args, **kwargs):
        self.quantity = self._dec(self.quantity)
        self.waste_percent = self._dec(self.waste_percent)
        super().save(*args, **kwargs)

    def effective_unit(self, unit_size):
        """How many stock units (g/ml) actually go into 1 packaging, accounting for tolerance.
        Tolerance is not applied to piece-counted bags. Single source of truth for all
        stock/write-off calculations of own packagings."""
        from decimal import Decimal
        base = self._dec(unit_size)
        if base <= 0:
            return base
        if self.unit == 'pcs':
            return base
        return base * (Decimal('1') + self._dec(self.waste_percent) / Decimal('100'))

    def sync_variants(self):
        qty = self._dec(self.quantity)
        for v in self.variants.all():
            if v.stock_manual:
                continue  # manual stock level — do not touch
            if v.unit_size:
                eu = self.effective_unit(v.unit_size)
                v.stock = int(qty // eu) if eu > 0 else 0
                v.save(update_fields=['stock', 'updated_at'])

    # ── Single point for changing a bag's stock via query ────────────────────────────
    # The own packaging's variant.stock is not a fact but DERIVED from the bag's quantity. A
    # queryset
    # .update()/F() bypasses post_save, so the recalculation of packagings had to be added
    # by hand after each call — and a forgotten one, or one interrupted by an exception,
    # leaves a lie in the DB («the bag is full, the product is not in stock»). These two methods
    # make the write and the resync inseparable: it is impossible to forget the recalculation.

    @classmethod
    def _mutate(cls, bag_id, new_quantity_expr):
        from django.db import transaction
        if not bag_id:
            return None
        with transaction.atomic():
            updated = cls.objects.filter(pk=bag_id).update(quantity=new_quantity_expr)
            if not updated:
                return None
            bag = cls.objects.filter(pk=bag_id).first()
            if bag is not None:
                bag.sync_variants()
            return bag

    @classmethod
    def apply_delta(cls, bag_id, delta, allow_negative=False):
        """quantity += delta (negative = write-off) clamped to ≥0 + packaging recalculation.
        allow_negative=True — a deliberate write-off against a planned delivery (the manager
        confirmed the negative when creating the order): the clamp is not applied, the bag
        (and the derived stock of the packagings via sync_variants) may go negative."""
        from decimal import Decimal
        from django.db.models import F, Value
        from django.db.models.functions import Greatest
        delta = cls._dec(delta)
        if allow_negative:
            return cls._mutate(bag_id, F('quantity') + delta)
        return cls._mutate(bag_id, Greatest(F('quantity') + delta, Value(Decimal('0'))))

    @classmethod
    def set_quantity(cls, bag_id, value):
        """quantity = value (absolute value), clamped to >= 0, plus recalculation of the packagings."""
        from decimal import Decimal
        value = cls._dec(value)
        return cls._mutate(bag_id, value if value > 0 else Decimal('0'))


class Supplier(models.Model):
    """Supplier of dropshipping/hybrid products (Bradas is pure dropshipping, ARS is hybrid 50/50).
    Each supplier ships its own separate parcel (Nova Poshta, branch)."""
    KIND_CHOICES = [('drop', 'Dropshipping (pure)'), ('hybrid', 'Hybrid 50/50')]
    code = models.CharField(max_length=20, unique=True, verbose_name='Code')
    name = models.CharField(max_length=200, verbose_name='Name')
    base_url = models.URLField(blank=True, verbose_name='Supplier website')
    kind = models.CharField(max_length=10, choices=KIND_CHOICES, default='drop', verbose_name='Type')
    # Regular branches (up to 30 kg) do not accept bulky goods (agrofibre, netting).
    # A flag rather than a hardcoded supplier code, so the next carrier doesn't have to be added to
    # JS.
    requires_cargo_branch = models.BooleanField(
        default=False, verbose_name='Nova Poshta cargo branches only',
        help_text='Parcels from this supplier can only be shipped to cargo '
                  'branches (no weight limit, or up to 200–1000 kg).',
    )
    is_active = models.BooleanField(default=True, verbose_name='Active')

    class Meta:
        verbose_name = 'Supplier'
        verbose_name_plural = 'Suppliers'
        ordering = ['name']

    def __str__(self):
        return self.name

import re as _re

_UNIT_UNSET = object()  # marker for "unit_size was not loaded" (deferred/.only())

# Trailing weight label at the end of the string: number + unit (possibly followed by a descriptive
# word, like "300 g bag").
# LANGUAGE DATA: the unit words are Ukrainian (кг = kg, г = g, мл = ml, л = l, шт = pcs) because the
# catalog's product/variant names are typed in Ukrainian by the shop staff.
_UNIT_TAIL_RE = _re.compile(r'(\d+(?:[.,]\d+)?)\s*(кг|г|мл|л|шт)\b', _re.IGNORECASE)
# bag unit → (attribute that is the source of the weight label, small unit, large unit)
# The attribute names are the English names stored in the catalog DB ("Weight", "Volume", "Quantity").
# LANGUAGE DATA: the unit words (г/кг = g/kg, мл/л = ml/l, шт = pcs) stay Ukrainian, see above.
_UNIT_META = {'g': ('Weight', 'г', 'кг'), 'ml': ('Volume', 'мл', 'л'), 'pcs': ('Quantity', 'шт', 'шт')}


def format_unit_label(size, unit='g'):
    """unit_size (integer in g/ml/pcs) → storefront label following the catalog convention:
    <1000 → "N g", multiple of 1000 → "N kg", otherwise "N,N kg" (decimal comma). For pcs — "N pcs".
    The unit words in the output are Ukrainian (see _UNIT_META)."""
    from decimal import Decimal
    small, big = _UNIT_META.get(unit, _UNIT_META['g'])[1:]
    if unit == 'pcs':
        return f'{int(size)} {small}'
    s = Decimal(str(size))
    if s >= 1000:
        v = s / Decimal('1000')
        txt = str(int(v)) if v == v.to_integral_value() else str(v.normalize()).replace('.', ',')
        return f'{txt} {big}'
    return f'{int(s)} {small}'


def _rewrite_unit_tail(text, label):
    """Replace the LAST weight token in the string with the new label, keeping the descriptive tail
    (e.g. "300 g bag" → "500 g bag"). If there is no token, the string is left
    unchanged (None)."""
    if not text:
        return None
    matches = list(_UNIT_TAIL_RE.finditer(text))
    if not matches:
        return None
    m = matches[-1]
    new = text[:m.start()] + label + text[m.end():]
    return new if new != text else None


def sync_variant_unit_label(variant):
    """Sync the variant's storefront weight label with unit_size (the "Packaging size" field).
    ONLY updates representations that already exist — the weight tail in name and the
    attribute value (Weight/Volume/Quantity, see _UNIT_META) — and does not create new
    ones (to avoid spawning
    extra selectors and spoiling manually set names). Called from ProductVariant.save
    when unit_size changes. Returns True if name was changed (needs saving)."""
    if not variant.unit_size or not variant.pk:
        return False
    unit = variant.bulk_source.unit if variant.bulk_source_id else 'g'
    attr_name = _UNIT_META.get(unit, _UNIT_META['g'])[0]
    label = format_unit_label(variant.unit_size, unit)

    # 2) Weight attribute: replace the value with the new one, keeping the descriptive tail
    #    ("300 g bag" → "500 g bag"). A shared AttributeValue is not mutated —
    #    find-or-create + relink, so neighboring variants aren't affected.
    current = [av for av in variant.attribute_values.all() if av.attribute.name == attr_name]
    if current:
        av = current[0]
        new_value = _rewrite_unit_tail(av.value, label) or label
        if new_value != av.value:
            attr = av.attribute
            target, _ = AttributeValue.objects.get_or_create(attribute=attr, value=new_value)
            variant.attribute_values.remove(*current)
            variant.attribute_values.add(target)

    # 1) name: update the weight tail only if it is already there
    new_name = _rewrite_unit_tail(variant.name, label)
    if new_name:
        variant.name = new_name
        return True
    return False


class ProductVariant(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='variants', verbose_name='Product')
    name = models.CharField(max_length=300, blank=True, verbose_name='Name')
    description = models.TextField(blank=True, verbose_name='Variant description')
    raw_text = models.TextField(blank=True, default='', verbose_name='Raw variant text (draft for AI)')
    price = models.DecimalField(max_digits=10, decimal_places=2, verbose_name='Price')
    old_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, verbose_name='Old price')
    discount_percent = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True, verbose_name='Discount %')
    discount_amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, verbose_name='Discount, UAH')
    # IntegerField (not Positive) is deliberate: a manager can confirm a write-off
    # above the stock level for a scheduled delivery (OrderItem.allow_negative_stock),
    # in which case the stock level/bulk bag temporarily goes negative until the supplier invoice
    # arrives.
    stock = models.IntegerField(default=0, verbose_name='Stock')
    stock_manual = models.BooleanField(
        default=False, verbose_name='Manual stock',
        help_text='If True, variant.stock is set manually and is NOT recalculated from the bag (for own packagings).',
    )
    sku = models.CharField(max_length=10, unique=True, blank=True, null=True, verbose_name='SKU')
    slug = models.SlugField(max_length=300, unique=True, blank=True, null=True, verbose_name='Slug')
    is_active = models.BooleanField(default=True, verbose_name='Visible')
    is_dropshipping = models.BooleanField(default=False, verbose_name='Dropshipping')
    is_supplier_fallback = models.BooleanField(
        default=False, db_index=True, verbose_name='Unprocessed (from supplier, no template)',
        help_text='True — the variant was imported via _fallback_group, without a formatter. '
                  "If all of a product's variants are like this, the product is hidden automatically.",
    )
    is_hybrid = models.BooleanField(default=False, verbose_name='50/50')
    supplier_sku = models.CharField(max_length=100, blank=True, verbose_name='Supplier SKU')
    supplier_url = models.URLField(blank=True, verbose_name='Supplier URL')
    supplier_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, verbose_name='Purchase price')
    supplier_min_qty = models.PositiveIntegerField(
        default=1, verbose_name='Supplier minimum batch',
        help_text='For some products Bradas shows the price per 1 pc but sells only in batches '
                  '(e.g. a pack of 50 pcs). Then purchase cost = price × this quantity.')
    supplier = models.ForeignKey('Supplier', on_delete=models.PROTECT, null=True, blank=True, related_name='variants', verbose_name='Supplier (dropshipping/hybrid)')
    price_auto = models.BooleanField(default=False, verbose_name='Auto price')
    stock_auto = models.BooleanField(default=False, verbose_name='Auto availability')
    last_synced_at = models.DateTimeField(null=True, blank=True, verbose_name='Last synced')
    bulk_source = models.ForeignKey('BulkSource', on_delete=models.SET_NULL, null=True, blank=True, related_name='variants', verbose_name='Bag')
    unit_size = models.PositiveIntegerField(null=True, blank=True, verbose_name='Packaging size')
    # Sheet of insert stickers printed for this packaging (A4, 28 stickers).
    paired_variant = models.OneToOneField(
        'self', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='+', verbose_name='Paired variant',
    )
    out_of_stock_at = models.DateTimeField(
        null=True, blank=True, db_index=True, verbose_name='Sold out',
        help_text='When the packaging hit zero. Cleared automatically on restocking. '
                  'Empty means either it is in stock or the zero predates the start of tracking.',
    )
    low_stock_at = models.DateTimeField(
        null=True, blank=True, db_index=True, verbose_name='Became low',
        help_text='Since when the stock has been continuously below the dynamic "Low" threshold. '
                  'Maintained by catalog.notifications.low_stock_variants() on every '
                  'calculation (not by a signal) — the threshold itself is dynamic, so recomputing it '
                  'on every stock change is unnecessary. Cleared as soon as the threshold is exceeded again '
                  'or the product sells out to zero.',
    )
    low_stock_threshold_override = models.PositiveIntegerField(
        null=True, blank=True, verbose_name='Manual "Low" threshold',
        help_text='If set, ignores the auto calculation (sales rate) and the '
                  '"not enough sales history" gate; the "Low" threshold = this number. Empty = auto.',
    )
    attribute_values = models.ManyToManyField(AttributeValue, blank=True, related_name='variants', verbose_name='Attributes')
    labels = models.ManyToManyField(Label, through='VariantLabel', blank=True, related_name='variants', verbose_name='Labels')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Product variant'
        verbose_name_plural = 'Product variants'
        indexes = [
            models.Index(fields=['product', 'is_active']),
            models.Index(fields=['is_dropshipping']),
        ]

    @classmethod
    def from_db(cls, db, field_names, values):
        inst = super().from_db(db, field_names, values)
        inst._loaded_unit_size = inst.unit_size if 'unit_size' in field_names else _UNIT_UNSET
        return inst

    def save(self, *args, **kwargs):
        if not self.name and self.product_id:
            self.name = self.product.name
        # Only the imports (xlsx and dropshipping) set the slug, the CRM form did not, so
        # variants created manually were left with slug=NULL. Done the same way as in Product.
        if not self.slug:
            base = slugify(self.name or '') or f'variant-{self.sku or "new"}'
            self.slug = unique_slug(self.__class__, base, self.pk)
            uf = kwargs.get('update_fields')
            if uf is not None and 'slug' not in uf:
                kwargs['update_fields'] = list(uf) + ['slug']
        _prev_us = getattr(self, '_loaded_unit_size', _UNIT_UNSET)
        # Auto-link of own packaging to the product's bulk bag when "Packaging
        # size" (unit_size) is set
        # but no bag is attached (a typical cause of "out of stock": unit_size is
        # set,
        # the bag exists, but there is no link between them — the JS linking in the form is
        # unreliable). Only on a
        # FRESH set/change of unit_size — not retroactively on every save, otherwise it would affect
        # variants deliberately left on manual stock. Old orphan data — via the
        # link_orphan_bulk_variants command.
        _us_fresh = bool(self.unit_size and (_prev_us is _UNIT_UNSET or self.unit_size != _prev_us))
        if _us_fresh and not self.bulk_source_id and self.product_id:
            _bag = BulkSource.objects.filter(product_id=self.product_id).order_by('pk').first()
            if _bag is not None:
                self.bulk_source = _bag
                uf = kwargs.get('update_fields')
                if uf is not None and 'bulk_source' not in uf:
                    kwargs['update_fields'] = list(uf) + ['bulk_source']
        # Auto-recalculation of stock from the bag: bag attached + packaging weight + stock NOT
        # manual. Otherwise stock stayed 0 until you re-saved the bag itself (sync_variants).
        if self.bulk_source_id and self.unit_size and not self.stock_manual:
            bag = self.bulk_source
            if bag and bag.quantity is not None:
                eu = bag.effective_unit(self.unit_size)
                new_stock = int(bag._dec(bag.quantity) // eu) if eu > 0 else 0
                if new_stock != self.stock:
                    self.stock = new_stock
                    uf = kwargs.get('update_fields')
                    if uf is not None and 'stock' not in uf:
                        kwargs['update_fields'] = list(uf) + ['stock']
        # Change of "Packaging size" (unit_size) on an existing variant → pull
        # through the storefront
        # weight label (name tail + the Weight attribute), since for the customer these are
        # different fields (unit_size
        # is not exposed by the serializer). Only on a real change of an existing value.
        _sync_label = bool(self.pk and self.unit_size
                           and _prev_us is not _UNIT_UNSET and self.unit_size != _prev_us)
        if _sync_label and sync_variant_unit_label(self):
            uf = kwargs.get('update_fields')
            if uf is not None and 'name' not in uf:
                kwargs['update_fields'] = list(uf) + ['name']
        super().save(*args, **kwargs)
        self._loaded_unit_size = self.unit_size
        if self.paired_variant_id:
            ProductVariant.objects.filter(pk=self.paired_variant_id)\
                .exclude(paired_variant_id=self.pk)\
                .update(paired_variant_id=self.pk)
        ProductVariant.objects.filter(paired_variant_id=self.pk)\
            .exclude(pk=self.paired_variant_id or 0)\
            .update(paired_variant=None)

    @classmethod
    def generate_unique_sku(cls, *, also_check_bundle=False):
        """Random 4-digit SKU, unique among variants (and optionally among Bundles)."""
        import random
        while True:
            sku = str(random.randint(1000, 9999))
            if cls.objects.filter(sku=sku).exists():
                continue
            if also_check_bundle:
                from .models import Bundle
                if Bundle.objects.filter(sku=sku).exists():
                    continue
            return sku

    @property
    def effective_discount_percent(self):
        """Variant discount; if not set, the brand discount is used."""
        if self.discount_percent:
            return self.discount_percent
        brand = self.product.brand if self.product_id else None
        if brand and brand.discount_percent:
            return brand.discount_percent
        return None

    @property
    def effective_price(self):
        """Final variant price with discount — for computing bundle prices."""
        from decimal import Decimal
        if self.price is None:
            return None
        base = Decimal(str(self.price))
        if self.old_price and Decimal(str(self.old_price)) > base:
            price = base
        else:
            pct = self.effective_discount_percent
            if pct and Decimal('0') < Decimal(str(pct)) < Decimal('100'):
                price = (base * (Decimal('1') - Decimal(str(pct)) / Decimal('100'))).quantize(Decimal('0.01'))
            else:
                price = base
        # Fixed discount in UAH — applied on top of % (as in Bundle._apply_own_discount).
        # Positive only: a negative 'discount' would otherwise RAISE the price (guard against an
        # admin mistake).
        if self.discount_amount and self.discount_amount > 0:
            price = (price - Decimal(str(self.discount_amount))).quantize(Decimal('0.01'))
            if price <= 0:
                price = Decimal('0.01')
        return price

    @property
    def final_price_whole(self):
        """Item price in an order: effective_price (variant/brand discount) in whole hryvnias —
        same as create_order on the storefront. For manual orders in the CRM."""
        from decimal import Decimal, ROUND_HALF_UP
        ep = self.effective_price
        if ep is None:
            return None
        return ep.quantize(Decimal('1'), rounding=ROUND_HALF_UP)

    def __str__(self):
        return self.name or str(self.product)


class VariantPrintout(models.Model):
    """Sheet of insert stickers for a single packaging. There can be several.

    A separate model rather than a field on the packaging: originally it was a FileField, but a
    single
    packaging can have more than one sheet (different languages, different density per sheet,
    a separate insert with application rates). Tied to the packaging because the weight is
    printed on each sticker — a product with 5 packagings has 5 different sheets.

    django_cleanup deletes the file from disk along with the row.
    """
    variant = models.ForeignKey(
        'ProductVariant', on_delete=models.CASCADE, related_name='printouts',
        verbose_name='Packaging',
    )
    file = models.FileField(upload_to='printouts/', verbose_name='Sheet')
    sort_order = models.PositiveIntegerField(default=0, verbose_name='Order')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Printout'
        verbose_name_plural = 'Printouts'
        ordering = ['sort_order', 'pk']

    def __str__(self):
        return os.path.basename(self.file.name or '') or f'Printout #{self.pk}'

    @property
    def basename(self):
        return os.path.basename(self.file.name or '')


# @cleanup.ignore: one physical file is deliberately shared between ProductImage rows — photo
# dedup by URL/hash in import_dropshipping and copying when duplicating a product
# (image=img.image). Auto-deleting the file would break neighboring images.
# File deletion on manual photo deletion is in the views.py endpoints.
@cleanup.ignore
class ProductImage(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='images', verbose_name='Product', null=True, blank=True)
    variant = models.ForeignKey('ProductVariant', on_delete=models.SET_NULL, null=True, blank=True, related_name='images', verbose_name='Variant')
    color_value = models.ForeignKey(
        'AttributeValue', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='product_images', verbose_name='Color',
        help_text='The photo belongs to this color — shared by all variants of this color.'
    )
    temp_key = models.CharField(max_length=64, blank=True, null=True, db_index=True)
    image = models.ImageField(upload_to='products/', verbose_name='Image')
    sort_order = models.PositiveIntegerField(default=0, verbose_name='Order')
    is_main = models.BooleanField(default=False, verbose_name='Cover photo')
    is_hidden = models.BooleanField(default=False, verbose_name='Hidden')
    source_url = models.URLField(
        max_length=500, blank=True, null=True, db_index=True,
        verbose_name='Source URL (for re-import)',
        help_text='Filled in when importing from xlsx. Images without a source_url are left untouched.'
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Product photo'
        verbose_name_plural = 'Product photos'
        ordering = ['sort_order']

    def __str__(self):
        if self.variant_id:
            return f'Variant photo #{self.variant_id}'
        return f'Photo {self.product.name if self.product else "?"}'


class ProductCharacteristic(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, null=True, blank=True, related_name='characteristics', verbose_name='Product')
    variant = models.ForeignKey(ProductVariant, on_delete=models.CASCADE, null=True, blank=True, related_name='characteristics', verbose_name='Variant')
    name = models.CharField(max_length=100, verbose_name='Name')
    value = models.CharField(max_length=300, verbose_name='Value')
    color_hex = models.CharField(max_length=7, blank=True, verbose_name='Color', help_text='Optional: HEX color for the swatch next to the value (as with a color attribute).')
    sort_order = models.PositiveIntegerField(default=0, verbose_name='Order')

    class Meta:
        verbose_name = 'Characteristic'
        verbose_name_plural = 'Characteristics'
        ordering = ['sort_order']

    def __str__(self):
        return f'{self.name}: {self.value}'


class ProductComposition(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='compositions', verbose_name='Product')
    name = models.CharField(max_length=200, verbose_name='Name')
    value = models.CharField(max_length=300, blank=True, verbose_name='Value')
    sort_order = models.PositiveIntegerField(default=0, verbose_name='Order')

    class Meta:
        verbose_name = 'Composition'
        verbose_name_plural = 'Composition'
        ordering = ['sort_order']

    def __str__(self):
        return f'{self.name}: {self.value}'


class VariantLabel(models.Model):
    variant    = models.ForeignKey(ProductVariant, on_delete=models.CASCADE, related_name='variant_labels', verbose_name='Variant')
    label      = models.ForeignKey(Label, on_delete=models.CASCADE, related_name='variant_labels', verbose_name='Label')
    expires_at = models.DateTimeField(null=True, blank=True, verbose_name='Valid until (empty = indefinite)')
    is_auto    = models.BooleanField(default=False, verbose_name='Automatic')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('variant', 'label')
        verbose_name = 'Variant label'
        verbose_name_plural = 'Variant labels'

    def __str__(self):
        return f'{self.variant} → {self.label}'

    @property
    def is_active(self):
        from django.utils import timezone
        return self.expires_at is None or self.expires_at > timezone.now()


class DropshippingVariant(ProductVariant):
    """Proxy model for a separate dropshipping section in the admin."""
    class Meta:
        proxy = True
        verbose_name = 'Dropshipping product'
        verbose_name_plural = 'Dropshipping'


class DropshippingProduct(Product):
    """Proxy product model for editing dropshipping in the product-with-packagings format."""
    class Meta:
        proxy = True
        verbose_name = 'Dropshipping product'
        verbose_name_plural = 'Dropshipping products'


class Bundle(models.Model):
    name = models.CharField(max_length=300, verbose_name='Name', default='')
    slug = models.SlugField(max_length=300, unique=True, blank=True, verbose_name='Slug')
    categories = models.ManyToManyField(Category, blank=True, related_name='bundles', verbose_name='Categories')
    description = models.TextField(blank=True, verbose_name='Description')
    raw_text = models.TextField(blank=True, default='', verbose_name='Raw text (draft for AI)')
    sku = models.CharField(max_length=10, unique=True, blank=True, null=True, verbose_name='SKU')
    price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, verbose_name='Price')
    stock = models.PositiveIntegerField(null=True, blank=True, verbose_name='Stock')
    discount_percent = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True, verbose_name='Discount %')
    discount_amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, verbose_name='Discount, UAH')
    application_norms = models.TextField(blank=True, default='', verbose_name='Usage / Norms')
    # --- Printed bundle instructions (also the electronic one in the customer's account) ---
    # Stored as plain Quill HTML, WITHOUT wrapper tokens: no custom blots are
    # registered in the editor, so Quill strips any <span data-*> when loading
    # the value. Therefore the markup lives ALONGSIDE, in instruction_map.
    instruction_html = models.TextField(
        blank=True, default='', verbose_name='Instructions (print)',
        help_text='Printed instructions placed in the box. The same text is opened '
                  'to the customer in "My orders". It does NOT go to the public "Usage" tab.',
    )
    # LLM markup: which exact name each drug goes by IN THIS text and
    # which catalog product it is. Needed because the name in the instructions almost never
    # matches the catalog name ("Magnesium sulfate" in the text vs. "Magnesium sulfate Brand X
    # 25 kg" in the catalog), and auto-replace must find ALL mentions without guessing.
    # Format: {"version": 1, "source_stamp": "<sha1 of the text at the time of markup>",
    #          "mentions": [{"surface": "Product A", "product": 6804,
    #                        "kind": "component"|"external", "doses": ["50 g"]}]}
    # kind=external — a drug mentioned as advice or an upsell ("Product B, can be
    # bought additionally"); it must NEVER be substituted when
    # the composition changes.
    instruction_map = models.JSONField(
        default=dict, blank=True, editable=False, verbose_name='Instructions markup',
    )
    instruction_stamp = models.CharField(
        max_length=40, blank=True, default='', editable=False,
        verbose_name='Instructions fingerprint',
        help_text='sha1 of the body. If it changes, all corrections made for a changed composition become stale.',
    )
    instruction_updated_at = models.DateTimeField(
        null=True, blank=True, editable=False, verbose_name='Instructions updated',
    )
    labels = models.ManyToManyField('Label', blank=True, related_name='bundles', verbose_name='Labels')
    sort_order = models.PositiveIntegerField(default=0, verbose_name='Order')
    meta_title = models.CharField(max_length=200, blank=True, verbose_name='SEO Title',
                                  help_text='Tab title / Google results title. If empty, the bundle name is used.')
    meta_description = models.TextField(blank=True, verbose_name='SEO Description',
                                        help_text='Description in Google results (up to ~160 characters). If empty, the description is used.')
    is_active = models.BooleanField(default=True, verbose_name='Visible')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Bundle'
        verbose_name_plural = 'Bundles'

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return _site_url(f"/bundle/{self.slug}")

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = unique_slug(self.__class__, slugify(self.name), self.pk)
        # An empty SKU is stored as NULL so the unique constraint doesn't fail on ''.
        if not self.sku:
            self.sku = None
        super().save(*args, **kwargs)

    def _apply_own_discount(self, price):
        """Applies the packaging's own discount (discount_percent / discount_amount)."""
        from decimal import Decimal
        if price is None:
            return None
        final = Decimal(str(price))
        if self.discount_percent and Decimal('0') < Decimal(str(self.discount_percent)) < Decimal('100'):
            final = final * (Decimal('1') - Decimal(str(self.discount_percent)) / Decimal('100'))
        if self.discount_amount:
            final = final - Decimal(str(self.discount_amount))
        final = final.quantize(Decimal('0.01'))
        return final if final > 0 else Decimal('0.01')

    def _items_cached(self, *select):
        """Packaging components. If items are prefetched (storefront — bundles_list /
        BundleDetailSerializer), take them from the prefetch cache with no SQL. Otherwise
        (orders/signals/admin/bot) the same select_related query as before,
        so the query count and behavior of non-prefetched calls are unchanged."""
        if 'items' in getattr(self, '_prefetched_objects_cache', {}):
            return list(self.items.all())
        return list(self.items.select_related(*select).all())

    def get_base_price(self):
        """Price without discounts: the explicit packaging price or the sum of the components' BASE prices."""
        from decimal import Decimal
        if self.price is not None:
            return Decimal(str(self.price))
        items = self._items_cached('product_variant')
        if not items:
            return None
        return sum(
            (Decimal(str(it.product_variant.price)) * it.quantity for it in items),
            Decimal('0'),
        )

    def get_price(self):
        """Final packaging price with discounts. The discount is either its own
        (the packaging's discount_percent/amount) or from the components: if the price
        is computed from the components, their already discounted prices are used."""
        from decimal import Decimal
        if self.price is not None:
            return self._apply_own_discount(self.price)
        items = self._items_cached('product_variant', 'product_variant__product__brand')
        if not items:
            return None
        # Component price: with discount (effective_price) if apply_discount;
        # otherwise the base price without discount. Consistent with the "Apply component
        # discount to the price" checkbox.
        total = sum(
            (
                Decimal(str(
                    it.product_variant.effective_price if it.apply_discount
                    else it.product_variant.price
                )) * it.quantity
                for it in items
            ),
            Decimal('0'),
        )
        return self._apply_own_discount(total)

    def get_old_price(self):
        """Old (struck-through) price for the storefront, or None if there is no discount."""
        base = self.get_base_price()
        final = self.get_price()
        if base is None or final is None:
            return None
        return base if base > final else None

    def get_stock(self):
        if self.stock is not None:
            return self.stock
        items = self._items_cached('product_variant', 'product_variant__bulk_source')
        if not items:
            return 0
        from collections import defaultdict
        from decimal import Decimal
        # 1) Total consumption of regular variants (including those with a bulk_source but no
        #    unit_size)
        regular_qty = defaultdict(int)  # variant_id -> total qty per bundle
        regular_variant = {}            # variant_id -> ProductVariant
        # 2) Total consumption from bags (packagings): bag_id -> total g/ml per bundle.
        # Computed in Decimal (as sync_variants/ProductVariant.save — the single source of truth),
        # because float-floor at exact multiple boundaries (leftover==0) diverged from them by ±1.
        by_bag_total = defaultdict(lambda: Decimal('0'))
        by_bag_obj = {}
        for it in items:
            if it.quantity <= 0:
                continue
            v = it.product_variant
            # Manual stock (stock_manual=True) is counted like a regular variant,
            # not as bag-fed (otherwise a single bag would "pin" the rest to a stale calculation).
            if v.bulk_source_id and v.unit_size and not v.stock_manual:
                by_bag_total[v.bulk_source_id] += v.bulk_source.effective_unit(v.unit_size) * it.quantity
                by_bag_obj[v.bulk_source_id] = v.bulk_source
            else:
                regular_qty[v.pk] += it.quantity
                regular_variant[v.pk] = v
        stocks = []
        for vid, total_qty in regular_qty.items():
            v = regular_variant[vid]
            stocks.append((v.stock or 0) // total_qty)
        for bag_id, total in by_bag_total.items():
            bag = by_bag_obj[bag_id]
            if total > 0 and bag.quantity is not None:
                stocks.append(int(bag._dec(bag.quantity) // total))
        return min(stocks) if stocks else 0


@cleanup.ignore
class BundleImage(models.Model):
    bundle = models.ForeignKey(Bundle, on_delete=models.CASCADE, related_name='images', verbose_name='Bundle', null=True, blank=True)
    temp_key = models.CharField(max_length=64, blank=True, null=True, db_index=True)
    image = models.ImageField(upload_to='bundles/', verbose_name='Image')
    sort_order = models.PositiveIntegerField(default=0, verbose_name='Order')
    is_main = models.BooleanField(default=False, verbose_name='Main photo')
    is_hidden = models.BooleanField(default=False, verbose_name='Hidden')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Bundle photo'
        verbose_name_plural = 'Bundle photos'
        ordering = ['sort_order']

    def __str__(self):
        return f'Photo {self.bundle.name}'


class BundleItem(models.Model):
    bundle = models.ForeignKey(Bundle, on_delete=models.CASCADE, related_name='items', verbose_name='Bundle')
    product_variant = models.ForeignKey(ProductVariant, on_delete=models.CASCADE, verbose_name='Product variant')
    quantity = models.PositiveIntegerField(default=1, verbose_name='Quantity')
    apply_discount = models.BooleanField(
        default=True, verbose_name='Apply component discount to the price',
        help_text='If True, the component price in the bundle auto-price is taken with its discount (effective_price), '
                  'otherwise the base price without a discount.',
    )

    class Meta:
        verbose_name = 'Bundle item'
        verbose_name_plural = 'Bundle items'
        # Without an explicit ordering, the composition came back in the physical row order of the
        # table, which changes with any UPDATE (for some bundles it didn't match the order of
        # addition anyway). Order of addition is stable and clear to the manager.
        ordering = ['pk']

    def __str__(self):
        return f'{self.product_variant.name} x{self.quantity}'


class BundleInstructionOverride(models.Model):
    """Instructions for a CHANGED bundle composition.

    The key is not the order number but the substitution signature: "Product A → Product B"
    recurs
    in dozens of orders, so the manager proofreads it once, and all subsequent orders
    with the same substitution print without questions.

    Lifecycle: pending (auto-replace draft) → ready (confirmed by a person)
    or same (the substitution doesn't require a text change). `is_stale` is set when the base
    instructions were edited after confirmation — then the correction goes
    back for review instead of being printed silently.
    """
    PENDING, READY, SAME = 'pending', 'ready', 'same'
    STATUS_CHOICES = [
        (PENDING, 'Needs review'),
        (READY, 'Corrected'),
        (SAME, 'Text unchanged'),
    ]

    bundle = models.ForeignKey(
        Bundle, on_delete=models.CASCADE, related_name='instruction_overrides',
        verbose_name='Bundle',
    )
    signature = models.CharField(max_length=40, db_index=True, verbose_name='Substitution signature')
    summary = models.CharField(
        max_length=500, blank=True, default='', verbose_name='Substitution',
        help_text='Human-readable description, e.g. "Product A → Product B".',
    )
    status = models.CharField(
        max_length=10, default=PENDING, db_index=True, choices=STATUS_CHOICES,
        verbose_name='Status',
    )
    instruction_html = models.TextField(blank=True, default='', verbose_name='Instructions')
    # Auto-replace draft: the drug name has been substituted, the dose stayed the old one and
    # is known to be possibly wrong — which is exactly what the person checks.
    auto_substituted = models.BooleanField(
        default=False, verbose_name='Name filled in automatically',
    )
    base_stamp = models.CharField(
        max_length=40, blank=True, default='', editable=False,
        verbose_name='Base instructions fingerprint',
    )
    is_stale = models.BooleanField(
        default=False, db_index=True, verbose_name='Base instructions changed',
    )
    composition = models.JSONField(
        default=list, blank=True, editable=False, verbose_name='Composition the correction was made for',
    )
    hits = models.PositiveIntegerField(default=0, verbose_name='Times used')
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='+', verbose_name='Confirmed by',
    )
    resolved_at = models.DateTimeField(null=True, blank=True, verbose_name='Confirmed')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Instructions for a changed composition'
        verbose_name_plural = 'Instructions: substitutions in orders'
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['bundle', 'signature'],
                name='bundleinstroverride_uniq_bundle_signature',
            ),
        ]

    def __str__(self):
        return f'{self.bundle} — {self.summary or self.signature[:8]}'


class InstructionJob(models.Model):
    """A bundle instruction run that failed because of the AI limit and was queued.

    When the limit is exhausted, the "Run through AI" button (on the "Instructions
    (print)" tab) doesn't raise an error but leaves here a snapshot of what was
    in the form: the name, text and composition. The scheduler (resolve_pending_instructions, every
    10 min) finishes the queue when the limit recovers and writes the result to the bundle itself —
    see instruction_jobs.py.

    The result is written only if the bundle is still as the human saw it: the text wasn't replaced
    with something else after queuing, the composition matches. Otherwise — STALE: we don't silently
    overwrite someone else's edits.
    """
    PENDING, DONE, STALE, FAILED = 'pending', 'done', 'stale', 'failed'
    STATUS_CHOICES = [
        (PENDING, 'Queued'), (DONE, 'Processed'),
        (STALE, 'Not applied'), (FAILED, 'Failed'),
    ]

    bundle = models.ForeignKey(
        Bundle, null=True, blank=True, on_delete=models.CASCADE, related_name='instruction_jobs',
        verbose_name='Bundle',
        help_text='Empty until the bundle is saved: it is linked on the first "Save".',
    )
    name = models.CharField(max_length=255, blank=True, default='', verbose_name='Name')
    text = models.TextField(verbose_name='Instruction text from the form')
    items = models.JSONField(default=list, verbose_name='Composition from the form', help_text='[{variant_id, qty}]')
    status = models.CharField(max_length=10, default=PENDING, db_index=True, choices=STATUS_CHOICES,
                              verbose_name='Status')
    note = models.CharField(max_length=500, blank=True, default='', verbose_name='Reason / note')
    attempts = models.PositiveSmallIntegerField(default=0, verbose_name='Attempts')
    seen = models.BooleanField(default=False, verbose_name='Manager saw the result')
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='+', verbose_name='Queued by',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = 'Queue entry: bundle instructions'
        verbose_name_plural = 'Queue: bundle instructions'
        ordering = ['created_at']

    def __str__(self):
        return f'{self.name or self.bundle_id or "?"} — {self.get_status_display()}'


def _tracks_running_out(v):
    """Whether it makes sense to say "sold out" about this packaging.

    Dropshipping and hybrid are always made to order — they don't run out. Unprocessed
    supplier packagings are not a storefront product at all.
    """
    return not (v.is_dropshipping or v.is_hybrid or v.is_supplier_fallback)


def sync_out_of_stock_mark(variant, commit=True):
    """Sets the date when the packaging hit zero, and clears it on restocking.

    We store ONLY the date: the "Sold out" list in the CRM is a selection of stock=0,
    not a separate table of rows that would have to be kept in sync with the real
    stock level (rows in such a table inevitably drift from the warehouse).

    Returns True if the mark changed.
    """
    if variant is None or not variant.pk:
        return False
    if not _tracks_running_out(variant) or (variant.stock or 0) > 0:
        desired = None
    else:
        from django.utils import timezone
        desired = variant.out_of_stock_at or timezone.now()
    if variant.out_of_stock_at == desired:
        return False
    if commit:
        # .update() — to avoid triggering post_save (otherwise the signal would loop).
        ProductVariant.objects.filter(pk=variant.pk).update(out_of_stock_at=desired)
    variant.out_of_stock_at = desired
    return True


class Review(models.Model):
    product = models.ForeignKey('Product', null=True, blank=True, on_delete=models.CASCADE, related_name='reviews', verbose_name='Product')
    bundle  = models.ForeignKey('Bundle',  null=True, blank=True, on_delete=models.CASCADE, related_name='reviews', verbose_name='Bundle')
    variant = models.ForeignKey('ProductVariant', null=True, blank=True, on_delete=models.SET_NULL, related_name='reviews', verbose_name='Variant')

    parent       = models.ForeignKey('self', null=True, blank=True, on_delete=models.CASCADE, related_name='replies', verbose_name='Reply to')

    user         = models.ForeignKey('users.User', null=True, blank=True, on_delete=models.SET_NULL, related_name='reviews', verbose_name='User')
    author_name  = models.CharField(max_length=100, verbose_name='Name')
    author_email = models.EmailField(blank=True, verbose_name='Email')
    rating       = models.PositiveSmallIntegerField(
        validators=[MaxValueValidator(5)], null=True, blank=True,
        verbose_name='Rating',
        help_text='If empty, this is a question/comment rather than a rated review.',
    )
    text         = models.TextField(verbose_name='Text')
    is_approved  = models.BooleanField(default=False, verbose_name='Approved')
    is_rejected  = models.BooleanField(default=False, verbose_name='Rejected')
    moderation_verdict = models.CharField(
        max_length=10, blank=True, db_index=True,
        choices=[('pass', 'Skipped'), ('flag', 'For moderation'), ('error', 'LLM error')],
        verbose_name='LLM verdict',
    )
    moderation_reason  = models.CharField(max_length=200, blank=True, verbose_name='Reason')
    created_at   = models.DateTimeField(auto_now_add=True, verbose_name='Date')

    class Meta:
        verbose_name = 'Review'
        verbose_name_plural = 'Reviews'
        ordering = ['-created_at']
        indexes = [
            # Covers the storefront filter filter(is_approved, parent__isnull).order_by(created_at)
            # on product/bundle pages and in the general review list.
            models.Index(fields=['is_approved', 'parent', 'created_at']),
        ]

    def __str__(self):
        target = self.product or self.bundle
        return f'{self.author_name} — {target} ({self.rating}★)'


class DropSyncEvent(models.Model):
    """Log of changes from the supplier (bradas), generated in sync_dropshipping."""
    KIND_NEW_PRODUCT  = 'new_product'
    KIND_NEW_VARIANT  = 'new_variant'
    KIND_NEW_BATCH    = 'new_batch'
    KIND_REMOVED      = 'removed'
    KIND_RETURNED     = 'returned'
    KIND_PRICE_CHANGE = 'price_change'
    KIND_DELETED      = 'deleted'
    KIND_CHOICES = [
        (KIND_NEW_PRODUCT,  'New product (needs review)'),
        (KIND_NEW_VARIANT,  'New variant of an existing product'),
        (KIND_NEW_BATCH,    'New products from supplier (batch)'),
        (KIND_REMOVED,      'Discontinued'),
        (KIND_RETURNED,     'Back on sale'),
        (KIND_PRICE_CHANGE, 'Large price change'),
        (KIND_DELETED,      'Permanently deleted'),
    ]

    kind = models.CharField(max_length=20, choices=KIND_CHOICES, verbose_name='Type')
    variant = models.ForeignKey(
        'ProductVariant', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='+',
        verbose_name='Variant',
    )
    supplier_sku = models.CharField(max_length=100, blank=True, db_index=True, verbose_name='Supplier SKU')
    message = models.TextField(blank=True, verbose_name='Message')
    is_seen = models.BooleanField(default=False, verbose_name='Read')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='Date')

    class Meta:
        verbose_name = 'Sync event'
        verbose_name_plural = 'Sync events'
        ordering = ['-created_at']

    def __str__(self):
        return f'[{self.get_kind_display()}] {self.supplier_sku or "—"}'


class StockNotification(models.Model):
    """A "Notify me when available" subscription — for exactly one target: a
    product
    packaging or a bundle packaging."""
    email = models.EmailField(verbose_name='Email')
    variant = models.ForeignKey(
        'ProductVariant', null=True, blank=True, on_delete=models.CASCADE,
        related_name='notifications', verbose_name='Variant',
    )
    bundle = models.ForeignKey(
        'Bundle', null=True, blank=True, on_delete=models.CASCADE,
        related_name='stock_notifications', verbose_name='Bundle',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    is_notified = models.BooleanField(default=False, verbose_name='Notified')

    class Meta:
        verbose_name = 'Back-in-stock subscription'
        verbose_name_plural = 'Back-in-stock subscriptions'
        unique_together = [('email', 'variant'), ('email', 'bundle')]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(variant__isnull=False, bundle__isnull=True)
                    | models.Q(variant__isnull=True, bundle__isnull=False)
                ),
                name='stocknotif_exactly_one_target',
            ),
        ]

    def __str__(self):
        return f'{self.email} → {self.target_name}'

    @property
    def target_name(self):
        if self.variant_id:
            return self.variant.name or self.variant.product.name
        return self.bundle.name

    @property
    def target_sku(self):
        if self.variant_id:
            return self.variant.sku or ''
        return self.bundle.sku or ''

    def target_available(self):
        """The same condition as on the storefront: the target is shown and the packaging has stock >
        0,
        the bundle has Bundle.get_stock() > 0 (computed from components).
        A hidden target waits: an email with a link to a 404 is worse than a later one."""
        if self.variant_id:
            v = self.variant
            return v.is_active and v.product.is_active and (v.stock or 0) > 0
        b = self.bundle
        return b.is_active and b.get_stock() > 0

    @classmethod
    def send_due(cls, qs=None):
        """Send emails for subscriptions whose target is already in stock. Returns
        the number sent.

        The row is "claimed" by a conditional update(is_notified=False → True) before sending,
        so parallel calls (stock signal + scheduler pass) don't send
        the same email twice. update() bypasses post_save, so the CRM counter
        is refreshed explicitly here."""
        import logging
        from core.email import send_stock_notification
        qs = cls.objects.all() if qs is None else qs
        sent = 0
        for notif in qs.filter(is_notified=False).select_related(
            'variant__product', 'bundle',
        ):
            if not notif.target_available():
                continue
            if not cls.objects.filter(pk=notif.pk, is_notified=False).update(is_notified=True):
                continue
            try:
                send_stock_notification(notif)
            except Exception:
                logging.getLogger(__name__).exception('stock notification #%s: send failed', notif.pk)
            sent += 1
        if sent:
            from .notifications import broadcast_notification_counts
            broadcast_notification_counts()
        return sent


from django.db.models.signals import post_save, post_delete, pre_save
from django.dispatch import receiver

@receiver(post_save, sender=BulkSource)
def bulk_source_post_save(sender, instance, **kwargs):
    instance.sync_variants()


def sync_product_is_active(product_id):
    """Overall product visibility is derived: a product is active if and only if
    it has at least one active NON-fallback variant. So variant visibility "overlays"
    the product: enable a variant and the product becomes visible by itself, disable all of them and
    it disappears."""
    if not product_id:
        return
    active = ProductVariant.objects.filter(
        product_id=product_id, is_active=True, is_supplier_fallback=False,
    ).exists()
    # .update() with no recursion; exclude — so we write only on a real change.
    Product.objects.filter(pk=product_id).exclude(is_active=active).update(is_active=active)


@receiver(post_save, sender=ProductVariant)
@receiver(post_delete, sender=ProductVariant)
def variant_changed_sync_product(sender, instance, **kwargs):
    sync_product_is_active(instance.product_id)


@receiver(post_save, sender=ProductVariant)
def resync_out_of_stock_mark(sender, instance, created=False, **kwargs):
    """A freshly created packaging at zero hasn't "run out" of anything yet, so we don't mark it."""
    if created and not (instance.stock or 0):
        return
    sync_out_of_stock_mark(instance, commit=True)


@receiver(pre_save, sender=Bundle)
def stamp_bundle_instruction(sender, instance, **kwargs):
    """Keep the instruction fingerprint and the edit date.

    The fingerprint is the only way to notice that the base instructions were changed AFTER
    the manager confirmed the correction for a changed composition. Then the correction
    is marked stale and goes back for review instead of being printed silently
    (catalog/instruction_resolve.py, is_stale branch).

    There is no separate "draft/ready" state: if the text is non-empty, the instruction is in
    effect.
    """
    import hashlib
    from django.utils import timezone

    body = (instance.instruction_html or '').strip()
    stamp = hashlib.sha1(body.encode('utf-8')).hexdigest() if body else ''
    if stamp == instance.instruction_stamp:
        return
    instance.instruction_stamp = stamp
    instance.instruction_updated_at = timezone.now()


@receiver(post_save, sender=VariantPrintout)
def resync_printout_thumb(sender, instance, **kwargs):
    """Redraw the sheet preview.

    The signal is attached to the printout itself, not to the packaging: the row is saved only
    when a sheet was actually added or replaced. Previously the field lived on ProductVariant, and
    the signal fired on EVERY packaging save — including batches under
    select_for_update in sync_variants on every sale."""
    from .printout_thumb import make_thumb
    make_thumb(instance)


@receiver(post_delete, sender=VariantPrintout)
def drop_printout_thumb(sender, instance, **kwargs):
    from .printout_thumb import delete_thumb
    delete_thumb(instance.pk)


class NotificationState(models.Model):
    """State of a card on the "Notifications" page: 0=new, 1=viewed, 2=dismissed.

    Not an FK to a specific model — just (section, object_id), so that one table
    serves all tabs (payments / reviews / questions / stock / dropsync).
    """
    SECTION_CHOICES = [
        ('payments',   'Payments'),
        ('requisites', 'Payment details'),
        ('reviews',    'Reviews'),
        ('questions',  'Comments'),
        ('stock',      'Subscriptions'),
        ('stock_like', 'Favorites of out-of-stock items'),
        ('stock_out',  'Sold out'),
        ('stock_low',  'Low'),
        ('dropsync',   'Supplier changes'),
    ]
    section = models.CharField(max_length=20, choices=SECTION_CHOICES, db_index=True)
    object_id = models.IntegerField()
    step = models.PositiveSmallIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Notification state'
        verbose_name_plural = 'Notification states'
        constraints = [
            models.UniqueConstraint(fields=['section', 'object_id'],
                                     name='notifstate_uniq_section_obj'),
        ]
        indexes = [
            models.Index(fields=['section', 'object_id'], name='notifstate_section_obj_idx'),
        ]

    def __str__(self):
        return f'{self.section}#{self.object_id} step={self.step}'


class StockWriteOff(models.Model):
    """Manual write-off from stock (defect, spoilage, shortage) — recognized from
    a photo/text via AI, confirmed by the operator on the /admin/catalog/product/ page.

    Audit trail: what and how much was written off, and when. Unlike Invoice, there is no
    rollback function (no applied_delta/revert): a write-off is one-time and
    deliberately simpler than a stock receipt. If a mistake is made, enter a stock receipt by hand."""
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='Date')
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, verbose_name='Written off by',
    )
    note = models.TextField(blank=True, verbose_name='Note')
    source_text = models.TextField(blank=True, verbose_name='Recognized text / AI note')

    class Meta:
        verbose_name = 'Write-off'
        verbose_name_plural = 'Write-off'
        ordering = ['-created_at']

    def __str__(self):
        return f'Write-off from {self.created_at:%d.%m.%Y %H:%M}'


class StockWriteOffItem(models.Model):
    """A single write-off line — a snapshot of the name/SKU at the time of write-off
    (the variant may later change or be deleted, the record stays readable)."""
    write_off = models.ForeignKey(StockWriteOff, on_delete=models.CASCADE, related_name='items')
    variant = models.ForeignKey(
        'ProductVariant', on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
        verbose_name='Packaging',
    )
    variant_name = models.CharField(max_length=300, blank=True, verbose_name='Name (snapshot)')
    variant_sku = models.CharField(max_length=20, blank=True, verbose_name='SKU (snapshot)')
    quantity = models.PositiveIntegerField(verbose_name='Quantity')

    class Meta:
        verbose_name = 'Write-off item'
        verbose_name_plural = 'Write-off items'

    def __str__(self):
        return f'{self.variant_name or "?"} × {self.quantity}'


class SiteSettings(models.Model):
    """Global site settings. Singleton — always pk=1."""

    maintenance_mode = models.BooleanField(
        default=False,
        verbose_name='Maintenance mode',
        help_text='If enabled, the site shows a maintenance page instead of the catalog. '
                  'The admin (/admin/) and staff users keep working.',
    )

    min_order_amount = models.DecimalField(
        max_digits=10, decimal_places=0, default=250,
        verbose_name='Minimum order amount',
        help_text='Orders with physical goods below this amount cannot be placed. '
                  'Orders consisting solely of courses are not affected by the limit.',
    )

    # ── Contacts/social networks for SEO Organization markup (JSON-LD on the home page) ──
    # Edited by the manager without a redeploy; the cache is cleared in save().
    org_phone = models.CharField(max_length=40, blank=True, verbose_name='Phone (for SEO)')
    org_email = models.EmailField(blank=True, verbose_name='Email (for SEO)')
    org_address = models.CharField(max_length=300, blank=True, verbose_name='Address (for SEO)')
    org_instagram_url = models.URLField(max_length=300, blank=True, verbose_name='Instagram')
    org_facebook_url = models.URLField(max_length=300, blank=True, verbose_name='Facebook')
    org_telegram_url = models.URLField(max_length=300, blank=True, verbose_name='Telegram')

    class Meta:
        verbose_name = 'Settings'
        verbose_name_plural = 'Settings'

    def __str__(self):
        return 'Settings'

    def save(self, *args, **kwargs):
        # Singleton: always pk=1
        self.pk = 1
        super().save(*args, **kwargs)
        # Clear the cache — middleware reads maintenance_mode, the frontend reads the public
        # settings.
        from django.core.cache import cache
        cache.delete('site_settings:maintenance')
        cache.delete('site_settings:public')

    def delete(self, *args, **kwargs):
        # Forbid deleting the singleton.
        pass

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class PaymentCard(models.Model):
    """Payment details card for paying an order. Shown on the payment page.

    Each card has its own threshold — the minimum order amount from which it is
    visible to the customer. The payment page shows the cards with the highest threshold
    that does not exceed the order amount (i.e. the "most expensive" group that still fits).
    The manager edits the list in settings.
    """

    # FK to the singleton (always pk=1) — needed to edit the payment details
    # as an inline table right in the settings form.
    settings = models.ForeignKey(
        SiteSettings, on_delete=models.CASCADE,
        related_name='payment_cards', default=1,
    )
    threshold = models.DecimalField(
        max_digits=10, decimal_places=0, default=0,
        verbose_name='Threshold',
    )
    bank = models.CharField('Bank', max_length=100)
    card_number = models.CharField('Card number', max_length=32)
    holder = models.CharField('Owner', max_length=255)
    sort_order = models.PositiveIntegerField('Order', default=0)
    is_active = models.BooleanField(
        default=True,
        verbose_name='Show on site',
        help_text='If disabled, the payment detail is not shown to customers on the '
                  'payment page (it can still be sent personally).',
    )

    class Meta:
        verbose_name = 'Payment detail'
        verbose_name_plural = 'Payment details'
        ordering = ['threshold', 'sort_order', 'id']

    def __str__(self):
        return f'from {int(self.threshold)} UAH: {self.bank} {self.card_number}'

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        from django.core.cache import cache
        cache.delete('site_settings:public')

    def delete(self, *args, **kwargs):
        super().delete(*args, **kwargs)
        from django.core.cache import cache
        cache.delete('site_settings:public')


class Favorite(models.Model):
    """A product/bundle/course added to "Favorites" on the site. Stored server-side
    to see demand in the CRM (what people want to buy, especially what is out of stock).
    Both a logged-in user and an anonymous one can like — linked via device_id (uuid from
    localStorage). One record = one like of one target from one device."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.CASCADE, related_name='favorites',
        verbose_name='User',
    )
    # Browser/device identifier for anonymous likes (uuid from localStorage).
    device_id = models.CharField(
        max_length=64, blank=True, default='', db_index=True,
        verbose_name='Device (anonymous)',
    )
    # Like target — exactly one of three. CASCADE: product deleted — the like disappears
    # (unlike orders, there is no point keeping like history).
    product_variant = models.ForeignKey(
        ProductVariant, null=True, blank=True, on_delete=models.CASCADE,
        related_name='favorites', verbose_name='Product variant',
    )
    bundle = models.ForeignKey(
        Bundle, null=True, blank=True, on_delete=models.CASCADE,
        related_name='favorites', verbose_name='Bundle',
    )
    course = models.ForeignKey(
        'courses.Course', null=True, blank=True, on_delete=models.CASCADE,
        related_name='favorites', verbose_name='Course',
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='Added')

    class Meta:
        verbose_name = 'Favorites'
        verbose_name_plural = 'Favorites'
        ordering = ['-created_at']
        constraints = [
            # Dedup: one target per device. Partial unique (target is not NULL).
            models.UniqueConstraint(
                fields=['device_id', 'product_variant'],
                name='fav_uniq_device_variant',
                condition=models.Q(product_variant__isnull=False),
            ),
            models.UniqueConstraint(
                fields=['device_id', 'bundle'],
                name='fav_uniq_device_bundle',
                condition=models.Q(bundle__isnull=False),
            ),
            models.UniqueConstraint(
                fields=['device_id', 'course'],
                name='fav_uniq_device_course',
                condition=models.Q(course__isnull=False),
            ),
        ]

    def __str__(self):
        return self.target_name

    @property
    def target_name(self):
        if self.product_variant_id and self.product_variant:
            return self.product_variant.name
        if self.bundle_id and self.bundle:
            return self.bundle.name
        if self.course_id and self.course:
            return f'Course: {self.course.name}'
        return 'Deleted target'



class VariantEmbedding(models.Model):
    """Variant name vector for semantic matching (bundles, supplier invoices).

    Gemini embeddings are strictly rate-limited (~100 names/min), and the catalog is ~1000 names.
    Computing the matrix on the fly in process memory is impossible: any restart
    or catalog change required recomputing everything and hit 429. So the
    vector lives here, and only new/renamed variants are recomputed.

    `name` — the text the vector was computed for. If it diverges from the
    current variant.name, the vector is considered stale.
    """
    variant = models.OneToOneField(
        ProductVariant, on_delete=models.CASCADE, related_name='embedding',
        verbose_name='Variant',
    )
    name = models.CharField(max_length=300, verbose_name='Name at calculation time')
    vector = models.BinaryField(verbose_name='Vector (float32)')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='Updated')

    class Meta:
        verbose_name = 'Variant name vector'
        verbose_name_plural = 'Variant name vectors'

    def __str__(self):
        return self.name
