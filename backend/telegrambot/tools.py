"""Claude agent tools: the only way the agent affects the shop.

Each tool is a pair: an async wrapper (for the SDK) + a synchronous _*_sync (ORM).
The wrappers are created by the build_tools(ctx) factory: ToolContext is closed over in them, so
the agent cannot forge telegram_user_id or the write-operation limit.

Before changing anything, every write function writes a RollbackEntry, so any action
can be rolled back (via the revert tool or the rollback_command CLI).
"""
from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation

from asgiref.sync import sync_to_async
from django.conf import settings
from django.utils import timezone

from claude_agent_sdk import tool, ToolAnnotations

from . import rollback
from .context import ToolContext, WriteLimitExceeded

# Product fields the agent is allowed to change via update_product_field.
# is_active is NOT here: it is derived (maintained by the sync_product_is_active signal).
EDITABLE_PRODUCT_FIELDS = {
    'name', 'description', 'application_norms', 'meta_title', 'meta_description',
}


# ─── helpers ────────────────────────────────────────────────────────────────

def _content(obj) -> dict:
    return {'content': [{'type': 'text', 'text': json.dumps(obj, ensure_ascii=False)}]}


def _err(msg: str) -> dict:
    return {'content': [{'type': 'text', 'text': msg}], 'is_error': True}


def _admin_url(path: str) -> str:
    base = getattr(settings, 'SITE_URL', '').rstrip('/')
    return f'{base}{path}'


def _parse_price(value) -> Decimal:
    try:
        price = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f'Invalid price: {value!r}')
    if price <= 0:
        raise ValueError('The price must be greater than 0')
    return price


# ─── read-only: synchronous ORM logic ───────────────────────────────────────

def _find_product_sync(query: str) -> dict:
    from django.db.models import Q
    from catalog.models import ProductVariant

    query = (query or '').strip()
    if not query:
        return {'count': 0, 'matches': []}

    qs = (ProductVariant.objects
          .filter(Q(name__icontains=query)
                  | Q(sku__iexact=query)
                  | Q(slug__icontains=query)
                  | Q(product__name__icontains=query))
          .select_related('product')
          .order_by('product__name', 'id')[:20])

    matches = [{
        'variant_id': v.id,
        'product_id': v.product_id,
        'product_name': v.product.name,
        'variant_name': v.name,
        'sku': v.sku,
        'price': str(v.price),
        'stock': v.stock,
        'is_active': v.is_active,
    } for v in qs]
    return {'count': len(matches), 'matches': matches}


def _get_product_sync(product_id: int) -> dict:
    from catalog.models import Product

    p = (Product.objects.filter(pk=product_id)
         .prefetch_related('variants', 'categories').first())
    if p is None:
        raise ValueError(f'Product #{product_id} not found')
    return {
        'product_id': p.id,
        'name': p.name,
        'is_active': p.is_active,
        'description': p.description[:500],
        'categories': [{'id': c.id, 'name': c.name} for c in p.categories.all()],
        'variants': [{
            'variant_id': v.id, 'name': v.name, 'sku': v.sku,
            'price': str(v.price), 'stock': v.stock, 'is_active': v.is_active,
        } for v in p.variants.all()],
    }


def _count_unprocessed_orders_sync() -> dict:
    from orders.models import Order
    new_qs = Order.objects.filter(status='new')
    return {
        'unprocessed': new_qs.count(),
        'unprocessed_paid': new_qs.filter(payment_status='paid').count(),
    }


def _serialize_order(o, with_items: bool = False) -> dict:
    from orders.models import format_phone
    d = {
        'order_number': o.order_number,
        'status': o.get_status_display(),
        'payment_status': o.get_payment_status_display(),
        'customer_name': o.customer_name,
        'customer_phone': format_phone(o.customer_phone),
        'total_price': str(o.total_price) if o.total_price is not None else None,
        'delivery_type': o.get_delivery_type_display(),
        'created_at': timezone.localtime(o.created_at).strftime('%Y-%m-%d %H:%M'),
    }
    if with_items:
        d['items'] = [
            {'name': i.display_name, 'qty': i.quantity, 'price': str(i.price)}
            for i in o.items.all()
        ]
    return d


def _list_unprocessed_orders_sync(limit: int = 10) -> dict:
    from orders.models import Order
    limit = max(1, min(int(limit or 10), 50))
    qs = Order.objects.filter(status='new').order_by('-created_at')[:limit]
    return {'count': len(qs), 'orders': [_serialize_order(o) for o in qs]}


def _get_recent_orders_sync(limit: int = 5, status: str = '') -> dict:
    """Latest orders of ANY status (for 'what is the last order', 'the last 5').
    Optional status filter: new/processing/shipped/delivered/cancelled."""
    from orders.models import Order
    limit = max(1, min(int(limit or 5), 50))
    qs = Order.objects.order_by('-created_at')
    valid = {c[0] for c in Order.STATUS_CHOICES}
    if status and status in valid:
        qs = qs.filter(status=status)
    qs = qs[:limit]
    return {'count': len(qs), 'orders': [_serialize_order(o) for o in qs]}


def _get_order_sync(order_number) -> dict:
    from orders.models import Order
    q = str(order_number).strip()
    o = (Order.objects.filter(order_number=q)
         .prefetch_related('items').first())
    if o is None:  # partial match (the operator may not have given the full number)
        o = (Order.objects.filter(order_number__icontains=q)
             .prefetch_related('items').order_by('-created_at').first())
    if o is None:
        raise ValueError(f'Order "{q}" not found')
    return _serialize_order(o, with_items=True)


def _search_orders_sync(product: str = '', days=None, status: str = '',
                        limit: int = 50) -> dict:
    """Order analytics: filter by product (product), period (days back),
    status. Returns the number of orders, units of the product sold and revenue.

    The product is matched by tokens (all words from product must appear in the
    item), so 'Plant food 100 ml' will find the right pack size."""
    import re
    from datetime import timedelta
    from django.db.models import Q
    from django.utils import timezone
    from orders.models import Order

    limit = max(1, min(int(limit or 50), 200))
    tokens = [t for t in re.split(r'[\s,]+', (product or '').strip()) if t]
    main = max(tokens, key=len) if tokens else ''  # longest word, used for the DB filter

    oqs = Order.objects.all()
    if days:
        oqs = oqs.filter(created_at__gte=timezone.now() - timedelta(days=int(days)))
    if status and status in {c[0] for c in Order.STATUS_CHOICES}:
        oqs = oqs.filter(status=status)
    if main:
        oqs = oqs.filter(
            Q(items__product_variant_name__icontains=main)
            | Q(items__product_variant__name__icontains=main)
            | Q(items__product_variant_sku__icontains=main)
        ).distinct()
    oqs = oqs.order_by('-created_at').prefetch_related('items')[:limit]

    def _item_matches(i):
        hay = f'{i.display_name} {i.product_variant_sku or ""}'.lower()
        return all(tok.lower() in hay for tok in tokens)

    orders, total_units, revenue = [], 0, Decimal('0')
    for o in oqs:
        items = [i for i in o.items.all() if _item_matches(i)] if tokens else list(o.items.all())
        if tokens and not items:
            continue
        qty = sum(i.quantity for i in items)
        rev = sum((i.price * i.quantity for i in items), Decimal('0'))
        total_units += qty
        revenue += rev
        orders.append({
            'order_number': o.order_number,
            'created_at': timezone.localtime(o.created_at).strftime('%Y-%m-%d %H:%M'),
            'status': o.get_status_display(),
            'qty_of_product': qty,
            'order_total': str(o.total_price) if o.total_price is not None else None,
        })
    return {
        'orders_count': len(orders),
        'total_units': total_units,
        'product_revenue': str(revenue),
        'filter': {'product': product, 'days': days, 'status': status},
        'orders': orders,
    }


def _list_categories_sync() -> dict:
    from catalog.models import Category
    cats = Category.objects.all().order_by('sort_order', 'name')
    return {'categories': [
        {'id': c.id, 'name': c.name, 'parent_id': c.parent_id} for c in cats
    ]}


def _fetch_url_description_sync(url: str) -> dict:
    import requests
    from bs4 import BeautifulSoup

    url = (url or '').strip()
    if not (url.startswith('http://') or url.startswith('https://')):
        raise ValueError('Only http/https URLs are allowed')

    MAX_BYTES = 2_000_000
    resp = requests.get(url, timeout=15, stream=True, headers={
        'User-Agent': 'Mozilla/5.0 (compatible; ShopBot/1.0)'
    })
    resp.raise_for_status()
    chunks, size = [], 0
    for chunk in resp.iter_content(chunk_size=16384, decode_unicode=False):
        chunks.append(chunk)
        size += len(chunk)
        if size > MAX_BYTES:
            break
    html = b''.join(chunks).decode(resp.encoding or 'utf-8', errors='replace')

    soup = BeautifulSoup(html, 'html.parser')
    for tag in soup(['script', 'style', 'noscript', 'header', 'footer', 'nav']):
        tag.decompose()
    text = ' '.join(soup.get_text(separator=' ').split())
    return {'url': url, 'text': text[:8000]}


# ─── write: synchronous ORM logic (writes a RollbackEntry) ───────────────────

def _create_product_draft_sync(ctx: ToolContext, *, name, category_id, price,
                               stock=0, description='') -> dict:
    from django.db import transaction
    from catalog.models import Product, ProductVariant, Category, ProductImage

    name = (name or '').strip()
    if not name:
        raise ValueError('The product name is required')
    price = _parse_price(price)
    stock = max(0, int(stock or 0))

    category = Category.objects.filter(pk=category_id).first()
    if category is None:
        raise ValueError(f'Category #{category_id} not found (list: list_categories)')

    with transaction.atomic():
        product = Product(name=name, description=description or '')
        product.save()
        product.categories.add(category)
        # Draft: is_active=False hides the product from the storefront (signal
        # sync_product_is_active). Publishing is a separate step, publish_variant.
        variant = ProductVariant(product=product, price=price, stock=stock,
                                 is_active=False)
        variant.save()

        image_ids = []
        if ctx.image_temp_key:
            imgs = ProductImage.objects.filter(temp_key=ctx.image_temp_key)
            image_ids = list(imgs.values_list('id', flat=True))
            imgs.update(product=product, temp_key=None)

        entry = rollback.record(
            command_log_id=ctx.command_log_id, action='create_product',
            model_label='catalog.Product', object_pk=product.id,
            before=None,
            after={'product_id': product.id, 'variant_id': variant.id,
                   'image_ids': image_ids},
        )

    return {
        'product_id': product.id,
        'variant_id': variant.id,
        'is_active': False,
        'photos_attached': len(image_ids),
        'admin_url': _admin_url(f'/admin/catalog/product/{product.id}/change/'),
        'rollback_id': entry.id,
        'note': 'A DRAFT was created (hidden). To publish — publish_variant.',
    }


def _publish_variant_sync(ctx: ToolContext, *, variant_id) -> dict:
    from catalog.models import ProductVariant
    v = ProductVariant.objects.filter(pk=variant_id).first()
    if v is None:
        raise ValueError(f'Variant #{variant_id} not found')
    if v.is_active:
        return {'variant_id': v.id, 'is_active': True, 'note': 'Already published'}

    before = {'is_active': False}
    v.is_active = True
    v.save(update_fields=['is_active'])  # the signal will make the product visible
    entry = rollback.record(
        command_log_id=ctx.command_log_id, action='publish_variant',
        model_label='catalog.ProductVariant', object_pk=v.id,
        before=before, after={'is_active': True},
    )
    return {'variant_id': v.id, 'is_active': True, 'rollback_id': entry.id,
            'note': 'Published — the product is visible on the storefront.'}


def _update_variant_stock_sync(ctx: ToolContext, *, variant_id, stock) -> dict:
    from catalog.models import ProductVariant
    stock = int(stock)
    v = ProductVariant.objects.filter(pk=variant_id).first()
    if v is None:
        raise ValueError(f'Variant #{variant_id} not found')

    before = {'stock': v.stock}
    v.stock = stock
    v.save(update_fields=['stock'])  # update_fields so that the label signals fire
    entry = rollback.record(
        command_log_id=ctx.command_log_id, action='update_stock',
        model_label='catalog.ProductVariant', object_pk=v.id,
        before=before, after={'stock': stock},
    )
    return {'variant_id': v.id, 'stock': stock, 'was': before['stock'],
            'rollback_id': entry.id}


def _update_variant_price_sync(ctx: ToolContext, *, variant_id, price) -> dict:
    from catalog.models import ProductVariant
    price = _parse_price(price)
    v = ProductVariant.objects.filter(pk=variant_id).first()
    if v is None:
        raise ValueError(f'Variant #{variant_id} not found')

    before = {'price': str(v.price)}  # Decimal as a string for JSON
    v.price = price
    v.save(update_fields=['price'])
    entry = rollback.record(
        command_log_id=ctx.command_log_id, action='update_price',
        model_label='catalog.ProductVariant', object_pk=v.id,
        before=before, after={'price': str(price)},
    )
    return {'variant_id': v.id, 'price': str(price), 'was': before['price'],
            'rollback_id': entry.id}


def _update_product_field_sync(ctx: ToolContext, *, product_id, field, value) -> dict:
    from catalog.models import Product
    if field not in EDITABLE_PRODUCT_FIELDS:
        raise ValueError(
            f'The field {field!r} cannot be changed. Allowed: '
            f'{sorted(EDITABLE_PRODUCT_FIELDS)}'
        )
    p = Product.objects.filter(pk=product_id).first()
    if p is None:
        raise ValueError(f'Product #{product_id} not found')
    value = str(value)
    if field == 'name' and not value.strip():
        raise ValueError('The name cannot be empty')

    before = {field: getattr(p, field)}
    setattr(p, field, value)
    p.save(update_fields=[field])
    entry = rollback.record(
        command_log_id=ctx.command_log_id, action='update_field',
        model_label='catalog.Product', object_pk=p.id,
        before=before, after={field: value},
    )
    return {'product_id': p.id, 'field': field, 'value': value,
            'was': before[field], 'rollback_id': entry.id}


def _create_blog_from_url_sync(ctx: ToolContext, *, url) -> dict:
    """Creates a DRAFT blog post from a link: YouTube (transcript -> article) or a
    regular article (scraping -> LLM). Reuses blog/views.py."""
    import requests
    from bs4 import BeautifulSoup
    from django.core.files.base import ContentFile
    from blog.models import BlogPost
    from blog.views import (
        YOUTUBE_RX, _clean_title_with_ai, _fetch_transcript, _generate_article_html,
    )
    from core import llm

    url = (url or '').strip()
    if not (url.startswith('http://') or url.startswith('https://')):
        raise ValueError('Only http/https links are allowed')

    title, content_html, youtube_url = '', '', ''
    thumb_bytes, thumb_name = None, ''

    m = YOUTUBE_RX.search(url)
    if m:  # ── YouTube: transcript → article ──
        video_id = m.group(1)
        youtube_url = url
        raw_title = ''
        try:
            oe = requests.get('https://www.youtube.com/oembed',
                              params={'url': f'https://www.youtube.com/watch?v={video_id}',
                                      'format': 'json'}, timeout=10).json()
            raw_title = oe.get('title', '') or ''
        except Exception:
            pass
        title = _clean_title_with_ai(raw_title) or f'Video {video_id}'
        for variant in ('maxresdefault', 'sddefault', 'hqdefault', 'mqdefault'):
            try:
                rr = requests.get(f'https://img.youtube.com/vi/{video_id}/{variant}.jpg', timeout=10)
                if rr.status_code == 200 and rr.content:
                    thumb_bytes, thumb_name = rr.content, f'youtube-{video_id}.jpg'
                    break
            except Exception:
                continue
        transcript, lang = _fetch_transcript(video_id)
        if transcript:
            content_html = _generate_article_html(title, transcript, lang) or ''
    else:  # ── Regular article: scraping → LLM ──
        resp = requests.get(url, timeout=15, headers={'User-Agent': 'Mozilla/5.0'})
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')
        ogt = soup.find('meta', property='og:title')
        raw_title = (ogt.get('content') if ogt else None) or (
            soup.title.string if soup.title else '') or ''
        ogi = soup.find('meta', property='og:image')
        img_url = (ogi.get('content') if ogi else '') or ''
        for tag in soup(['script', 'style', 'noscript', 'header', 'footer', 'nav']):
            tag.decompose()
        text = ' '.join(soup.get_text(separator=' ').split())[:12000]
        prompt = (
            'You are given the raw text of an article from a website. Rewrite it as a tidy blog '
            'post in Ukrainian, in HTML (tags <p>,<h2>,<ul>,<li>,<strong>). '
            'Do not make anything up — rely only on the text. '
            'Return JSON {"title":"...","content_html":"..."}.'
        )
        data = llm.text_to_json(f'{prompt}\n\nHint title: {raw_title}\n\nText:\n{text}')
        title = (data.get('title') or raw_title or 'Untitled').strip()
        content_html = data.get('content_html') or ''
        if img_url:
            try:
                ir = requests.get(img_url, timeout=10)
                if ir.status_code == 200 and ir.content:
                    thumb_bytes, thumb_name = ir.content, 'cover.jpg'
            except Exception:
                pass

    post = BlogPost(title=title[:200], content=content_html,
                    youtube_url=youtube_url, is_active=False)
    post.save()  # slug is auto-generated
    if thumb_bytes:
        post.preview_image.save(thumb_name, ContentFile(thumb_bytes), save=True)

    entry = rollback.record(
        command_log_id=ctx.command_log_id, action='create_blog',
        model_label='blog.BlogPost', object_pk=post.id,
        before=None, after={'blog_id': post.id},
    )
    return {
        'blog_id': post.id, 'title': post.title, 'is_active': False,
        'has_cover': bool(thumb_bytes), 'has_content': bool(content_html),
        'admin_url': _admin_url(f'/admin/blog/blogpost/{post.id}/change/'),
        'rollback_id': entry.id,
        'note': 'A DRAFT post was created (hidden). To publish — publish_blog.',
    }


def _publish_blog_sync(ctx: ToolContext, *, blog_id) -> dict:
    from blog.models import BlogPost
    p = BlogPost.objects.filter(pk=blog_id).first()
    if p is None:
        raise ValueError(f'Post #{blog_id} not found')
    if p.is_active:
        return {'blog_id': p.id, 'is_active': True, 'note': 'Already published'}
    p.is_active = True
    p.save(update_fields=['is_active'])
    entry = rollback.record(
        command_log_id=ctx.command_log_id, action='publish_blog',
        model_label='blog.BlogPost', object_pk=p.id,
        before={'is_active': False}, after={'is_active': True},
    )
    return {'blog_id': p.id, 'is_active': True, 'rollback_id': entry.id,
            'note': 'Published — the post is visible.'}


def _create_bundle_sync(ctx: ToolContext, *, name, items, category_id=None,
                        price=None, description='') -> dict:
    """Creates a bundle DRAFT (Bundle + items).
    items = [{variant_id, qty}]. price is optional — otherwise it is computed from the products."""
    from django.db import transaction
    from django.core.files.base import ContentFile
    from catalog.models import (
        Bundle, BundleItem, ProductVariant, Category,
        ProductImage, BundleImage,
    )

    name = (name or '').strip()
    if not name:
        raise ValueError('The bundle name is required')
    if not items:
        raise ValueError('The bundle must contain at least one product (items=[{variant_id, qty}])')

    norm = []
    for it in items:
        vid = int(it['variant_id'])
        qty = max(1, int(it.get('qty', 1)))
        if not ProductVariant.objects.filter(pk=vid).exists():
            raise ValueError(f'Variant #{vid} not found (search through find_product)')
        norm.append((vid, qty))

    with transaction.atomic():
        bundle = Bundle(name=name, description=description or '', is_active=False)
        if price is not None:
            bundle.price = _parse_price(price)
        bundle.save()
        if category_id:
            cat = Category.objects.filter(pk=category_id).first()
            if cat:
                bundle.categories.add(cat)
        for vid, qty in norm:
            BundleItem.objects.create(bundle=bundle, product_variant_id=vid, quantity=qty)

        if ctx.image_temp_key:
            pi = ProductImage.objects.filter(temp_key=ctx.image_temp_key).first()
            if pi:
                data = pi.image.read()
                BundleImage.objects.create(
                    bundle=bundle, image=ContentFile(data, name=f'{ctx.image_temp_key}.jpg'),
                    is_main=True)
                pi.delete()

        entry = rollback.record(
            command_log_id=ctx.command_log_id, action='create_bundle',
            model_label='catalog.Bundle', object_pk=bundle.id,
            before=None, after={'bundle_id': bundle.id},
        )
        computed = bundle.get_price()

    return {
        'bundle_id': bundle.id, 'items': len(norm),
        'price': str(computed) if computed is not None else None,
        'is_active': False,
        'admin_url': _admin_url(f'/admin/catalog/bundle/{bundle.id}/change/'),
        'rollback_id': entry.id,
        'note': 'A DRAFT bundle was created (hidden). The price is calculated from the products '
                'if not set. To publish — publish_bundle.',
    }


def _publish_bundle_sync(ctx: ToolContext, *, bundle_id) -> dict:
    from catalog.models import Bundle
    b = Bundle.objects.filter(pk=bundle_id).first()
    if b is None:
        raise ValueError(f'Bundle #{bundle_id} not found')
    if b.is_active:
        return {'bundle_id': b.id, 'is_active': True, 'note': 'Already published'}
    b.is_active = True
    b.save(update_fields=['is_active'])
    entry = rollback.record(
        command_log_id=ctx.command_log_id, action='publish_bundle',
        model_label='catalog.Bundle', object_pk=b.id,
        before={'is_active': False}, after={'is_active': True},
    )
    return {'bundle_id': b.id, 'is_active': True, 'rollback_id': entry.id,
            'note': 'Published — the bundle is visible.'}


def _revert_sync(ctx: ToolContext, *, rollback_id=None) -> dict:
    from .models import RollbackEntry
    if rollback_id in (None, '', 'last'):
        entry = (RollbackEntry.objects.filter(is_reverted=False)
                 .order_by('-created_at').first())
        if entry is None:
            raise ValueError('Nothing to roll back')
    else:
        entry = RollbackEntry.objects.filter(pk=int(rollback_id)).first()
        if entry is None:
            raise ValueError(f'Rollback #{rollback_id} not found')
    msg = rollback.apply_revert(entry)
    return {'reverted_id': entry.id, 'message': msg}


# ─── factory of async tools for the SDK ──────────────────────────────────────

def build_tools(ctx: ToolContext) -> list:
    """Returns a list of SdkMcpTool with the context closed over.

    Names: the agent sees them as mcp__shop__<name>. Only these tools are allowed
    (see agent.build_options: allowed_tools + can_use_tool deny-by-default)."""

    ro = ToolAnnotations(readOnlyHint=True)
    rw = ToolAnnotations(readOnlyHint=False)

    async def _run_write(sync_fn, **kw):
        try:
            ctx.bump_write()
        except WriteLimitExceeded as e:
            return _err(str(e))
        try:
            res = await sync_to_async(sync_fn)(ctx, **kw)
            return _content(res)
        except Exception as e:
            return _err(f'Error: {e}')

    async def _run_read(sync_fn, **kw):
        try:
            res = await sync_to_async(sync_fn)(**kw)
            return _content(res)
        except Exception as e:
            return _err(f'Error: {e}')

    # ── read-only ──
    @tool('find_product', 'Find products/variants by name, SKU or slug. '
          'Returns a list of matches with variant_id, price and stock.',
          {'query': str}, annotations=ro)
    async def find_product(args):
        return await _run_read(_find_product_sync, query=args['query'])

    @tool('get_product', 'Product details by product_id: variants, prices, stock, categories.',
          {'product_id': int}, annotations=ro)
    async def get_product(args):
        return await _run_read(_get_product_sync, product_id=args['product_id'])

    @tool('count_unprocessed_orders',
          'How many unprocessed orders (status "New") and how many of them are paid.',
          {}, annotations=ro)
    async def count_unprocessed_orders(args):
        return await _run_read(_count_unprocessed_orders_sync)

    @tool('list_unprocessed_orders', 'A list of the latest UNPROCESSED orders (status "New").',
          {'type': 'object', 'properties': {'limit': {'type': 'integer'}}},
          annotations=ro)
    async def list_unprocessed_orders(args):
        return await _run_read(_list_unprocessed_orders_sync, limit=args.get('limit', 10))

    @tool('get_recent_orders', 'The latest orders of ANY status (for "what is the '
          'last order", "the last 5 orders"). Optional status: '
          'new/processing/shipped/delivered/cancelled.',
          {'type': 'object', 'properties': {
              'limit': {'type': 'integer'}, 'status': {'type': 'string'}}},
          annotations=ro)
    async def get_recent_orders(args):
        return await _run_read(_get_recent_orders_sync,
                               limit=args.get('limit', 5), status=args.get('status', ''))

    @tool('get_order', 'Order details by number (with line items).',
          {'order_number': str}, annotations=ro)
    async def get_order(args):
        return await _run_read(_get_order_sync, order_number=args['order_number'])

    @tool('search_orders', 'Search/analytics of orders by product (product), '
          'period (days — how many days back) and status. Returns the number '
          'of orders, the units of the product sold and the revenue from it. Example: '
          'product="Plant food 100 ml", days=7.',
          {'type': 'object', 'properties': {
              'product': {'type': 'string'},
              'days': {'type': 'integer'},
              'status': {'type': 'string'},
              'limit': {'type': 'integer'}}}, annotations=ro)
    async def search_orders(args):
        return await _run_read(_search_orders_sync,
                               product=args.get('product', ''), days=args.get('days'),
                               status=args.get('status', ''), limit=args.get('limit', 50))

    @tool('list_categories', 'A list of all categories (id, name, parent_id) — '
          'needed to choose category_id when creating a product.',
          {}, annotations=ro)
    async def list_categories(args):
        return await _run_read(_list_categories_sync)

    @tool('fetch_url_description',
          'Download a page by URL and return the cleaned text (for a product description).',
          {'url': str}, annotations=ro)
    async def fetch_url_description(args):
        return await _run_read(_fetch_url_description_sync, url=args['url'])

    # ── write ──
    @tool('create_product_draft',
          'Create a product DRAFT (hidden, is_active=False) with one variant. '
          'The photo (if there was one in the message) is attached automatically. '
          'To publish, call publish_variant afterwards.',
          {'type': 'object', 'properties': {
              'name': {'type': 'string'},
              'category_id': {'type': 'integer'},
              'price': {'type': 'number'},
              'stock': {'type': 'integer'},
              'description': {'type': 'string'},
          }, 'required': ['name', 'category_id', 'price']}, annotations=rw)
    async def create_product_draft(args):
        return await _run_write(
            _create_product_draft_sync, name=args['name'],
            category_id=args['category_id'], price=args['price'],
            stock=args.get('stock', 0), description=args.get('description', ''))

    @tool('publish_variant', 'Publish a variant (is_active=True) — the product becomes '
          'visible on the storefront.', {'variant_id': int}, annotations=rw)
    async def publish_variant(args):
        return await _run_write(_publish_variant_sync, variant_id=args['variant_id'])

    @tool('update_variant_stock', 'Set the stock of a variant.',
          {'variant_id': int, 'stock': int}, annotations=rw)
    async def update_variant_stock(args):
        return await _run_write(_update_variant_stock_sync,
                                variant_id=args['variant_id'], stock=args['stock'])

    @tool('update_variant_price', 'Set the price of a variant (UAH).',
          {'variant_id': int, 'price': float}, annotations=rw)
    async def update_variant_price(args):
        return await _run_write(_update_variant_price_sync,
                                variant_id=args['variant_id'], price=args['price'])

    @tool('update_product_field',
          'Change one text field of a product (name/description/application_norms/'
          'meta_title/meta_description).',
          {'product_id': int, 'field': str, 'value': str}, annotations=rw)
    async def update_product_field(args):
        return await _run_write(_update_product_field_sync,
                                product_id=args['product_id'],
                                field=args['field'], value=args['value'])

    @tool('create_blog_from_url',
          'Create a blog post DRAFT from a link: a YouTube video (from the '
          'transcript) or a regular article (from scraping). Hidden until '
          'published. Publishing — publish_blog.',
          {'url': str}, annotations=rw)
    async def create_blog_from_url(args):
        return await _run_write(_create_blog_from_url_sync, url=args['url'])

    @tool('publish_blog', 'Publish a blog post (is_active=True).',
          {'blog_id': int}, annotations=rw)
    async def publish_blog(args):
        return await _run_write(_publish_blog_sync, blog_id=args['blog_id'])

    @tool('create_bundle',
          'Create a bundle DRAFT from products. items — a list of {variant_id, qty} '
          '(look up variant_id through find_product). price is optional (otherwise it is '
          'calculated from the products). Hidden until published. Publishing — publish_bundle.',
          {'type': 'object', 'properties': {
              'name': {'type': 'string'},
              'items': {'type': 'array', 'items': {'type': 'object', 'properties': {
                  'variant_id': {'type': 'integer'}, 'qty': {'type': 'integer'}}}},
              'category_id': {'type': 'integer'},
              'price': {'type': 'number'},
              'description': {'type': 'string'},
          }, 'required': ['name', 'items']}, annotations=rw)
    async def create_bundle(args):
        return await _run_write(
            _create_bundle_sync, name=args['name'], items=args['items'],
            category_id=args.get('category_id'), price=args.get('price'),
            description=args.get('description', ''))

    @tool('publish_bundle', 'Publish a bundle (is_active=True).',
          {'bundle_id': int}, annotations=rw)
    async def publish_bundle(args):
        return await _run_write(_publish_bundle_sync, bundle_id=args['bundle_id'])

    @tool('revert', 'Roll back a change by rollback_id (or the last one if not specified).',
          {'type': 'object', 'properties': {'rollback_id': {'type': 'integer'}}},
          annotations=rw)
    async def revert(args):
        # revert does not count toward the write limit and no RollbackEntry is written for the
        # rollback itself
        try:
            res = await sync_to_async(_revert_sync)(ctx, rollback_id=args.get('rollback_id'))
            return _content(res)
        except Exception as e:
            return _err(f'Rollback error: {e}')

    return [
        find_product, get_product, count_unprocessed_orders,
        list_unprocessed_orders, get_recent_orders, get_order, search_orders,
        list_categories, fetch_url_description, create_product_draft,
        publish_variant, update_variant_stock, update_variant_price,
        update_product_field, create_blog_from_url, publish_blog,
        create_bundle, publish_bundle, revert,
    ]


# Tool names (for allowed_tools and can_use_tool in agent.py).
TOOL_NAMES = [
    'find_product', 'get_product', 'count_unprocessed_orders',
    'list_unprocessed_orders', 'get_recent_orders', 'get_order', 'search_orders',
    'list_categories', 'fetch_url_description', 'create_product_draft',
    'publish_variant', 'update_variant_stock', 'update_variant_price',
    'update_product_field', 'create_blog_from_url', 'publish_blog',
    'create_bundle', 'publish_bundle', 'revert',
]
