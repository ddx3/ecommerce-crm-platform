import hashlib
import json
import logging
import os
import re
import time
import mimetypes
import numpy as np
import requests
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import JsonResponse, HttpResponse
from django.http.request import RawPostDataException
from django.views.decorators.http import require_POST
from django.views.decorators.csrf import csrf_exempt
from django.utils.crypto import constant_time_compare
from django.contrib.admin.views.decorators import staff_member_required

from catalog.models import ProductVariant
from catalog import instruction_resolve
from .models import Invoice, InvoiceItem, InvoiceNameMapping, Order, format_phone
from . import monobank
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle

log = logging.getLogger(__name__)

# Safe extensions for user uploads and the size limit.
ALLOWED_UPLOAD_EXTS = {'jpg', 'jpeg', 'jpe', 'jfif', 'png', 'webp', 'gif', 'bmp', 'heic', 'heif', 'pdf'}
MAX_UPLOAD_BYTES = 8 * 1024 * 1024  # 8 MB

# ISO base-media brands (HEIF/HEIC/AVIF). iPhone writes photos not only as 'heic' —
# very often the major brand = 'mif1'/'msf1' (HEIF still image), which previously failed
# the magic-bytes check even though .heic was allowed → the customer saw a rejection.
_BMFF_BRANDS = (
    b'heic', b'heix', b'heim', b'heis', b'hevc', b'hevm', b'hevs',
    b'heif', b'mif1', b'msf1', b'avif', b'avis',
)

# The minimum order amount (UAH) now lives in CRM → Settings
# (SiteSettings.min_order_amount). It applies only if the order contains
# physical products — purely digital orders (courses) are not subject to the limit.

# Magic bytes for a basic type check
_FILE_MAGICS = (
    (b'\xff\xd8\xff', 'jpg'),
    (b'\x89PNG\r\n\x1a\n', 'png'),
    (b'GIF87a', 'gif'),
    (b'GIF89a', 'gif'),
    (b'BM', 'bmp'),
    (b'%PDF-', 'pdf'),
)


def _looks_like_allowed_file(file_obj) -> bool:
    """We check magic bytes so as not to trust the extension alone."""
    pos = file_obj.tell() if hasattr(file_obj, 'tell') else 0
    try:
        head = file_obj.read(32)
    finally:
        try:
            file_obj.seek(pos)
        except Exception:
            pass
    if any(head.startswith(magic) for magic, _ in _FILE_MAGICS):
        return True
    # WebP: specifically RIFF....WEBP, not just any RIFF container (AVI/WAV etc.).
    if head[:4] == b'RIFF' and head[8:12] == b'WEBP':
        return True
    # ISO base-media (HEIF/HEIC/AVIF): 'ftyp' box at offset 4, then the major brand
    # and compatible brands — accept if any known brand is present.
    if head[4:8] == b'ftyp' and any(brand in head for brand in _BMFF_BRANDS):
        return True
    return False


def _money(value) -> Decimal:
    return Decimal(str(value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def _money_whole(value) -> Decimal:
    """The price of a product/bundle line is always a whole hryvnia (consistent with the storefront,
    where prices are rounded to whole numbers). Does not touch courses/delivery."""
    return Decimal(str(value)).quantize(Decimal('1'), rounding=ROUND_HALF_UP)


def _create_monobank_payment(order) -> dict:
    """Creates a Monobank invoice and saves invoice_id in Order.
    Returns {'pageUrl': ..., 'invoiceId': ...} — the frontend does window.location = pageUrl."""
    site_url = settings.SITE_URL.rstrip('/')
    backend_url = settings.BACKEND_URL.rstrip('/')
    # Pass the email in the redirect — so that CheckoutSuccess can show guests the
    # "create an account in 1 click" form without an extra backend
    # request.
    from urllib.parse import quote
    email_q = f'&email={quote(order.customer_email)}' if order.customer_email else ''
    # t is a token so that CheckoutSuccess can publicly check the guest's payment status.
    token_q = f'&t={quote(order.public_token)}' if order.public_token else ''
    redirect_url = f'{site_url}/checkout/success?order={order.order_number}{email_q}{token_q}'
    webhook_url = f'{backend_url}/api/monobank/webhook/'
    data = monobank.create_invoice(
        order,
        redirect_url=redirect_url,
        webhook_url=webhook_url,
    )
    invoice_id = data.get('invoiceId') or ''
    if invoice_id:
        Order.objects.filter(pk=order.pk).update(monobank_invoice_id=invoice_id)
        order.monobank_invoice_id = invoice_id
    return {'pageUrl': data.get('pageUrl'), 'invoiceId': invoice_id}


# UserRateThrottle, not ScopedRateThrottle: in Scoped, `allow_request` overwrites
# self.scope with view.throttle_scope, and function-based views do not have it, so
# scope becomes None and the limit SILENTLY lets all requests through. The key is user.pk or IP.
class OrderCreateThrottle(UserRateThrottle):
    scope = 'order_create'


class NPProxyThrottle(UserRateThrottle):
    scope = 'np_proxy'


class PaymentActionThrottle(UserRateThrottle):
    scope = 'payment_action'


class InstructionPdfThrottle(UserRateThrottle):
    scope = 'instruction_pdf'


# ─── Nova Poshta public proxy (for checkout) ──────────────────────────────────

# Branches/parcel lockers change slowly; long TTL + background warm-up (the
# warmup_np_cache command via cron) => the buyer almost always hits a warm cache and doesn't wait
# for
# full pagination of a large city (~13s). A 7-day margin even if cron goes down.
NP_WH_CACHE_TTL = 60 * 60 * 24 * 7

# Streets of a settlement (for door-to-door address delivery) change rarely —
# we keep a long TTL so as not to spend the NP quota on repeated searches for the same street.
NP_STREETS_CACHE_TTL = 60 * 60 * 24 * 7


def _np_get_all_warehouses(api_key, city_ref):
    """Loads ALL NP branches/parcel lockers with pagination. Cached to avoid burning the quota.

    Large cities have thousands of points (Kyiv ~7600 → 16 pages of 500). NP returns
    branches first, and parcel lockers later across the pages, so a broken pagination cut off
    exactly the parcel lockers. Besides, NP throttles quick successive requests ('To many
    requests'),
    so between pages we pause + retry the throttle, rather than treating it as the end of the list.

    Returns None on an NP failure BEFORE the first successful page — so the proxy does NOT cache
    emptiness for 24h (a transient timeout would otherwise stick for all buyers).
    An empty list [] means a genuinely empty city (first page succeeded, 0 items)."""
    PAGE_SIZE = 500
    PAGE_DELAY = 0.55       # NP throttles faster than ~2 req/s on this method
    MAX_RETRIES = 5         # retries on 'To many requests' for a single page
    MAX_PAGES = 60          # safety cap: 60*500 = 30k points (more than enough)

    all_data = []
    page = 1
    while page <= MAX_PAGES:
        payload = {
            'apiKey': api_key,
            'modelName': 'AddressGeneral',
            'calledMethod': 'getWarehouses',
            'methodProperties': {'SettlementRef': city_ref, 'Language': 'UA', 'Limit': PAGE_SIZE, 'Page': page},
        }
        data = None
        for attempt in range(MAX_RETRIES):
            try:
                resp = requests.post('https://api.novaposhta.ua/v2.0/json/', json=payload, timeout=10)
                resp.raise_for_status()
                data = resp.json()
            except (requests.RequestException, ValueError) as e:
                log.warning('NP getWarehouses failed (page %s, try %s): %s', page, attempt + 1, e)
                return all_data if page > 1 else None
            if data.get('success'):
                break
            # success:false. Throttling ('To many requests' / 'Try again after…') is NOT the end of
            # the
            # list: wait and repeat the same page, otherwise we lose parcel lockers.
            errs = ' '.join(str(x) for x in (data.get('errors') or [])) + \
                   ' '.join(str(x) for x in (data.get('info') or []))
            if 'many request' in errs.lower() or 'try again' in errs.lower():
                time.sleep(PAGE_DELAY * (attempt + 2))  # linear backoff
                continue
            # Other business error (broken ref / exhausted quota / key blocked).
            if page == 1:
                log.warning('NP getWarehouses business error: %s', data.get('errors'))
                return None
            data = None  # on subsequent pages — we break off below as a failure
            break

        if data is None or not data.get('success'):
            # Could not get the page even with retries: keep what has already been collected,
            # but on the first page — None (don't cache emptiness).
            log.warning('NP getWarehouses page %s unresolved after retries', page)
            return all_data if page > 1 else None

        items = data.get('data', []) or []
        all_data.extend(items)
        if len(items) < PAGE_SIZE:
            break
        page += 1
        time.sleep(PAGE_DELAY)
    return all_data


def _np_call(method: str, params: dict):
    """Unified NP call with handling of timeouts and network errors."""
    payload = {
        'apiKey': settings.NOVA_POSHTA_API_KEY,
        'modelName': 'Address',
        'calledMethod': method,
        'methodProperties': params,
    }
    try:
        resp = requests.post('https://api.novaposhta.ua/v2.0/json/', json=payload, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except requests.Timeout:
        return {'success': False, 'errors': ['NP timeout']}
    except requests.RequestException as e:
        log.warning('NP %s failed: %s', method, e)
        return {'success': False, 'errors': ['NP request failed']}


@api_view(['GET'])
@permission_classes([])
@throttle_classes([NPProxyThrottle])
def nova_poshta_public_proxy(request):
    method = request.GET.get('method', '')
    if method not in ('searchSettlements', 'getWarehouses', 'searchSettlementStreets'):
        return Response({'error': 'forbidden'}, status=403)
    if not settings.NOVA_POSHTA_API_KEY:
        return Response({'error': 'NP key is not configured'}, status=503)

    if method == 'searchSettlementStreets':
        # Address delivery: street search within a settlement (SettlementRef ≡ cityRef).
        settlement_ref = (request.GET.get('city_ref', '') or '').strip()
        if not settlement_ref or len(settlement_ref) > 64:
            return Response({'error': 'invalid city_ref'}, status=400)
        q = (request.GET.get('q', '') or '').strip()[:128]
        if len(q) < 2:
            # Short query — we don't poke NP; the format is uniform with searchSettlements.
            return Response({'success': True, 'data': [{'Addresses': []}]})
        cache_key = f'np:str:{settlement_ref}:{q.lower()}'
        cached = cache.get(cache_key)
        if cached is not None:
            return Response(cached)
        res = _np_call('searchSettlementStreets',
                       {'StreetName': q, 'SettlementRef': settlement_ref, 'Limit': 12, 'Page': 1})
        if res.get('success'):
            cache.set(cache_key, res, NP_STREETS_CACHE_TTL)
        return Response(res)

    if method == 'getWarehouses':
        city_ref = (request.GET.get('city_ref', '') or '').strip()
        if not city_ref or len(city_ref) > 64:
            return Response({'error': 'invalid city_ref'}, status=400)
        cache_key = f'np:wh:{city_ref}'
        data = cache.get(cache_key)
        if data is None:
            fetched = _np_get_all_warehouses(settings.NOVA_POSHTA_API_KEY, city_ref)
            if fetched:
                cache.set(cache_key, fetched, NP_WH_CACHE_TTL)  # only a successful non-empty result
                data = fetched
            elif fetched is None:
                # NP failure — do NOT cache. 502 → the frontend shows an error state (retry), not
                # "the city has no branches" (otherwise it would stick for
                # 24h for everyone).
                return Response({'success': False, 'data': [], 'error': 'NP unavailable'}, status=502)
            else:
                # A city that genuinely has no branches — also not cached for a day (rare).
                return Response({'success': True, 'data': []})
        return Response({'success': True, 'data': data})

    q = (request.GET.get('q', '') or '').strip()[:128]
    cache_key = f'np:set:{q.lower()}'
    cached = cache.get(cache_key)
    if cached is not None:
        return Response(cached)
    res = _np_call(method, {'CityName': q, 'Limit': 12, 'Page': 1})
    # Cache only a successful response — otherwise an error response would stick for 24h.
    if res.get('success'):
        cache.set(cache_key, res, 60 * 60 * 24)
    return Response(res)


# ─── Order create (public) ─────────────────────────────────────────────────

class _OrderValidationError(Exception):
    """Item validation error INSIDE transaction.atomic().

    We raise instead of `return Response(...)` so the exception leaves the atomic block and the
    transaction ROLLS BACK. Otherwise a plain return ends the block normally and a partial order
    (with stock already written off) gets committed — phantom orders.
    """

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _get_or_create_client(name, phone, email):
    """Guest order → the customer is automatically added to "Customers". Looks up an existing
    one
    by email/phone (last 9 digits), otherwise creates a new one. No duplicates."""
    import re
    from users.models import User
    email = (email or '').strip().lower() or None
    digits = re.sub(r'\D', '', phone or '')
    tail = digits[-9:] if len(digits) >= 9 else ''
    if email:
        u = User.objects.filter(email__iexact=email).first()
        if u:
            return u
    if tail:
        for u in User.objects.filter(phone__contains=tail):
            if re.sub(r'\D', '', u.phone or '')[-9:] == tail:
                return u
    parts = (name or '').strip().split()
    fn = parts[0] if parts else ''
    ln = ' '.join(parts[1:]) if len(parts) > 1 else ''
    if digits.startswith('380'):
        full = '+' + digits
    elif digits.startswith('0'):
        full = '+38' + digits
    elif tail:
        full = '+380' + tail
    else:
        full = ''
    u = User(role='client', first_name=fn, last_name=ln, phone=full, email=email, is_active=True)
    u.set_unusable_password()
    u.save()
    return u


def _build_shipments(order, drop_addr_by_code=None):
    """Builds the order's shipments: 1 «own» (own + bundles/courses + in-stock hybrid)
    + one «drop» shipment per supplier (Bradas, ARS...).
    Drop is always a Nova Poshta branch. Only for new orders (old ones use the legacy fields)."""
    from collections import defaultdict
    from .models import Shipment
    drop_by_supplier = defaultdict(list)
    has_own = False
    for it in order.items.select_related('product_variant__supplier').all():
        v = it.product_variant if it.product_variant_id else None
        if it.was_dropshipped and v and v.supplier_id:
            drop_by_supplier[v.supplier].append(it)
        else:
            has_own = True  # own, bundles, courses, in-stock hybrid
    addr = order.delivery_address or ''
    is_addr = addr.lower().startswith(('address delivery', 'адресна доставка'))  # new English marker or the legacy Ukrainian one
    # Pickup — there is no own shipment; drop items still go by Nova Poshta separately.
    if order.delivery_type == 'pickup':
        has_own = False
    if has_own:
        Shipment.objects.create(
            order=order, kind='own', supplier=None,
            carrier=order.delivery_type or 'nova_poshta',
            address_kind='address' if is_addr else 'warehouse',
            address=addr,
        )
    # Drop address per supplier (from checkout drop_shipments by supplier.code);
    # fallback — legacy fields (drop_delivery_address / delivery_address for allDrop).
    drop_addr_by_code = drop_addr_by_code or {}
    fallback = order.drop_delivery_address or order.delivery_address or ''
    for supplier in drop_by_supplier:
        Shipment.objects.create(
            order=order, kind='drop', supplier=supplier,
            carrier='nova_poshta', address_kind='warehouse',
            address=drop_addr_by_code.get(supplier.code) or fallback,
        )


@api_view(['POST'])
@permission_classes([])
@throttle_classes([OrderCreateThrottle])
def create_order(request):
    from .models import Order, OrderItem
    from catalog.models import ProductVariant, Bundle
    from courses.models import Course
    from rest_framework_simplejwt.authentication import JWTAuthentication

    data = request.data if isinstance(request.data, dict) else {}
    if not data:
        try:
            data = json.loads(request.body or b'{}')
        except json.JSONDecodeError:
            return Response({'error': 'invalid json'}, status=400)
        except RawPostDataException:
            # DRF has already read the stream (empty JSON `{}`): there is no data, the regular field
            # check below will give 400, not 500.
            data = {}

    # A trusted first-party client (the Flora app backend) presents a shared
    # secret and bypasses the browser antibot token — it is a server, not a form. Safe
    # by default: with an empty FLORA_ORDER_SECRET the bypass is not activated and behavior
    # for the site/existing clients is unchanged.
    _flora_secret = getattr(settings, 'FLORA_ORDER_SECRET', '')
    _flora_trusted = bool(_flora_secret) and constant_time_compare(
        request.headers.get('X-Flora-Order-Secret', ''), _flora_secret)
    if not _flora_trusted:
        from core.antibot import guard
        rejected = guard(data)
        if rejected:
            return rejected

    required = ['customer_name', 'customer_phone', 'delivery_type', 'payment_method', 'items']
    # Delivery address is required for all methods except pickup.
    if data.get('delivery_type') != 'pickup':
        required.append('delivery_address')
    for field in required:
        if not data.get(field):
            return Response({'error': f'Field {field} is required'}, status=400)

    items_in = data.get('items') or []
    if not isinstance(items_in, list) or not items_in:
        return Response({'error': 'items must be a non-empty list'}, status=400)
    if len(items_in) > 200:
        return Response({'error': 'too many items'}, status=400)

    payment_method = data['payment_method']
    if payment_method not in dict(Order.PAYMENT_CHOICES):
        return Response({'error': 'invalid payment_method'}, status=400)
    if data['delivery_type'] not in dict(Order.DELIVERY_CHOICES):
        return Response({'error': 'invalid delivery_type'}, status=400)

    # Optionally link to the user if a token is present
    user = None
    try:
        jwt_auth = JWTAuthentication()
        result = jwt_auth.authenticate(request)
        if result:
            user, _ = result
    except Exception:
        user = None

    # Normalize the items up front (no DB operations)
    normalized = []
    for raw in items_in:
        if not isinstance(raw, dict):
            return Response({'error': 'invalid item'}, status=400)
        try:
            qty = int(raw.get('qty', 1))
        except (TypeError, ValueError):
            return Response({'error': 'invalid qty'}, status=400)
        if qty <= 0 or qty > 1000:
            return Response({'error': 'qty must be 1..1000'}, status=400)
        if raw.get('is_course'):
            # A course is bound to the account after payment — there is nowhere to grant a guest
            # access. We forbid purchase without authorization before the order is even created.
            if user is None or not user.is_authenticated:
                return Response(
                    {'error': 'To buy a course, please sign in or register.'},
                    status=401,
                )
            try:
                cid = int(raw.get('variant_id'))  # for a course variant_id = course.id
            except (TypeError, ValueError):
                return Response({'error': 'course id required'}, status=400)
            # A course is intangible, qty is always 1.
            normalized.append({'kind': 'course', 'course_id': cid, 'qty': 1})
        elif raw.get('is_bundle'):
            slug = (raw.get('slug') or '').strip()
            if not slug:
                return Response({'error': 'bundle slug required'}, status=400)
            normalized.append({'kind': 'bundle', 'slug': slug, 'qty': qty})
        else:
            try:
                vid = int(raw.get('variant_id'))
            except (TypeError, ValueError):
                return Response({'error': 'variant_id required'}, status=400)
            normalized.append({'kind': 'variant', 'variant_id': vid, 'qty': qty})

    try:
        with transaction.atomic():
            # Payment details sent to the phone only make sense for a bank transfer.
            contact_requested = bool(data.get('payment_contact_requested')) and payment_method == 'bank_transfer'
            # Guest without an account → automatically create/find the customer in "Customers".
            if user is None:
                try:
                    user = _get_or_create_client(
                        data.get('customer_name'), data.get('customer_phone'), data.get('customer_email'))
                except Exception:
                    user = None  # don't break the order if the customer couldn't be created
            order = Order.objects.create(
                user=user,
                customer_name=str(data['customer_name'])[:200],
                customer_phone=str(data['customer_phone'])[:20],
                customer_email=str(data.get('customer_email', ''))[:254],
                delivery_type=data['delivery_type'],
                delivery_address=str(data.get('delivery_address', ''))[:1000],
                drop_delivery_address=str(data.get('drop_delivery_address', ''))[:1000],
                payment_method=payment_method,
                payment_contact_requested=contact_requested,
                comment=str(data.get('comment', ''))[:2000],
            )

            # Reservation within ONE cart (fix for the "shared bulk bag" problem):
            # one own-packaging variant
            # drawn by several items (a bundle component + a separate item of the same
            # bag) would otherwise pass the availability check twice against the full bag —
            # a silent oversell on write-off. We accumulate the real demand and check against it.
            from collections import defaultdict
            from catalog.models import BulkSource as _BulkSource
            bag_reserved = defaultdict(Decimal)   # bag_id -> reserved g/ml
            unit_reserved = defaultdict(int)      # variant_id -> reserved units
            bag_initial = {}                      # bag_id -> bag stock at the start of the transaction
            stock_initial = {}                    # variant_id -> variant.stock level at the start
            # IMPORTANT: we record the INITIAL stock on first access and then compute
            # «start − reserve». We do not re-read the current BulkSource.quantity, because for
            # cod/bank_transfer stock is written off immediately in this same loop (post_save
            # signal) —
            # the current value is then already reduced, and double counting (reserve + actual
            # write-off)
            # would wrongly block a valid cart. For monobank the write-off is deferred — the reserve
            # remains the only protection against overselling a shared bag within the cart.
            def _bag_room(bag_id):
                if bag_id not in bag_initial:
                    q = _BulkSource.objects.filter(pk=bag_id).values_list('quantity', flat=True).first()
                    bag_initial[bag_id] = Decimal(str(q)) if q is not None else Decimal('0')
                return bag_initial[bag_id] - bag_reserved[bag_id]
            def _stock_room(vid):
                if vid not in stock_initial:
                    s = ProductVariant.objects.filter(pk=vid).values_list('stock', flat=True).first()
                    stock_initial[vid] = s or 0
                return stock_initial[vid] - unit_reserved[vid]
            total = Decimal('0')
            for n in normalized:
                if n['kind'] == 'course':
                    try:
                        course = Course.objects.get(pk=n['course_id'], is_active=True)
                    except Course.DoesNotExist:
                        raise _OrderValidationError(f'course not found: {n["course_id"]}')
                    if course.price is None or Decimal(str(course.price)) <= 0:
                        raise _OrderValidationError(f'course has no price: {n["course_id"]}')
                    from courses.models import CoursePurchase
                    if CoursePurchase.objects.filter(user=user, course=course).exists():
                        raise _OrderValidationError(f'Course "{course.name}" has already been purchased.')
                    price = _money(course.price)
                    OrderItem.objects.create(
                        order=order, course=course,
                        quantity=1, price=price,
                    )
                    total += price
                    continue
                if n['kind'] == 'bundle':
                    try:
                        bundle = Bundle.objects.select_for_update().get(slug=n['slug'], is_active=True)
                    except Bundle.DoesNotExist:
                        raise _OrderValidationError(f'bundle not found: {n["slug"]}')
                    # Lock the rows of the component variants and bulk bags, so the stock check and
                    # write-off are
                    # atomic with respect to concurrent orders (a bundle's stock actually lives in
                    # its components,
                    # not in Bundle).
                    _comp_variants = [
                        bi.product_variant
                        for bi in bundle.items.select_related('product_variant').all()
                        if bi.product_variant_id
                    ]
                    _comp_pks = [v.pk for v in _comp_variants]
                    if _comp_pks:
                        list(ProductVariant.objects.select_for_update().filter(pk__in=_comp_pks))
                    _bag_ids = {v.bulk_source_id for v in _comp_variants if v.bulk_source_id}
                    if _bag_ids:
                        from catalog.models import BulkSource
                        list(BulkSource.objects.select_for_update().filter(pk__in=_bag_ids))
                    server_price = bundle.get_price()
                    if server_price is None or Decimal(str(server_price)) <= 0:
                        raise _OrderValidationError(f'bundle has no price: {n["slug"]}')
                    available = bundle.get_stock()
                    if (available or 0) < n['qty']:
                        raise _OrderValidationError(
                            f'bundle "{bundle.name}" out of stock '
                            f'(have {available}, need {n["qty"]})'
                        )
                    # Reserve the real demand of components against shared bags/stock levels
                    # within the cart (bags/variants are already locked above via
                    # select_for_update).
                    for bi in bundle.items.select_related('product_variant').all():
                        cv = bi.product_variant
                        if not cv:
                            continue
                        if cv.bulk_source_id and cv.unit_size and not cv.stock_manual:
                            need = cv.bulk_source.effective_unit(cv.unit_size) * bi.quantity * n['qty']
                            if _bag_room(cv.bulk_source_id) < need:
                                raise _OrderValidationError(
                                    f'bundle "{bundle.name}" out of stock')
                            bag_reserved[cv.bulk_source_id] += need
                        elif ((not cv.bulk_source_id or cv.stock_manual)
                                and not getattr(cv, 'is_dropshipping', False)
                                and not getattr(cv, 'is_hybrid', False)):
                            # Piece-based accounting: non-bulk OR own packaging on MANUAL stock
                            # (stock_manual is «detached» from the bag → _consume_variant writes off
                            # variant.stock, so the reserve must also be against variant.stock, not
                            # the bag).
                            need_u = bi.quantity * n['qty']
                            if _stock_room(cv.pk) < need_u:
                                raise _OrderValidationError(
                                    f'bundle "{bundle.name}" out of stock')
                            unit_reserved[cv.pk] += need_u
                    price = _money_whole(server_price)
                    OrderItem.objects.create(
                        order=order, bundle=bundle,
                        quantity=n['qty'], price=price,
                    )
                else:
                    try:
                        variant = ProductVariant.objects.select_for_update().get(pk=n['variant_id'])
                    except ProductVariant.DoesNotExist:
                        raise _OrderValidationError(f'variant not found: {n["variant_id"]}')
                    # The product may have been deactivated/hidden between adding to the cart and
                    # checkout (withdrawn from sale, supplier fallback without a template).
                    # We do not allow buying what is no longer sold.
                    if not variant.is_active or variant.is_supplier_fallback:
                        raise _OrderValidationError(f'"{variant.name}" is no longer available')
                    if variant.price is None or Decimal(str(variant.price)) <= 0:
                        raise _OrderValidationError(f'variant has no price: {n["variant_id"]}')
                    # Stock check for a regular variant (not bulk-source/dropshipping/hybrid).
                    # A hybrid (50/50) sells beyond stock — the excess is dropshipped
                    # (see OrderItem.save → was_dropshipped), so we skip the hard check.
                    if ((not variant.bulk_source_id or variant.stock_manual)
                            and not getattr(variant, 'is_dropshipping', False)
                            and not getattr(variant, 'is_hybrid', False)):
                        # Piece-based accounting (variant.stock): non-bulk OR own packaging on
                        # manual
                        # stock. Manual own packaging is written off from variant.stock
                        # (_consume_variant, gate `not stock_manual`), so the reserve is also
                        # against variant.stock, not against the bag (otherwise different pools →
                        # oversell).
                        if _stock_room(variant.pk) < n['qty']:
                            raise _OrderValidationError(
                                f'"{variant.name}" out of stock (have {variant.stock or 0}, need {n["qty"]})'
                            )
                        unit_reserved[variant.pk] += n['qty']
                    elif variant.bulk_source_id and variant.unit_size and not variant.stock_manual:
                        # Bulk-source: the real stock is in the bag. We lock the bag row
                        # and check the available g/ml (taking the cart's reserve into account).
                        _BulkSource.objects.select_for_update().filter(pk=variant.bulk_source_id).first()
                        _need = variant.bulk_source.effective_unit(variant.unit_size) * n['qty']
                        if _bag_room(variant.bulk_source_id) < _need:
                            raise _OrderValidationError(f'"{variant.name}" out of stock')
                        bag_reserved[variant.bulk_source_id] += _need
                    # Apply the discount (effective_discount_percent with fallback to brand).
                    # variant.price is the BASE price, final = base * (1 - %/100).
                    base_price = Decimal(str(variant.price))
                    pct = variant.effective_discount_percent
                    explicit_old = variant.old_price if getattr(variant, 'old_price', None) else None
                    if explicit_old is not None and Decimal(str(explicit_old)) > base_price:
                        final_price = base_price
                        old_for_store = Decimal(str(explicit_old))
                    elif pct is not None and Decimal(str(pct)) > 0 and Decimal(str(pct)) < 100:
                        final_price = (base_price * (Decimal('1') - Decimal(str(pct)) / Decimal('100'))).quantize(Decimal('0.01'))
                        old_for_store = base_price
                    else:
                        final_price = base_price
                        old_for_store = None
                    # Fixed discount in UAH (on top of %) — previously ignored in the order price.
                    # Positive only (a negative one would otherwise increase the price).
                    disc_amount = getattr(variant, 'discount_amount', None)
                    if disc_amount and Decimal(str(disc_amount)) > 0:
                        reduced = final_price - Decimal(str(disc_amount))
                        if reduced > 0:
                            if old_for_store is None:
                                old_for_store = base_price
                            final_price = reduced.quantize(Decimal('0.01'))
                    price = _money_whole(final_price)
                    OrderItem.objects.create(
                        order=order, product_variant=variant,
                        quantity=n['qty'], price=price,
                        old_price=_money_whole(old_for_store) if old_for_store is not None else None,
                    )
                total += price * n['qty']

            # Minimum order amount — only for orders with physical products.
            # Courses (digital) are not subject to the limit.
            from catalog.models import SiteSettings
            min_order_amount = SiteSettings.get().min_order_amount
            has_physical = any(n['kind'] in ('variant', 'bundle') for n in normalized)
            if has_physical and total < min_order_amount:
                raise _OrderValidationError(
                    f'Minimum order amount: {int(min_order_amount)} UAH.'
                )

            order.total_price = _money(total)
            order.save(update_fields=['total_price'])

            # Build shipments (1 own + one per each drop supplier).
            drop_addr_by_code = {
                str(d.get('supplier_code')): str(d.get('address', ''))[:1000]
                for d in (data.get('drop_shipments') or []) if d.get('supplier_code')
            }
            _build_shipments(order, drop_addr_by_code)

            # Email after commit, so it does not slow down the request inside the transaction
            transaction.on_commit(lambda o=order: _safe_send_order_confirmation(o))

    except _OrderValidationError as e:
        # The transaction has already rolled back (the exception left atomic) — no orphan order
        # or stock write-off remains. Return the error to the client.
        return Response({'error': str(e)}, status=e.status)
    except (InvalidOperation, ValueError) as e:
        log.exception('create_order failed: %s', e)
        return Response({'error': 'invalid data'}, status=400)

    # Where the customer came from (ad UTM tags) — for the "Analyst". Outside the order
    # transaction: a failure to save the tags must not affect the order itself.
    from analyst.attribution import save_order_attribution
    save_order_attribution(order.pk, data.get('attribution'))

    # Create the Monobank invoice AFTER the transaction commits — an external HTTP call (up to 15s)
    # must not hold row locks on stock. The order is already saved;
    # if the invoice fails, the client can pay again via /orders (pay_order).
    response_payload = {'ok': True, 'order_number': order.order_number, 'token': order.public_token}
    if payment_method == 'monobank':
        try:
            mono = _create_monobank_payment(order)
        except monobank.MonobankNotConfigured:
            return Response({'error': 'Monobank is not configured'}, status=503)
        except monobank.MonobankError as e:
            log.error('monobank create failed: %s', e)
            return Response({'error': 'Failed to create the Monobank payment'}, status=502)
        response_payload['payment_url'] = mono['pageUrl']
        response_payload['invoice_id'] = mono['invoiceId']

    return Response(response_payload)


def _safe_send_order_confirmation(order):
    """Confirmation email in a background thread (like OCR of screenshots). Otherwise slow SMTP
    would hold the worker synchronously in the on_commit callback and slow down the create_order
    response."""
    import threading

    def _job():
        try:
            from core.email import send_order_confirmation
            send_order_confirmation(order)
        except Exception:
            log.exception('send_order_confirmation (bg) failed order=%s', getattr(order, 'order_number', '?'))
        finally:
            # A separate thread has its own DB connection; close it so it doesn't leak.
            from django.db import connection
            connection.close()

    threading.Thread(target=_job, daemon=True).start()


# ─── Monobank webhook ────────────────────────────────────────────────────────

@csrf_exempt
@require_POST
def monobank_webhook(request):
    """Webhook from Monobank Acquiring. Body is InvoiceStatusResponse, the X-Sign header is an ECDSA
    signature.
    Steps: verify the signature → find the order (by reference or monobank_invoice_id) →
    check the amount → idempotently update payment_status."""
    body = request.body or b''
    x_sign = request.headers.get('X-Sign', '')

    if not body:
        return HttpResponse('empty body', status=400)

    if not monobank.verify_signature(body, x_sign):
        log.warning('monobank_webhook: invalid signature')
        return HttpResponse('invalid signature', status=400)

    try:
        payload = json.loads(body.decode('utf-8'))
    except (ValueError, json.JSONDecodeError):
        return HttpResponse('invalid payload', status=400)

    invoice_id = payload.get('invoiceId') or ''
    reference = payload.get('reference') or ''
    status_value = (payload.get('status') or '').lower()
    ccy = payload.get('ccy')
    amount_kopecks = payload.get('amount')

    if not invoice_id:
        return HttpResponse('no invoiceId', status=400)

    # Find the order: first by invoice_id, fall back to reference (=order_number)
    order_qs = Order.objects.filter(monobank_invoice_id=invoice_id)
    if not order_qs.exists() and reference:
        order_qs = Order.objects.filter(order_number=reference)

    try:
        with transaction.atomic():
            order = order_qs.select_for_update().first()
            if not order:
                # The webhook may have arrived before the invoice_id update in the DB.
                # Return 200 so Monobank does not retry endlessly — but log it.
                log.warning('monobank_webhook: order not found invoice=%s ref=%s', invoice_id, reference)
                return HttpResponse('order not found')

            # If invoice_id is not recorded yet (race with create_order) — save it
            if not order.monobank_invoice_id and invoice_id:
                order.monobank_invoice_id = invoice_id
                order.save(update_fields=['monobank_invoice_id'])

            if status_value in monobank.SUCCESS_STATUSES:
                if order.payment_status == 'paid':
                    return HttpResponse('already paid')
                # Reconcile the amount and currency
                if ccy is not None and int(ccy) != 980:
                    log.error('monobank_webhook: wrong ccy=%s order=%s', ccy, order.order_number)
                    return HttpResponse('wrong ccy', status=400)
                expected = _money(order.total_price or 0)
                got = monobank.kopecks_to_money(int(amount_kopecks or 0))
                if got != expected:
                    log.error(
                        'monobank_webhook: amount mismatch order=%s expected=%s got=%s',
                        order.order_number, expected, got,
                    )
                    return HttpResponse('amount mismatch', status=400)
                order.payment_status = 'paid'
                order.payment_notification = True
                order.save(update_fields=['payment_status', 'payment_notification'])
            elif status_value in monobank.FAILURE_STATUSES:
                # Payment failed/reversed. Payment status is binary (pending/paid),
                # there is no separate 'failed' — leave as is (pending → the client can try
                # again; paid is not touched on a return after success). Only log.
                log.info('monobank_webhook: order=%s status=%s', order.order_number, status_value)
            else:
                log.info('monobank_webhook: order=%s intermediate status=%s', order.order_number, status_value)
    except Exception:
        log.exception('monobank_webhook: failed invoice=%s', invoice_id)
        return HttpResponse('internal error', status=500)

    return HttpResponse('ok')


# ─── Monobank Personal webhook (statements of the sole-proprietor account) ──────
#
# Mono Personal Webhook specifics:
#   - The webhook has NO signature (unlike Acquiring). Protection is a secret
#     in the URL itself (`MONOBANK_PERSONAL_WEBHOOK_SECRET` in .env).
#   - Before activation Mono sends a GET to the URL to check availability.
#     It must return 200 OK with an empty body.
#   - Payload: {"type": "StatementItem", "data": {"account": "...",
#                                                  "statementItem": {...}}}
#   - Mono retries for up to 12 hours, so 2xx is critical.

@csrf_exempt
def monobank_personal_webhook(request, secret: str):
    """Accepts a StatementItem from the Mono Personal API.

    URL: /api/webhooks/monobank-personal/<secret>/
    Registration: `python manage.py mono_set_webhook https://your-domain/api/webhooks/monobank-personal/<secret>/`
    """
    expected = getattr(settings, 'MONOBANK_PERSONAL_WEBHOOK_SECRET', '')
    if not expected or not constant_time_compare(secret, expected):
        log.warning('mono_personal_webhook: bad secret (got prefix=%s)', secret[:6])
        return HttpResponse('forbidden', status=403)

    if request.method == 'GET':
        # Mono checks the URL before activation — just return 200.
        return HttpResponse('ok')
    if request.method != 'POST':
        return HttpResponse('method not allowed', status=405)

    body = request.body or b''
    if not body:
        return HttpResponse('ok')  # empty ping

    try:
        payload = json.loads(body.decode('utf-8'))
    except (ValueError, json.JSONDecodeError):
        log.warning('mono_personal_webhook: invalid JSON')
        return HttpResponse('invalid payload', status=400)

    if payload.get('type') != 'StatementItem':
        return HttpResponse('ok')  # do nothing for other types

    data = payload.get('data') or {}
    account_id = data.get('account') or ''
    item = data.get('statementItem') or {}

    if not account_id or not item:
        return HttpResponse('incomplete payload', status=400)

    # Find the BankAccount, if we monitor this account.
    from orders.models import BankAccount, BankTransaction
    from orders import monobank_personal as mono_pers
    from orders import bank_matcher

    account = BankAccount.objects.filter(source='mono', external_id=account_id, is_active=True).first()
    if not account:
        log.info('mono_personal_webhook: account %s not registered, ignoring', account_id[:12])
        return HttpResponse('ok')  # not our account — skip silently

    parsed = next(iter(mono_pers.iter_statement_items([item], account_id)), None)
    if not parsed:
        return HttpResponse('ok')

    try:
        tx, created = BankTransaction.objects.update_or_create(
            source='mono', external_id=parsed['external_id'],
            defaults={
                'account': account,
                'transacted_at': parsed['transacted_at'],
                'amount': parsed['amount'],
                'currency_code': parsed['currency_code'],
                'description': parsed['description'],
                'comment': parsed['comment'],
                'counterparty_name': parsed['counterparty_name'],
                'raw_data': parsed['raw_data'],
            },
        )
        if created and tx.is_credit:
            # Trigger the matcher right here — faster than waiting for cron.
            try:
                bank_matcher.match_transaction(tx)
            except Exception:
                log.exception('mono_personal_webhook: matcher failed for tx=%s', tx.external_id)
    except Exception:
        log.exception('mono_personal_webhook: save failed account=%s', account_id[:12])
        return HttpResponse('internal error', status=500)

    return HttpResponse('ok')


# ─── Per-order bundle components snapshot (CRM order form) ───────────────────

@staff_member_required
def order_item_components(request):
    """Per-item snapshot of bundle composition (OrderItemBundleComponent) for
    editing in the order form. Returns {item_pk: [components]} — only for
    existing bundle items (whose snapshot was already created by the signal)."""
    from .models import OrderItemBundleComponent
    ids = [x.strip() for x in request.GET.get('item_ids', '').split(',') if x.strip().isdigit()]
    if not ids:
        return JsonResponse({'items': {}})
    qs = OrderItemBundleComponent.objects.filter(
        order_item_id__in=ids
    ).select_related('product_variant__product').prefetch_related(
        'product_variant__images', 'product_variant__product__images',
    ).order_by('pk')
    result = {}
    for c in qs:
        pv = c.product_variant
        photo = ''
        slug = ''
        if pv:
            img = (
                pv.images.order_by('sort_order', 'pk').first() or
                (pv.product.images.filter(variant__isnull=True).order_by('sort_order', 'pk').first()
                 or pv.product.images.order_by('sort_order', 'pk').first() if pv.product_id else None)
            )
            if img and img.image:
                photo = request.build_absolute_uri(img.image.url)
            slug = pv.product.slug if (pv.product_id and pv.product.slug) else ''
        result.setdefault(str(c.order_item_id), []).append({
            'comp_id': c.pk,
            'variant_id': pv.pk if pv else None,
            'product_id': pv.product_id if pv else None,
            'name': c.display_name,
            'sku': c.product_variant_sku or (pv.sku if pv else '') or '',
            'qty': c.quantity,
            'price': str(pv.price) if (pv and pv.price is not None) else '',
            'stock': (pv.stock if pv else 0) or 0,
            'photo': photo,
            'site_url': (settings.SITE_URL.rstrip('/') + '/product/' + slug + '?variant=' + str(pv.pk)) if (slug and pv) else '',
        })
    return JsonResponse({'items': result})


# ─── Nova Poshta proxy ───────────────────────────────────────────────────────

@staff_member_required
def nova_poshta_proxy(request):
    method = request.GET.get('method', '')
    if method not in ('searchSettlements', 'getWarehouses', 'searchSettlementStreets'):
        return JsonResponse({'error': 'forbidden'}, status=403)
    if not settings.NOVA_POSHTA_API_KEY:
        return JsonResponse({'error': 'NP key is not configured'}, status=503)

    if method == 'searchSettlementStreets':
        settlement_ref = (request.GET.get('city_ref', '') or '').strip()
        if not settlement_ref or len(settlement_ref) > 64:
            return JsonResponse({'error': 'invalid city_ref'}, status=400)
        q = (request.GET.get('q', '') or '').strip()[:128]
        if len(q) < 2:
            return JsonResponse({'success': True, 'data': [{'Addresses': []}]})
        # Shared cache key with the public proxy — the CRM picks up the warm checkout cache.
        cache_key = f'np:str:{settlement_ref}:{q.lower()}'
        cached = cache.get(cache_key)
        if cached is not None:
            return JsonResponse(cached)
        res = _np_call('searchSettlementStreets',
                       {'StreetName': q, 'SettlementRef': settlement_ref, 'Limit': 12, 'Page': 1})
        if res.get('success'):
            cache.set(cache_key, res, NP_STREETS_CACHE_TTL)
        return JsonResponse(res)

    if method == 'getWarehouses':
        city_ref = (request.GET.get('city_ref', '') or '').strip()
        if not city_ref or len(city_ref) > 64:
            return JsonResponse({'error': 'invalid city_ref'}, status=400)
        # Shared cache key with the public proxy (nova_poshta_public_proxy) — the CRM picks up
        # cities warmed by warmup_np_cache and does not do full pagination (~13s for Kyiv) every
        # time.
        cache_key = f'np:wh:{city_ref}'
        data = cache.get(cache_key)
        if data is None:
            fetched = _np_get_all_warehouses(settings.NOVA_POSHTA_API_KEY, city_ref)
            if fetched:
                cache.set(cache_key, fetched, NP_WH_CACHE_TTL)
                data = fetched
            elif fetched is None:
                return JsonResponse({'success': False, 'data': [], 'error': 'NP unavailable'}, status=502)
            else:
                return JsonResponse({'success': True, 'data': []})
        return JsonResponse({'success': True, 'data': data})

    q = (request.GET.get('q', '') or '').strip()[:128]
    return JsonResponse(_np_call(method, {'CityName': q, 'Limit': 12, 'Page': 1}))


# ─── Invoice processing helpers ──────────────────────────────────────────────

def _cosine_sim(query_vec, db_matrix):
    """query_vec: (d,), db_matrix: (n, d) -> (n,)"""
    q = query_vec / (np.linalg.norm(query_vec) + 1e-10)
    norms = np.linalg.norm(db_matrix, axis=1, keepdims=True) + 1e-10
    return (db_matrix / norms) @ q


def _get_embeddings_batched(client, texts, task_type='RETRIEVAL_DOCUMENT', batch_size=100):
    from core.limits import enforce_gemini
    enforce_gemini(len(texts))  # embeddings — the only real money spent
    from google.genai import types
    all_embeddings = []
    for i in range(0, len(texts), batch_size):
        batch = texts[i:i + batch_size]
        result = client.models.embed_content(
            model='gemini-embedding-001',
            contents=batch,
            config=types.EmbedContentConfig(task_type=task_type)
        )
        all_embeddings.extend([e.values for e in result.embeddings])
        if i + batch_size < len(texts):
            time.sleep(0.4)
    return np.array(all_embeddings)


def _catalog_embeddings(client, variants):
    """variants — a list of dicts with 'id','name', sorted by id.
    Returns a matrix aligned with the order of variants.

    Vectors come from the DB index (core.embeddings). Previously the matrix was computed on the fly
    and cached in memory: any product rename reset the cache, and a run over
    the whole catalog does not fit the Gemini quota (~100 names/min) and failed with 429."""
    from core import embeddings as emb
    matrix, missing = emb.variant_matrix(client, [v['id'] for v in variants])
    if missing >= len(variants):
        raise RuntimeError(
            'The name index is empty — run "python manage.py warm_embeddings"'
        )
    return matrix


INVOICE_PROMPT = """Analyze the image of a goods invoice (it is written in Ukrainian).
Extract only the product information: names, quantities and, if present, the prices written by hand with a pen (these are the prices for the website).

JSON format:
{
 "invoice_date": "DD-MM-YYYY",
 "items": [
   {
     "name": "full product name without the article number",
     "quantity": number,
     "website_price": number|null
   }
 ]
}

Rules:
- If the product name contains an article number, remove it from the name.
- Ignore any prices other than the handwritten ones.
- Such handwritten prices may not exist for every product, so use null for the ones that have none.
- Ignore everything that is not part of the invoice.
- Return only JSON, with no explanations."""


# ─── Process invoice endpoint ────────────────────────────────────────────────

@staff_member_required
@require_POST
def process_invoice(request):
    from .models import InvoicePhoto, bag_item_payload as _bag_item_payload

    photos = request.FILES.getlist('photos[]')
    if not photos:
        photos = [request.FILES.get('photo')]
    photos = [p for p in photos if p]
    if not photos:
        return JsonResponse({'error': 'No photo'}, status=400)

    # Read all bytes before saving to DB (Django closes files after save)
    photos_data = []
    for photo in photos:
        photo.seek(0)
        photos_data.append((photo.read(), mimetypes.guess_type(photo.name)[0] or 'image/jpeg'))
        photo.seek(0)

    invoice = Invoice.objects.create(photo=photos[0])
    for extra_photo in photos[1:]:
        extra_photo.seek(0)
        InvoicePhoto.objects.create(invoice=invoice, photo=extra_photo)

    try:
        from core import llm
        # Vision OCR of an invoice via a Claude subscription (one or several photos).
        # Claude accepts jpeg/png/webp/gif; other mime types are converted to jpeg.
        _ALLOWED = {'image/jpeg', 'image/png', 'image/webp', 'image/gif'}
        images = [(b, mt if mt in _ALLOWED else 'image/jpeg') for b, mt in photos_data]
        # Dynamic operator hints (notes from a specific invoice) — mixed into the prompt.
        notes = (request.POST.get('notes') or '').strip()
        prompt = INVOICE_PROMPT
        if notes:
            prompt += ('\n\nAdditional instructions from the operator for THIS invoice (take them into account when '
                       'parsing; the JSON format and rules above remain mandatory):\n' + notes)
        raw = llm.images_to_json(images, prompt=prompt)
        # The model may return a list (one entry per photo) — merge into a single object.
        if isinstance(raw, list):
            invoice_data = {
                'invoice_date': next((r.get('invoice_date') for r in raw if r.get('invoice_date')), ''),
                'items': [item for r in raw for item in r.get('items', [])],
            }
        else:
            invoice_data = raw
    except Exception as e:
        invoice.delete()
        return JsonResponse({'error': f'AI error: {str(e)}'}, status=500)

    # Save invoice date
    date_str = invoice_data.get('invoice_date', '')
    if date_str:
        try:
            from datetime import datetime
            invoice.invoice_date = datetime.strptime(date_str, '%d-%m-%Y').date()
            invoice.save(update_fields=['invoice_date'])
        except Exception:
            pass

    invoice_items = invoice_data.get('items', [])
    if not invoice_items:
        return JsonResponse({'invoice_id': invoice.id, 'invoice_date': date_str, 'items': []})

    # Fetch all variants (stable order by id — needed for the matrix cache)
    variants = list(ProductVariant.objects.select_related('product').order_by('id').values(
        'id', 'name', 'price', 'stock'
    ))
    if not variants:
        return JsonResponse({'invoice_id': invoice.id, 'invoice_date': date_str, 'items': []})

    # Matching invoice names against variants is done via Gemini embeddings (Claude has no
    # embedding model), so we create the genai client separately here.
    if not settings.GEMINI_API_KEY:
        invoice.delete()
        return JsonResponse({'error': 'GEMINI_API_KEY is not configured'}, status=503)
    try:
        from google import genai
        client = genai.Client(api_key=settings.GEMINI_API_KEY)
    except Exception as e:
        invoice.delete()
        return JsonResponse({'error': f'Gemini initialization error: {str(e)}'}, status=500)

    # Vectorize variant names (cache — recomputed only when the catalog changes)
    try:
        db_matrix = _catalog_embeddings(client, variants)
    except Exception as e:
        invoice.delete()
        return JsonResponse({'error': f'Vectorization error: {str(e)}'}, status=500)

    # Pre-load name mappings for fast lookup
    all_invoice_names = [item['name'].strip().lower() for item in invoice_items]
    mappings = {
        m.invoice_name: m.variant_id
        for m in InvoiceNameMapping.objects.filter(invoice_name__in=all_invoice_names)
    }
    variants_by_id = {v['id']: v for v in variants}

    # Batch vectorization of queries: embed in a single call all names
    # that are not in the saved mappings (instead of N sequential requests).
    query_vecs = {}
    to_embed = [
        (i, item['name'])
        for i, item in enumerate(invoice_items)
        if not (item['name'].strip().lower() in mappings
                and mappings[item['name'].strip().lower()] in variants_by_id)
    ]
    if to_embed:
        try:
            embs = _get_embeddings_batched(
                client, [n for _, n in to_embed], task_type='RETRIEVAL_QUERY'
            )
            for (idx, _), vec in zip(to_embed, embs):
                query_vecs[idx] = np.array(vec)
        except Exception:
            pass

    # Match each invoice item
    # Cache of full variant objects (product + bag) for detecting a stock receipt of "own packaging".
    mv_cache = {}
    def _mv(vid):
        if vid not in mv_cache:
            mv_cache[vid] = (ProductVariant.objects
                             .select_related('product', 'bulk_source')
                             .filter(pk=vid).first())
        return mv_cache[vid]

    result_items = []
    for i, item in enumerate(invoice_items):
        name_key = item['name'].strip().lower()

        # Check saved mapping first
        if name_key in mappings and mappings[name_key] in variants_by_id:
            match = variants_by_id[mappings[name_key]]
            score = 1.0
        elif i in query_vecs:
            sims = _cosine_sim(query_vecs[i], db_matrix)
            best_idx = int(np.argmax(sims))
            score = float(sims[best_idx])
            match = variants[best_idx]
        else:
            match = variants[0]
            score = 0.0
        website_price = item.get('website_price')
        quantity = item.get('quantity', 1)

        inv_item = InvoiceItem.objects.create(
            invoice=invoice,
            invoice_name=item['name'],
            quantity=quantity,
            website_price=website_price,
            matched_variant_id=match['id'],
            similarity=score,
        )

        row = {
            'id': inv_item.id,
            'invoice_name': item['name'],
            'quantity': quantity,
            'website_price': str(website_price) if website_price is not None else '',
            'matched_variant_id': match['id'],
            'matched_variant_name': match['name'],
            'similarity': round(score, 3),
            'current_stock': match['stock'] or 0,
            'current_price': str(match['price']),
            'from_mapping': name_key in mappings,
        }
        mvobj = _mv(match['id'])
        row['product_name'] = (mvobj.product.name if (mvobj and mvobj.product_id) else match['name'])
        row.update(_bag_item_payload(mvobj, item['name'], quantity))
        result_items.append(row)

    return JsonResponse({
        'invoice_id': invoice.id,
        'invoice_date': invoice.invoice_date.strftime('%d.%m.%Y') if invoice.invoice_date else '',
        'photo_url': invoice.photo.url,
        'items': result_items,
    })


# ─── AI order parsing (photo/text → form draft) ───────────────────────

ORDER_PARSE_PROMPT = """You parse an order for an online shop. The input is a photo
and/or text with the order data (a screenshot of a chat, an e-mail, a voice message as text,
a photo of a note). The data may be incomplete: extract only what is really there and
leave the rest null. Do not make anything up.

Return ONLY JSON:
{
  "customer_name": "full name or null",
  "customer_phone": "phone in the format +380XXXXXXXXX, or as given, or null",
  "customer_email": "email or null",
  "delivery_type": "nova_poshta" | "pickup" | null,
  "delivery_city": "city or null",
  "delivery_warehouse": "branch/parcel locker/postcode or null",
  "delivery_address": "the full delivery line as one text (city + branch/address) or null",
  "payment_method": "monobank" | "cod" | "bank_transfer" | "cash" | "own_expense" | null,
  "comment": "the customer's wishes (time to call, etc.) or null",
  "items": [
    {"name": "product name as in the order", "quantity": number, "price": number|null}
  ]
}

Rules:
- delivery_type: the customer writes in Ukrainian; «самовивіз» (pickup) / «заберу сам» (I will pick it up myself) / «заберу у вас» (I will pick it up from you) / «зі складу» (from the warehouse) → pickup (then delivery_city, delivery_warehouse and delivery_address = null). Delivery by a carrier is Nova Poshta only → nova_poshta. If delivery is not mentioned at all → null.
- payment_method: «накладений» (cash on delivery) / «при отриманні» (on receipt) / «накладка» (COD, slang) → cod; «оплата на картку» (payment to a card) / «переказ» (transfer) → bank_transfer; «монобанк» / «оплата онлайн» (online payment) → monobank; «готівка» / «готівкою» / «кешем» (cash) / «оплачу на місці» (I will pay on the spot; usually with pickup) → cash; «за наш рахунок» (on us) / «за рахунок магазину» (at the shop's expense) / «подарунок» (a gift) → own_expense. If unclear → null.
- quantity: if not given — 1.
- Keep name as in the source (with weight/volume, if given): «Rooting powder 60 ml», «calcium nitrate». Do not normalize it to the catalog — the system will do that.
- Clean the phone and email of extra characters.
- If there are no items — items: []."""


# Cache of the catalog embeddings matrix for order matching. Separate from the
# core.embeddings index: that one holds only variants, while order matching covers variants +
# bundles, so the set of rows differs.
_ORDER_EMBED_CACHE = {'sig': None, 'matrix': None}

# Persistent on-disk cache of the catalog embeddings matrix: survives a
# process restart (otherwise the first order after a restart pays ~26s to vectorize
# the whole catalog via Gemini). We re-vectorize only when the set
# of (type, id, name) changes — names change rarely.
_EMBED_CACHE_DIR = os.path.join(settings.BASE_DIR, '.embed_cache')
_ORDER_EMBED_FILE = os.path.join(_EMBED_CACHE_DIR, 'order_catalog.npz')


def _entries_sig(entries):
    """Catalog signature that is stable across processes (hash() is unsuitable — the seed
    for strings is random on each run, so it does not work for a disk cache)."""
    h = hashlib.md5()
    for e in entries:
        h.update(('%s|%s|%s\n' % (e['type'], e['id'], e['name'])).encode('utf-8'))
    return h.hexdigest()


def _order_match_entries():
    """Catalog for matching order items: active variants (excluding service
    fallbacks) + bundles. Stable (type, id) order — needed for the cache."""
    from catalog.models import Bundle
    entries = []
    for v in (ProductVariant.objects.filter(is_supplier_fallback=False)
              .order_by('id').values('id', 'name')):
        entries.append({'type': 'variant', 'id': v['id'], 'name': v['name'] or ''})
    for b in Bundle.objects.order_by('id').values('id', 'name'):
        entries.append({'type': 'bundle', 'id': b['id'], 'name': b['name'] or ''})
    return entries


_ORDER_EMBED_REDIS_KEY = 'embed:order:catalog'  # shared between processes, survives a web restart


def _order_catalog_matrix(client, entries):
    sig = _entries_sig(entries)
    # 1) in-process memory cache
    if _ORDER_EMBED_CACHE['sig'] == sig and _ORDER_EMBED_CACHE['matrix'] is not None:
        return _ORDER_EMBED_CACHE['matrix']
    # 2) Redis — shared by all processes and survives a container restart
    #    (otherwise every cold start vectorizes the whole catalog and burns the daily
    #    Gemini limit within a few orders).
    try:
        cached = cache.get(_ORDER_EMBED_REDIS_KEY)
        if cached and cached.get('sig') == sig and cached.get('matrix') is not None:
            matrix = cached['matrix']
            _ORDER_EMBED_CACHE.update({'sig': sig, 'matrix': matrix})
            return matrix
    except Exception:
        pass
    # 3) on-disk cache (local development; may not persist in a container)
    try:
        if os.path.exists(_ORDER_EMBED_FILE):
            data = np.load(_ORDER_EMBED_FILE, allow_pickle=False)
            if str(data['sig']) == sig:
                matrix = data['matrix']
                _ORDER_EMBED_CACHE.update({'sig': sig, 'matrix': matrix})
                try:
                    cache.set(_ORDER_EMBED_REDIS_KEY, {'sig': sig, 'matrix': matrix}, None)
                except Exception:
                    pass
                return matrix
    except Exception:
        pass
    # 4) vectorize (once per catalog change) and cache everywhere
    matrix = _get_embeddings_batched(client, [e['name'] for e in entries]).astype('float32')
    _ORDER_EMBED_CACHE.update({'sig': sig, 'matrix': matrix})
    try:
        cache.set(_ORDER_EMBED_REDIS_KEY, {'sig': sig, 'matrix': matrix}, None)
    except Exception:
        pass
    try:
        os.makedirs(_EMBED_CACHE_DIR, exist_ok=True)
        np.savez(_ORDER_EMBED_FILE, matrix=matrix, sig=np.array(sig))
    except Exception:
        pass
    return matrix


def _order_variant_cards(ids, request):
    """id → variant card in the variant_search format (compatible with oiPickItem)."""
    out = {}
    if not ids:
        return out
    qs = (ProductVariant.objects.filter(pk__in=ids)
          .select_related('product__brand').prefetch_related('images', 'product__images'))
    for v in qs:
        photo = (
            v.images.order_by('sort_order', 'pk').first()
            or (v.product.images.filter(variant__isnull=True).order_by('sort_order', 'pk').first()
                if v.product_id else None)
            or (v.product.images.order_by('sort_order', 'pk').first() if v.product_id else None)
        )
        out[v.pk] = {
            'type': 'variant', 'id': v.pk, 'product_id': v.product_id,
            'name': v.name or (v.product.name if v.product_id else ''),
            'sku': v.sku or '', 'price': str(v.price), 'final_price': str(v.final_price_whole),
            'stock': v.stock,
            'packaging_kind': v.product.packaging_kind if v.product_id else '',
            'photo': request.build_absolute_uri(photo.image.url) if photo else None,
        }
    return out


def _order_bundle_cards(ids, request):
    from catalog.models import Bundle
    from catalog.views import _bundle_items_data
    out = {}
    if not ids:
        return out
    qs = Bundle.objects.filter(pk__in=ids).prefetch_related(
        'images',
        'items__product_variant__product',
        'items__product_variant__images',
        'items__product_variant__product__images',
    )
    for b in qs:
        photo = b.images.order_by('sort_order', 'pk').first()
        _price = b.get_price() or 0
        _stock = b.get_stock() or 0
        out[b.pk] = {
            'type': 'bundle', 'id': b.pk, 'product_id': None,
            'name': b.name, 'sku': b.sku or '',
            'price': str(_price), 'stock': _stock,
            'photo': request.build_absolute_uri(photo.image.url) if photo else None,
            'items': _bundle_items_data(b, request),
        }
    return out


def _match_order_items(items, request):
    """Adds up to 3 catalog candidates (with a similarity %) to each item.

    Matching uses Gemini embeddings (cosine). If there is no key or the vector failed —
    a text fallback (icontains on name/sku). Candidates are in the format that
    oiPickItem in the order form consumes directly."""
    entries = _order_match_entries()
    names = [(str(it.get('name') or '')).strip() for it in items]

    # ── query embeddings (batched) ──
    db_matrix, query_vecs = None, {}
    client = None
    if settings.GEMINI_API_KEY and entries:
        try:
            from google import genai
            client = genai.Client(api_key=settings.GEMINI_API_KEY)
            db_matrix = _order_catalog_matrix(client, entries)
        except Exception:
            client, db_matrix = None, None
    if client is not None and db_matrix is not None:
        idxs = [i for i, n in enumerate(names) if n]
        if idxs:
            try:
                embs = _get_embeddings_batched(
                    client, [names[i] for i in idxs], task_type='RETRIEVAL_QUERY')
                for i, vec in zip(idxs, embs):
                    query_vecs[i] = np.array(vec)
            except Exception:
                pass

    # ── collect top candidates (as (entry, score)) for all items ──
    per_item = []
    for i, name in enumerate(names):
        picks = []
        if i in query_vecs and db_matrix is not None:
            sims = _cosine_sim(query_vecs[i], db_matrix)
            for j in np.argsort(sims)[::-1][:3]:
                picks.append((entries[int(j)], float(sims[int(j)])))
        elif name:
            picks = _text_match_entries(name)
        per_item.append(picks)

    # ── bulk fetch of candidate cards (photo/price/stock) ──
    v_ids = {e['id'] for picks in per_item for e, _ in picks if e['type'] == 'variant'}
    b_ids = {e['id'] for picks in per_item for e, _ in picks if e['type'] == 'bundle'}
    vcards = _order_variant_cards(v_ids, request)
    bcards = _order_bundle_cards(b_ids, request)

    out = []
    for i, it in enumerate(items):
        qty = it.get('quantity') or it.get('qty') or 1
        try:
            qty = max(1, int(qty))
        except (TypeError, ValueError):
            qty = 1
        candidates = []
        for e, score in per_item[i]:
            card = vcards.get(e['id']) if e['type'] == 'variant' else bcards.get(e['id'])
            if not card:
                continue
            card = dict(card)
            card['score'] = round(max(0.0, min(1.0, score)), 3)
            candidates.append(card)
        out.append({
            'raw_name': str(it.get('name') or ''),
            'qty': qty,
            'price': it.get('price'),
            'candidates': candidates,
        })
    return out


def _text_match_entries(name):
    """Fallback without embeddings: top-3 (entry, score) by icontains name/sku.
    score=0.6 — so the UI files it under "medium confidence" (show
    candidates).
    If the full name finds nothing — try the first meaningful word (an order name often
    includes a volume: "Rooting powder 250 ml" → search "Rooting")."""
    from catalog.models import Bundle
    from django.db.models import Q

    def _search(q):
        picks = []
        for v in (ProductVariant.objects.filter(is_supplier_fallback=False)
                  .filter(Q(name__icontains=q) | Q(sku__icontains=q))[:3]):
            picks.append(({'type': 'variant', 'id': v.pk, 'name': v.name or ''}, 0.6))
        if len(picks) < 3:
            for b in Bundle.objects.filter(Q(name__icontains=q) | Q(sku__icontains=q))[:3 - len(picks)]:
                picks.append(({'type': 'bundle', 'id': b.pk, 'name': b.name or ''}, 0.6))
        return picks

    name = (name or '').strip()
    if not name:
        return []
    picks = _search(name)
    if not picks:
        first = re.split(r'[\s,]+', name)[0]
        if len(first) >= 3 and first.lower() != name.lower():
            picks = _search(first)
    return picks


@staff_member_required
@require_POST
def parse_order(request):
    """Photo/text of an order → a draft for the CRM order creation form.
    Saves nothing: returns the recognized fields + items with candidates."""
    photos = [p for p in request.FILES.getlist('photos[]') if p]
    text = (request.POST.get('text') or '').strip()
    if not photos and not text:
        return JsonResponse({'error': 'Add a photo or the text of the order'}, status=400)

    from core import llm
    _t0 = time.perf_counter()
    try:
        if photos:
            _ALLOWED = {'image/jpeg', 'image/png', 'image/webp', 'image/gif'}
            images = []
            for p in photos:
                p.seek(0)
                mt = mimetypes.guess_type(p.name)[0] or 'image/jpeg'
                images.append((p.read(), mt if mt in _ALLOWED else 'image/jpeg'))
            prompt = ORDER_PARSE_PROMPT
            if text:
                prompt += '\n\nAdditional text for the photo:\n' + text
            # sonnet — more stable and more accurate than haiku at extracting the address (city/
            # Nova Poshta branch) and reading photos. Speed is now held by the embeddings cache,
            # not the model, so we can afford the more expensive/higher-quality one.
            raw = llm.images_to_json(images, prompt=prompt, model='sonnet')
        else:
            raw = llm.text_to_json(ORDER_PARSE_PROMPT + '\n\n---\n\n' + text, model='sonnet')
    except Exception as e:
        return JsonResponse({'error': f'AI error: {e}'}, status=500)
    _t1 = time.perf_counter()

    if isinstance(raw, list):
        raw = raw[0] if raw else {}
    if not isinstance(raw, dict):
        return JsonResponse({'error': 'The AI returned an invalid response'}, status=500)

    items = raw.get('items') or []
    matched = _match_order_items(items, request) if items else []
    _t2 = time.perf_counter()
    log.warning('parse_order timing: LLM=%.1fs  matching=%.1fs  total=%.1fs',
                _t1 - _t0, _t2 - _t1, _t2 - _t0)

    def _s(key):
        v = raw.get(key)
        return '' if v is None else str(v).strip()

    return JsonResponse({
        'customer_name': _s('customer_name'),
        'customer_phone': _s('customer_phone'),
        'customer_email': _s('customer_email'),
        'delivery_type': _s('delivery_type'),
        'delivery_city': _s('delivery_city'),
        'delivery_warehouse': _s('delivery_warehouse'),
        'delivery_address': _s('delivery_address'),
        'payment_method': _s('payment_method'),
        'comment': _s('comment'),
        'items': matched,
    })


# ─── Write-off ─────────────────────────────────────────────────────

WRITEOFF_PARSE_PROMPT = """You parse a list of products to be written off from the stock
of an online shop (defects, spoilage, shortage, mix-ups, etc.). The input is a photo
and/or text (a note, a photo of the products, a handwritten list). Extract only the products and
quantities that are really being written off. Do not make anything up.

Return ONLY JSON:
{
  "items": [
    {"name": "product name as in the source", "quantity": number}
  ]
}

Rules:
- quantity: if not given — 1.
- Keep name as in the source (with weight/volume, if given): «Rooting powder 60 ml». Do not normalize it to the catalog — the system will do that.
- If there are no items — items: []."""


@staff_member_required
@require_POST
def parse_writeoff(request):
    """Photo/text of a write-off → a draft of items with catalog candidates.
    Saves nothing (like parse_order) — only recognizes and matches."""
    photos = [p for p in request.FILES.getlist('photos[]') if p]
    text = (request.POST.get('text') or '').strip()
    if not photos and not text:
        return JsonResponse({'error': 'Add a photo or the text of the write-off'}, status=400)

    from core import llm
    try:
        if photos:
            _ALLOWED = {'image/jpeg', 'image/png', 'image/webp', 'image/gif'}
            images = []
            for p in photos:
                p.seek(0)
                mt = mimetypes.guess_type(p.name)[0] or 'image/jpeg'
                images.append((p.read(), mt if mt in _ALLOWED else 'image/jpeg'))
            prompt = WRITEOFF_PARSE_PROMPT
            if text:
                prompt += '\n\nAdditional text:\n' + text
            raw = llm.images_to_json(images, prompt=prompt, model='sonnet')
        else:
            raw = llm.text_to_json(WRITEOFF_PARSE_PROMPT + '\n\n---\n\n' + text, model='sonnet')
    except Exception as e:
        return JsonResponse({'error': f'AI error: {e}'}, status=500)

    if isinstance(raw, list):
        raw = raw[0] if raw else {}
    if not isinstance(raw, dict):
        return JsonResponse({'error': 'The AI returned an invalid response'}, status=500)

    items = raw.get('items') or []
    # _match_order_items also matches bundles — for a write-off this is not an error (a bundle
    # is also a valid candidate, though a rare one); we leave it as is for simplicity.
    matched = _match_order_items(items, request) if items else []
    return JsonResponse({'items': matched})


@staff_member_required
@require_POST
def confirm_writeoff(request):
    """Apply the confirmed write-off items: decreases stock and saves an
    audit record (StockWriteOff). The same stock write path as the invoice
    stock receipt (confirm_invoice), just with a negative delta — own packaging on a
    bag goes through BulkSource.apply_delta (otherwise variant.save() would recompute
    stock from the bag and wipe a direct write), the rest — directly via variant.stock.
    No revert (unlike an invoice) — deliberately simpler."""
    from catalog.models import BulkSource, StockWriteOff, StockWriteOffItem

    data = json.loads(request.body)
    raw_items = data.get('items', [])
    note = (data.get('note') or '').strip()
    source_text = (data.get('source_text') or '').strip()

    with transaction.atomic():
        wo = StockWriteOff.objects.create(
            note=note, source_text=source_text,
            created_by=request.user if request.user.is_authenticated else None,
        )
        applied = 0
        for item in raw_items:
            variant_id = item.get('variant_id')
            if not variant_id:
                continue
            try:
                quantity = max(1, int(item.get('quantity') or 1))
            except (TypeError, ValueError):
                continue
            try:
                # select_for_update(of=('self',)) — the same technique as in
                # confirm_invoice: lock only the variant row, not the bulk_source join.
                variant = ProductVariant.objects.select_for_update(of=('self',)).select_related(
                    'product', 'bulk_source').get(pk=variant_id)
            except ProductVariant.DoesNotExist:
                continue

            # Own packaging on a bag: a direct decrease of variant.stock will not hold —
            # save() recomputes it from the bag (the same invariant as in the stock receipt).
            _bag_variant = bool(
                getattr(variant.product, 'packaging_kind', None) == 'own'
                and variant.bulk_source_id and variant.unit_size and not variant.stock_manual
            )
            if _bag_variant:
                amount = variant.bulk_source.effective_unit(variant.unit_size) * quantity
                BulkSource.apply_delta(variant.bulk_source_id, -amount)
            else:
                variant.stock = max(0, (variant.stock or 0) - quantity)
                variant.save(update_fields=['stock'])

            StockWriteOffItem.objects.create(
                write_off=wo, variant_id=variant_id,
                variant_name=variant.name or '', variant_sku=variant.sku or '',
                quantity=quantity,
            )
            applied += 1

        if not applied:
            transaction.set_rollback(True)
            return JsonResponse({'error': 'No valid items'}, status=400)

    return JsonResponse({'ok': True, 'write_off_id': wo.id, 'applied': applied})


@staff_member_required
@require_POST
def pending_stock_returns(request):
    """One-shot reads and clears the list of products automatically returned to stock
    via a product/bundle replacement in an order item (OrderAdmin.save_formset →
    _stash_pending_stock_returns). The frontend calls this when the order page loads,
    to show the return confirmation modal if needed."""
    items = request.session.pop('pending_stock_returns', None) or []
    if items:
        from catalog.models import BulkSource
        bag_ids = {it['bag_id'] for it in items if it.get('bag_id')}
        variant_ids = {it['variant_id'] for it in items if not it.get('bag_id')}
        bag_stock = ({b.pk: str(b.quantity) for b in BulkSource.objects.filter(pk__in=bag_ids)}
                     if bag_ids else {})
        variant_stock = ({v.pk: v.stock for v in ProductVariant.objects.filter(pk__in=variant_ids)}
                         if variant_ids else {})
        for it in items:
            it['current_stock'] = (bag_stock.get(it['bag_id']) if it.get('bag_id')
                                    else variant_stock.get(it['variant_id']))
    return JsonResponse({'items': items})


@staff_member_required
@require_POST
def confirm_stock_returns(request):
    """Applies the manager's decision: how much of what was automatically returned to stock
    (on a product replacement in the order) to actually keep. The manager may keep MORE
    than was automatically returned (e.g. knew that less was actually written off) —
    the difference in both directions is written the same way as a write-off
    (BulkSource.apply_delta / variant.stock), just with the opposite sign.

    original_amount/bag_id come from the frontend (the same it received via
    /pending-stock-returns/) — we do NOT read the session here: pending-stock-returns has already
    released it once so that the modal does not pop up again on skip/close."""
    from catalog.models import BulkSource
    from decimal import Decimal, InvalidOperation

    data = json.loads(request.body)
    applied = 0
    with transaction.atomic():
        for dec in (data.get('decisions') or []):
            try:
                variant_id = int(dec.get('variant_id'))
                original = Decimal(str(dec.get('original_amount')))
                keep = Decimal(str(dec.get('keep_amount')))
            except (TypeError, ValueError, InvalidOperation):
                continue
            if keep < 0:
                keep = Decimal('0')
            delta = original - keep
            if delta == 0:
                continue
            bag_id = dec.get('bag_id')
            if bag_id:
                BulkSource.apply_delta(bag_id, -delta)
            else:
                variant = ProductVariant.objects.select_for_update(of=('self',)).filter(pk=variant_id).first()
                if variant:
                    variant.stock = max(0, (variant.stock or 0) - int(delta))
                    variant.save(update_fields=['stock'])
            applied += 1
    return JsonResponse({'ok': True, 'applied': applied})


# ─── Confirm invoice endpoint ─────────────────────────────────────────────────

@staff_member_required
@require_POST
def confirm_invoice(request):
    from django.db import transaction
    data = json.loads(request.body)
    invoice_id = data.get('invoice_id')
    items = data.get('items', [])

    try:
        invoice = Invoice.objects.get(pk=invoice_id)
    except Invoice.DoesNotExist:
        return JsonResponse({'error': 'Invoice not found'}, status=404)

    if invoice.status == 'confirmed':
        return JsonResponse({'error': 'Already confirmed'}, status=400)

    skipped_ids = data.get('skipped_ids', [])
    skipped_ids_set = set(int(x) for x in skipped_ids if x)
    if skipped_ids_set:
        InvoiceItem.objects.filter(pk__in=skipped_ids_set).update(confirmed=False)
        skipped_names = list(
            InvoiceItem.objects.filter(pk__in=skipped_ids_set)
            .exclude(invoice_name='').values_list('invoice_name', flat=True)
        )
        if skipped_names:
            InvoiceNameMapping.objects.filter(
                invoice_name__in=[n.strip().lower() for n in skipped_names]
            ).delete()

    # Snapshot of stock/price BEFORE this invoice's edits — one per variant.
    # Without it, duplicates (the same product on two lines or from two photos) would take a
    # "step" snapshot (stock already increased), and the revert would restore the intermediate
    # value instead of the original. The snapshot is taken only on first touch.
    original_by_variant = {}

    with transaction.atomic():
        # Lock the invoice and RE-check the status under the lock — otherwise two parallel
        # confirms (two tabs / POST retry) would both pass check-then-act and double the
        # receipt (both the bag and piece stock).
        invoice = Invoice.objects.select_for_update().get(pk=invoice_id)
        if invoice.status == 'confirmed':
            return JsonResponse({'error': 'Already confirmed'}, status=400)
        for item in items:
            variant_id = item.get('variant_id')
            quantity = int(item.get('quantity') or 0)
            new_stock = item.get('new_stock')
            price_raw = item.get('price') or None
            item_id = item.get('id')

            # Safety: never process a skipped item even if JS sent it
            if item_id and int(item_id) in skipped_ids_set:
                continue

            if not variant_id:
                continue

            try:
                # of=('self',) — lock only the variant row (not the nullable bulk_source join),
                # so that a parallel customer sale (_consume_variant also takes
                # select_for_update on this row) is serialized and is not lost to last-write-wins
                # in the read-modify-write of piece stock below.
                variant = ProductVariant.objects.select_for_update(of=('self',)).select_related(
                    'product', 'bulk_source').get(pk=variant_id)
            except ProductVariant.DoesNotExist:
                continue

            # A variant with bag accounting (own + bag + unit_size, not manual): a piece-based
            # write to variant.stock will NOT hold — ProductVariant.save recomputes stock from
            # the bag and wipes it. So the receipt of such a variant ALWAYS goes into the bag, even
            # if the frontend did not set to_bag (e.g. after a manual re-match).
            _bag_variant = bool(
                getattr(variant.product, 'packaging_kind', None) == 'own'
                and variant.bulk_source_id and variant.unit_size and not variant.stock_manual
            )
            # ── Receipt of "own packaging": top up the weight in the BAG, not in
            # variant.stock ──
            if item.get('to_bag') or _bag_variant:
                from catalog.models import BulkSource
                from .models import parse_pack_weight
                from django.db.models import F
                inv_item = InvoiceItem.objects.filter(pk=item_id).first() if item_id else None
                inv_name = inv_item.invoice_name if inv_item else ''
                parsed = parse_pack_weight(inv_name)
                # Bag: the variant's own, otherwise the PRODUCT's existing bag (do not spawn
                # duplicates — a representative of a collapsed own product might come without
                # bulk_source).
                bag = variant.bulk_source or BulkSource.objects.filter(
                    product_id=variant.product_id).order_by('pk').first()
                # How much to top up: an explicit value from the form, otherwise weight from the
                # name × quantity —
                # ONLY if the unit matches the bag's (otherwise we do not corrupt the bag with
                # another unit).
                try:
                    bag_amount = Decimal(str(item.get('bag_amount') or 0))
                except Exception:
                    bag_amount = Decimal('0')
                if bag_amount <= 0 and parsed:
                    target_unit = bag.unit if bag else parsed[1]
                    if parsed[1] == target_unit:
                        bag_amount = parsed[0] * Decimal(str(quantity))
                if bag_amount < 0:
                    bag_amount = Decimal('0')
                if bag is None:
                    unit = parsed[1] if parsed else 'g'
                    bag = BulkSource.objects.create(
                        product=variant.product, name='Bag', quantity=0, unit=unit)
                    # Seed the bag with the existing piece stock by weight — exactly effective_unit
                    # per pack, so that the stock recomputation returns the SAME stock (not a floor
                    # with error).
                    seed = Decimal(str(variant.stock or 0)) * bag.effective_unit(variant.unit_size or 0)
                    if seed > 0:
                        BulkSource.set_quantity(bag.pk, seed)
                        bag.refresh_from_db()
                    variant.bulk_source = bag
                    variant.save(update_fields=['bulk_source'])
                if bag_amount:
                    # apply_delta itself resyncs the stock of all the bag's packagings
                    BulkSource.apply_delta(bag.pk, bag_amount)
                    bag.refresh_from_db()
                # We do NOT touch the variant price: in the invoice, for a bag it is the WHOLESALE
                # price per
                # package (e.g. 1329 UAH/5 kg), not the retail price of a pack. We store
                # website_price only for the record.
                if item_id:
                    if inv_item and inv_item.invoice_name:
                        name_key = inv_item.invoice_name.strip().lower()
                        if bool(item.get('remember_name', False)):
                            InvoiceNameMapping.objects.update_or_create(
                                invoice_name=name_key, defaults={'variant_id': variant_id})
                        else:
                            InvoiceNameMapping.objects.filter(invoice_name=name_key).delete()
                    InvoiceItem.objects.filter(pk=item_id).update(
                        matched_variant_id=variant_id,
                        quantity=quantity,
                        website_price=Decimal(str(price_raw)) if price_raw else None,
                        confirmed=True,
                        applied_bag_id=bag.pk,
                        applied_bag_amount=bag_amount,
                        applied_delta=None,
                        original_stock=None,
                        manually_edited=bool(item.get('manually_edited', False)),
                    )
                continue

            if variant_id in original_by_variant:
                original_stock, original_price = original_by_variant[variant_id]
            else:
                original_stock = variant.stock or 0
                original_price = variant.price
                original_by_variant[variant_id] = (original_stock, original_price)

            stock_before = variant.stock or 0
            if new_stock is not None:
                variant.stock = int(new_stock)
            else:
                variant.stock = (variant.stock or 0) + quantity
            applied_delta = (variant.stock or 0) - stock_before
            update_fields = ['stock']
            if price_raw:
                try:
                    variant.price = Decimal(str(price_raw))
                    update_fields.append('price')
                except Exception:
                    pass
            variant.save(update_fields=update_fields)

            manually_edited = bool(item.get('manually_edited', False))
            # fail-safe: save the name mapping ONLY on an explicit remember_name=true.
            # If the key is absent (old/out-of-sync frontend) — do NOT save,
            # otherwise names silently end up in the reference without the user's intent.
            remember_name = bool(item.get('remember_name', False))
            if item_id:
                inv_item = InvoiceItem.objects.filter(pk=item_id).first()
                if inv_item and inv_item.invoice_name:
                    name_key = inv_item.invoice_name.strip().lower()
                    if remember_name:
                        InvoiceNameMapping.objects.update_or_create(
                            invoice_name=name_key,
                            defaults={'variant_id': variant_id},
                        )
                    else:
                        InvoiceNameMapping.objects.filter(invoice_name=name_key).delete()
                InvoiceItem.objects.filter(pk=item_id).update(
                    matched_variant_id=variant_id,
                    quantity=quantity,
                    website_price=Decimal(str(price_raw)) if price_raw else None,
                    confirmed=True,
                    original_stock=original_stock,
                    original_price=original_price,
                    applied_delta=applied_delta,
                    manually_edited=manually_edited,
                )

        from django.utils import timezone
        invoice.status = 'confirmed'
        invoice.confirmed_at = timezone.now()
        invoice.save(update_fields=['status', 'confirmed_at'])
    return JsonResponse({'ok': True})


# ─── Revert invoice endpoint ─────────────────────────────────────────────────

@staff_member_required
@require_POST
def revert_invoice(request):
    data = json.loads(request.body)
    invoice_id = data.get('invoice_id')
    revert_price = data.get('revert_price', False)
    revert_mapping = data.get('revert_mapping', False)

    try:
        invoice = Invoice.objects.get(pk=invoice_id)
    except Invoice.DoesNotExist:
        return JsonResponse({'error': 'Invoice not found'}, status=404)

    if invoice.status != 'confirmed':
        return JsonResponse({'error': 'Invoice is not confirmed'}, status=400)

    with transaction.atomic():
        # Invoice lock + re-check of the status under the lock — mirrors confirm_invoice
        # (views.py ~1786-1792). Without it two parallel reverts (double click / POST retry)
        # both pass check-then-act and subtract the receipt TWICE: variant.stock and the
        # bag are permanently understated by the whole receipt of the invoice. The second revert now
        # waits on the
        # row lock, sees status='reverted' and is rejected.
        invoice = Invoice.objects.select_for_update().get(pk=invoice_id)
        if invoice.status != 'confirmed':
            return JsonResponse({'error': 'Invoice is not confirmed'}, status=400)

        if revert_mapping:
            names = [
                it.invoice_name.strip().lower()
                for it in invoice.items.filter(confirmed=True)
                if it.invoice_name
            ]
            if names:
                InvoiceNameMapping.objects.filter(invoice_name__in=names).delete()
                invoice.items.filter(confirmed=True, similarity=1.0).update(similarity=None)

        # Group by variant: an invoice may contain the same variant on several
        # lines (duplicates). The stock state is decreased by EXACTLY the applied receipt delta,
        # rather than restoring the absolute original_stock — otherwise sales that happened between
        # confirmation and revert would be wiped out (phantom stock → selling into negative stock).
        from collections import defaultdict
        delta_by_variant = defaultdict(int)   # new invoices: sum of applied deltas
        abs_by_variant = {}                   # legacy (applied_delta IS NULL): absolute revert
        price_by_variant = {}                 # price restore
        bag_delta = defaultdict(Decimal)      # receipt of "own packaging": how much was topped up into each bag

        for item in invoice.items.filter(confirmed=True).select_related('matched_variant'):
            # Receipt into a bag: subtract exactly the topped-up weight (as with stock — not an
            # absolute value).
            if item.applied_bag_id and item.applied_bag_amount:
                bag_delta[item.applied_bag_id] += item.applied_bag_amount
                continue
            variant = item.matched_variant
            if not variant:
                continue
            if item.applied_delta is not None:
                delta_by_variant[variant.pk] += item.applied_delta
            elif item.original_stock is not None:
                abs_by_variant[variant.pk] = item.original_stock
            if revert_price and item.original_price is not None:
                price_by_variant[variant.pk] = item.original_price

        # Revert of bag top-ups: quantity -= topped-up amount (clamped to ≥0), then recompute the
        # packagings.
        if bag_delta:
            from catalog.models import BulkSource
            for bag_id, amount in bag_delta.items():
                BulkSource.apply_delta(bag_id, -amount)

        touched = set(delta_by_variant) | set(abs_by_variant) | set(price_by_variant)
        for vid in touched:
            try:
                # Lock the variant row — so that a parallel sale during the revert is not lost to
                # last-write-wins (read-modify-write of stock below). We are already inside
                # transaction.atomic().
                variant = ProductVariant.objects.select_for_update().get(pk=vid)
            except ProductVariant.DoesNotExist:
                continue
            update_fields = []
            if vid in delta_by_variant:
                variant.stock = max(0, (variant.stock or 0) - delta_by_variant[vid])
                update_fields.append('stock')
            elif vid in abs_by_variant:
                variant.stock = abs_by_variant[vid]
                update_fields.append('stock')
            if vid in price_by_variant:
                variant.price = price_by_variant[vid]
                update_fields.append('price')
            if update_fields:
                variant.save(update_fields=update_fields)

        from django.utils import timezone
        invoice.status = 'reverted'
        invoice.reverted_at = timezone.now()
        invoice.save(update_fields=['status', 'reverted_at'])
    return JsonResponse({'ok': True})


# ─── Inline field update ─────────────────────────────────────────────────────

@staff_member_required
@require_POST
def update_order_field(request):
    from .models import Order
    data = json.loads(request.body)
    order_id = data.get('id')
    field = data.get('field')
    value = data.get('value')

    # Allowed enum fields — value must be one of choices.
    enum_fields = {
        'status': dict(Order.STATUS_CHOICES),
        'payment_status': dict(Order.PAYMENT_STATUS_CHOICES),
    }
    # Allowed free-text fields — any string (empty clears the value).
    text_fields = {'manager_comment', 'source'}

    if field in enum_fields:
        if value not in enum_fields[field]:
            return JsonResponse({'error': 'Invalid value'}, status=400)
    elif field in text_fields:
        if value is None:
            value = ''
        if not isinstance(value, str):
            return JsonResponse({'error': 'Invalid value type'}, status=400)
        value = value.strip()
        if len(value) > 5000:
            return JsonResponse({'error': 'Too long'}, status=400)
    else:
        return JsonResponse({'error': 'Invalid field'}, status=400)

    try:
        order = Order.objects.get(pk=order_id)
    except Order.DoesNotExist:
        return JsonResponse({'error': 'Not found'}, status=404)

    setattr(order, field, value)
    try:
        order.save(update_fields=[field])
    except ValidationError as e:
        # Order.save() — state machine: invalid status/payment transition.
        msg = '; '.join(e.messages) if hasattr(e, 'messages') else str(e)
        return JsonResponse({'error': msg}, status=400)
    return JsonResponse({'ok': True})


# ─── Generate packing slips docx ─────────────────────────────────────────────

def export_orders_qs(ids):
    """Export orders in CANONICAL order.

    There is one order for both files — both the docx with orders and the PDF with
    instructions. If they were computed separately, the stack of sheets would eventually diverge
    from the document and the packer would insert the wrong instruction without noticing.
    """
    return Order.objects.filter(pk__in=ids).prefetch_related(
        'items__product_variant__product',
        'items__bundle_components__product_variant__product',
        'items__bundle__items__product_variant__product',
    ).order_by('created_at')


def build_export_plan(ids, excluded_item_ids=None):
    """[(sequence number, order, [items])] — what will actually be printed.

    The number is the same one docx prints as "Order No. N" and that goes into the
    header
    of the instruction sheet. Orders without any visible item are dropped, courses
    are always excluded (intangible item).
    """
    excluded = set(excluded_item_ids or ())
    orders = export_orders_qs(ids)
    for o in orders:
        for it in o.items.all():
            if it.course_id:
                excluded.add(it.pk)
    plan = []
    for o in orders:
        items = [it for it in o.items.all() if it.pk not in excluded]
        if items:
            plan.append((len(plan) + 1, o, items))
    return plan, excluded


@staff_member_required
@require_POST
def generate_docx(request):
    from io import BytesIO
    from datetime import datetime
    from docx import Document
    from docx.enum.style import WD_STYLE_TYPE
    from docx.enum.text import WD_LINE_SPACING
    from docx.shared import Pt, Inches
    from .models import Order, OrderItem, OrderItemBundleComponent
    from .models import (_consume as _stock_consume, _restore as _stock_restore,
                         _ensure_bundle_components_snapshot, _stock_consumable)
    from catalog.models import Bundle

    ids = request.POST.getlist('ids[]')
    if not ids:
        from django.http import HttpResponseBadRequest
        return HttpResponseBadRequest('No orders')

    excluded_ids_raw = request.POST.getlist('excluded_item_ids[]')
    excluded_item_ids = set()
    for v in excluded_ids_raw:
        try:
            excluded_item_ids.add(int(v))
        except (TypeError, ValueError):
            continue

    def _id_set(name):
        out = set()
        for v in request.POST.getlist(name):
            try:
                out.add(int(v))
            except (TypeError, ValueError):
                continue
        return out

    # Per-order options from the modal.
    pay_we_order_ids = _id_set('pay_we_order_ids[]')
    note_order_ids = _id_set('note_order_ids[]')

    # ── Inline edits from the modal ──
    # edits_json structure:
    # {
    #   "<order_item_id>": {
    #       "variant_id": int,             # for a non-bundle — replace the ProductVariant
    #       "quantity": int,               # qty of the whole item (bundle or non-bundle)
    #       "bundle_id": int,              # for a bundle — replace the Bundle (re-applying the
    #       components)
    #       "components": {
    #           "<comp_id>": {"variant_id": int?, "quantity": int?, "delete": bool},
    #           "new": [{"variant_id": int, "quantity": int}, ...]
    #       }
    #   }
    # }
    edits_raw = request.POST.get('edits_json', '').strip()
    edits_map = {}
    if edits_raw:
        try:
            parsed = json.loads(edits_raw)
            if isinstance(parsed, dict):
                for k, v in parsed.items():
                    try:
                        iid = int(k)
                    except (TypeError, ValueError):
                        continue
                    if not isinstance(v, dict):
                        continue
                    edits_map[iid] = v
        except json.JSONDecodeError:
            pass

    def _to_int(x):
        try:
            return int(x)
        except (TypeError, ValueError):
            return None

    # Fields that actually modify the DB (trigger lock/transaction).
    # `attach_photo` is only a flag for the docx and does not touch the DB.
    _DB_EDIT_KEYS = ('variant_id', 'quantity', 'bundle_id')
    _COMP_DB_KEYS = ('variant_id', 'quantity', 'delete')

    def _has_db_edits(rec):
        if not isinstance(rec, dict):
            return False
        if any(k in rec for k in _DB_EDIT_KEYS):
            return True
        comps = rec.get('components') if isinstance(rec.get('components'), dict) else None
        if comps:
            for c in comps.values():
                if isinstance(c, dict) and any(k in c for k in _COMP_DB_KEYS):
                    return True
        return False

    db_edit_ids = [pk for pk, rec in edits_map.items() if _has_db_edits(rec)]

    affected_order_ids = set()
    if db_edit_ids:
        with transaction.atomic():
            # Order cache for the _stock_consumable check (an UNPAID monobank order
            # does not hold stock, so editing its items must not write off stock levels).
            _orders_by_id = {o.pk: o for o in Order.objects.filter(pk__in=[int(i) for i in ids])}

            def _reconsume(it):
                """Write the item off again ONLY if stock should be held right now. The emergency
                branches (edit target not found) used to call _stock_consume unconditionally;
                for an unpaid monobank order that wrote off stock prematurely (same gate
                as on the normal path below)."""
                if _stock_consumable(_orders_by_id.get(it.order_id)):
                    _stock_consume(it)
            # `of=('self',)` — lock only OrderItem, without a LEFT JOIN on the nullable
            # FK (bundle). Otherwise Postgres fails with
            # «FOR UPDATE cannot be applied to the nullable side of an outer join».
            items_qs = (
                OrderItem.objects
                .select_for_update(of=('self',))
                .filter(pk__in=db_edit_ids, order_id__in=[int(i) for i in ids])
                .select_related('product_variant', 'bundle')
            )
            for item in items_qs:
                rec = edits_map.get(item.pk) or {}
                if not isinstance(rec, dict):
                    continue
                new_qty       = _to_int(rec.get('quantity'))
                if new_qty is not None and new_qty <= 0:
                    new_qty = None
                new_variant_id      = _to_int(rec.get('variant_id'))
                new_bundle_id       = _to_int(rec.get('bundle_id'))
                comp_edits          = rec.get('components') if isinstance(rec.get('components'), dict) else {}

                # Determine the item's target type from edits
                target_type = None  # 'variant' | 'bundle' | None (no type change)
                if new_variant_id is not None:
                    target_type = 'variant'
                elif new_bundle_id is not None:
                    target_type = 'bundle'
                current_type = 'bundle' if item.bundle_id else 'variant'
                type_changing = bool(target_type) and target_type != current_type

                # Will there be a real change?
                changed = False
                if type_changing:
                    changed = True
                elif target_type == 'variant' and new_variant_id != item.product_variant_id:
                    changed = True
                elif target_type == 'bundle' and new_bundle_id != item.bundle_id:
                    changed = True
                if new_qty is not None and new_qty != item.quantity:
                    changed = True
                if current_type == 'bundle' and not type_changing and comp_edits:
                    changed = True
                if not changed:
                    continue

                # 1. Return stock from the item's current state
                _stock_restore(item)

                rebuild_snapshot_from_template = False
                drop_bundle_components = (current_type == 'bundle' and target_type == 'variant')

                # ── Item type change ──
                if type_changing and target_type == 'bundle':
                    # Product → Bundle
                    target_bundle = Bundle.objects.filter(pk=new_bundle_id).first()
                    if not target_bundle:
                        _reconsume(item); continue
                    item.product_variant = None
                    item.product_variant_id = None
                    item.bundle = target_bundle
                    item.bundle_id = target_bundle.pk
                    item.product_variant_name = target_bundle.name
                    item.product_variant_sku = target_bundle.sku or ''
                    try:
                        item.price = target_bundle.get_price() or 0
                    except Exception:
                        pass
                    item.was_dropshipped = False
                    rebuild_snapshot_from_template = True
                elif type_changing and target_type == 'variant':
                    # Bundle → Product
                    new_variant = ProductVariant.objects.select_for_update().filter(pk=new_variant_id).first()
                    if not new_variant:
                        _reconsume(item); continue
                    item.bundle = None
                    item.bundle_id = None
                    item.product_variant = new_variant
                    item.product_variant_id = new_variant.pk
                    item.product_variant_name = ''
                    item.product_variant_sku = ''
                    if new_variant.price is not None:
                        item.price = new_variant.price
                    item.old_price = new_variant.old_price if getattr(new_variant, 'old_price', None) else None
                elif target_type == 'variant':
                    # Regular ProductVariant swap (type unchanged)
                    if new_variant_id != item.product_variant_id:
                        try:
                            new_variant = ProductVariant.objects.select_for_update().get(pk=new_variant_id)
                        except ProductVariant.DoesNotExist:
                            _reconsume(item); continue
                        item.product_variant = new_variant
                        item.product_variant_id = new_variant.pk
                        item.product_variant_name = ''
                        item.product_variant_sku = ''
                        if new_variant.price is not None:
                            item.price = new_variant.price
                        item.old_price = new_variant.old_price if getattr(new_variant, 'old_price', None) else None
                elif target_type == 'bundle':
                    # Regular Bundle replacement (the type does not change)
                    if new_bundle_id != item.bundle_id:
                        try:
                            new_bundle = Bundle.objects.get(pk=new_bundle_id)
                        except Bundle.DoesNotExist:
                            _reconsume(item); continue
                        item.bundle = new_bundle
                        item.bundle_id = new_bundle.pk
                        rebuild_snapshot_from_template = True
                        item.product_variant_name = new_bundle.name
                        item.product_variant_sku = new_bundle.sku or ''
                        try:
                            item.price = new_bundle.get_price() or 0
                        except Exception:
                            pass
                    item.was_dropshipped = False

                # qty
                if new_qty is not None:
                    item.quantity = new_qty

                # was_dropshipped for a non-bundle (regular swap or type change to product)
                if not item.bundle_id:
                    v = item.product_variant
                    if v is not None:
                        v.refresh_from_db()
                        if getattr(v, 'is_dropshipping', False):
                            item.was_dropshipped = True
                        elif getattr(v, 'is_hybrid', False) and (v.stock or 0) < (item.quantity or 0):
                            item.was_dropshipped = True
                        else:
                            item.was_dropshipped = False

                # 2. Save the item
                item.stock_applied = False
                item.save()

                # If the item is no longer a bundle, wipe the component snapshot
                if drop_bundle_components:
                    item.bundle_components.all().delete()

                # ── If a bundle, update the component snapshot ──
                if item.bundle_id:
                    if rebuild_snapshot_from_template:
                        # Erase the snapshot and copy from the new Bundle template
                        item.bundle_components.all().delete()
                        _ensure_bundle_components_snapshot(item)
                    # Now apply the component edits (for both the existing snapshot and the new one)
                    if comp_edits:
                        # Existing edits
                        existing = {c.pk: c for c in item.bundle_components.select_related('product_variant').all()}
                        for ck, cv in comp_edits.items():
                            if ck == 'new':
                                continue
                            cid = _to_int(ck)
                            if cid is None or cid not in existing:
                                continue
                            comp = existing[cid]
                            if not isinstance(cv, dict):
                                continue
                            if cv.get('delete'):
                                comp.delete()
                                continue
                            cv_variant_id = _to_int(cv.get('variant_id'))
                            cv_qty = _to_int(cv.get('quantity'))
                            if cv_variant_id and cv_variant_id != comp.product_variant_id:
                                try:
                                    new_var = ProductVariant.objects.get(pk=cv_variant_id)
                                except ProductVariant.DoesNotExist:
                                    new_var = None
                                if new_var:
                                    comp.product_variant = new_var
                                    comp.product_variant_id = new_var.pk
                                    comp.product_variant_name = new_var.name or ''
                                    comp.product_variant_sku = new_var.sku or ''
                            if cv_qty is not None and cv_qty > 0:
                                comp.quantity = cv_qty
                            comp.save()
                        # New components
                        new_list = comp_edits.get('new')
                        if isinstance(new_list, list):
                            for nc in new_list:
                                if not isinstance(nc, dict):
                                    continue
                                nv_id = _to_int(nc.get('variant_id'))
                                nv_qty = _to_int(nc.get('quantity')) or 1
                                if not nv_id:
                                    continue
                                try:
                                    nvar = ProductVariant.objects.get(pk=nv_id)
                                except ProductVariant.DoesNotExist:
                                    continue
                                OrderItemBundleComponent.objects.create(
                                    order_item=item,
                                    product_variant=nvar,
                                    quantity=max(1, nv_qty),
                                    product_variant_name=nvar.name or '',
                                    product_variant_sku=nvar.sku or '',
                                )

                # 3. Write off stock for the new configuration, but only if stock should be
                # held right now. An UNPAID monobank order holds stock only after
                # payment, so we don't write off prematurely (otherwise stock gets "eaten up"
                # by unpaid invoices when items are edited in the packing modal).
                if _stock_consumable(_orders_by_id.get(item.order_id)):
                    _stock_consume(item)
                affected_order_ids.add(item.order_id)

            # 4. Recalculate Order.total_price for the affected orders
            if affected_order_ids:
                for o in Order.objects.filter(pk__in=affected_order_ids):
                    new_total = sum(((it.price or 0) * it.quantity) for it in o.items.all())
                    o.total_price = new_total
                    o.save(update_fields=['total_price'])

    orders = Order.objects.filter(pk__in=ids).prefetch_related(
        'items__product_variant__product',
        'items__bundle_components__product_variant__product',
        'items__bundle__items__product_variant__product',
    ).order_by('created_at')

    # Courses are always excluded from the docx: they are an intangible item. They are irrelevant
    # for the
    # supplier/courier: not for "packaging", not for "all products",
    # and not for "Products:" in the order block. The exclusion goes through the existing
    # mechanism
    # excluded_item_ids → the course price is not included in adjusted_total, the row is not
    # printed, the summary doesn't see it. It also works when the manager accidentally ticks the
    # checkbox
    # on a course in the preview; the backend removes it anyway.
    for _o in orders:
        for _item in _o.items.all():
            if _item.course_id:
                excluded_item_ids.add(_item.pk)

    def _bundle_components(item):
        """Returns the bundle components from the snapshot (single source of truth after edits)."""
        comps = list(item.bundle_components.select_related('product_variant', 'product_variant__product').all())
        if comps:
            # Unified interface (as bundle.items used to be returned): attributes .product_variant,
            # .quantity
            return comps
        # Fallback for very old items without a snapshot
        if not item.bundle_id or not item.bundle:
            return []
        return list(item.bundle.items.select_related('product_variant', 'product_variant__product').all())

    def round_price(num):
        if num is None:
            return 0
        num = int(num)
        r = num % 100
        if r <= 25:   return num - r
        elif r <= 75: return num - r + 50
        else:         return num - r + 100

    doc = Document()
    section = doc.sections[0]
    section.page_width = Inches(8.27)
    section.page_height = Inches(11.69)
    section.left_margin = Inches(0.1)
    section.right_margin = Inches(0)
    section.top_margin = Inches(0.1)
    section.bottom_margin = Inches(0)

    style = doc.styles.add_style('CustomStyle', WD_STYLE_TYPE.PARAGRAPH)
    style.font.size = Pt(22)

    # ── Collect the data for the summary page ──────────────────────────
    include_packing = request.POST.get('include_packing', '1') != '0'

    own_entries_map = {}     # variant_id -> {group, unit_size, name, qty} — own packaging only
    bundle_entries_map = {}  # bundle_id -> {name, qty}
    all_items_map = {}       # ('v', variant_id) | ('s', snapshot_key) -> {name, qty}

    def _add_own(variant, qty):
        if not variant or getattr(variant.product, 'packaging_kind', None) != 'own':
            return
        key = variant.pk
        if key in own_entries_map:
            own_entries_map[key]['qty'] += qty
            return
        # Group: paired_product (factory pack) if present, otherwise the own product itself
        prod = variant.product
        group_id = prod.paired_product_id or prod.pk
        group_name = (prod.paired_product.name if prod.paired_product_id else prod.name)
        own_entries_map[key] = {
            'group_id': group_id,
            'group_name': group_name,
            'unit_size': float(variant.unit_size) if variant.unit_size else 0.0,
            'name': variant.name,
            'qty': qty,
        }

    def _add_all(variant, qty, snap_name='', snap_sku='', is_drop=False):
        """Adds to the "All products" block. Excludes own packaging and dropshipping.
        Bundle items don't get here, only bundle components (as regular products).
        """
        if qty <= 0:
            return
        if is_drop:
            return
        if variant is not None and getattr(variant.product, 'packaging_kind', None) == 'own':
            return
        if variant is not None and getattr(variant, 'is_dropshipping', False):
            return
        if variant is not None:
            key = ('v', variant.pk)
            name = variant.name or snap_name or snap_sku or 'Deleted variant'
        else:
            key = ('s', snap_sku or snap_name or 'unknown')
            name = snap_name or snap_sku or 'Deleted variant'
        if key in all_items_map:
            all_items_map[key]['qty'] += qty
        else:
            all_items_map[key] = {'name': name, 'qty': qty}

    def _add_bundle(item, qty):
        key = item.bundle_id
        name = item.product_variant_name or item.display_name
        if key in bundle_entries_map:
            bundle_entries_map[key]['qty'] += qty
            bundle_entries_map[key]['orders'].add(item.order_id)
            return
        bundle_entries_map[key] = {'name': name, 'qty': qty, 'orders': {item.order_id}}

    for o in orders:
        for item in o.items.all():
            if item.pk in excluded_item_ids:
                continue
            if item.bundle_id:
                _add_bundle(item, item.quantity)
                for bi in _bundle_components(item):
                    comp_qty = bi.quantity * item.quantity
                    _add_own(bi.product_variant, comp_qty)
                    _add_all(
                        bi.product_variant, comp_qty,
                        getattr(bi, 'product_variant_name', '') or '',
                        getattr(bi, 'product_variant_sku', '') or '',
                    )
            else:
                _add_own(item.product_variant, item.quantity)
                _add_all(
                    item.product_variant, item.quantity,
                    item.product_variant_name or '',
                    item.product_variant_sku or '',
                    is_drop=bool(item.was_dropshipped),
                )

    has_summary_page = include_packing and bool(
        own_entries_map or bundle_entries_map or all_items_map
    )

    # ── First page: packaging + bundles + all products ───────────────
    if has_summary_page:
        p_sum = doc.add_paragraph(style=style)
        p_sum.paragraph_format.line_spacing = Pt(20)
        today = datetime.now().strftime('%d.%m')

        if own_entries_map:
            own_entries = sorted(
                own_entries_map.values(),
                key=lambda e: (e['group_name'].lower(), e['unit_size'], e['name'].lower()),
            )
            r = p_sum.add_run(f'PACKING {today}\n'); r.bold = True; r.font.size = Pt(20)
            for idx, e in enumerate(own_entries, 1):
                r = p_sum.add_run(f'{idx}. {e["name"]} - '); r.font.size = Pt(16)
                r = p_sum.add_run(f'{int(e["qty"])} pcs\n'); r.bold = True; r.font.size = Pt(16)

        if bundle_entries_map:
            if own_entries_map:
                p_sum.add_run('\n')
            r = p_sum.add_run('BUNDLES\n'); r.bold = True; r.font.size = Pt(20)
            bundle_entries = sorted(
                bundle_entries_map.values(),
                key=lambda e: (e['name'] or '').lower(),
            )
            for idx, e in enumerate(bundle_entries, 1):
                r = p_sum.add_run(f'{idx}. {e["name"]} - '); r.font.size = Pt(16)
                # (N) is the number of orders the bundle appears in: that many
                # stock printouts are needed (3 bundles in 1 order = 1).
                r = p_sum.add_run(f'{int(e["qty"])} pcs'); r.bold = True; r.font.size = Pt(16)
                r = p_sum.add_run(f' ({len(e["orders"])})\n'); r.font.size = Pt(16)

        if all_items_map:
            if own_entries_map or bundle_entries_map:
                p_sum.add_run('\n')
            r = p_sum.add_run('ALL PRODUCTS\n'); r.bold = True; r.font.size = Pt(20)
            all_entries = sorted(
                all_items_map.values(),
                key=lambda e: (e['name'] or '').lower(),
            )
            for idx, e in enumerate(all_entries, 1):
                r = p_sum.add_run(f'{idx}. {e["name"]} - '); r.font.size = Pt(16)
                r = p_sum.add_run(f'{int(e["qty"])} pcs\n'); r.bold = True; r.font.size = Pt(16)

    p = doc.add_paragraph(style=style)
    p.paragraph_format.line_spacing = Pt(20)

    # Exclude orders with no items left (all deleted
    # or marked in excluded_item_ids in the modal); an empty page in the docx
    # is not needed.
    def _has_visible_items(o):
        for it in o.items.all():
            if it.pk not in excluded_item_ids:
                return True
        return False

    visible_orders = [o for o in orders if _has_visible_items(o)]

    if not visible_orders and not has_summary_page:
        from django.http import HttpResponseBadRequest
        return HttpResponseBadRequest(
            'No items to export: all items are deleted or excluded.'
        )

    has_packing_page = has_summary_page
    for index, order in enumerate(visible_orders, 1):
        if index > 1 or has_packing_page:
            p = doc.add_page_break()
        p.style = style
        p.paragraph_format.line_spacing = Pt(20)

        r = p.add_run(f'Order #{index}')
        r.font.size = Pt(16); r.bold = True
        # The label in the heading is the only sign of pickup on the sheet: the packer uses it to
        # decide
        # whether to stick on a waybill (such an order has no address at all).
        if order.delivery_type == 'pickup':
            r = p.add_run('  PICKUP')
            r.font.size = Pt(16); r.bold = True
        if order.pk in pay_we_order_ids:
            r = p.add_run('  ON THE HOUSE')
            r.font.size = Pt(16); r.bold = True

        # Pickup may have no customer data — empty lines are not printed.
        if order.customer_name:
            p.add_run('\nRecipient: ')
            p.add_run(order.customer_name).bold = True

        if order.customer_phone:
            p.add_run('\nPhone number: ')
            p.add_run(format_phone(order.customer_phone)).bold = True

        if order.delivery_type != 'pickup':
            r = p.add_run('\nAddress: '); r.font.size = Pt(16)
            r = p.add_run(order.delivery_address or '')
            r.font.size = Pt(16); r.bold = True

        # Recalculate total: Order.total_price − Σ(excluded item.price × qty)
        excluded_sum = 0
        for it in order.items.all():
            if it.pk in excluded_item_ids:
                excluded_sum += (it.price or 0) * it.quantity
        adjusted_total = (order.total_price or 0) - excluded_sum
        if adjusted_total < 0:
            adjusted_total = 0
        p.add_run(f'\nEstimated value: {round_price(adjusted_total)}').bold = True

        p.add_run('\n\nProducts:\n').bold = True

        total_count = 0
        item_idx = 0

        def _resolve_photo_path(item_):
            """File path of the photo to insert into the docx.
               For a product: variant photo (color_value scheme) → product photo.
               For a bundle: packaging photo → bundle photo → photo of the first component."""
            try:
                from catalog.serializers import _variant_images_qs
                if item_.bundle_id and item_.bundle:
                    img = item_.bundle.images.order_by('sort_order', 'pk').first()
                    if img and img.image:
                        return img.image.path
                    # first component
                    first_comp = item_.bundle_components.select_related('product_variant').first()
                    pv = first_comp.product_variant if first_comp else None
                    if not pv:
                        first_bi = item_.bundle.items.select_related('product_variant').first()
                        pv = first_bi.product_variant if first_bi else None
                    if pv:
                        vi = next(iter(_variant_images_qs(pv)), None) or (
                            pv.product.images.order_by('sort_order', 'pk').first()
                            if pv.product_id else None
                        )
                        return vi.image.path if vi and vi.image else None
                    return None
                v = item_.product_variant
                if v:
                    img = next(iter(_variant_images_qs(v)), None) or (
                        v.product.images.order_by('sort_order', 'pk').first() if v.product_id else None
                    )
                    return img.image.path if img and img.image else None
            except Exception:
                return None
            return None

        # `p` currently holds the order header with line_spacing=Pt(20). Images in
        # such a paragraph get cropped or overlap the text because line spacing is
        # fixed. So we draw the photo in a SEPARATE paragraph with auto height,
        # and then continue the text in a new paragraph.
        current_p = p

        def _start_text_paragraph():
            nonlocal current_p
            current_p = doc.add_paragraph(style=style)
            current_p.paragraph_format.line_spacing = Pt(20)

        def _add_picture_paragraph(photo_path):
            try:
                pic_p = doc.add_paragraph(style=style)
                pic_p.paragraph_format.line_spacing_rule = WD_LINE_SPACING.SINGLE
                pic_p.paragraph_format.space_before = Pt(0)
                pic_p.paragraph_format.space_after = Pt(2)
                pic_p.add_run().add_picture(photo_path, width=Inches(1.6))
            except Exception:
                pass

        # order_by('pk') is mandatory: OrderItem has no Meta.ordering, so without
        # it Postgres returns rows in unpredictable order, which is why
        # products in the printout went from last to first. pk grows in
        # the order the items were added to the order.
        for item in order.items.order_by('pk'):
            if item.pk in excluded_item_ids:
                continue
            # The «attach photo» flag, for both regular products and bundles.
            attach_photo = bool((edits_map.get(item.pk) or {}).get('attach_photo'))
            item_idx += 1
            if item.bundle_id:
                bundle_name = item.product_variant_name or item.display_name
                current_p.add_run(f'{item_idx}. {bundle_name}')
                current_p.add_run(' - ')
                # If there will be a photo, don't add \n at the end, otherwise an
                # extra empty line appears between the text and the image.
                tail = '' if attach_photo else '\n'
                current_p.add_run(f'{item.quantity} pcs{tail}').bold = True
                if attach_photo:
                    photo_path = _resolve_photo_path(item)
                    if photo_path:
                        _add_picture_paragraph(photo_path)
                        _start_text_paragraph()
                    else:
                        current_p.add_run('\n')
                bundle_items = _bundle_components(item)
                bundle_total = sum(bi.quantity * item.quantity for bi in bundle_items)
                # Component indent: one space to the right of the bundle name,
                # so the contents line up visually right under the products.
                comp_indent = ' ' * (len(str(item_idx)) + 3)
                comp_edits_map = (edits_map.get(item.pk) or {}).get('components') or {}
                for bi_idx, bi in enumerate(bundle_items):
                    comp_qty = bi.quantity * item.quantity
                    comp_name = getattr(bi, 'display_name', None) or (
                        bi.product_variant.name if getattr(bi, 'product_variant', None) else 'Deleted component'
                    )
                    # «attach photo» for a component: the key in edits_json is comp.pk (str).
                    comp_pk = getattr(bi, 'pk', None)
                    comp_attach = bool(
                        comp_pk
                        and isinstance(comp_edits_map, dict)
                        and isinstance(comp_edits_map.get(str(comp_pk)), dict)
                        and comp_edits_map[str(comp_pk)].get('attach_photo')
                    )
                    is_last = (bi_idx == len(bundle_items) - 1)
                    current_p.add_run(f'{comp_indent}{comp_name}')
                    current_p.add_run(' - ')
                    if is_last:
                        tail_text = f'{comp_qty} pcs   Total per bundle {bundle_total} pcs'
                    else:
                        tail_text = f'{comp_qty} pcs'
                    # If there will be a photo, don't add \n after qty (the image goes into a
                    # separate paragraph).
                    current_p.add_run(tail_text + ('' if comp_attach else '\n')).bold = True
                    if comp_attach:
                        pv = getattr(bi, 'product_variant', None)
                        comp_photo = None
                        try:
                            from catalog.serializers import _variant_images_qs
                            if pv:
                                img = next(iter(_variant_images_qs(pv)), None) or (
                                    pv.product.images.order_by('sort_order', 'pk').first()
                                    if pv.product_id else None
                                )
                                if img and img.image:
                                    comp_photo = img.image.path
                        except Exception:
                            comp_photo = None
                        if comp_photo:
                            _add_picture_paragraph(comp_photo)
                            _start_text_paragraph()
                        else:
                            current_p.add_run('\n')
                total_count += bundle_total
            else:
                current_p.add_run(f'{item_idx}. {item.display_name}')
                current_p.add_run(' - ')
                tail = '' if attach_photo else '\n'
                current_p.add_run(f'{item.quantity} pcs{tail}').bold = True
                if attach_photo:
                    photo_path = _resolve_photo_path(item)
                    if photo_path:
                        _add_picture_paragraph(photo_path)
                        _start_text_paragraph()
                    else:
                        current_p.add_run('\n')
                total_count += item.quantity

        current_p.add_run(f'\nTotal number of items: {total_count}\n').bold = True
        # Comment at the end of the page, if checked in the modal.
        # The text comes from the edited textarea (fallback to the DB field).
        if order.pk in note_order_ids:
            note_txt = request.POST.get(f'note_text_{order.pk}')
            if note_txt is None:
                note_txt = order.manager_comment or ''
            note_txt = note_txt.strip()
            if note_txt:
                r = current_p.add_run('\n' + note_txt)
                r.bold = True
                r.font.size = Pt(22)

    buf = BytesIO()
    doc.save(buf)
    buf.seek(0)

    from urllib.parse import quote
    fname = request.POST.get('filename', '').strip() or f'orders_{datetime.now().strftime("%d.%m.%Y")}'
    fname = ''.join(c for c in fname if c not in r'\/:*?"<>|')

    # ── Old mode: the browser submits a hidden form and gets the docx directly.
    # Kept working so nothing breaks until the modal is updated.
    token = (request.POST.get('export_token') or '').strip()
    if not token:
        fname_encoded = quote(fname + '.docx', safe='')
        response = HttpResponse(
            buf.getvalue(),
            content_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        )
        response['Content-Disposition'] = f"attachment; filename*=UTF-8''{fname_encoded}"
        return response

    # ── New mode: two files.
    # Why not «POST for the docx + a separate GET for the PDF»: this POST MUTATES THE DB (stock,
    # bundle stock, totals; the transaction.atomic block above). A second request would either
    # find the data from before the edits or apply the edits a second time. So one request
    # produces both files, and the browser then fetches them ready-made by token.
    payload = _export_write_files(token, fname, buf.getvalue(), request, ids,
                                  excluded_item_ids)
    return JsonResponse(payload)


# Ready export files live here: two separate downloads can't be served from one response,
# so we serve links.
def _export_dir(token=None):
    from django.conf import settings
    base = os.path.join(settings.BASE_DIR, '_exports')
    return os.path.join(base, token) if token else base


def _export_sweep(keep_token, max_age_sec=2 * 3600):
    """Remove other people's old exports. Without this the directory grows forever."""
    import shutil
    import time
    base = _export_dir()
    if not os.path.isdir(base):
        return
    now = time.time()
    for name in os.listdir(base):
        if name == keep_token:
            continue
        p = os.path.join(base, name)
        try:
            if now - os.path.getmtime(p) > max_age_sec:
                shutil.rmtree(p, ignore_errors=True)
        except OSError:
            pass


def _export_write_files(token, fname, docx_bytes, request, ids, excluded_item_ids):
    """Write the docx and build the instructions PDF. Returns the payload for the modal."""
    import re as _re

    safe_token = _re.sub(r'[^A-Za-z0-9_-]', '', token)[:64]
    if not safe_token:
        safe_token = 'export'
    d = _export_dir(safe_token)
    os.makedirs(d, exist_ok=True)
    _export_sweep(safe_token)

    with open(os.path.join(d, 'orders.docx'), 'wb') as fh:
        fh.write(docx_bytes)
    with open(os.path.join(d, 'name.txt'), 'w', encoding='utf-8') as fh:
        fh.write(fname)

    payload = {
        'ok': True,
        'docx_url': f'/admin/orders/export-file/{safe_token}/docx/',
        'pdf_url': None,
        'sheets': 0, 'blanks': 0, 'kits': 0,
        'placeholders': [], 'substituted': [], 'warnings': [],
    }

    if request.POST.get('include_instructions') not in ('1', 'true', 'on'):
        return payload

    # A PDF build failure is NOT fatal: the stock edits are already committed, and the docx must
    # reach the manager regardless.
    try:
        from . import instruction_docx
        plan, excluded = build_export_plan(ids, excluded_item_ids)
        # Word, not PDF: duplex printing from a PDF has to be enabled in the printer
        # dialog. In a .docx each instruction starts with a page break, so
        # ordinary single-sided printing already puts each on its own sheet.
        # The number of sheets equals the number of ORDERS that contain a bundle, not the sum of
        # units.
        data, report = instruction_docx.build(plan, excluded, user=request.user)
        if data is None:
            return payload
        with open(os.path.join(d, 'instructions.docx'), 'wb') as fh:
            fh.write(data)
        payload['pdf_url'] = f'/admin/orders/export-file/{safe_token}/instructions/'
        payload['sheets'] = report.get('sheets', 0)
        payload['blanks'] = 0
        payload['kits'] = report.get('kits', 0)
        payload['placeholders'] = report.get('placeholders', [])
        payload['substituted'] = report.get('substituted', [])
        payload['duplex'] = bool(report.get('duplex'))
        # The flag is needed at DOWNLOAD time, which is a separate request, so we store
        # it next to the file rather than keeping it in memory.
        if payload['duplex']:
            with open(os.path.join(d, 'duplex'), 'w', encoding='utf-8') as fh:
                fh.write('1')
    except Exception as e:
        log.exception('instruction docx build failed')
        payload['warnings'].append(f'Could not build the instructions: {e}. '
                                   f'The orders are downloaded as usual.')
    return payload


@staff_member_required
def export_file(request, token, kind):
    """Serve the ready export file by token."""
    import re as _re
    from datetime import datetime
    from urllib.parse import quote
    from django.http import FileResponse, Http404

    safe_token = _re.sub(r'[^A-Za-z0-9_-]', '', token)[:64]
    d = _export_dir(safe_token)
    names = {'docx': ('orders.docx', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', '.docx'),
             'instructions': ('instructions.docx', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', '.docx'),
             # 'pdf' is kept as the old key: links from an already-open export
             # panel must not break after the code update.
             'pdf': ('instructions.docx', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', '.docx')}
    if kind not in names:
        raise Http404
    fn, ctype, ext = names[kind]
    path = os.path.join(d, fn)
    if not os.path.exists(path):
        raise Http404

    base = 'orders'
    try:
        with open(os.path.join(d, 'name.txt'), encoding='utf-8') as fh:
            base = fh.read().strip() or base
    except OSError:
        pass
    if kind == 'docx':
        label = base
    else:
        # The name must itself say how to print: the manager sees the file in the downloads
        # list without the export panel, where this is explained.
        import re as _re2
        m = _re2.search(r'\d{2}\.\d{2}\.\d{4}', base)
        date = m.group(0) if m else datetime.now().strftime('%d.%m.%Y')
        duplex = os.path.exists(os.path.join(d, 'duplex'))
        label = ('duplex instructions ' if duplex else 'instructions ') + date
    resp = FileResponse(open(path, 'rb'), content_type=ctype)
    resp['Content-Disposition'] = ("attachment; filename*=UTF-8''"
                                   + quote(label + ext, safe=''))
    return resp


# ─── Variant search (for invoice item editing) ────────────────────────────────

@staff_member_required
def search_variants(request):
    q = request.GET.get('q', '').strip()
    if not q or len(q) < 2:
        return JsonResponse({'results': []})
    from django.db.models import Q
    from catalog.serializers import _supplier_dict
    # order_by gives a deterministic slice (Meta has no ordering; without it LIMIT on Postgres gives
    # an
    # arbitrary window). Take extra (an own product has many packagings that collapse into one).
    qs = (ProductVariant.objects
        .filter(Q(name__icontains=q) | Q(sku__icontains=q))
        .select_related('product__brand', 'bulk_source', 'supplier')
        .prefetch_related('images', 'product__images')
        .order_by('name', 'id')[:120])
    # "Own packaging" is sold from a bulk bag, so all its variants (250 g, 500
    # g…) draw
    # from one stock. In the replacement search we COLLAPSE them to one PRODUCT (don't multiply
    # packagings).
    # Factory packs stay per-variant (they are separate product lines). The representative of an own
    # product is the
    # variant with a bag + unit_size (so a stock receipt into the bag is re-detected correctly).
    own_rep = {}      # product_id → representative variant
    order = []        # [('own', pid) | ('factory', vid)] — order of first appearance
    factory_v = {}    # vid → variant
    for v in qs:
        is_own = bool(v.product_id and v.product.packaging_kind == 'own')
        if is_own:
            cur = own_rep.get(v.product_id)
            if cur is None:
                own_rep[v.product_id] = v
                order.append(('own', v.product_id))
            elif (v.bulk_source_id and v.unit_size) and not (cur.bulk_source_id and cur.unit_size):
                own_rep[v.product_id] = v   # best representative (with a bag)
        elif v.id not in factory_v:
            factory_v[v.id] = v
            order.append(('factory', v.id))

    results = []
    for kind, key in order:
        v = own_rep[key] if kind == 'own' else factory_v[key]
        is_own = (kind == 'own')
        img = v.images.first() or (v.product.images.first() if v.product_id else None)
        photo = request.build_absolute_uri(img.image.url) if img and img.image else ''
        results.append({
            'id': v.id,
            # For own we show the product's GENERAL name (without the packaging weight), for factory
            # the variant name.
            'name': (v.product.name if is_own and v.product_id else v.name),
            'sku': '' if is_own else (v.sku or ''),
            'price': str(v.price) if v.price is not None else '',
            'final_price': str(v.final_price_whole) if v.price is not None else '',
            'stock': v.stock or 0,
            'photo': photo,
            # Own packaging data, so that on replacement a stock receipt into the bulk bag is
            # re-detected correctly.
            'is_own': is_own,
            'product_name': v.product.name if v.product_id else v.name,
            'unit_size': v.unit_size,
            'bag_current': float(v.bulk_source.quantity) if v.bulk_source_id else None,
            'bag_unit': {'g': 'g', 'ml': 'ml', 'pcs': 'pcs'}.get(
                v.bulk_source.unit if v.bulk_source_id else 'g', 'g'),
            # Supplier and dropshipping flags: without them a just-added item does not
            # generate a dropshipping parcel in the CRM pickers (itemIsDrop saw supplier=null),
            # and the parcel appeared only after the form was reloaded.
            'is_dropshipping': bool(v.is_dropshipping),
            'is_hybrid': bool(v.is_hybrid),
            'supplier': _supplier_dict(v.supplier) if v.supplier_id else None,
        })
        if len(results) >= 20:
            break
    return JsonResponse({'results': results})


# ─── Bundle search & composition (for order item bundle editing) ─────────────

@staff_member_required
def search_bundles(request):
    """Search bundles by name/SKU. Returns up to 20 candidates."""
    from catalog.models import Bundle
    from django.db.models import Q
    q = request.GET.get('q', '').strip()
    if not q or len(q) < 2:
        return JsonResponse({'results': []})
    qs = (Bundle.objects
        .filter(Q(name__icontains=q) | Q(sku__icontains=q))
        .prefetch_related('images')[:20])
    results = []
    for b in qs:
        bimg = b.images.first()
        bphoto = request.build_absolute_uri(bimg.image.url) if bimg and bimg.image else ''
        try:
            price = b.get_price() or 0
        except Exception:
            price = 0
        try:
            stock = b.get_stock() or 0
        except Exception:
            stock = 0
        results.append({
            'id': b.pk,
            'name': b.name,
            'sku': b.sku or '',
            'price': str(price), 'stock': stock,
            'is_active': b.is_active,
            'photo': bphoto,
        })
    return JsonResponse({'results': results})


@staff_member_required
def bundle_items(request, bundle_id):
    """Bundle composition — to redraw the preview after selecting/replacing it in an order item."""
    from catalog.models import Bundle
    from catalog.serializers import _variant_images_qs
    try:
        b = Bundle.objects.prefetch_related('items__product_variant__product').get(pk=bundle_id)
    except Bundle.DoesNotExist:
        return JsonResponse({'error': 'Bundle not found'}, status=404)

    def _photo(pv):
        if not pv:
            return ''
        img = next(iter(_variant_images_qs(pv)), None) or (
            pv.product.images.order_by('sort_order', 'pk').first() if pv.product_id else None
        )
        return request.build_absolute_uri(img.image.url) if img and img.image else ''

    items = []
    for it in b.items.all():
        pv = it.product_variant
        items.append({
            'variant_id': pv.pk if pv else None,
            'name': pv.name if pv else 'Deleted component',
            'sku': pv.sku if pv else '',
            'qty_per_set': it.quantity,
            'photo': _photo(pv),
        })
    return JsonResponse({'bundle_id': b.pk, 'bundle_name': b.name or '', 'items': items})



def _get_order_for_user(request, order_number):
    """Look up an order bound to its owner (IDOR protection)."""
    qs = Order.objects.filter(order_number=order_number)
    user = getattr(request, 'user', None)
    if user is None or not user.is_authenticated:
        return None
    if user.is_staff:
        return qs.first()
    return qs.filter(user=user).first()


def _get_order_for_payment_action(request, order_number):
    """Order for payment actions (receipt screenshot / requisites request).
    Owner or staff: by JWT token; a guest order (user=None):
    only by the unpredictable public_token (order_number is a sequential counter, predictable,
    so it alone doesn't protect against IDOR; previously anyone could enumerate numbers to
    touch other people's guest orders)."""
    qs = Order.objects.filter(order_number=order_number)
    user = getattr(request, 'user', None)
    if user is not None and user.is_authenticated:
        if user.is_staff:
            return qs.first()
        own = qs.filter(user=user).first()
        if own:
            return own
    token = ''
    if isinstance(getattr(request, 'data', None), dict):
        token = (request.data.get('token') or '').strip()
    if not token:
        # In GET links (monobank redirect, success page) the frontend passes
        # the token as ?t=...; POST actions send ?token=/{token}. We accept both.
        token = (request.query_params.get('token') or request.query_params.get('t') or '').strip()
    if not token:
        return None
    # Match only by the unpredictable public_token: guest checkout auto-creates a
    # CRM customer (user != NULL), so the old gate `user__isnull=True` NEVER
    # matched such orders (a latent bug on the guest success page). A valid
    # token is sufficient proof for reading the status ONLY (we don't expose PII). It also
    # unblocks the order flow from the Flora app.
    return qs.filter(public_token=token).first()


@api_view(['POST'])
@permission_classes([])
@throttle_classes([PaymentActionThrottle])
def request_payment_details(request, order_number):
    from core.antibot import guard
    rejected = guard(request.data if isinstance(request.data, dict) else {}, label=f'request-details:{order_number}')
    if rejected:
        return rejected
    order = _get_order_for_payment_action(request, order_number)
    if not order:
        return Response({'error': 'Order not found'}, status=404)
    note = '📲 The customer asks to send the payment details to their phone number'
    fields = []
    if note not in (order.manager_comment or ''):
        order.manager_comment = (order.manager_comment + '\n' + note).strip() if order.manager_comment else note
        fields.append('manager_comment')
    # Lands in the CRM "Payment details" queue (not "Payments"); the
    # manager calls back.
    if not order.payment_contact_requested:
        order.payment_contact_requested = True
        fields.append('payment_contact_requested')
    if fields:
        order.save(update_fields=fields)
    # A repeat request "revives" the card: if the manager has already marked the requisites
    # as sent (step>0) and the customer presses again, return it to the active queue.
    # delete() sends a post_delete signal → counters broadcast.
    from catalog.models import NotificationState
    NotificationState.objects.filter(
        section='requisites', object_id=order.id, step__gt=0
    ).delete()
    return Response({'ok': True})


@api_view(['POST'])
@permission_classes([])
@throttle_classes([PaymentActionThrottle])
def upload_payment_screenshot(request, order_number):
    from core.antibot import guard
    rejected = guard(request.data if hasattr(request, 'data') else {}, label=f'screenshot:{order_number}')
    if rejected:
        return rejected
    order = _get_order_for_payment_action(request, order_number)
    if not order:
        log.warning('screenshot upload %s: order not found / token mismatch', order_number)
        return Response({'error': 'Order not found'}, status=404)

    photo = request.FILES.get('screenshot')
    if not photo:
        log.warning('screenshot upload %s: no file in request', order_number)
        return Response({'error': 'No file received'}, status=400)

    if photo.size > MAX_UPLOAD_BYTES:
        log.warning('screenshot upload %s: too big (%s bytes)', order_number, photo.size)
        return Response({'error': f'Maximum size: {MAX_UPLOAD_BYTES // (1024 * 1024)} MB'}, status=400)

    ext = os.path.splitext(photo.name)[1].lower().lstrip('.')
    if ext not in ALLOWED_UPLOAD_EXTS:
        log.warning('screenshot upload %s: rejected ext %r (name=%r)', order_number, ext, photo.name)
        return Response({'error': 'Only photos and PDF files are allowed'}, status=400)

    if not _looks_like_allowed_file(photo):
        log.warning('screenshot upload %s: failed magic-byte check (ext=%s)', order_number, ext)
        return Response({'error': 'Invalid file format'}, status=400)

    # The order is already paid. This is NOT a dead end: the customer still wants to send
    # the receipt (or just make sure it arrived). We accept the screenshot as confirmation
    # and return a friendly already_paid so the frontend shows "already paid, thank you"
    # instead of an error (previously this was a 400 → the customer kept pointlessly hammering the
    # button).
    already_paid = order.payment_status == 'paid'

    order.payment_screenshot = photo
    order.payment_notification = True
    order.save(update_fields=['payment_screenshot', 'payment_notification'])

    # Auto-OCR for bank_transfer is moved to a background thread AFTER commit: Vision OCR
    # via LLM can take tens of seconds, and in the synchronous request flow it would
    # hold a worker and exhaust the pool. The upload itself returns 200 immediately (like the review
    # moderation pattern in catalog/signals.py). Backstop: the process_payment_screenshots cron.
    # For an already-paid order OCR matching is pointless, so we skip it.
    if not already_paid and order.payment_method == 'bank_transfer':
        _schedule_screenshot_ocr(order.pk)

    return Response({'ok': True, 'already_paid': already_paid})


def _schedule_screenshot_ocr(order_pk: int):
    """Runs OCR matching of the screenshot in the background after the request transaction commits."""
    import threading

    def _job():
        try:
            from orders.screenshot_match import match_screenshot
            order = Order.objects.get(pk=order_pk)
            res = match_screenshot(order)
            log.info('screenshot OCR (bg) order=%s → %s', order.order_number, res.reason)
        except Order.DoesNotExist:
            return
        except Exception:
            log.exception('screenshot OCR (bg) failed for order_pk=%s', order_pk)
        finally:
            # A separate thread has its own DB connection; close it so it doesn't leak.
            from django.db import connection
            connection.close()

    transaction.on_commit(lambda: threading.Thread(target=_job, daemon=True).start())


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def mark_order_paid(request, order_number):
    """The customer reports having paid by bank transfer; the manager then confirms manually.
    Does NOT set payment_status='paid' directly, only a flag for the manager's queue."""
    try:
        order = Order.objects.get(order_number=order_number, user=request.user)
    except Order.DoesNotExist:
        return Response({'error': 'Order not found'}, status=404)
    if order.payment_method != 'bank_transfer':
        return Response({'error': 'Not applicable to this payment method'}, status=400)
    if order.payment_status == 'paid':
        return Response({'ok': True, 'note': 'already paid'})
    note = '🟡 The customer says they have paid (needs verification)'
    if note not in (order.manager_comment or ''):
        order.manager_comment = (order.manager_comment + '\n' + note).strip() if order.manager_comment else note
    order.payment_notification = True
    order.save(update_fields=['payment_notification', 'manager_comment'])
    return Response({'ok': True, 'pending_review': True})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def pay_order(request, order_number):
    try:
        order = Order.objects.get(order_number=order_number, user=request.user)
    except Order.DoesNotExist:
        return Response({'error': 'Order not found'}, status=404)

    if order.payment_status == 'paid':
        return Response({'error': 'Already paid'}, status=400)

    if order.payment_method == 'monobank':
        try:
            mono = _create_monobank_payment(order)
        except monobank.MonobankNotConfigured:
            return Response({'error': 'Monobank is not configured'}, status=503)
        except monobank.MonobankError as e:
            log.error('monobank pay_order failed: %s', e)
            return Response({'error': 'Failed to create the Monobank payment'}, status=502)
        return Response({'payment_url': mono['pageUrl'], 'invoice_id': mono['invoiceId']})

    return Response({'error': 'This order is not paid online'}, status=400)


@staff_member_required
@require_POST
def admin_order_payment_link(request, order_id):
    """CRM: the "Payment link" button in the order card. Works for any
    order (regardless of payment_method/payment_status: the manager decides
    when the customer needs an alternative Monobank payment link).
    Always creates a fresh invoice and voids the previous unpaid one, so the customer can't
    pay a stale amount if the stock/price of the order changed between clicks."""
    try:
        order = Order.objects.get(pk=order_id)
    except Order.DoesNotExist:
        return JsonResponse({'error': 'Order not found'}, status=404)

    old_invoice_id = order.monobank_invoice_id
    if old_invoice_id:
        try:
            monobank.remove_invoice(old_invoice_id)
        except monobank.MonobankError as e:
            log.warning('admin_order_payment_link: remove old invoice failed order=%s: %s', order.order_number, e)

    try:
        mono = _create_monobank_payment(order)
    except monobank.MonobankNotConfigured:
        return JsonResponse({'error': 'Monobank is not configured'}, status=503)
    except monobank.MonobankError as e:
        log.error('admin_order_payment_link failed order=%s: %s', order.order_number, e)
        return JsonResponse({'error': 'Failed to create the Monobank payment'}, status=502)

    return JsonResponse({'payment_url': mono['pageUrl']})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
@throttle_classes([PaymentActionThrottle])
def change_payment_method(request, order_number):
    """The customer changes the payment method of their still-unpaid order.

    Stock write-off/return is reconciled by the
    _order_status_changed signal itself (orders/models.py): it compares
    _stock_consumable_state BEFORE and AFTER saving and decides whether to
    write off/return stock (e.g. unpaid monobank → cod writes off
    immediately). Here we just save the new field value."""
    try:
        order = Order.objects.get(order_number=order_number, user=request.user)
    except Order.DoesNotExist:
        return Response({'error': 'Order not found'}, status=404)

    if order.payment_status == 'paid':
        return Response({'error': 'The order is already paid'}, status=400)
    if order.status == 'cancelled':
        return Response({'error': 'The order is cancelled'}, status=400)

    method = request.data.get('payment_method')
    if method not in dict(Order.PAYMENT_CHOICES):
        return Response({'error': 'Unknown payment method'}, status=400)

    if method != order.payment_method:
        order.payment_method = method
        order.save(update_fields=['payment_method'])
    return Response({'ok': True, 'payment_method': order.payment_method})


@api_view(['GET'])
@permission_classes([])
@throttle_classes([PaymentActionThrottle])
def order_public_status(request, order_number):
    """Payment status for the success page (including after a monobank redirect).
    Owner/staff: by JWT; guest: by public_token (?t=...). Returns the status only."""
    order = _get_order_for_payment_action(request, order_number)
    if not order:
        return Response({'error': 'Order not found'}, status=404)
    return Response({
        'order_number': order.order_number,
        'payment_method': order.payment_method,
        'payment_status': order.payment_status,
        'status': order.status,
        # So that the success page after a Monobank redirect can lead the buyer
        # to "My courses", not only to "My orders". Not PII.
        'has_courses': order.items.filter(course__isnull=False).exists(),
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def my_orders(request):
    orders = Order.objects.filter(user=request.user).prefetch_related(
        'items__course',
        'items__product_variant__images',
        'items__product_variant__product',
        'items__product_variant__product__images',
        'items__bundle__images',
        'items__bundle__items__product_variant__images',
        'items__bundle__items__product_variant__product',
        'items__bundle__items__product_variant__product__images',
        # The actual composition of THIS item (snapshot), not the current bundle template.
        'items__bundle_components__product_variant__images',
        'items__bundle_components__product_variant__product',
        'items__bundle_components__product_variant__product__images',
    )
    from catalog.serializers import _variant_images_qs

    def variant_photo(variant):
        """Variant photo; fallback to the product's general photo (as on the storefront).
        For products without colors the photo sits at product level (variant=NULL)."""
        img = next(iter(_variant_images_qs(variant)), None)
        if img is None and variant.product_id:
            prod_imgs = sorted(
                (i for i in variant.product.images.all() if not i.is_hidden),
                key=lambda i: (i.sort_order, i.pk),
            )
            img = next((i for i in prod_imgs if i.variant_id is None), None) or (prod_imgs[0] if prod_imgs else None)
        return request.build_absolute_uri(img.image.url) if img and img.image else ''

    def bundle_photo(bundle):
        """The bundle's main photo — as on the storefront. Photos of the products from the bundle
        composition are NOT substituted (as on the storefront)."""
        imgs = [i for i in bundle.images.all() if not i.is_hidden]
        url = lambda i: request.build_absolute_uri(i.image.url) if i and i.image else ''
        gen = sorted(imgs, key=lambda x: (not x.is_main, x.sort_order, x.pk))
        return url(gen[0]) if gen else ''

    result = []
    for order in orders:
        items = []
        for item in order.items.all():
            if item.product_variant_id and item.product_variant:
                photo = variant_photo(item.product_variant)
                items.append({
                    'name': item.product_variant.name,
                    'sku': item.product_variant.sku or '',
                    'slug': item.product_variant.product.slug,
                    'is_bundle': False,
                    'quantity': item.quantity,
                    'price': str(item.price),
                    'image': photo,
                })
            elif item.course_id and item.course:
                # A course has no product_variant but has a snapshot name "Course: …", so
                # without this branch it fell into the "product deleted" branch and was shown
                # to the customer as a deleted item with no photo or link.
                course = item.course
                items.append({
                    'name': course.name,
                    'sku': '',
                    'slug': '',
                    'is_bundle': False,
                    'is_course': True,
                    'course_slug': course.slug,
                    'quantity': item.quantity,
                    'price': str(item.price),
                    'image': (request.build_absolute_uri(course.cover_image.url)
                              if course.cover_image else ''),
                })
            elif item.product_variant_name and not item.bundle_id:
                # Product deleted: show the snapshot from the order (bundles
                # have bundle_id and are handled below, even if a snapshot name exists)
                items.append({
                    'name': item.product_variant_name,
                    'sku': item.product_variant_sku or '',
                    'slug': '',
                    'is_bundle': False,
                    'quantity': item.quantity,
                    'price': str(item.price),
                    'image': '',
                    'deleted': True,
                })
            elif item.bundle_id and item.bundle:
                composition = []
                bundle = item.bundle
                photo = bundle_photo(bundle)
                # The actual composition of THIS item (the OrderItemBundleComponent snapshot), not
                # the
                # current Bundle.items template — if the manager edited this order's composition or
                # the
                # bundle template changed later, the customer sees what actually arrived in the box,
                # not what is sold on the storefront now.
                snapshot = list(item.bundle_components.all())
                if snapshot:
                    for comp in snapshot:
                        pv = comp.product_variant
                        composition.append({
                            'name': comp.display_name,
                            'sku': (pv.sku or '') if pv else (comp.product_variant_sku or ''),
                            'slug': pv.product.slug if pv else '',
                            'quantity': comp.quantity,
                            'image': variant_photo(pv) if pv else '',
                        })
                else:
                    # Legacy items without a snapshot (created via bulk_create before the snapshot
                    # was introduced): still displayed from the template, as before.
                    for bi in bundle.items.all():
                        pv = bi.product_variant
                        composition.append({
                            'name': pv.name,
                            'sku': pv.sku or '',
                            'slug': pv.product.slug,
                            'quantity': bi.quantity,
                            'image': variant_photo(pv),
                        })
                # In an order (unlike the storefront) we show at least something: if the
                # bundle has no photo of its own, take the first component's photo.
                if not photo and composition:
                    photo = next((c['image'] for c in composition if c['image']), '')
                # Priority: the frozen instruction_snapshot (it won't change however much
                # the bundle template is edited later). Live resolve is only a fallback for
                # old orders without a snapshot; create_pending=False: merely showing the order
                # list must not create a "verify the replacement" task for the
                # manager;
                # that's the job of the print flow in the CRM.
                has_instruction = bool(item.instruction_snapshot.strip())
                if not has_instruction:
                    instr = instruction_resolve.resolve_order_item(item, create_pending=False)
                    has_instruction = instr.kind == instruction_resolve.KIND_OK and bool(instr.html.strip())
                items.append({
                    'id': item.pk,
                    'name': bundle.name,
                    'sku': bundle.sku or '',
                    'slug': bundle.slug,
                    'is_bundle': True,
                    'quantity': item.quantity,
                    'price': str(item.price),
                    'image': photo,
                    'composition': composition,
                    'has_instruction': has_instruction,
                })
        result.append({
            'id': order.id,
            'order_number': order.order_number,
            'status': order.status,
            'status_display': order.get_status_display(),
            'payment_status': order.payment_status,
            'payment_status_display': order.get_payment_status_display(),
            'payment_method': order.payment_method,
            'delivery_type': order.delivery_type,
            'delivery_type_display': order.get_delivery_type_display(),
            'delivery_address': order.delivery_address,
            'comment': order.comment,
            'total_price': str(order.total_price) if order.total_price else '',
            'created_at': order.created_at.strftime('%d.%m.%Y'),
            'items': items,
        })
    return Response(result)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
@throttle_classes([InstructionPdfThrottle])
def order_item_instruction(request, order_number, item_id):
    """PDF instruction for a specific bundle item in the customer's OWN order.

    The instruction is resolved against the item's actual composition (order snapshot), not
    the current bundle template: if the composition/text has changed since, the customer
    still sees what actually arrived in the box."""
    try:
        order = Order.objects.get(order_number=order_number, user=request.user)
    except Order.DoesNotExist:
        return Response({'error': 'Order not found'}, status=404)
    try:
        item = order.items.get(pk=item_id, bundle__isnull=False)
    except order.items.model.DoesNotExist:
        return Response({'error': 'Item not found'}, status=404)

    from . import instruction_print
    try:
        pdf, reason = instruction_print.render_single(item, user=request.user)
    except Exception:
        log.exception('render_single instruction failed for item %s', item_id)
        return Response({'error': 'Could not build the instruction'}, status=500)
    if pdf is None:
        return Response({'error': reason or 'Instruction unavailable'}, status=404)

    resp = HttpResponse(pdf, content_type='application/pdf')
    resp['Content-Disposition'] = f'inline; filename="instruction-{order.order_number}.pdf"'
    return resp
