import os
from email.message import MIMEPart

from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils.html import strip_tags
from django.conf import settings


def send_email(to: str, subject: str, template: str, context: dict, inline_images=None):
    """inline_images: list of (cid, bytes, subtype) - images embedded directly in the email via
    Content-ID (`<img src="cid:...">`). Unlike data: URIs (Gmail strips them) and external URLs
    (which depend on BACKEND_URL/the domain), CID attachments display in all clients and do not
    depend on any settings."""
    if not to:
        return
    context.setdefault('site_name', settings.SITE_NAME)
    context.setdefault('site_url', settings.SITE_URL)
    context.setdefault('backend_url', getattr(settings, 'BACKEND_URL', 'http://localhost:8000'))
    html = render_to_string(f'email/{template}.html', context)
    # Plain-text version (generated from the HTML) - without it some clients and spam filters
    # penalize the email.
    text_body = strip_tags(html).strip()
    msg = EmailMultiAlternatives(
        subject=subject,
        body=text_body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[to],
    )
    msg.attach_alternative(html, 'text/html')
    for cid, data, subtype in (inline_images or []):
        # Content-Disposition: inline + Content-ID - so the client shows the photo in the email body
        # (not as an attachment) and can find it by `cid:` from the HTML.
        part = MIMEPart()
        part.set_content(data, maintype='image', subtype=subtype)
        part['Content-ID'] = f'<{cid}>'
        part['Content-Disposition'] = 'inline'
        msg.attach(part)
    try:
        msg.send()
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f'Email send error: {e}')


def _media_file_path(rel_url: str):
    """/media/.. -> absolute filesystem path (or None)."""
    if not rel_url or rel_url.startswith(('http://', 'https://', 'data:', 'cid:')):
        return None
    path = rel_url.split('?', 1)[0].split('#', 1)[0].lstrip('/')
    prefix = settings.MEDIA_URL.lstrip('/')
    if prefix and path.startswith(prefix):
        full = os.path.join(settings.MEDIA_ROOT, path[len(prefix):].lstrip('/'))
        return full if os.path.exists(full) else None
    return None


def _thumbnail_bytes(path: str, box: int = 140):
    """Compressed photo thumbnail -> (bytes, subtype). None on error.
    The email shows the photo at 64px, so 140px (x2 for sharpness) is enough, and the email does not
    bloat (a full-size 1080x1080 is several MB each)."""
    try:
        import io
        from PIL import Image
        im = Image.open(path)
        im.thumbnail((box, box))
        buf = io.BytesIO()
        if im.mode in ('RGBA', 'LA', 'P'):
            im.save(buf, 'PNG', optimize=True)
            subtype = 'png'
        else:
            im.convert('RGB').save(buf, 'JPEG', quality=82, optimize=True)
            subtype = 'jpeg'
        return buf.getvalue(), subtype
    except Exception:
        return None


def build_order_confirmation_context(order):
    """Shared context for the confirmation email and the PDF receipt (same layout).

    items[].photo_url is a RELATIVE URL (/media/...). send_order_confirmation adds the absolute http
    (for Gmail), while for the PDF link_callback resolves paths to the filesystem - no extra HTTP
    request to ourselves."""
    from catalog.serializers import _variant_images_qs

    def _variant_photo(pv):
        if not pv:
            return ''
        img = next(iter(_variant_images_qs(pv)), None) or (
            pv.product.images.first() if pv.product_id else None)
        return img.image.url if img and img.image else ''

    def _photo_by_sku(sku):
        """Fallback for snapshot items (import with no link to the catalog):
        look up the photo by SKU — variant → bundle."""
        from catalog.models import ProductVariant, Bundle
        v = ProductVariant.objects.filter(sku=sku).first()
        if v:
            return _variant_photo(v)
        b = Bundle.objects.filter(sku=sku).first()
        if b:
            bimg = b.images.first()
            if bimg and bimg.image:
                return bimg.image.url
            bi = b.items.select_related('product_variant').first()
            if bi and bi.product_variant:
                return _variant_photo(bi.product_variant)
        return ''

    items = []
    for item in order.items.all().select_related(
        'product_variant__product', 'bundle', 'course'
    ).prefetch_related(
        'product_variant__images', 'product_variant__product__images', 'bundle__images',
        'bundle_components__product_variant__images',
        'bundle_components__product_variant__product__images',
    ):
        photo_url = ''
        link = ''
        if item.product_variant_id and item.product_variant:
            v = item.product_variant
            if v.product_id:
                link = f'/product/{v.product.slug}'
            photo_url = _variant_photo(v)
        elif item.course_id and item.course:
            # A course is a digital product: link not to the course storefront page but to the
            # account area, where it is already unlocked (see the "Your course" block in
            # the template).
            link = '/my-courses'
            if item.course.cover_image:
                photo_url = item.course.cover_image.url
        elif item.bundle_id and item.bundle:
            link = f'/bundle/{item.bundle.slug}'
            # Photo fallback as on the storefront: bundle → first component.
            img = item.bundle.images.first()
            if img and img.image:
                photo_url = img.image.url
            else:
                comp = item.bundle_components.first()
                photo_url = _variant_photo(comp.product_variant if comp else None)
        if not photo_url and item.display_sku:
            photo_url = _photo_by_sku(item.display_sku)
        items.append({
            'name': item.display_name,        # fall back to the snapshot if the product was deleted
            'sku': item.display_sku,
            'link': link,
            'photo_url': photo_url,
            'price': item.price,
            'quantity': item.quantity,
            'total': item.get_total(),
        })
    from orders.models import format_phone
    course_items = [i for i in order.items.all() if i.course_id]
    return {
        'order': order,
        'items': items,
        'formatted_phone': format_phone(order.customer_phone),
        # A course is not shipped: delivery lines for a purely digital order are a lie ("Nova Poshta
        # at carrier rates" for something that goes nowhere).
        'has_courses': bool(course_items),
        'only_courses': bool(course_items) and len(course_items) == len(items),
        # Pickup: there is no carrier and no delivery address either - both template lines would
        # otherwise show emptiness and a false note about rates.
        'is_pickup': order.delivery_type == 'pickup',
        # The site and the API are on the same domain, so BACKEND_URL leads to the frontend as well.
        'courses_url': (getattr(settings, 'BACKEND_URL', '') or '').rstrip('/') + '/my-courses',
    }


def send_order_confirmation(order):
    email = order.customer_email
    if not email:
        return
    ctx = build_order_confirmation_context(order)
    # Embed the photos directly in the email (Content-ID) so images always display regardless of
    # BACKEND_URL/domain/https. Fall back to an absolute URL if the file happens to be unavailable
    # on disk (then at least something shows, as before).
    backend = (getattr(settings, 'BACKEND_URL', '') or '').rstrip('/')
    inline_images = []
    cid_by_path = {}  # dedup: one file - one attachment
    for it in ctx['items']:
        url = it.get('photo_url')
        if not url:
            continue
        path = _media_file_path(url)
        if path:
            cid = cid_by_path.get(path)
            if cid is None:
                thumb = _thumbnail_bytes(path)
                if thumb:
                    cid = f'photo{len(inline_images)}'
                    inline_images.append((cid, thumb[0], thumb[1]))
                    cid_by_path[path] = cid
            if cid:
                it['photo_url'] = f'cid:{cid}'
                continue
        # fallback: full http(s) URL (Gmail proxies external images)
        if not url.startswith('http'):
            it['photo_url'] = backend + url
    send_email(
        to=email,
        subject=f'Order #{order.order_number} received',
        template='order_confirmation',
        context=ctx,
        inline_images=inline_images,
    )


def send_stock_notification(notif):
    """A "back in stock" email for a StockNotification subscription (a product
    packaging or a bundle packaging)."""
    if notif.variant_id:
        variant = notif.variant
        from catalog.serializers import _variant_images_qs
        link = f'/product/{variant.product.slug}'
        price = variant.price
        img = next(iter(_variant_images_qs(variant)), None) or variant.product.images.first()
        what = 'product'
    else:
        bundle = notif.bundle
        link = f'/bundle/{bundle.slug}'
        price = bundle.get_price()
        img = bundle.images.first()
        what = 'bundle'
    photo_url = ''
    inline_images = []
    if img and img.image:
        # Embed the photo in the email (Content-ID) rather than using a relative URL - otherwise the
        # image does not load in the mail client. Fall back to an absolute URL if the file is
        # unavailable.
        path = _media_file_path(img.image.url)
        thumb = _thumbnail_bytes(path) if path else None
        if thumb:
            inline_images.append(('photo0', thumb[0], thumb[1]))
            photo_url = 'cid:photo0'
        else:
            backend = (getattr(settings, 'BACKEND_URL', '') or '').rstrip('/')
            photo_url = backend + img.image.url
    send_email(
        to=notif.email,
        subject=f'{what.capitalize()} is back in stock',
        template='stock_notification',
        context={
            'what': what, 'name': notif.target_name, 'price': price, 'link': link,
            'sku': notif.target_sku, 'photo_url': photo_url,
        },
        inline_images=inline_images,
    )


def send_registration_email(user):
    if not user.email:
        return
    from orders.models import format_phone
    send_email(
        to=user.email,
        subject='Welcome!',
        template='registration',
        context={'user': user, 'formatted_phone': format_phone(user.phone)},
    )
