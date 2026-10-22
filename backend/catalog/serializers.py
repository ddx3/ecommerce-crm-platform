from rest_framework import serializers
from django.db.models import Q
from django.utils import timezone
from .models import Category, Product, ProductVariant, ProductImage, ProductCharacteristic, ProductComposition, Label, VariantLabel, Bundle, BundleImage, BundleItem, Review, AttributeValue


class CategorySerializer(serializers.ModelSerializer):
    children = serializers.SerializerMethodField()
    image_url = serializers.SerializerMethodField()

    class Meta:
        model = Category
        fields = ['id', 'name', 'slug', 'image_url', 'parent_id', 'children', 'sort_order']

    def get_children(self, obj):
        # flat — flat list/bundles: children are not needed at all.
        # Otherwise we recurse the whole tree down to the leaves (Fertilizers → Fertilizers →
        # Biofertilizers…).
        if self.context.get('flat'):
            return []
        children = obj.children.filter(is_active=True).order_by('sort_order', 'name')
        vis = self.context.get('visible_cat_ids')
        if vis is not None:
            children = children.filter(pk__in=vis)
        return CategorySerializer(children, many=True, context=self.context).data

    def get_image_url(self, obj):
        request = self.context.get('request')
        if obj.image and request:
            return request.build_absolute_uri(obj.image.url)
        return None


class LabelSerializer(serializers.ModelSerializer):
    class Meta:
        model = Label
        fields = ['id', 'name', 'color', 'slug']


class ProductImageSerializer(serializers.ModelSerializer):
    url = serializers.SerializerMethodField()
    display_url = serializers.SerializerMethodField()

    class Meta:
        model = ProductImage
        fields = ['id', 'url', 'display_url', 'is_main', 'sort_order', 'variant']

    def get_url(self, obj):
        request = self.context.get('request')
        if obj.image and request:
            return request.build_absolute_uri(obj.image.url)
        return None

    def get_display_url(self, obj):
        # Lightweight webp version (≤1000px) for showing in the gallery — faster LCP.
        # The original (`url`) stays for zoom in the lightbox.
        return _thumb_url(self.context.get('request'), obj.image, 'large')


def _thumb_url(request, image_field, alias='catalog'):
    """Absolute URL of the downscaled photo version (alias from THUMBNAIL_ALIASES, ~400px).
    The thumbnail is generated on demand and cached on disk; the original is untouched.
    On any generation error — fall back to the original so the card does not break."""
    if not image_field:
        return None
    try:
        from easy_thumbnails.files import get_thumbnailer
        url = get_thumbnailer(image_field)[alias].url
    except Exception:
        url = image_field.url
    return request.build_absolute_uri(url) if request else url


def _variant_images_qs(variant):
    """Photos available to this variant:
       - photos with color_value == the variant's color (new scheme),
       - legacy photos with variant=variant (old scheme, for non-dropshipping products),
       - if the variant has no color at all — return legacy variant-bound ones.
    Used through a late import in admin/views/email — DO NOT remove as «unused».
    """
    color_av = variant.attribute_values.filter(
        attribute__is_color=True
    ).order_by('pk').first()
    if color_av is None:
        return variant.images.all().order_by('sort_order', 'pk')
    return ProductImage.objects.filter(
        Q(product=variant.product_id, color_value=color_av)
        | Q(variant=variant)
    ).order_by('sort_order', 'pk').distinct()


def _variant_images_cached(variant):
    """The same as _variant_images_qs, but reads from the prefetch cache
    (attribute_values__attribute, images, product.images) — WITHOUT SQL. Intended for hot lists
    (admin orders) where these relations are already prefetched.
    Semantics and order are IDENTICAL to the original (verified A/B on all items):
      • color_av = the first av with attribute.is_color by ascending pk;
      • if color_av is None → variant.images sorted by (sort_order, pk);
      • otherwise union {product.images with color_value==color_av} ∪ {variant.images},
        deduped by pk, sorted by (sort_order, pk);
      • is_hidden is NOT filtered (as in the original).
    Returns a list (all call-sites only iterate / take the first — they don't chain)."""
    color_av = min(
        (av for av in variant.attribute_values.all()
         if av.attribute_id and av.attribute and av.attribute.is_color),
        key=lambda av: av.pk, default=None,
    )
    if color_av is None:
        return sorted(variant.images.all(), key=lambda i: (i.sort_order, i.pk))
    by_pk = {}
    if variant.product_id:
        for i in variant.product.images.all():
            if i.color_value_id == color_av.pk:
                by_pk[i.pk] = i
    for i in variant.images.all():
        by_pk[i.pk] = i
    return sorted(by_pk.values(), key=lambda i: (i.sort_order, i.pk))


class ProductCharacteristicSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductCharacteristic
        fields = ['name', 'value', 'color_hex', 'sort_order']


class ProductCompositionSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductComposition
        fields = ['name', 'value', 'sort_order']


class VariantLabelSerializer(serializers.ModelSerializer):
    label = LabelSerializer()
    is_active = serializers.SerializerMethodField()

    class Meta:
        model = VariantLabel
        fields = ['label', 'expires_at', 'is_active']

    def get_is_active(self, obj):
        return obj.expires_at is None or obj.expires_at > timezone.now()


class AttributeValueSerializer(serializers.ModelSerializer):
    attribute_id = serializers.IntegerField(source='attribute.id', read_only=True)
    attribute_name = serializers.CharField(source='attribute.name', read_only=True)
    is_color = serializers.BooleanField(source='attribute.is_color', read_only=True)

    class Meta:
        model = AttributeValue
        fields = ['id', 'attribute_id', 'attribute_name', 'value', 'color_hex', 'is_color']


def _supplier_dict(s) -> dict:
    """Supplier for the frontend/CRM. requires_cargo_branch narrows the choice of Nova Poshta branches
    in the parcel picker for this supplier (regular branches do not accept the oversized
    dimensions)."""
    return {
        'code': s.code,
        'name': s.name,
        'requires_cargo_branch': bool(s.requires_cargo_branch),
    }


def _variant_shipping_origin(v) -> str:
    """Where the variant ships from: our stock / Bradas dropshipping / 50-50 (hybrid)."""
    if getattr(v, 'is_hybrid', False):
        return 'hybrid'
    if getattr(v, 'is_dropshipping', False):
        return 'bradas'
    return 'own'


def _variant_card_dict(v, request):
    """Short description of the variant for the catalog card / bundle composition."""
    attrs = [{
        'id': av.id,
        'attribute_id': av.attribute_id,
        'attribute_name': av.attribute.name if av.attribute_id else '',
        'value': av.value,
        'color_hex': av.color_hex or '',
        'is_color': bool(av.color_hex),
    } for av in v.attribute_values.all()]
    # First visible photo of the variant (to substitute on the card when selected).
    vimg = next(
        iter(sorted(
            [i for i in v.images.all() if not i.is_hidden],
            key=lambda i: (i.sort_order, i.pk),
        )),
        None,
    )
    image_url = None
    if vimg and request:
        image_url = request.build_absolute_uri(vimg.image.url)
    elif vimg:
        image_url = vimg.image.url
    image_thumb = _thumb_url(request, vimg.image if vimg else None)
    # Active labels of exactly this variant (not a union over the product) — so the badges on the
    # card match the selected variant. Filter/sort in Python over the prefetch cache.
    now = timezone.now()
    vlabels = sorted(
        (vl.label for vl in v.variant_labels.all()
         if vl.label and (vl.expires_at is None or vl.expires_at > now)),
        key=lambda l: (Label.display_rank(l.slug), l.name or ''),
    )
    return {
        'id': v.id, 'name': v.name,
        'price': float(v.price) if v.price is not None else None,
        'stock': v.stock, 'sku': v.sku,
        'attribute_values': attrs,
        'image': image_url,
        'image_thumb': image_thumb,
        'shipping_origin': _variant_shipping_origin(v),
        'supplier': (_supplier_dict(v.supplier) if v.supplier_id else None),
        'old_price': float(v.old_price) if v.old_price is not None else None,
        'discount_percent': v.discount_percent,
        'discount_amount': float(v.discount_amount) if v.discount_amount is not None else None,
        'effective_discount': v.effective_discount_percent,
        'active_labels': [{'name': l.name, 'color': l.color, 'slug': l.slug} for l in vlabels],
    }


class ProductVariantSerializer(serializers.ModelSerializer):
    images = serializers.SerializerMethodField()
    active_labels = serializers.SerializerMethodField()
    characteristics = ProductCharacteristicSerializer(many=True, read_only=True)
    effective_discount = serializers.SerializerMethodField()
    in_stock = serializers.SerializerMethodField()
    attribute_values = AttributeValueSerializer(many=True, read_only=True)
    shipping_origin = serializers.SerializerMethodField()
    supplier = serializers.SerializerMethodField()

    class Meta:
        model = ProductVariant
        fields = [
            'id', 'name', 'sku', 'price', 'old_price',
            'discount_percent', 'discount_amount', 'effective_discount',
            'stock', 'in_stock', 'is_active',
            'images', 'active_labels', 'characteristics', 'attribute_values',
            'description', 'shipping_origin', 'supplier',
        ]

    def get_shipping_origin(self, obj):
        return _variant_shipping_origin(obj)

    def get_supplier(self, obj):
        return _supplier_dict(obj.supplier) if obj.supplier_id else None

    def get_images(self, obj):
        # is_hidden was not filtered — a hidden variant photo still made it into the API response
        # (data leak); aligned with ProductDetailSerializer.get_images below.
        images = [i for i in obj.images.all() if not i.is_hidden]
        return ProductImageSerializer(images, many=True, context=self.context).data

    def get_active_labels(self, obj):
        now = timezone.now()
        vls = obj.variant_labels.filter(
            label__isnull=False
        ).select_related('label').filter(
            Q(expires_at__isnull=True) | Q(expires_at__gt=now)
        )
        labels = sorted(
            (vl.label for vl in vls),
            key=lambda l: (Label.display_rank(l.slug), l.name or ''),
        )
        return [{'name': l.name, 'color': l.color, 'slug': l.slug} for l in labels]

    def get_effective_discount(self, obj):
        return obj.effective_discount_percent

    def get_in_stock(self, obj):
        if obj.stock > 0:
            return True
        # Hybrid (50/50) is always in stock: own stock ran out, the surplus will be sent via
        # dropshipping. Pure dropshipping is NOT always: its stock = the supplier's stock level
        # (synced from bradas), so 0 = nowhere to buy → "out of stock".
        if getattr(obj, 'is_hybrid', False):
            return True
        # Legacy: manual "Made to order" label on an own variant
        from .models import Label as _Label
        now = timezone.now()
        has_drop_label = obj.variant_labels.filter(
            label__slug=_Label.SLUG_PID_ZAMOVLENNYA,
        ).filter(
            Q(expires_at__isnull=True) | Q(expires_at__gt=now)
        ).exists()
        return has_drop_label


class ProductListSerializer(serializers.ModelSerializer):
    """For the product list — only basic information."""
    main_image = serializers.SerializerMethodField()
    main_image_thumb = serializers.SerializerMethodField()
    min_price = serializers.SerializerMethodField()
    old_price = serializers.SerializerMethodField()
    brand_name = serializers.SerializerMethodField()
    brand_slug = serializers.SerializerMethodField()
    has_discount = serializers.SerializerMethodField()
    labels = serializers.SerializerMethodField()
    variants = serializers.SerializerMethodField()
    paired = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = ['id', 'name', 'slug', 'brand_name', 'brand_slug', 'main_image', 'main_image_thumb', 'min_price', 'old_price', 'has_discount', 'labels', 'variants', 'packaging_kind', 'paired']

    def _pick_main_image(self, obj):
        # Priority among visible photos (is_hidden=False):
        # 1) Main (is_main=True) — of any type;
        # 2) the first photo of the earliest created variant (if the product has variant photos at
        #    all);
        # 3) otherwise — the first general photo (variant=NULL);
        # 4) fallback — any first visible photo.
        # We work with prefetched obj.images.all()/obj.variants.all() in Python — no N+1.
        # Same semantics: filter by is_hidden, sort ('sort_order','pk'), the first variant is the
        # smallest pk (equivalent to .order_by('pk').first()).
        visible = [i for i in obj.images.all() if not i.is_hidden]
        key = lambda i: (i.sort_order, i.pk)
        img = min((i for i in visible if i.is_main), key=key, default=None)
        if not img:
            if any(i.variant_id for i in visible):
                first_variant = min(obj.variants.all(), key=lambda v: v.pk, default=None)
                if first_variant:
                    img = min((i for i in visible if i.variant_id == first_variant.pk), key=key, default=None)
        if not img:
            img = min((i for i in visible if i.variant_id is None), key=key, default=None)
        if not img:
            img = min(visible, key=key, default=None)
        return img

    def get_main_image(self, obj):
        request = self.context.get('request')
        img = self._pick_main_image(obj)
        if img and request:
            return request.build_absolute_uri(img.image.url)
        return None

    def get_main_image_thumb(self, obj):
        # Downscaled version of the same main photo for the catalog card.
        img = self._pick_main_image(obj)
        return _thumb_url(self.context.get('request'), img.image if img else None)

    # ── helpers that work with prefetched obj.variants.all() (no extra SQL) ──

    def _active_variants(self, obj):
        # Cache in context so we don't sort 4 times for every product in the list
        cache = getattr(obj, '_active_variants_cache', None)
        if cache is None:
            cache = sorted(
                [v for v in obj.variants.all() if v.is_active],
                key=lambda v: (v.price if v.price is not None else 0),
            )
            obj._active_variants_cache = cache
        return cache

    def _has_pid_zamovlennya(self, variant):
        from .models import Label as _Label
        now = timezone.now()
        for vl in variant.variant_labels.all():
            if vl.label and vl.label.slug == _Label.SLUG_PID_ZAMOVLENNYA:
                if vl.expires_at is None or vl.expires_at > now:
                    return True
        return False

    def min_price_variant(self, obj):
        """The packaging whose price we show as the product price. Extracted separately so other
        consumers (chat/feed cards) can take the discount from the SAME packaging too, instead of
        making their own choice."""
        variants = self._active_variants(obj)
        if not variants:
            return None
        in_stock = [v for v in variants if (v.stock or 0) > 0]
        if in_stock:
            return in_stock[0]
        # A hybrid is always available (made to order) — its price is also «in stock». Pure
        # dropshipping with no supplier stock is NOT considered available (see get_in_stock).
        on_demand = [
            v for v in variants
            if getattr(v, 'is_hybrid', False)
            or self._has_pid_zamovlennya(v)
        ]
        return on_demand[0] if on_demand else variants[0]

    def get_min_price(self, obj):
        v = self.min_price_variant(obj)
        if v is None or v.price is None:
            return None
        return float(v.price)

    def get_old_price(self, obj):
        variants = self._active_variants(obj)
        if not variants:
            return None
        v = variants[0]
        return float(v.old_price) if v.old_price else None

    def get_brand_name(self, obj):
        return obj.brand.name if obj.brand else None

    def get_brand_slug(self, obj):
        return obj.brand.slug if obj.brand else None

    def get_has_discount(self, obj):
        for v in self._active_variants(obj):
            if v.discount_percent is not None or v.old_price is not None:
                return True
        return False

    def get_labels(self, obj):
        now = timezone.now()
        seen = {}
        for variant in self._active_variants(obj):
            for vl in variant.variant_labels.all():
                if vl.expires_at is not None and vl.expires_at <= now:
                    continue
                lbl = vl.label
                if not lbl:
                    continue
                seen.setdefault(lbl.slug or f'id{lbl.id}', lbl)
        labels = sorted(
            seen.values(),
            key=lambda l: (Label.display_rank(l.slug), l.name or ''),
        )
        return [{'name': l.name, 'color': l.color, 'slug': l.slug} for l in labels]

    def get_variants(self, obj):
        request = self.context.get('request')
        return [_variant_card_dict(v, request) for v in self._active_variants(obj)]

    def _has_in_stock_variant(self, product):
        """At least one active variant with stock>0 OR with the "Made to order" label."""
        for v in product.variants.all():
            if not v.is_active:
                continue
            if (v.stock or 0) > 0:
                return True
            if self._has_pid_zamovlennya(v):
                return True
        return False

    def get_paired(self, obj):
        # Avoid recursion: we don't show a nested paired.
        if self.context.get('is_nested_pair'):
            return None
        p = obj.paired_product
        if not p or not p.is_active:
            return None
        # We don't check the stock level: the packaging switcher must stay on the card even when the
        # opposite packaging is sold out (consistent with get_paired_product).
        ctx = {**self.context, 'is_nested_pair': True}
        return ProductListSerializer(p, context=ctx).data


class ProductDetailSerializer(serializers.ModelSerializer):
    """For the product page — full information."""
    variants = ProductVariantSerializer(many=True)
    images = serializers.SerializerMethodField()
    characteristics = serializers.SerializerMethodField()
    compositions = ProductCompositionSerializer(many=True, read_only=True)

    def get_images(self, obj):
        # All visible product photos (general + bound to variants) in a single order.
        # Photos with is_hidden=True are not returned — the admin marked them as not to be
        # displayed.
        # The main one (is_main) goes first (by significance, as in the card's main photo), then by
        # sort_order, pk. Consistent with _pick_main_image.
        qs = obj.images.filter(is_hidden=False).order_by('-is_main', 'sort_order', 'pk')
        return ProductImageSerializer(qs, many=True, context=self.context).data

    def get_characteristics(self, obj):
        # At the product level we show only general attributes (variant=NULL).
        # Variant attributes come inside each ProductVariantSerializer.
        qs = obj.characteristics.filter(variant__isnull=True).order_by('sort_order', 'pk')
        return ProductCharacteristicSerializer(qs, many=True).data
    categories = CategorySerializer(many=True, context={'flat': True})
    brand_name = serializers.SerializerMethodField()
    brand_slug = serializers.SerializerMethodField()
    paired_product = serializers.SerializerMethodField()
    reviews_count = serializers.SerializerMethodField()
    avg_rating = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = [
            'id', 'name', 'slug', 'description', 'application_norms',
            'brand_name', 'brand_slug', 'categories', 'images', 'variants', 'characteristics', 'compositions',
            'meta_title', 'meta_description',
            'packaging_kind', 'paired_product', 'reviews_count', 'avg_rating',
        ]

    def get_reviews_count(self, obj):
        # Matches the length of GET /product_reviews (approved top-level reviews) — lets the
        # frontend show the number in the tab without a separate heavy list query.
        return obj.reviews.filter(is_approved=True, parent__isnull=True).count()

    def get_avg_rating(self, obj):
        # Average rating over approved top-level reviews with a rating (rating=NULL is a
        # question/comment, not a rating). For JSON-LD aggregateRating. None if there are no
        # ratings.
        from django.db.models import Avg
        avg = obj.reviews.filter(
            is_approved=True, parent__isnull=True, rating__isnull=False
        ).aggregate(a=Avg('rating'))['a']
        return round(avg, 1) if avg is not None else None

    def get_brand_name(self, obj):
        return obj.brand.name if obj.brand else None

    def get_brand_slug(self, obj):
        return obj.brand.slug if obj.brand else None

    def get_paired_product(self, obj):
        # We show the "Packaging type" switcher while the paired product is active.
        # We don't check the stock level: a side that is out of stock leads to a product that opens
        # and shows "Out of stock" + a subscription — otherwise the switcher
        # would vanish on a pair where the opposite packaging is sold out.
        p = obj.paired_product
        if not p or not p.is_active:
            return None
        return {'id': p.id, 'slug': p.slug, 'name': p.name, 'packaging_kind': p.packaging_kind}


def _bundle_label_dicts(bundle):
    """Bundle labels for the storefront (like active_labels for products): stored labels +
    a guaranteed 'Sale' label based on the fact of a discount (aligned with get_old_price —
    the bundle's own discount or one from components). Dedup and ordering are as for products
    (display_rank). Normally the signal has already added 'Sale' to the M2M, so only the
    rare component discounts cause an extra query."""
    labels = list(bundle.labels.all())
    if not any(l.slug == Label.SLUG_AKTSIA for l in labels):
        try:
            has_discount = bundle.get_old_price() is not None
        except Exception:
            has_discount = False
        if has_discount:
            ak = Label.objects.filter(slug=Label.SLUG_AKTSIA).first()
            if ak:
                labels.append(ak)
    labels = sorted(labels, key=lambda l: (Label.display_rank(l.slug), l.name or ''))
    return [{'name': l.name, 'color': l.color, 'slug': l.slug} for l in labels]


class BundleImageSerializer(serializers.ModelSerializer):
    url = serializers.SerializerMethodField()

    class Meta:
        model = BundleImage
        fields = ['id', 'url', 'is_main']

    def get_url(self, obj):
        request = self.context.get('request')
        if obj.image and request:
            return request.build_absolute_uri(obj.image.url)
        return None


class BundleListSerializer(serializers.ModelSerializer):
    main_image = serializers.SerializerMethodField()
    main_image_thumb = serializers.SerializerMethodField()
    price = serializers.SerializerMethodField()
    old_price = serializers.SerializerMethodField()
    stock = serializers.SerializerMethodField()
    active_labels = serializers.SerializerMethodField()

    class Meta:
        model = Bundle
        fields = ['id', 'name', 'slug', 'sku', 'main_image', 'main_image_thumb',
                  'price', 'old_price', 'stock', 'active_labels', 'is_active']

    def get_active_labels(self, obj):
        return _bundle_label_dicts(obj)

    def _pick_main_image(self, obj):
        """Main photo file of the bundle (FieldFile) or None. Cached on obj,
        so get_main_image and get_main_image_thumb don't duplicate the query."""
        cache = getattr(obj, '_main_image_file_cache', False)
        if cache is not False:
            return cache
        img = obj.images.filter(is_hidden=False).order_by('-is_main', 'sort_order', 'pk').first()
        f = img.image if img else None
        obj._main_image_file_cache = f
        return f

    def get_main_image(self, obj):
        request = self.context.get('request')
        f = self._pick_main_image(obj)
        if f and request:
            return request.build_absolute_uri(f.url)
        # No photo of its own — return None, the frontend shows a placeholder (icon).
        # Do NOT substitute the photo of a product from the bundle's contents.
        return None

    def get_main_image_thumb(self, obj):
        return _thumb_url(self.context.get('request'), self._pick_main_image(obj))

    def get_price(self, obj):
        p = obj.get_price()
        return float(p) if p else None

    def get_old_price(self, obj):
        old = obj.get_old_price()
        return float(old) if old is not None else None

    def get_stock(self, obj):
        return obj.get_stock()


class BundleItemSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source='product_variant.product.name')
    product_slug = serializers.CharField(source='product_variant.product.slug')
    variant_id = serializers.IntegerField(source='product_variant_id')
    variant_name = serializers.CharField(source='product_variant.name')
    image = serializers.SerializerMethodField()
    product = serializers.SerializerMethodField()

    class Meta:
        model = BundleItem
        fields = ['product_name', 'product_slug', 'variant_id', 'variant_name', 'quantity', 'image', 'product']

    def get_product(self, obj):
        product = obj.product_variant.product
        data = ProductListSerializer(product, context=self.context).data
        # A hidden product (is_active=False) is sold only as part of a bundle.
        # ProductListSerializer returns only active variants, so such a product has
        # an empty list — we substitute exactly the variant that is part of the bundle,
        # and the is_active flag (the frontend makes the card non-clickable, no standalone
        # purchase).
        data['is_active'] = product.is_active
        pv = obj.product_variant
        if not any(v['id'] == pv.id for v in data['variants']):
            data['variants'].append(_variant_card_dict(pv, self.context.get('request')))
            if data.get('min_price') is None and pv.price is not None:
                data['min_price'] = float(pv.price)
        return data

    def get_image(self, obj):
        request = self.context.get('request')
        # First look for the variant's photo, otherwise the product's first photo in the unified
        # order.
        img = (
            obj.product_variant.images.order_by('sort_order', 'pk').first()
            or obj.product_variant.product.images.order_by('sort_order', 'pk').first()
        )
        if img and request:
            return request.build_absolute_uri(img.image.url)
        return None


class BundleDetailSerializer(serializers.ModelSerializer):
    items = BundleItemSerializer(many=True)
    images = serializers.SerializerMethodField()
    price = serializers.SerializerMethodField()
    old_price = serializers.SerializerMethodField()
    stock = serializers.SerializerMethodField()
    active_labels = serializers.SerializerMethodField()
    reviews_count = serializers.SerializerMethodField()
    avg_rating = serializers.SerializerMethodField()

    class Meta:
        model = Bundle
        fields = ['id', 'name', 'slug', 'sku', 'price', 'old_price', 'stock',
                  'description', 'application_norms', 'is_active', 'items', 'images',
                  'active_labels',
                  'meta_title', 'meta_description', 'reviews_count', 'avg_rating']

    def get_active_labels(self, obj):
        return _bundle_label_dicts(obj)

    def get_images(self, obj):
        # The main photo (is_main) goes first — by significance — then sort_order, pk.
        qs = obj.images.filter(is_hidden=False).order_by('-is_main', 'sort_order', 'pk')
        return BundleImageSerializer(qs, many=True, context=self.context).data

    def get_price(self, obj):
        p = obj.get_price()
        return float(p) if p else None

    def get_old_price(self, obj):
        old = obj.get_old_price()
        return float(old) if old is not None else None

    def get_stock(self, obj):
        return obj.get_stock()

    def get_reviews_count(self, obj):
        return obj.reviews.filter(is_approved=True, parent__isnull=True).count()

    def get_avg_rating(self, obj):
        from django.db.models import Avg
        avg = obj.reviews.filter(
            is_approved=True, parent__isnull=True, rating__isnull=False
        ).aggregate(a=Avg('rating'))['a']
        return round(avg, 1) if avg is not None else None


class ReviewReplySerializer(serializers.ModelSerializer):
    is_admin = serializers.SerializerMethodField()

    class Meta:
        model = Review
        fields = ['id', 'author_name', 'text', 'created_at', 'parent_id', 'is_admin']

    def get_is_admin(self, obj):
        # A reply 'from the shop': either from a staff user (a manager via the site),
        # or an imported reply with no user attached (customer replies
        # always have a user, since they require authorization).
        u = obj.user
        if u:
            return bool(u.is_staff or u.is_superuser)
        return obj.parent_id is not None


class ReviewSerializer(serializers.ModelSerializer):
    replies = serializers.SerializerMethodField()
    variant_name = serializers.SerializerMethodField()

    class Meta:
        model = Review
        fields = ['id', 'author_name', 'rating', 'text', 'created_at', 'replies', 'variant_name']

    def get_variant_name(self, obj):
        return obj.variant.name if obj.variant_id else None

    def get_replies(self, obj):
        # Collect all replies in the tree recursively
        def collect(review):
            result = []
            for r in review.replies.filter(is_approved=True).order_by('created_at'):
                result.append(r)
                result.extend(collect(r))
            return result
        return ReviewReplySerializer(collect(obj), many=True).data


class ReviewCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Review
        # author_name/email are now taken from request.user in the view; the frontend doesn't send
        # them.
        # rating is optional: None = comment/question, a number = rated review.
        fields = ['rating', 'text']

    def validate_rating(self, value):
        # 0 is treated as 'no rating' (question/comment).
        if value == 0:
            return None
        return value

    def validate_text(self, value):
        v = (value or '').strip()
        if len(v) < 3:
            raise serializers.ValidationError('The text is too short')
        if len(v) > 5000:
            raise serializers.ValidationError('The text is too long')
        return v


