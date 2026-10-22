import json
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Q
from django.http import JsonResponse

from . import video_access
from .models import Course, CourseLesson


def _owner_only(request):
    """The Courses section is available only to the owner (_CoursesOwnerOnly in admin.py);
    staff_member_required is not enough here."""
    from .admin import _CoursesOwnerOnly
    return (getattr(request.user, 'email', '') or '').lower() == _CoursesOwnerOnly.OWNER_EMAIL


def _abs(request, filefield):
    try:
        return request.build_absolute_uri(filefield.url) if filefield else None
    except Exception:
        return None


def _variant_image(v):
    """Main photo of a packaging: the packaging's own photo → the shared product photo (variant=NULL)
    → any product photo. Filtered in Python, because .filter() on a prefetch would make
    an extra query per packaging."""
    vimgs = sorted([i for i in v.images.all() if not i.is_hidden],
                   key=lambda i: (not i.is_main, i.sort_order, i.pk))
    if vimgs:
        return vimgs[0].image
    if not v.product_id:
        return None
    imgs = [i for i in v.product.images.all() if not i.is_hidden]
    own = [i for i in imgs if i.variant_id is None] or imgs
    own.sort(key=lambda i: (not i.is_main, i.sort_order, i.pk))
    return own[0].image if own else None


def _bundle_image(bundle):
    """Bundle's main photo."""
    own = [i for i in bundle.images.all() if not i.is_hidden]
    own.sort(key=lambda i: (not i.is_main, i.sort_order, i.pk))
    return own[0].image if own else None


def _rel_variant(v, request):
    """Packaging row for the table/search. The name is as on the storefront: the packaging's
    own name, or, if empty (the product's only packaging), the product name."""
    p = v.product
    from catalog.serializers import _variant_shipping_origin
    origin = _variant_shipping_origin(v)
    stock = int(v.stock or 0)
    return {
        'type': 'variant', 'id': v.pk, 'product_id': v.product_id,
        'name': v.name or (p.name if p else ''),
        'sku': v.sku or '',
        'price': float(v.price) if v.price is not None else None,
        'is_active': bool(v.is_active),
        # Availability is computed the same way as on the storefront (utils/stock.ts
        # variantInStock):
        # a hybrid is always in stock; when own stock runs out, we top up via dropshipping.
        'stock': stock,
        'origin': origin,
        'in_stock': (origin == 'hybrid') or stock > 0,
        # "own packaging" is a product weighed out of a bulk bag; the manager sees it
        # via a label,
        # as in the order (packaging_kind lives on the product, not on the packaging).
        'packaging_kind': (p.packaging_kind or '') if p else '',
        'photo': _abs(request, _variant_image(v)),
        'admin_url': '/admin/catalog/product/%d/change/' % v.product_id if v.product_id else '',
        'site_url': ('%s/product/%s?variant=%d' % (settings.SITE_URL.rstrip('/'), p.slug, v.pk)
                     if (p and p.slug) else ''),
    }


def _bundle_components(b, request):
    """Bundle contents for expanding a row (as in the order form)."""
    from catalog.views import _bundle_items_data
    try:
        return _bundle_items_data(b, request)
    except Exception:
        return []


def _rel_bundle(b, request):
    sku = b.sku or ''
    try:
        bprice = float(b.get_price() or 0) or None
    except Exception:
        bprice = None
    try:
        bstock = int(b.get_stock() or 0)
    except Exception:
        bstock = 0
    return {
        'type': 'bundle', 'id': b.pk, 'name': b.name, 'sku': sku,
        'price': bprice,
        'stock': bstock,
        'origin': 'own',
        'in_stock': bstock > 0,
        'items': _bundle_components(b, request),
        'is_active': bool(b.is_active),
        'photo': _abs(request, _bundle_image(b)),
        'admin_url': '/admin/catalog/bundle/%d/change/' % b.pk,
        'site_url': '%s/bundle/%s' % (settings.SITE_URL.rstrip('/'), b.slug) if b.slug else '',
    }


@staff_member_required
def course_related_search(request):
    """Search for the "Products and bundles" table in the course form.

    GRANULARITY: ProductVariant (packaging) / Bundle — same as in order search. A bundle is
    a flat entity (price/stock on the Bundle itself). The CRM also sees hidden
    items — is_active hides only on the storefront.
    """
    if not _owner_only(request):
        return JsonResponse({'error': 'forbidden'}, status=403)
    q = (request.GET.get('q') or '').strip()
    if len(q) < 2:
        return JsonResponse({'results': []})
    from catalog.models import Bundle, ProductVariant
    vq = Q(name__icontains=q) | Q(sku__icontains=q) | Q(product__name__icontains=q)
    bq = Q(name__icontains=q) | Q(sku__icontains=q)
    if q.isdigit():
        vq |= Q(pk=q)
        bq |= Q(pk=q)
    variants = (ProductVariant.objects.filter(vq).distinct()
                .select_related('product')
                .prefetch_related('images', 'product__images')
                .order_by('product__name', 'name', 'pk')[:20])
    bundles = (Bundle.objects.filter(bq).distinct()
               .prefetch_related('images').order_by('name', 'pk')[:10])
    results = [_rel_variant(v, request) for v in variants]
    results += [_rel_bundle(b, request) for b in bundles]
    return JsonResponse({'results': results})


@staff_member_required
def course_related_details(request):
    """Data of already linked items, to draw the table when the form loads."""
    if not _owner_only(request):
        return JsonResponse({'error': 'forbidden'}, status=403)
    from catalog.models import Bundle, ProductVariant
    vids = [int(x) for x in (request.GET.get('variants') or '').split(',') if x.strip().isdigit()]
    bids = [int(x) for x in (request.GET.get('bundles') or '').split(',') if x.strip().isdigit()]
    out = []
    if vids:
        qs = (ProductVariant.objects.filter(pk__in=vids).select_related('product')
              .prefetch_related('images', 'product__images'))
        by_id = {v.pk: v for v in qs}
        out += [_rel_variant(by_id[i], request) for i in vids if i in by_id]
    if bids:
        qs = Bundle.objects.filter(pk__in=bids).prefetch_related('images')
        by_id = {b.pk: b for b in qs}
        out += [_rel_bundle(by_id[i], request) for i in bids if i in by_id]
    return JsonResponse({'results': out})


@staff_member_required
def admin_course_video(request):
    """Video preview in the CRM form.

    It does not play directly from /media/: nginx returns 404 for /media/courses/lessons/
    (paywall, see courses/video_access.py), and the public /api/courses/video/
    requires a CoursePurchase, which the owner does not have. So we serve it via the same
    X-Accel-Redirect, but by the admin session and only to the owner.
    """
    if not _owner_only(request):
        return JsonResponse({'error': 'forbidden'}, status=403)
    kind = request.GET.get('kind', 'full')
    obj_id = request.GET.get('id')
    if kind == 'lesson':
        obj = CourseLesson.objects.filter(pk=obj_id).first()
        f = obj.video_file if obj else None
    else:
        obj = Course.objects.filter(pk=obj_id).first()
        f = obj.full_video_file if obj else None
    if not f:
        return JsonResponse({'error': 'not found'}, status=404)
    return video_access.serve(f)


@staff_member_required
def save_course_field(request):
    """Instant-save of course fields from the CRM list (like save-variant-field / save-brand-field).

    The bottom submit-row is hidden in the CRM, so there is nothing to save with standard
    list_editable;
    we write right on click/blur of the field.
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    # The Courses section is closed to everyone except the owner (_CoursesOwnerOnly in admin.py),
    # so staff_member_required is not enough here.
    from .admin import _CoursesOwnerOnly
    if (getattr(request.user, 'email', '') or '').lower() != _CoursesOwnerOnly.OWNER_EMAIL:
        return JsonResponse({'error': 'forbidden'}, status=403)

    data = json.loads(request.body)
    field = data.get('field')
    if field not in {'is_active', 'price'}:
        return JsonResponse({'error': 'forbidden'}, status=403)
    try:
        course = Course.objects.get(pk=data.get('id'))
    except Course.DoesNotExist:
        return JsonResponse({'error': 'not found'}, status=404)

    if field == 'is_active':
        course.is_active = bool(data.get('value'))
    else:
        try:
            price = Decimal(str(data.get('value')).replace(',', '.').strip())
        except (InvalidOperation, AttributeError):
            return JsonResponse({'error': 'bad price'}, status=400)
        if price < 0:
            return JsonResponse({'error': 'bad price'}, status=400)
        course.price = price

    course.save(update_fields=[field])
    return JsonResponse({'ok': True, 'value': str(getattr(course, field))})
