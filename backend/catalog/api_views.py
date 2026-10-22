import hmac
import os
from datetime import timedelta

from django.db.models import Q, Min, Max, Exists, OuterRef, FloatField, Case, When, IntegerField, Prefetch
from django.db.models.functions import Greatest
from django.contrib.postgres.search import TrigramWordSimilarity
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework.decorators import api_view, permission_classes, throttle_classes, authentication_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.pagination import PageNumberPagination
from rest_framework.throttling import ScopedRateThrottle, UserRateThrottle, SimpleRateThrottle

from .models import Category, Product, ProductVariant, Bundle, Review, StockNotification, Brand, AttributeValue, Label, Favorite
from .serializers import (
    CategorySerializer,
    ProductListSerializer,
    ProductDetailSerializer,
    BundleListSerializer,
    BundleDetailSerializer,
    ReviewSerializer,
    ReviewCreateSerializer,
)
from .flora_feed import unit_payload




class ProductPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100


class ReviewCreateThrottle(UserRateThrottle):
    """Throttles only POST; GET requests are not affected."""
    scope = 'review_create'

    def allow_request(self, request, view):
        if request.method != 'POST':
            return True
        return super().allow_request(request, view)


def _require_auth_post(request):
    """A POST requires authentication. Returns None on GET."""
    if request.method == 'POST' and not request.user.is_authenticated:
        return Response({'detail': 'Authentication required'}, status=401)
    return None


def _review_author(request):
    """Review author's name/email: for a logged-in user — from User, for a guest — from the request
    body
    (guest_name/guest_email, the same format as the guest chat).
    Returns (name, email, error_response|None)."""
    if request.user.is_authenticated:
        name, email = _author_from_user(request.user)
        return name, email, None
    name = (request.data.get('guest_name') or '').strip()[:100]
    email = (request.data.get('guest_email') or '').strip()[:254]
    if not name:
        return None, None, Response({'guest_name': 'Enter your name'}, status=400)
    return name, email, None


def _author_from_user(user):
    """Take name/email from User, ignoring arbitrary fields sent by the frontend."""
    parts = [getattr(user, 'first_name', '') or '', getattr(user, 'last_name', '') or '']
    full = ' '.join(p for p in parts if p).strip()
    if not full:
        full = (user.email or '').split('@', 1)[0] or 'User'
    return full[:100], (user.email or '')


def _visible_category_ids():
    """IDs of categories whose subtree contains at least one active product or bundle.
    Includes all ancestors (so the path in the tree stays visible). Categories with no
    visible content are not shown on the frontend."""
    direct = set()
    direct |= set(Product.objects.filter(is_active=True, variants__is_active=True)
                  .values_list('categories', flat=True))
    direct |= set(Bundle.objects.filter(is_active=True)
                  .values_list('categories', flat=True))
    direct.discard(None)
    parent_of = dict(Category.objects.values_list('id', 'parent_id'))
    visible = set()
    for cid in direct:
        c = cid
        while c is not None and c not in visible:
            visible.add(c)
            c = parent_of.get(c)
    return visible


@api_view(['GET'])
def categories_list(request):
    """Category tree (active + with visible content, top level + children)."""
    vis = _visible_category_ids()
    roots = Category.objects.filter(
        is_active=True, parent__isnull=True, pk__in=vis
    ).order_by('sort_order', 'name')
    data = CategorySerializer(roots, many=True, context={'request': request, 'visible_cat_ids': vis}).data
    return Response(data)


@api_view(['GET'])
def categories_flat(request):
    """Flat list of active categories with visible content."""
    vis = _visible_category_ids()
    cats = Category.objects.filter(is_active=True, pk__in=vis).order_by('sort_order', 'name')
    data = CategorySerializer(cats, many=True, context={'request': request, 'flat': True, 'visible_cat_ids': vis}).data
    return Response(data)


@api_view(['GET'])
def products_list(request):
    """
    List of products with filters.
    ?category=slug  — filter by category (including subcategories)
    ?search=...     — search by name
    ?ordering=price|-price|new  — sorting
    ?label=novynka|aktsia|khit  — filter by label
    ?in_stock=1     — in-stock only
    """
    # Only products with at least one active variant — without one the card has neither a price nor
    # an SKU.
    qs = Product.objects.filter(is_active=True, variants__is_active=True).select_related(
        'paired_product', 'paired_product__brand'
    ).prefetch_related(
        'images', 'variants__images', 'variants__variant_labels__label', 'brand',
        'variants__attribute_values__attribute',
        'paired_product__images',
        'paired_product__variants__images',
        'paired_product__variants__variant_labels__label',
        'paired_product__variants__attribute_values__attribute',
    )

    # Category (including the whole subtree of descendants)
    category_slug = request.query_params.get('category')
    if category_slug:
        try:
            cat = Category.objects.get(slug=category_slug)
            cat_ids = cat.get_subtree_ids_self()
            qs = qs.filter(categories__in=cat_ids)
        except Category.DoesNotExist:
            pass

    # Search
    search = request.query_params.get('search')
    if search:
        qs = qs.annotate(
            sim=Greatest(
                TrigramWordSimilarity(search, 'name'),
                TrigramWordSimilarity(search, 'brand__name'),
                output_field=FloatField(),
            ),
            desc_sim=TrigramWordSimilarity(search, 'description'),
        ).filter(
            Q(name__icontains=search) |
            Q(variants__name__icontains=search) |
            Q(variants__sku__icontains=search) |
            Q(brand__name__icontains=search) |
            Q(description__icontains=search) |
            Q(sim__gte=0.35) |
            Q(desc_sim__gte=0.7)
        )

    # Brand (comma-separated — several, OR)
    brand_slug = request.query_params.get('brand')
    if brand_slug:
        brand_slugs = [s.strip() for s in brand_slug.split(',') if s.strip()]
        qs = qs.filter(brand__slug__in=brand_slugs)

    # Label (comma-separated — several, OR)
    label_slug = request.query_params.get('label')
    if label_slug:
        now = timezone.now()
        label_slugs = [s.strip() for s in label_slug.split(',') if s.strip()]
        # All conditions go in ONE filter() so that the variant's activity, slug and validity period
        # apply to the SAME variant_labels row/variant. Otherwise Django makes separate JOINs and a
        # product wrongly passes: e.g. the "new arrival" label sits on an INACTIVE
        # variant,
        # while activity is matched on another variant without the label (the card then shows the
        # product without the new-arrival label — get_labels takes only active ones).
        qs = qs.filter(
            Q(variants__is_active=True)
            & Q(variants__variant_labels__label__slug__in=label_slugs)
            & (
                Q(variants__variant_labels__expires_at__isnull=True)
                | Q(variants__variant_labels__expires_at__gt=now)
            )
        )

    # exclude_label — drop products that have an active label with a higher priority.
    # This keeps the homepage rows from duplicating: a new-arrival+hit product goes only into
    # "New arrivals",
    # while "Hits" exclude novynka/aktsia and pick up everything else.
    exclude_label = request.query_params.get('exclude_label')
    if exclude_label:
        ex_now = timezone.now()
        ex_slugs = [s.strip() for s in exclude_label.split(',') if s.strip()]
        if ex_slugs:
            qs = qs.exclude(
                Q(variants__is_active=True)
                & Q(variants__variant_labels__label__slug__in=ex_slugs)
                & (
                    Q(variants__variant_labels__expires_at__isnull=True)
                    | Q(variants__variant_labels__expires_at__gt=ex_now)
                )
            )

    # In-stock only. A hybrid (50/50) with an empty own stock also counts as "in
    # stock"
    # (it will be shipped via dropshipping) — consistent with get_in_stock/_available, otherwise it
    # would be shown as
    # available on the card but would drop out of this filter.
    if request.query_params.get('in_stock') == '1':
        qs = qs.filter(
            Q(variants__stock__gt=0) | Q(variants__is_hybrid=True),
            variants__is_active=True,
        )

    # Price range (by variants): both bounds and the active flag apply to ONE variant. Two separate
    # .filter() calls on variants
    # gave false matches across DIFFERENT variants (the minimum from one, the maximum from another)
    # and extra JOINs (4–8 s per request).
    price_cond = {}
    for param, lookup in (('price_min', 'price__gte'), ('price_max', 'price__lte')):
        raw = request.query_params.get(param)
        if raw:
            try:
                price_cond[lookup] = float(raw)
            except ValueError:
                pass
    if price_cond:
        qs = qs.filter(Exists(ProductVariant.objects.filter(product=OuterRef('pk'), is_active=True, **price_cond)))

    # Filter by attributes: ?attrs=12,34,56 — AttributeValue ids.
    # Values of one attribute are OR, values of different attributes are AND.
    attrs_param = request.query_params.get('attrs')
    if attrs_param:
        try:
            value_ids = [int(x) for x in attrs_param.split(',') if x.strip()]
        except ValueError:
            value_ids = []
        if value_ids:
            groups = {}
            for av in AttributeValue.objects.filter(id__in=value_ids).values('id', 'attribute_id'):
                groups.setdefault(av['attribute_id'], []).append(av['id'])
            for ids in groups.values():
                qs = qs.filter(variants__attribute_values__id__in=ids)

    # Exclude a specific product (e.g. the current one on the detail page)
    exclude_slug = request.query_params.get('exclude')
    if exclude_slug:
        qs = qs.exclude(slug=exclude_slug)

    # Filter by a list of slugs — for validating recentlyViewed from localStorage:
    # return only products that actually exist and are active (with active variants).
    slugs_param = request.query_params.get('slugs')
    if slugs_param:
        slugs = [s.strip() for s in slugs_param.split(',') if s.strip()]
        if not slugs:
            return Response({'count': 0, 'next': None, 'previous': None, 'results': [], 'variants_count': 0})
        qs = qs.filter(slug__in=slugs)

    qs = qs.distinct()

    # Annotation: at least one packaging is in stock. A hybrid with stock=0 counts as available
    # (like _available in ?as=variant mode and get_in_stock) — otherwise products with only
    # hybrid packagings would sink to the end of the '-has_stock' sort as out-of-stock.
    in_stock_sub = ProductVariant.objects.filter(
        Q(stock__gt=0) | Q(is_hybrid=True), product=OuterRef('pk'), is_active=True,
    )
    qs = qs.annotate(has_stock=Exists(in_stock_sub))

    # Sorting: in-stock first, then by the chosen criterion
    ordering = request.query_params.get('ordering', 'new')
    if search:
        qs = qs.order_by('-has_stock', '-sim')
    elif ordering == 'price':
        qs = qs.annotate(min_price=Min('variants__price')).order_by('-has_stock', 'min_price')
    elif ordering == '-price':
        qs = qs.annotate(min_price=Min('variants__price')).order_by('-has_stock', '-min_price')
    else:
        qs = qs.order_by('-has_stock', '-created_at')

    # Number of active variants across all products that passed the filters
    # (for the frontend counter when viewMode='variant').
    variants_count = ProductVariant.objects.filter(
        product__in=qs, is_active=True,
    ).count()

    # Search log for the "Analyst" — what people search for and do not find. Only the first
    # page
    # of results, so that paging does not multiply a single query.
    if search and request.query_params.get('page', '1') == '1':
        from analyst.attribution import log_search
        log_search(search, variants_count)

    # ?as=variant — pagination over INDIVIDUAL variants ("by packaging" mode).
    # Global sort: ALL in-stock variants first, then ALL out-of-stock ones — so that variants
    # with no stock always go to the end of the entire list, not to the end of the current page.
    if request.query_params.get('as') == 'variant':
        filtered_pids = list(qs.order_by().values_list('pk', flat=True))
        # A hybrid is treated as "in stock" for sorting — otherwise all 50/50 items
        # with an
        # empty own stock would sink to the end of the list as out-of-stock.
        # A pure dropshipping item is judged by the supplier's stock (stock>0), like regular stock.
        _available = Q(stock__gt=0) | Q(is_hybrid=True)
        prod_in_stock = ProductVariant.objects.filter(
            _available, product_id=OuterRef('product_id'), is_active=True,
        )
        vqs = ProductVariant.objects.filter(
            is_active=True, product_id__in=filtered_pids,
        ).annotate(
            v_in_stock=Case(When(_available, then=1), default=0, output_field=IntegerField()),
            prod_has_stock=Exists(prod_in_stock),
        )
        # With a label filter show ONLY packagings with that label — otherwise the product got into
        # the
        # selection through one new-arrival packaging, and in "by packaging" mode all
        # of its
        # packagings would be shown (including those without the new-arrival label).
        if label_slug:
            vqs = vqs.filter(
                Q(variant_labels__label__slug__in=label_slugs)
                & (
                    Q(variant_labels__expires_at__isnull=True)
                    | Q(variant_labels__expires_at__gt=now)
                )
            ).distinct()
        if search:
            vqs = vqs.annotate(vsim=Greatest(
                TrigramWordSimilarity(search, 'product__name'),
                TrigramWordSimilarity(search, 'product__brand__name'),
                output_field=FloatField(),
            )).order_by('-v_in_stock', '-vsim', 'product_id', 'id')
        elif ordering == 'price':
            vqs = vqs.order_by('-v_in_stock', 'price', 'id')
        elif ordering == '-price':
            vqs = vqs.order_by('-v_in_stock', '-price', 'id')
        else:
            vqs = vqs.order_by('-v_in_stock', '-prod_has_stock', '-product__created_at', 'product_id', 'id')

        # With a label filter vqs is narrowed to packagings with the label — and so is the counter.
        v_count = vqs.count() if label_slug else variants_count

        if request.query_params.get('all') == '1':
            page_variants = list(vqs)
        else:
            paginator = ProductPagination()
            page_variants = paginator.paginate_queryset(vqs, request)

        page_pids = list({v.product_id for v in page_variants})
        products_page = qs.filter(pk__in=page_pids)
        prod_data = ProductListSerializer(products_page, many=True, context={'request': request}).data
        variant_order = [{'product_id': v.product_id, 'variant_id': v.id} for v in page_variants]
        return Response({
            'count': v_count,
            'variants_count': v_count,
            'next': None,
            'previous': None,
            'results': prod_data,
            'variant_order': variant_order,
        })

    # ?all=1 — no pagination, return a single list (for the "Show all" button).
    if request.query_params.get('all') == '1':
        serializer = ProductListSerializer(qs, many=True, context={'request': request})
        return Response({
            'count': qs.count(),
            'variants_count': variants_count,
            'next': None,
            'previous': None,
            'results': serializer.data,
        })

    paginator = ProductPagination()
    page = paginator.paginate_queryset(qs, request)
    serializer = ProductListSerializer(page, many=True, context={'request': request})
    response = paginator.get_paginated_response(serializer.data)
    response.data['variants_count'] = variants_count
    return response


@api_view(['GET'])
@permission_classes([AllowAny])
def product_facets(request):
    """Available catalog filter values within a category:
    brands, labels, attributes with values, and the price range.
    ?category=slug — limit the count to a category (with its subtree)."""
    base = Product.objects.filter(is_active=True, variants__is_active=True)
    category_slug = request.query_params.get('category')
    if category_slug:
        try:
            cat = Category.objects.get(slug=category_slug)
            base = base.filter(categories__in=cat.get_subtree_ids_self())
        except Category.DoesNotExist:
            pass
    base = base.distinct()

    brands = list(
        Brand.objects.filter(is_active=True, products__in=base)
        .distinct().order_by('name').values('slug', 'name')
    )

    now = timezone.now()
    labels = list(
        Label.objects.filter(
            variant_labels__variant__product__in=base,
            variant_labels__variant__is_active=True,
        ).filter(
            Q(variant_labels__expires_at__isnull=True) |
            Q(variant_labels__expires_at__gt=now)
        ).distinct().order_by('name').values('slug', 'name', 'color')
    )

    attrs = {}
    av_qs = AttributeValue.objects.filter(
        variants__product__in=base, variants__is_active=True,
    ).select_related('attribute').distinct().order_by('attribute__name', 'value')
    for av in av_qs:
        a = attrs.setdefault(av.attribute_id, {
            'id': av.attribute_id,
            'name': av.attribute.name,
            'is_color': av.attribute.is_color,
            'values': [],
        })
        a['values'].append({'id': av.id, 'value': av.value, 'color_hex': av.color_hex})

    price = ProductVariant.objects.filter(
        product__in=base, is_active=True, price__isnull=False,
    ).aggregate(min=Min('price'), max=Max('price'))

    return Response({
        'brands': brands,
        'labels': labels,
        'attributes': list(attrs.values()),
        'price': {
            'min': float(price['min']) if price['min'] is not None else None,
            'max': float(price['max']) if price['max'] is not None else None,
        },
    })


@api_view(['GET'])
def product_detail(request, slug):
    """Product detail page."""
    try:
        product = Product.objects.filter(is_active=True).prefetch_related(
            'images',
            'variants__images',
            'variants__characteristics',
            'variants__variant_labels__label',
            'variants__attribute_values__attribute',
            'characteristics',
            'categories',
            'brand',
        ).get(slug=slug)
    except Product.DoesNotExist:
        return Response({'detail': 'Not found'}, status=404)

    serializer = ProductDetailSerializer(product, context={'request': request})
    return Response(serializer.data)


@api_view(['GET'])
def bought_together(request, slug):
    """Products that appear in the same bundles as the current one.

    We walk Product → variants → BundleItem → Bundle → items → ProductVariant → Product.
    Take distinct, exclude the current one.
    """
    try:
        limit = max(1, min(int(request.query_params.get('limit', 4)), 20))
    except ValueError:
        limit = 4

    qs = Product.objects.filter(
        is_active=True,
        variants__bundleitem__bundle__items__product_variant__product__slug=slug,
    ).exclude(slug=slug).prefetch_related(
        'images', 'variants__images', 'variants__variant_labels__label', 'brand',
    ).distinct().order_by('-created_at')[:limit]

    data = ProductListSerializer(qs, many=True, context={'request': request}).data
    return Response({'results': data})


@api_view(['GET'])
def bundles_list(request):
    """List of bundles."""
    qs = Bundle.objects.filter(is_active=True).prefetch_related('images')

    category_slug = request.query_params.get('category')
    if category_slug:
        # The entire category subtree, at any depth
        try:
            cat = Category.objects.get(slug=category_slug)
            qs = qs.filter(categories__in=cat.get_subtree_ids_self()).distinct()
        except Category.DoesNotExist:
            qs = qs.none()

    product_slug = request.query_params.get('product_slug')
    if product_slug:
        qs = qs.filter(items__product_variant__product__slug=product_slug).distinct()

    search = request.query_params.get('search')
    if search:
        qs = qs.annotate(
            sim=TrigramWordSimilarity(search, 'name'),
            desc_sim=TrigramWordSimilarity(search, 'description'),
        ).filter(
            Q(name__icontains=search) |
            Q(sku__icontains=search) |
            Q(description__icontains=search) |
            # Composition: "megafol" also finds the bundles that contain it. Previously
            # this worked through the
            # long bundle description, which is overridden after the variants were merged.
            Q(items__product_variant__product__name__icontains=search) |
            Q(items__product_variant__name__icontains=search) |
            Q(sim__gte=0.35) |
            Q(desc_sim__gte=0.7)
        ).order_by('-sim').distinct()

    qs = qs.order_by('created_at')

    # Sort: in stock first
    bundles = list(qs.prefetch_related(
        'items__product_variant__images',
        'items__product_variant__product__images',
        # Needed for the cache-hit path of get_price (effective_price→brand) and get_stock
        # (bulk_source),
        # otherwise on the prefetched path they would be loaded lazily (N+1).
        'items__product_variant__product__brand',
        'items__product_variant__bulk_source',
    ))

    bundles.sort(key=lambda b: 0 if b.get_stock() > 0 else 1)

    paginator = ProductPagination()
    page = paginator.paginate_queryset(bundles, request)
    serializer = BundleListSerializer(page, many=True, context={'request': request})
    return paginator.get_paginated_response(serializer.data)


@api_view(['GET'])
def search(request):
    q = request.query_params.get('q', '').strip()
    if not q or len(q) < 2:
        return Response({'variants': [], 'bundles': []})

    # Optional narrowing of the search to a category (with its whole subtree of descendants).
    cat_ids = None
    cat_slug = request.query_params.get('category')
    if cat_slug:
        try:
            cat_ids = Category.objects.get(slug=cat_slug).get_subtree_ids_self()
        except Category.DoesNotExist:
            cat_ids = None

    # Packagings: search by product name, description, brand, packaging name, SKU
    variants = ProductVariant.objects.filter(
        is_active=True,
        product__is_active=True,
    ).annotate(
        sim=Greatest(
            TrigramWordSimilarity(q, 'product__name'),
            TrigramWordSimilarity(q, 'name'),
            TrigramWordSimilarity(q, 'product__brand__name'),
            output_field=FloatField(),
        )
    ).filter(
        Q(product__name__icontains=q) |
        Q(product__brand__name__icontains=q) |
        Q(name__icontains=q) |
        Q(sku__icontains=q) |
        Q(sim__gte=0.35)
    )
    if cat_ids is not None:
        variants = variants.filter(product__categories__in=cat_ids).distinct()
    variants = variants.order_by('-sim').select_related('product', 'product__brand').prefetch_related('product__images', 'images')[:12]

    bundles = Bundle.objects.filter(
        is_active=True
    ).annotate(
        sim=TrigramWordSimilarity(q, 'name'),
        desc_sim=TrigramWordSimilarity(q, 'description'),
    ).filter(
        Q(name__icontains=q) |
        Q(description__icontains=q) |
        Q(sku__icontains=q) |
        Q(items__product_variant__product__name__icontains=q) |
        Q(items__product_variant__name__icontains=q) |
        Q(sim__gte=0.35) |
        Q(desc_sim__gte=0.7)
    )
    if cat_ids is not None:
        bundles = bundles.filter(categories__in=cat_ids)
    bundles = bundles.order_by('-sim').distinct().prefetch_related('images')[:4]

    def product_image(p):
        img = next(iter(sorted(p.images.all(), key=lambda i: (i.sort_order, i.pk))), None)
        return request.build_absolute_uri(img.image.url) if img else None

    def variant_image(v):
        # The packaging's own photo first, otherwise the product photo.
        img = next(iter(sorted(v.images.all(), key=lambda i: (i.sort_order, i.pk))), None)
        if img:
            return request.build_absolute_uri(img.image.url)
        return product_image(v.product)

    def bundle_image(b):
        img = next(iter(sorted(b.images.all(), key=lambda i: (i.sort_order, i.pk))), None)
        return request.build_absolute_uri(img.image.url) if img else None

    return Response({
        'variants': [
            {
                'product_slug': v.product.slug,
                'product_name': v.product.name,
                'variant_name': v.name,
                'sku': v.sku,
                'price': float(v.price) if v.price else None,
                'stock': v.stock,
                'in_stock': v.stock > 0 or v.is_hybrid,
                'image': variant_image(v),
            }
            for v in variants
        ],
        'bundles': [
            {
                'sku': b.sku,
                'slug': b.slug,
                'name': b.name,
                'image': bundle_image(b),
                'price': (float(b.get_price()) if b.get_price() else None),
                'stock': b.get_stock(),
                'in_stock': b.get_stock() > 0,
            }
            for b in bundles
        ],
    })


@api_view(['GET'])
def bundle_detail(request, slug):
    try:
        bundle = Bundle.objects.filter(is_active=True).prefetch_related(
            'images',
            'labels',
            'items__product_variant__images',
            'items__product_variant__product__images',
        ).get(slug=slug)
    except Bundle.DoesNotExist:
        return Response({'detail': 'Not found'}, status=404)

    serializer = BundleDetailSerializer(bundle, context={'request': request})
    return Response(serializer.data)


@api_view(['GET', 'POST'])
@permission_classes([AllowAny])
@throttle_classes([ReviewCreateThrottle])
def site_reviews(request):
    """General reviews about the shop (not tied to a product/bundle)."""
    if request.method == 'GET':
        reviews = Review.objects.filter(product__isnull=True, bundle__isnull=True, is_approved=True, parent__isnull=True).order_by('created_at')
        return Response(ReviewSerializer(reviews, many=True).data)

    name, email, err = _review_author(request)
    if err: return err

    serializer = ReviewCreateSerializer(data=request.data)
    if serializer.is_valid():
        auto_approve = request.user.is_staff or request.user.is_superuser
        serializer.save(user=request.user if request.user.is_authenticated else None,
                        author_name=name, author_email=email, is_approved=auto_approve)
        msg = 'Review published' if auto_approve else 'Review sent for moderation'
        return Response({'detail': msg}, status=201)
    return Response(serializer.errors, status=400)


@api_view(['POST'])
@throttle_classes([ReviewCreateThrottle])
def reply_to_review(request, review_id):
    err = _require_auth_post(request)
    if err: return err

    try:
        root = Review.objects.get(pk=review_id, is_approved=True, parent__isnull=True)
    except Review.DoesNotExist:
        return Response({'detail': 'Not found'}, status=404)

    # reply_id — a reply to a specific answer (for nesting)
    reply_id = request.data.get('reply_id')
    if reply_id:
        try:
            parent = Review.objects.get(pk=reply_id, is_approved=True)
        except Review.DoesNotExist:
            parent = root
    else:
        parent = root

    serializer = ReviewCreateSerializer(data=request.data)
    if serializer.is_valid():
        auto_approve = request.user.is_staff or request.user.is_superuser
        name, email = _author_from_user(request.user)
        reply = serializer.save(
            user=request.user, author_name=name, author_email=email,
            parent=parent,
            product=root.product,
            bundle=root.bundle,
            is_approved=auto_approve,
        )
        # If the reply is from a customer (not staff), return the root review to "new",
        # so the manager sees an active notification about a new reply in the same
        # row. Section: 'reviews' for rated ones, 'questions' for comments.
        if not (request.user.is_staff or request.user.is_superuser):
            from .models import NotificationState
            section = 'reviews' if root.rating is not None else 'questions'
            NotificationState.objects.update_or_create(
                section=section, object_id=root.id, defaults={'step': 0},
            )
            from .notifications import broadcast_notification_counts
            broadcast_notification_counts()
        msg = 'Reply published' if auto_approve else 'Reply sent for moderation'
        return Response({'detail': msg, 'id': reply.id, 'parent_id': reply.parent_id}, status=201)
    return Response(serializer.errors, status=400)


@api_view(['GET', 'POST'])
@permission_classes([AllowAny])
@throttle_classes([ReviewCreateThrottle])
def product_reviews(request, slug):
    try:
        product = Product.objects.get(slug=slug, is_active=True)
    except Product.DoesNotExist:
        return Response({'detail': 'Not found'}, status=404)

    if request.method == 'GET':
        reviews = product.reviews.filter(is_approved=True, parent__isnull=True).order_by('created_at')
        return Response(ReviewSerializer(reviews, many=True).data)

    name, email, err = _review_author(request)
    if err: return err

    serializer = ReviewCreateSerializer(data=request.data)
    if serializer.is_valid():
        auto_approve = request.user.is_staff or request.user.is_superuser
        variant_id = request.data.get('variant_id') or None
        serializer.save(user=request.user if request.user.is_authenticated else None,
                        author_name=name, author_email=email,
                        product=product, is_approved=auto_approve, variant_id=variant_id)
        msg = 'Review published' if auto_approve else 'Review sent for moderation'
        return Response({'detail': msg}, status=201)
    return Response(serializer.errors, status=400)


@api_view(['GET', 'POST'])
@permission_classes([AllowAny])
@throttle_classes([ReviewCreateThrottle])
def bundle_reviews(request, slug):
    try:
        bundle = Bundle.objects.get(slug=slug, is_active=True)
    except Bundle.DoesNotExist:
        return Response({'detail': 'Not found'}, status=404)

    if request.method == 'GET':
        reviews = bundle.reviews.filter(is_approved=True, parent__isnull=True).order_by('created_at')
        return Response(ReviewSerializer(reviews, many=True).data)

    name, email, err = _review_author(request)
    if err: return err

    serializer = ReviewCreateSerializer(data=request.data)
    if serializer.is_valid():
        auto_approve = request.user.is_staff or request.user.is_superuser
        serializer.save(user=request.user if request.user.is_authenticated else None,
                        author_name=name, author_email=email,
                        bundle=bundle, is_approved=auto_approve)
        msg = 'Review published' if auto_approve else 'Review sent for moderation'
        return Response({'detail': msg}, status=201)
    return Response(serializer.errors, status=400)




@api_view(['GET'])
@permission_classes([AllowAny])
def brands_list(request):
    from django.db.models import Exists, OuterRef
    # Only brands that have at least one active product — empty brands are not shown.
    brands = (Brand.objects.filter(is_active=True)
              .filter(Exists(Product.objects.filter(brand=OuterRef('pk'), is_active=True)))
              .prefetch_related('images').order_by('name'))
    result = []
    for b in brands:
        logo_url = None
        if b.logo:
            logo_url = request.build_absolute_uri(b.logo.url)
        else:
            img = b.images.first()
            if img:
                logo_url = request.build_absolute_uri(img.image.url)
        result.append({
            'id': b.id,
            'name': b.name,
            'slug': b.slug,
            'logo': logo_url,
        })
    return Response(result)


@api_view(['POST'])
@permission_classes([AllowAny])
def notify_stock(request):
    from core.antibot import guard
    rejected = guard(request.data)
    if rejected:
        return rejected
    email = (request.data.get('email') or '').strip()
    variant_id = request.data.get('variant_id')
    bundle_id = request.data.get('bundle_id')
    if not email or not (variant_id or bundle_id):
        return Response({'detail': 'email and variant_id are required'}, status=400)
    if bundle_id:
        bundle = Bundle.objects.filter(
            pk=bundle_id, is_active=True,
        ).first() if str(bundle_id).isdigit() else None
        if bundle is None:
            return Response({'detail': 'Bundle not found'}, status=404)
        if bundle.get_stock() > 0:
            return Response({'detail': 'The bundle is already in stock'}, status=400)
        target = {'bundle': bundle}
    else:
        variant = ProductVariant.objects.filter(pk=variant_id).first() if str(variant_id).isdigit() else None
        if variant is None:
            return Response({'detail': 'Variant not found'}, status=404)
        if variant.stock > 0:
            return Response({'detail': 'The product is already in stock'}, status=400)
        target = {'variant': variant}
    notif, created = StockNotification.objects.get_or_create(email=email, **target)
    if not created:
        if not notif.is_notified:
            return Response({'detail': 'You are already subscribed'}, status=200)
        # An email for this subscription was already sent, and the target is out of stock again —
        # the subscription revives,
        # otherwise the person sees "Thank you" and never gets a second email.
        from .models import NotificationState
        notif.is_notified = False
        notif.created_at = timezone.now()
        notif.save(update_fields=['is_notified', 'created_at'])
        NotificationState.objects.filter(section='stock', object_id=notif.pk).delete()
    return Response({'detail': 'Subscription created'}, status=201)


@api_view(['GET'])
@permission_classes([AllowAny])
def maintenance_status(request):
    """Whether maintenance mode is on. The frontend polls — once it is turned off, it sends the user back
to the site."""
    from .middleware import _get_maintenance
    enabled, message = _get_maintenance()
    if enabled:
        return Response({'maintenance': True, 'message': message}, status=503)
    return Response({'maintenance': False})


@api_view(['GET'])
@permission_classes([AllowAny])
def site_settings(request):
    """Public settings for the frontend: minimum order amount and payment details.

    The frontend fetches it once at startup. Cached — it changes rarely, and is reset when
    SiteSettings / PaymentCard is saved (see their .save()/.delete()).
    """
    from django.core.cache import cache
    from .models import SiteSettings, PaymentCard

    _CACHE_KEY = 'site_settings:public'
    data = cache.get(_CACHE_KEY)
    if data is None:
        s = SiteSettings.get()
        # Data of active cards only (hidden ones are not exposed), while the thresholds come from
        # ALL
        # (including hidden groups). This lets the frontend pick the right threshold by amount even
        # when its group is hidden, and not substitute someone else's payment details.
        cards = [
            {
                'bank': c.bank,
                'card': c.card_number,
                'name': c.holder,
                'threshold': int(c.threshold),
            }
            for c in PaymentCard.objects.filter(is_active=True)
        ]
        thresholds = sorted({int(c.threshold) for c in PaymentCard.objects.all()})
        data = {
            'min_order_amount': int(s.min_order_amount),
            'payment_cards': cards,
            'thresholds': thresholds,
        }
        cache.set(_CACHE_KEY, data, 300)
    return Response(data)


# ─── Favorites — server-side storage of likes for demand analytics ───

class FavoriteThrottle(SimpleRateThrottle):
    """Self-contained limit (does not depend on THROTTLE_RATES in settings) — to
    protect toggle/sync from flooding without getting in the way of normal liking."""
    scope = 'favorite'

    def get_rate(self):
        return '300/min'

    def get_cache_key(self, request, view):
        ident = request.user.pk if request.user.is_authenticated else self.get_ident(request)
        return f'throttle_favorite_{ident}'


def _resolve_favorite_target(kind, obj_id):
    """(kind, id) from the frontend → (model field name, target object). None if not found."""
    try:
        obj_id = int(obj_id)
    except (TypeError, ValueError):
        return None, None
    if kind == 'variant':
        return 'product_variant', ProductVariant.objects.filter(pk=obj_id).first()
    if kind == 'bundle':
        return 'bundle', Bundle.objects.filter(pk=obj_id).first()
    if kind == 'course':
        from courses.models import Course
        return 'course', Course.objects.filter(pk=obj_id).first()
    return None, None


@api_view(['POST'])
@permission_classes([AllowAny])
@throttle_classes([FavoriteThrottle])
def favorite_set(request):
    """Set the like state explicitly (idempotent). Body: {device_id, kind, id, liked: bool}.
    An explicit liked (rather than toggle) guarantees the backend always matches the frontend state
    —
    even if the state diverged (lost request, like before sync, etc.)."""
    device_id = (request.data.get('device_id') or '').strip()[:64]
    if not device_id:
        return Response({'detail': 'device_id is required'}, status=400)
    field, target = _resolve_favorite_target(request.data.get('kind'), request.data.get('id'))
    if not field or target is None:
        return Response({'detail': 'Target not found'}, status=404)
    liked = bool(request.data.get('liked'))
    lookup = {'device_id': device_id, field: target}
    if not liked:
        Favorite.objects.filter(**lookup).delete()
        return Response({'liked': False})
    user = request.user if request.user.is_authenticated else None
    Favorite.objects.get_or_create(**lookup, defaults={'user': user})
    return Response({'liked': True}, status=201)


@api_view(['POST'])
@permission_classes([AllowAny])
@throttle_classes([FavoriteThrottle])
def favorite_sync(request):
    """One-time upload of already existing localStorage likes. Body: {device_id, items:[{kind,id}]}.
    Idempotent (get_or_create) — a repeated call duplicates nothing."""
    device_id = (request.data.get('device_id') or '').strip()[:64]
    items = request.data.get('items')
    if not device_id or not isinstance(items, list):
        return Response({'detail': 'device_id and items are required'}, status=400)
    user = request.user if request.user.is_authenticated else None
    created = 0
    for it in items[:500]:  # cap against abuse
        if not isinstance(it, dict):
            continue
        field, target = _resolve_favorite_target(it.get('kind'), it.get('id'))
        if not field or target is None:
            continue
        _, was_created = Favorite.objects.get_or_create(
            device_id=device_id, **{field: target}, defaults={'user': user})
        if was_created:
            created += 1
    return Response({'synced': created})


# ── Flora plant-assistant feed (MyPlantApp): read-only, secret-gated ──────────
# Narrow contract so the assistant can read live price/stock/image by id (realtime
# cards) and pull text for incremental re-embedding. Auth is a shared secret header,
# NOT a user JWT — @authentication_classes([]) drops the default auth so the secret is
# the only gate; @throttle_classes([]) drops the default per-user throttles.

_FLORA_DEFER = ('supplier_min_qty', 'supplier_price', 'supplier_url')


def _flora_authorized(request):
    secret = os.getenv('SHOP_FLORA_API_SECRET', '')
    return bool(secret) and hmac.compare_digest(request.headers.get('X-Flora-Secret', ''), secret)


@api_view(['POST'])
@authentication_classes([])
@permission_classes([AllowAny])
@throttle_classes([])
def flora_catalog_batch(request):
    """By-id batch for Flora. Body: {"ids": ["prod-1","bundle-3", ...] (cap 200),
    "include": "text"?}. Returns {"items": {id: {...}}, "missing": [id, ...]}.
    Lean (card-only: price/old_price/in_stock/image/active) by default; include=text
    adds name/embed_text/context/categories/url for re-embedding."""
    if not _flora_authorized(request):
        return Response({'detail': 'forbidden'}, status=403)
    ids = request.data.get('ids') or []
    if not isinstance(ids, list):
        return Response({'detail': 'ids must be a list'}, status=400)
    ids = [x for x in ids if isinstance(x, str)][:200]
    include_text = (request.data.get('include') or request.query_params.get('include')) == 'text'

    prod_ids, bundle_ids = [], []
    for x in ids:
        if x.startswith('prod-'):
            try:
                prod_ids.append(int(x[5:]))
            except ValueError:
                pass
        elif x.startswith('bundle-'):
            try:
                bundle_ids.append(int(x[7:]))
            except ValueError:
                pass

    items = {}
    if prod_ids:
        p_qs = Product.objects.filter(id__in=prod_ids)
        # Realtime card refresh needs REMOVED products too (active:false) — so Flora can mark
        # them "Removed from sale" and read their paired partner. The re-embed
        # path (include=text)
        # must NOT index a hidden product, so it keeps the is_active filter.
        if include_text:
            p_qs = p_qs.filter(is_active=True)
        products = p_qs.select_related('paired_product').prefetch_related(
            Prefetch('variants', queryset=ProductVariant.objects.defer(*_FLORA_DEFER)),
            'variants__variant_labels__label', 'variants__images',
            'images', 'categories', 'compositions', 'characteristics',
            # partner's variants/images for the paired fallback card
            Prefetch('paired_product__variants', queryset=ProductVariant.objects.defer(*_FLORA_DEFER)),
            'paired_product__images',
        )
        for p in products:
            items[f'prod-{p.id}'] = unit_payload(p, 'product', request, include_text)
    if bundle_ids:
        b_qs = Bundle.objects.filter(id__in=bundle_ids)
        if include_text:
            b_qs = b_qs.filter(is_active=True)
        bundles = b_qs.prefetch_related(
            Prefetch('items__product_variant', queryset=ProductVariant.objects.defer(*_FLORA_DEFER)),
            'images', 'categories',
        )
        for b in bundles:
            items[f'bundle-{b.id}'] = unit_payload(b, 'bundle', request, include_text)

    missing = [x for x in ids if x not in items]
    return Response({'items': items, 'missing': missing})


@api_view(['GET'])
@authentication_classes([])
@permission_classes([AllowAny])
@throttle_classes([])
def flora_catalog_delta(request):
    """Ids of products/bundles whose TEXT changed since ?since=<ISO8601>, for Flora's
    re-embed safety-net cron. Response:
      {"product_ids":[...], "bundle_ids":[...], "now":"<ISO>", "truncated":bool,
       "next_product_since":"<ISO>|null", "next_bundle_since":"<ISO>|null"}
    `now` is the next watermark (captured BEFORE the queries, minus a small overlap, so a
    row committing during the query window isn't skipped — re-embed is idempotent, so the
    overlap only costs a few redundant embeds). On truncation, next_*_since is the last
    returned row's timestamp so the cron can page forward.

    Deliberately blind to (handled elsewhere): sale-driven stock — order code uses
    queryset .update() which bypasses auto_now/post_save (stock is served realtime);
    BundleItem add/remove — has no timestamp (covered by a periodic full rebuild);
    M2M-only edits that don't re-save the parent."""
    if not _flora_authorized(request):
        return Response({'detail': 'forbidden'}, status=403)
    since = request.query_params.get('since')
    dt = parse_datetime(since) if since else None
    if dt is None:
        return Response({'detail': 'since (ISO8601) required'}, status=400)
    try:
        limit = max(1, min(int(request.query_params.get('limit', 1000)), 5000))
    except (ValueError, TypeError):
        limit = 1000

    # Capture the watermark BEFORE the queries and back it off a little, so a write that
    # stamps updated_at before this point but commits after the SELECT is re-scanned next run.
    watermark = timezone.now() - timedelta(seconds=30)

    prods = list(
        Product.objects.filter(is_active=True, updated_at__gte=dt)
        .order_by('updated_at', 'id').values_list('id', 'updated_at')[:limit]
    )
    bundles = list(
        Bundle.objects.filter(is_active=True, updated_at__gte=dt)
        .order_by('updated_at', 'id').values_list('id', 'updated_at')[:limit]
    )
    prod_trunc = len(prods) >= limit
    bundle_trunc = len(bundles) >= limit
    return Response({
        'product_ids': [f'prod-{i}' for i, _ in prods],
        'bundle_ids': [f'bundle-{i}' for i, _ in bundles],
        'now': watermark.isoformat(),
        'truncated': prod_trunc or bundle_trunc,
        'next_product_since': prods[-1][1].isoformat() if (prod_trunc and prods) else None,
        'next_bundle_since': bundles[-1][1].isoformat() if (bundle_trunc and bundles) else None,
    })
