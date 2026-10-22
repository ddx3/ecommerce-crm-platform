import json
import logging
from core.jsonutils import js_json
from django.conf import settings
from django.utils import timezone
from django.contrib import admin
from django import forms
from catalog.admin import VariantPickerWidget
from catalog.models import ProductVariant, Bundle
from .models import Order, OrderItem, Invoice, InvoiceNameMapping, format_phone

log = logging.getLogger(__name__)


class OrderItemForm(forms.ModelForm):
    class Meta:
        model = OrderItem
        fields = ['product_variant', 'bundle', 'quantity', 'price', 'allow_negative_stock']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['product_variant'].widget = VariantPickerWidget()
        self.fields['product_variant'].label = 'Product'
        self.fields['product_variant'].required = False
        self.fields['bundle'].required = False
        self.fields['price'].required = False
        # Written/read by the JS items table (oi-table) as a plain hidden string,
        # not a checkbox; otherwise CheckboxInput.value_from_datadict would treat the item as
        # checked merely because the field is PRESENT in the POST, ignoring its value.
        self.fields['allow_negative_stock'].widget = forms.HiddenInput()
        self.fields['allow_negative_stock'].required = False


class OrderItemFormSet(forms.BaseInlineFormSet):
    """The same product/bundle cannot be two order items: stock would be written off
    twice, and the order total (computed from the items table in the CRM) would diverge from the
    items.
    The items table merges a repeated selection into the quantity; this is a safety net in case
    its JS did not run."""

    def clean(self):
        super().clean()
        seen = set()
        for form in self.forms:
            cd = getattr(form, 'cleaned_data', None)
            if not cd or cd.get('DELETE'):
                continue
            v, b = cd.get('product_variant'), cd.get('bundle')
            if v:
                key = ('v', v.pk)
            elif b:
                key = ('b', b.pk)
            else:
                continue
            if key in seen:
                raise forms.ValidationError(
                    '"%s" appears in the order twice. Remove the extra item and set the right '
                    'quantity on one of them.' % (v or b))
            seen.add(key)


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    form = OrderItemForm
    formset = OrderItemFormSet
    extra = 1
    fields = ['product_variant', 'bundle', 'quantity', 'price', 'allow_negative_stock']


class OrderAdminForm(forms.ModelForm):
    class Meta:
        model = Order
        exclude = ['user', 'order_number']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['delivery_type'].required = False
        delivery_choices = list(Order.DELIVERY_CHOICES)
        # Old Ukrposhta order: keep its option in the list, otherwise the widget
        # would open empty and "Save" would erase the delivery type.
        current_delivery = getattr(self.instance, 'delivery_type', '')
        if current_delivery and current_delivery not in dict(delivery_choices):
            delivery_choices += [c for c in Order.LEGACY_DELIVERY_CHOICES if c[0] == current_delivery]
        self.fields['delivery_type'].choices = [('', '— not selected —')] + delivery_choices
        # The type choice is visible: besides Nova Poshta there is pickup (rare, arranged in
        # person).
        # The NP picker and the parcel blocks in change_form depend on the field value;
        # when switched to "Pickup" the picker is hidden, no address is needed.
        if not current_delivery:
            self.initial['delivery_type'] = 'nova_poshta'
        self.fields['payment_method'].required = False
        self.fields['payment_method'].choices = [('', '— not selected —')] + list(Order.PAYMENT_CHOICES)
        # delivery_address is required in the model, but its widget is hidden by CSS
        # (the address is set by the NP picker). If a manager creates an order by hand and has not
        # yet
        # chosen a branch, the required error landed on an INVISIBLE field, so the form silently
        # was not saved ("nothing happens"). We make it optional;
        # delivery can be filled in later.
        self.fields['delivery_address'].required = False
        # Name and phone are required only for delivery: with pickup the customer comes
        # in person, and the data may be absent. The delivery type is known only after the form
        # is validated, so requiredness for Nova Poshta is checked by clean(), not by the field.
        self.fields['customer_name'].required = False
        self.fields['customer_phone'].required = False
        # Likewise for the dropshipping parcel address (also hidden).
        if 'drop_delivery_address' in self.fields:
            self.fields['drop_delivery_address'].required = False
        # Remove Safari contacts AutoFill (the person icon overlaps the phone/email)
        for f in self.fields.values():
            f.widget.attrs.setdefault('autocomplete', 'off')

    def clean_delivery_type(self):
        # Don't allow an empty value in the DB. But don't overwrite an already saved type either:
        # an unconditional fallback to 'nova_poshta' would silently cancel pickup on
        # any widget failure.
        value = self.cleaned_data.get('delivery_type')
        if value:
            return value
        return getattr(self.instance, 'delivery_type', '') or 'nova_poshta'

    def clean(self):
        cleaned = super().clean()
        # Pickup: there is no delivery address. We clear it on the server, not only in JS:
        # otherwise an order switched from NP would keep the old branch in the DB,
        # and the export and the letter would show a delivery that doesn't exist.
        if cleaned.get('delivery_type') == 'pickup':
            cleaned['delivery_address'] = ''
        else:
            for name in ('customer_name', 'customer_phone'):
                if name not in self.errors and not (cleaned.get(name) or '').strip():
                    self.add_error(name, forms.ValidationError(
                        self.fields[name].error_messages['required'], code='required'))
        return cleaned


def _order_item_data(item, request):
    def _first_photo(images_qs):
        # iterate prefetched cache instead of .first() (which issues a new query)
        img = next(iter(images_qs.all()), None)
        if img and img.image:
            return request.build_absolute_uri(img.image.url)
        return ''

    def _variant_photo(pv):
        """First photo of a variant, taking the new color_value scheme into account
        (the photo belongs to the color, not the variant). Fallback: the product photo.
        Uses the prefetch cache (_variant_images_cached), no N+1; semantics
        identical to _variant_images_qs (verified A/B on all order items)."""
        if not pv:
            return ''
        try:
            from catalog.serializers import _variant_images_cached
            img = next(iter(_variant_images_cached(pv)), None)
            if img and img.image:
                return request.build_absolute_uri(img.image.url)
        except Exception:
            pass
        if pv.product_id:
            return _first_photo(pv.product.images)
        return ''

    def _site_url(slug, kind, variant_id=None):
        """Public link to the site (SITE_URL + /product|/bundle/slug).
        For a product we add ?variant= so that exactly the variant from the item opens."""
        if not slug:
            return ''
        url = settings.SITE_URL.rstrip('/') + '/' + kind + '/' + slug
        if variant_id and kind == 'product':
            url += '?variant=' + str(variant_id)
        return url

    price_str = str(item.price) if item.price is not None else ''

    if item.bundle_id and item.bundle:
        bundle = item.bundle
        # Fallback chain: bundle photo → first component's photo.
        bundle_photo = _first_photo(bundle.images)
        # snapshot of this OrderItem's components (the single source of truth for the UI);
        # the current Bundle.items contents are used only as a fallback.
        comps = list(item.bundle_components.all())
        if not bundle_photo:
            # snapshot of this OrderItem's components or Bundle.items
            first_comp = comps[0] if comps else None
            if first_comp and first_comp.product_variant:
                bundle_photo = _variant_photo(first_comp.product_variant)
            else:
                first_bi = next(iter(bundle.items.all()), None)
                if first_bi and first_bi.product_variant:
                    bundle_photo = _variant_photo(first_bi.product_variant)
        bundle_items = []
        if comps:
            for c in comps:
                pv = c.product_variant
                bundle_items.append({
                    'comp_id': c.pk,
                    'comp_key': 'c%s' % c.pk,
                    'name': c.display_name,
                    'qty_per_set': c.quantity,
                    'qty': c.quantity * item.quantity,
                    'photo': _variant_photo(pv) if pv else '',
                    'variant_id': pv.pk if pv else None,
                    'sku': c.product_variant_sku or (pv.sku if pv else ''),
                    'stock': (pv.stock if pv else 0) or 0,
                })
        else:
            for bi in bundle.items.all():
                pv = bi.product_variant
                if pv:
                    bundle_items.append({
                        'comp_id': None,
                        'comp_key': 'b%s' % bi.pk,
                        'name': pv.name,
                        'qty_per_set': bi.quantity,
                        'qty': bi.quantity * item.quantity,
                        'photo': _variant_photo(pv),
                        'variant_id': pv.pk,
                        'sku': pv.sku or '',
                        'stock': pv.stock or 0,
                    })
        return {
            'item_id': item.pk,
            'name': bundle.name,
            'sku': bundle.sku or '',
            'qty': item.quantity,
            'price': price_str,
            'photo': bundle_photo,
            'is_bundle': True,
            'is_drop': False,
            'is_course': False,
            'bundle_id': item.bundle_id,
            'bundle_name': bundle.name,
            'bundle_items': bundle_items,
            'site_url': _site_url(getattr(bundle, 'slug', ''), 'bundle'),
        }
    elif item.course_id and item.course:
        # A course is an intangible item, not taken from stock. Marked blue and
        # excluded from picking/printing (like dropshipping, but a different color).
        c = item.course
        course_photo = ''
        if c.cover_image:
            try:
                course_photo = request.build_absolute_uri(c.cover_image.url)
            except Exception:
                course_photo = ''
        return {
            'item_id': item.pk,
            'name': c.name,
            'qty': item.quantity,
            'price': price_str,
            'photo': course_photo,
            'is_bundle': False,
            'is_drop': False,
            'is_course': True,
            'course_id': item.course_id,
        }
    elif item.product_variant_id and item.product_variant:
        pv = item.product_variant
        return {
            'item_id': item.pk,
            'name': pv.name,
            'sku': pv.sku or '',
            'qty': item.quantity,
            'price': price_str,
            'photo': _variant_photo(pv),
            'is_bundle': False,
            'is_drop': bool(item.was_dropshipped),
            'is_course': False,
            'variant_id': item.product_variant_id,
            'site_url': _site_url(getattr(pv.product, 'slug', '') if pv.product_id else '', 'product', pv.pk),
        }
    # Product/bundle deleted; use the snapshot from OrderItem
    return {
        'item_id': item.pk,
        'name': item.display_name,
        'sku': item.display_sku,
        'qty': item.quantity,
        'price': price_str,
        'photo': '',
        'is_bundle': False,
        'is_drop': bool(item.was_dropshipped),
        'is_course': False,
        'deleted': True,
    }


def _order_delivery_summary(o):
    """Compact delivery summary by parcels: "Nova Poshta (1-3)" /
    "Ukrposhta (1) · Nova Poshta (2, 3)". Numbering: own
    parcel = 1, dropshipping = 2,3...
    The «(N)» numbering only makes sense when there are SEVERAL parcels. For a single parcel (a
    typical
    order) we return empty, and rendering then goes by the legacy fields: type + address
    below (as before), without a redundant «(1)»."""
    from collections import OrderedDict
    ships = list(o.shipments.all())
    if len(ships) < 2:
        return []
    ordered = sorted(ships, key=lambda s: (s.kind != 'own', s.id))  # own first

    def compress(nums):
        nums = sorted(nums)
        out, i = [], 0
        while i < len(nums):
            j = i
            while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
                j += 1
            out.append('%d-%d' % (nums[i], nums[j]) if j - i >= 2 else ', '.join(str(n) for n in nums[i:j + 1]))
            i = j + 1
        return ', '.join(out)

    groups = OrderedDict()
    for idx, s in enumerate(ordered, 1):
        lbl = s.get_carrier_display()
        groups.setdefault(lbl, []).append(idx)
    return ['%s (%s)' % (lbl, compress(nums)) for lbl, nums in groups.items()]


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    form = OrderAdminForm
    save_on_top = True
    list_display = ['order_number', 'customer_name', 'customer_phone_display', 'status', 'payment_status', 'total_price', 'created_at']
    list_editable = ['status', 'payment_status']
    list_per_page = 25
    readonly_fields = ['order_number', 'created_at', 'updated_at']
    inlines = [OrderItemInline]  # parcels are handled not by a formset but by dynamic pickers (hidden JSON shipments_addr)
    ordering = ['-created_at']
    change_form_template = 'admin/orders/order/change_form.html'
    change_list_template = 'admin/orders/order/change_list.html'

    @admin.display(description='Phone', ordering='customer_phone')
    def customer_phone_display(self, obj):
        return format_phone(obj.customer_phone)


    # Deep prefetch for serializing items (photos of variants/bundles/components).
    _ITEMS_PREFETCH = (
        'shipments__supplier',  # _order_delivery_summary(o) reads o.shipments.all() in a loop, no N+1
        'items__product_variant__images',
        'items__product_variant__product__images',
        # attribute_values are needed by _variant_images_cached to pick color_av without SQL.
        'items__product_variant__attribute_values__attribute',
        'items__bundle__images',
        'items__bundle__items__product_variant__images',
        'items__bundle__items__product_variant__product__images',
        'items__bundle__items__product_variant__attribute_values__attribute',
        'items__bundle_components__product_variant__images',
        'items__bundle_components__product_variant__product__images',
        'items__bundle_components__product_variant__attribute_values__attribute',
    )

    def _filtered_order_qs(self, request):
        """Builds the orders QuerySet applying all filters/sorting
        from GET parameters. All filtering is on the server (DB), not in the browser."""
        from django.db.models import Q, Count, F, IntegerField
        from django.db.models.functions import Cast
        G = request.GET
        qs = Order.objects.all()

        # ── Order number ──
        num_mode = G.get('numMode', 'exact')
        if num_mode == 'exact' and G.get('numExact', '').strip():
            qs = qs.filter(order_number=G['numExact'].strip())
        elif num_mode == 'range':
            nf, nt = G.get('numFrom', '').strip(), G.get('numTo', '').strip()
            if nf or nt:
                qs = qs.filter(order_number__regex=r'^\d+$').annotate(
                    _onum=Cast('order_number', IntegerField()))
                if nf.isdigit():
                    qs = qs.filter(_onum__gte=int(nf))
                if nt.isdigit():
                    qs = qs.filter(_onum__lte=int(nt))

        # ── Customer (name / phone / email) ──
        q = G.get('q', '').strip()
        if q:
            qs = qs.filter(
                Q(customer_name__icontains=q) | Q(customer_phone__icontains=q)
                | Q(customer_email__icontains=q))

        # ── Status / payment / delivery ──
        statuses = [s for s in G.get('statuses', '').split(',') if s]
        if statuses:
            qs = qs.filter(status__in=statuses)
        payment = G.get('payment', 'all')
        if payment != 'all':
            qs = qs.filter(payment_status=payment)
        delivery = G.get('delivery', 'all')
        if delivery != 'all':
            qs = qs.filter(delivery_type=delivery)

        # ── Old (imported) orders ──
        # all (default): everything; new: only those placed on the site; old: only imported.
        imported = G.get('imported', 'all')
        if imported == 'new':
            qs = qs.filter(is_imported=False)
        elif imported == 'old':
            qs = qs.filter(is_imported=True)

        # ── Date / price ──
        if G.get('dateMin'):
            qs = qs.filter(created_at__date__gte=G['dateMin'])
        if G.get('dateMax'):
            qs = qs.filter(created_at__date__lte=G['dateMax'])
        if G.get('priceMin'):
            qs = qs.filter(total_price__gte=G['priceMin'])
        if G.get('priceMax'):
            qs = qs.filter(total_price__lte=G['priceMax'])

        # ── Comments ──
        cc = G.get('commClient', 'all')
        if cc == 'has':
            qs = qs.exclude(comment='')
        elif cc == 'none':
            qs = qs.filter(comment='')
        cm = G.get('commManager', 'all')
        if cm == 'has':
            qs = qs.exclude(manager_comment='')
        elif cm == 'none':
            qs = qs.filter(manager_comment='')
        src = G.get('commSource', 'all')
        if src == 'has':
            qs = qs.exclude(source='')
        elif src == 'none':
            qs = qs.filter(source='')

        # ── Dropshipping status (none/drop/mixed) ──
        drops = [d for d in G.get('drops', '').split(',') if d]
        if drops:
            phys = Q(items__bundle__isnull=True, items__course__isnull=True)
            qs = qs.annotate(
                _nphys=Count('items', filter=phys),
                _ndrop=Count('items', filter=phys & Q(items__was_dropshipped=True)),
            )
            dq = Q()
            if 'none' in drops:
                dq |= Q(_nphys=0) | Q(_ndrop=0)
            if 'drop' in drops:
                dq |= Q(_nphys__gt=0) & Q(_ndrop=F('_nphys'))
            if 'mixed' in drops:
                dq |= Q(_ndrop__gt=0) & ~Q(_ndrop=F('_nphys'))
            qs = qs.filter(dq)

        # ── Filter by products (by name; AND/OR) ──
        items = [s for s in G.get('items', '').split('||') if s]
        if items:
            mode = G.get('itemsMode', 'and')

            def _item_name_q(nm):
                # A direct product in the item + a product component inside a bundle
                # (snapshot OrderItemBundleComponent) — otherwise an order with a bundle
                # that contains the searched product was not found.
                return (
                    Q(items__product_variant__name__iexact=nm)
                    | Q(items__product_variant_name__iexact=nm)
                    | Q(items__bundle_components__product_variant__name__iexact=nm)
                    | Q(items__bundle_components__product_variant_name__iexact=nm)
                )
            if mode == 'and':
                for nm in items:
                    qs = qs.filter(_item_name_q(nm))
            else:
                iq = Q()
                for nm in items:
                    iq |= _item_name_q(nm)
                qs = qs.filter(iq)
            qs = qs.distinct()

        # ── Sorting ──
        sort = G.get('sort')
        direction = '-' if G.get('dir') == 'desc' else ''
        sort_map = {
            'order_number': 'order_number', 'customer_name': 'customer_name',
            'created_at': 'created_at', 'total_price': 'total_price',
            'status': 'status', 'payment_status': 'payment_status',
            'delivery_type': 'delivery_type',
        }
        if sort in sort_map:
            field = sort_map[sort]
            if sort == 'order_number':
                qs = qs.annotate(_snum=Cast('order_number', IntegerField()))
                field = '_snum'
            qs = qs.order_by(direction + field)
        else:
            qs = qs.order_by('-created_at')
        return qs

    def _serialize_order(self, o, request):
        items_data = [_order_item_data(item, request) for item in o.items.all()]
        # Courses are not counted in drop_status.
        non_bundle = [it for it in items_data if not it.get('is_bundle') and not it.get('is_course')]
        if not non_bundle:
            drop_status = 'none'
        else:
            drop_count = sum(1 for it in non_bundle if it.get('is_drop'))
            drop_status = 'none' if drop_count == 0 else ('drop' if drop_count == len(non_bundle) else 'mixed')
        non_bundle_all = [it for it in items_data if not it.get('is_bundle')]
        if not non_bundle_all:
            course_status = 'none'
        else:
            course_count = sum(1 for it in non_bundle_all if it.get('is_course'))
            course_status = 'none' if course_count == 0 else ('course' if course_count == len(non_bundle_all) else 'mixed')
        # Names of the order's dropshipping suppliers for badges in the list (Bradas, ARS...).
        drop_suppliers = sorted({
            it.product_variant.supplier.name
            for it in o.items.all()
            if it.was_dropshipped and it.product_variant_id and it.product_variant
            and getattr(it.product_variant, 'supplier_id', None)
        })
        return {
            'id': o.pk,
            'order_number': o.order_number,
            'customer_name': o.customer_name,
            'customer_phone': format_phone(o.customer_phone),
            'customer_email': o.customer_email,
            'status': o.status,
            'payment_status': o.payment_status,
            'payment_method': o.payment_method,
            'delivery_type': o.delivery_type,
            'delivery_address': o.delivery_address or '',
            'drop_delivery_address': o.drop_delivery_address or '',
            'total_price': str(o.total_price) if o.total_price is not None else '',
            'created_at': timezone.localtime(o.created_at).strftime('%d.%m.%Y %H:%M') if o.created_at else '',
            'comment': o.comment or '',
            'manager_comment': o.manager_comment or '',
            'source': o.source or '',
            'drop_status': drop_status,
            'drop_suppliers': drop_suppliers,
            'delivery_summary': _order_delivery_summary(o),
            'course_status': course_status,
            'is_imported': o.is_imported,
            'items': items_data,
        }

    def _orders_payload(self, request):
        """Current page of orders (after filters/sorting) + metadata."""
        from django.core.paginator import Paginator, InvalidPage
        try:
            page_size = int(request.GET.get('page_size', 25))
        except (TypeError, ValueError):
            page_size = 25
        page_size = 25 if page_size not in (25, 50) else page_size
        try:
            page_num = int(request.GET.get('page', 1))
        except (TypeError, ValueError):
            page_num = 1

        base = self._filtered_order_qs(request)
        # We paginate by id (cheap), then fetch the full rows with a deep prefetch.
        id_qs = base.values_list('id', flat=True)
        paginator = Paginator(id_qs, page_size)
        try:
            page = paginator.page(page_num)
        except InvalidPage:
            page = paginator.page(1)
            page_num = 1
        ids = list(page.object_list)
        orders = list(Order.objects.filter(id__in=ids).prefetch_related(*self._ITEMS_PREFETCH))
        pos = {oid: i for i, oid in enumerate(ids)}
        orders.sort(key=lambda o: pos.get(o.pk, 0))
        return {
            'orders': [self._serialize_order(o, request) for o in orders],
            'total': paginator.count,
            'num_pages': paginator.num_pages,
            'page': page_num,
            'page_size': page_size,
        }

    FACET_CACHE_KEY = 'orders:items_facet_names'

    def _items_facet(self, request=None):
        """Unique product names for the product-filter popup (the whole orders catalog).
        Photos are pulled by SKU from the catalog — so the filter has pictures,
        including snapshot items from the import (where sku matches a product).

        The heavy part is a full scan of OrderItem + OrderItemBundleComponent for
        unique names/SKUs (the latter — products inside bundles, otherwise
        search didn't find an order with a bundle that contains the searched product) —
        cached globally (the facet doesn't depend on request/filters) and invalidated by a
        signal on post_save/post_delete of both models (orders/signals.py), so it is
        always current. Photos are NOT cached — computed every time (fresh, with the right host)."""
        from django.core.cache import cache
        names = cache.get(self.FACET_CACHE_KEY)
        if names is None:
            from django.db.models.functions import Coalesce
            from .models import OrderItemBundleComponent
            seen, names = set(), []
            rows = (OrderItem.objects
                    .annotate(nm=Coalesce('product_variant__name', 'product_variant_name'))
                    .values_list('nm', 'product_variant_sku', 'product_variant__sku')
                    .order_by('nm'))
            comp_rows = (OrderItemBundleComponent.objects
                    .annotate(nm=Coalesce('product_variant__name', 'product_variant_name'))
                    .values_list('nm', 'product_variant_sku', 'product_variant__sku')
                    .order_by('nm'))
            for nm, snap_sku, pv_sku in list(rows.iterator()) + list(comp_rows.iterator()):
                if not nm:
                    continue
                key = nm.lower()
                if key in seen:
                    continue
                seen.add(key)
                names.append({'name': nm, 'sku': pv_sku or snap_sku or ''})
            names.sort(key=lambda x: x['name'].lower())
            cache.set(self.FACET_CACHE_KEY, names, 600)  # fallback TTL; the main invalidation is the signal
        # Photos are fresh each time (per-request, absolute URL); we don't mutate the cached names.
        items = [{'name': n['name'], 'sku': n['sku'], 'photo': None} for n in names]
        skus = {it['sku'] for it in items if it['sku']}
        photo_by_sku = self._photos_by_sku(skus, request) if skus else {}
        # is_own by SKU (per-request, like the photos), for the packaging label/filter.
        own_skus = set()
        if skus:
            from catalog.models import ProductVariant
            own_skus = set(ProductVariant.objects.filter(
                sku__in=skus, product__packaging_kind='own'
            ).values_list('sku', flat=True))
        for it in items:
            if it['sku']:
                it['photo'] = photo_by_sku.get(it['sku'])
            it['is_own'] = bool(it['sku'] and it['sku'] in own_skus)
        return items

    def _photos_by_sku(self, skus, request=None):
        """Bulk: SKU → photo URL (product variant → bundle). No N queries."""
        from catalog.models import ProductVariant, Bundle

        def absu(u):
            return request.build_absolute_uri(u) if request else u

        out = {}
        for v in (ProductVariant.objects.filter(sku__in=skus)
                  .select_related('product').prefetch_related('images', 'product__images')):
            if v.sku in out:
                continue
            img = next(iter(v.images.all()), None) or (
                next(iter(v.product.images.all()), None) if v.product_id else None)
            if img and img.image:
                out[v.sku] = absu(img.image.url)
        rest = skus - set(out)
        if rest:
            for b in Bundle.objects.filter(sku__in=rest).prefetch_related('images'):
                if b.sku in out:
                    continue
                img = next(iter(b.images.all()), None)
                if img and img.image:
                    out[b.sku] = absu(img.image.url)
        return out

    # ── "Analytics" dashboard (URL registered in config/urls.py) ───────────
    def _period_bounds(self, request):
        """Returns (start, end, prev_start, prev_end, key, label) in local time.
        The period is filtered by created_at. Supports presets and an arbitrary range.
        """
        from datetime import timedelta, datetime, time
        import calendar
        now = timezone.localtime()
        today = now.date()
        key = request.GET.get('period', 'month')
        if key not in ('today', '7d', '30d', '90d', 'year', 'all', 'month'):
            key = 'month'
        cfrom = (request.GET.get('from') or '').strip()
        cto = (request.GET.get('to') or '').strip()
        prev_start_d = prev_end_d = None  # None → a generalized window of the same length before the period

        def _parse(d):
            try:
                return datetime.strptime(d, '%Y-%m-%d').date()
            except (ValueError, TypeError):
                return None

        if cfrom or cto:
            d1 = _parse(cfrom) or today
            d2 = _parse(cto) or today
            if d2 < d1:
                d1, d2 = d2, d1
            key, label = 'custom', f'{d1.isoformat()} — {d2.isoformat()}'
            start_d, end_d = d1, d2
        elif key == 'all':
            # From the very first order (including the Horoshop history) to today.
            first = (Order.objects.exclude(status='cancelled')
                     .order_by('created_at').values_list('created_at', flat=True).first())
            start_d = timezone.localtime(first).date() if first else today
            end_d, label = today, 'All time'
        elif key == 'month':
            # Current calendar month: from the 1st to today.
            start_d, end_d, label = today.replace(day=1), today, 'This month'
            # Comparison is the same span of the previous month (MTD vs MTD),
            # otherwise at the start of a month an incomplete month would be compared with a full
            # one.
            pm_first = (start_d - timedelta(days=1)).replace(day=1)
            dim_prev = calendar.monthrange(pm_first.year, pm_first.month)[1]
            prev_start_d = pm_first
            prev_end_d = pm_first.replace(day=min(today.day, dim_prev))
        else:
            spans = {
                'today': (today, today, 'Today'),
                '7d': (today - timedelta(days=6), today, 'Last 7 days'),
                '30d': (today - timedelta(days=29), today, 'Last 30 days'),
                '90d': (today - timedelta(days=89), today, 'Last 90 days'),
                'year': (today - timedelta(days=364), today, 'Last year'),
            }
            start_d, end_d, label = spans[key]

        tz = timezone.get_current_timezone()
        start = timezone.make_aware(datetime.combine(start_d, time.min), tz)
        end = timezone.make_aware(datetime.combine(end_d, time.max), tz)
        if prev_start_d is not None:
            prev_start = timezone.make_aware(datetime.combine(prev_start_d, time.min), tz)
            prev_end = timezone.make_aware(datetime.combine(prev_end_d, time.max), tz)
        else:
            span = end - start
            prev_end = start - timedelta(microseconds=1)
            prev_start = prev_end - span
        return start, end, prev_start, prev_end, key, label

    # Letters/digits are kept; the rest (spaces, commas, colons, dropshipping asterisks) is noise.
    _NORM_RE = None
    # Snapshots of old bundle names: those items have no FK to a bundle.
    # Language data: Ukrainian words that old snapshot names start with
    # (комплект = bundle, бакова суміш / бакові суміші = tank mix, набір = set).
    _BUNDLE_RE = __import__('re').compile(r'^\s*(комплект|бакова суміш|бакові суміші|набір)', __import__('re').I)

    @classmethod
    def _norm_name(cls, s):
        import re
        if cls._NORM_RE is None:
            # Language data: keep Latin letters plus Ukrainian/Russian Cyrillic letters (old product names).
            cls._NORM_RE = re.compile(r'[^0-9a-zа-яіїєґ]+')
        return cls._NORM_RE.sub('', (s or '').lower())

    def _group_items(self, rows):
        """Groups order items into 'this is the same product' groups.

        One item in the history has DIFFERENT keys: the site writes an FK to the variant and its
        SKU, the old Horoshop import has its own numbering (3002236155 vs 3092)
        and the name spelled differently ("Sizarin rooting agent, 1 l" vs "Sizarin: Rooting agent, 1 l",
        '*' on dropshipping, Russian names).
        So we glue by ALL keys at once (union-find): a match of SKU OR
        normalized name stitches records into one product. Without this, one product
        stands as several rows in the top and at the same time in "Grew" and "Dropped".
        """
        parent = {}

        def find(x):
            while parent.setdefault(x, x) != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        for r in rows:
            keys = []
            if r.get('product_variant_id'):
                keys.append('v%s' % r['product_variant_id'])
            if r.get('bundle_id'):
                keys.append('b%s' % r['bundle_id'])
            if r.get('course_id'):
                keys.append('c%s' % r['course_id'])
            if r.get('product_variant_sku'):
                keys.append('s%s' % r['product_variant_sku'])
            nm = self._norm_name(r.get('product_variant_name') or r.get('bundle__name')
                                 or r.get('course__name'))
            if nm:
                keys.append('n%s' % nm)
            if not keys:
                keys.append('x%s' % r.get('id'))
            for k in keys[1:]:
                union(keys[0], k)
            r['_keys'] = keys
        for r in rows:
            r['_g'] = find(r['_keys'][0])
        return rows

    def _category_tree(self, item_qs):
        """Revenue by category as a TREE: a parent gets the sum of its whole subtree.

        Rules enforced here (otherwise the numbers lie):
        • each item is counted EXACTLY ONCE. A product lives in several
          categories (M2M), so we pick one, the deepest (most specific);
        • money goes up the whole ancestor chain, so a root = the sum of
          its own products + all descendants, and the sum of roots = period revenue;
        • a bundle is spread across the categories of ITS components in proportion to
          their value (composition is taken from the order snapshot if there is one);
        • courses and old imported items not linked to a product
          don't vanish but go to separate roots, so that the sum adds up.
        """
        from collections import defaultdict
        from catalog.models import Category, Product, ProductVariant, BundleItem
        from .models import OrderItemBundleComponent

        cats = list(Category.objects.values('id', 'name', 'parent_id'))
        parent = {c['id']: c['parent_id'] for c in cats}
        cname = {c['id']: c['name'] for c in cats}
        kids = defaultdict(list)
        for c in cats:
            kids[c['parent_id']].append(c['id'])

        _depth = {}
        def depth_of(cid):
            if cid not in _depth:
                d, cur = 0, cid
                while parent.get(cur):
                    cur = parent[cur]; d += 1
                _depth[cid] = d
            return _depth[cid]

        # One category per product: the deepest; on a tie, the smaller id (stable).
        prod_cat = {}
        for pid, cid in Product.categories.through.objects.values_list('product_id', 'category_id'):
            cur = prod_cat.get(pid)
            if cur is None or (depth_of(cid), -cid) > (depth_of(cur), -cur):
                prod_cat[pid] = cid
        var_prod = dict(ProductVariant.objects.values_list('id', 'product_id'))
        var_price = {v: float(p or 0) for v, p in ProductVariant.objects.values_list('id', 'price')}

        def cat_of_variant(vid):
            return prod_cat.get(var_prod.get(vid))

        # Bundle composition: weights for distributing the item's money.
        bv_items = defaultdict(list)
        for bid, vid, q in BundleItem.objects.values_list('bundle_id', 'product_variant_id', 'quantity'):
            bv_items[bid].append((vid, q))

        rows = self._group_items(list(item_qs.values(
            'id', 'order_id', 'product_variant_id', 'product_variant_sku', 'product_variant_name',
            'bundle_id', 'bundle__name', 'course_id', 'course__name',
            'price', 'quantity')))
        # Items from old imported orders have no FK, but they sit in the same
        # group as current sales of the same product (shared SKU or name
        # spelling). We take the category from there; otherwise the whole history falls
        # into "No category".
        g_variant, g_bundle = {}, {}
        for r in rows:
            if r['product_variant_id']:
                g_variant.setdefault(r['_g'], r['product_variant_id'])
            if r['bundle_id']:
                g_bundle.setdefault(r['_g'], r['bundle_id'])
        # Per-order bundle composition snapshots take priority over the template.
        snap = defaultdict(list)
        for oi, vid, q in OrderItemBundleComponent.objects.filter(
                order_item_id__in=[r['id'] for r in rows]).values_list('order_item_id', 'product_variant_id', 'quantity'):
            snap[oi].append((vid, q))
        # Old imported items have no FK; we recognize them in the catalog by
        # SKU and by the NORMALIZED name (the spelling changed over the years:
        # "Sizarin rooting agent, 1 l" vs "Sizarin: Rooting agent, 1 l").
        bv_by_name, var_by_name, var_by_sku = {}, {}, {}
        for bid, bname in Bundle.objects.values_list('id', 'name'):
            if bname:
                bv_by_name.setdefault(self._norm_name(bname), bid)
        for vid, nm, sku in ProductVariant.objects.values_list('id', 'name', 'sku'):
            if nm:
                var_by_name.setdefault(self._norm_name(nm), vid)
            if sku:
                var_by_sku.setdefault(sku, vid)

        own = defaultdict(float)          # key: category id or a pseudo-root ('course'/'legacy')
        def add(cid, amount):
            own[cid] += amount

        def spread(parts, amount):
            """Spread the item's amount across components in proportion to their value."""
            weights = []
            for vid, q in parts:
                cid = cat_of_variant(vid)
                if cid is None:
                    continue
                weights.append((cid, max(var_price.get(vid, 0.0), 0.01) * (q or 1)))
            tw = sum(w for _, w in weights)
            if not tw:
                return False
            for cid, w in weights:
                add(cid, amount * w / tw)
            return True

        for r in rows:
            amount = float(r['price'] or 0) * (r['quantity'] or 0)
            if not amount:
                continue
            if r['product_variant_id']:
                cid = cat_of_variant(r['product_variant_id'])
                add(cid if cid is not None else 'legacy', amount)
            elif r['bundle_id']:
                parts = snap.get(r['id']) or bv_items.get(r['bundle_id']) or []
                if not spread(parts, amount):
                    add('legacy', amount)
            elif r['course_id']:
                add('course', amount)
            else:
                # 1) product/bundle from the same group, 2) SKU, 3) normalized
                # name in the catalog, 4) archive.
                nm = self._norm_name(r['product_variant_name'])
                vid = (g_variant.get(r['_g']) or var_by_sku.get(r['product_variant_sku'] or '')
                       or var_by_name.get(nm))
                bvid = g_bundle.get(r['_g']) or bv_by_name.get(nm)
                cid = cat_of_variant(vid) if vid else None
                if cid is not None:
                    add(cid, amount)
                elif bvid and spread(snap.get(r['id']) or bv_items.get(bvid) or [], amount):
                    continue
                else:
                    # Bundles that no longer exist in the catalog are shown separately: their
                    # composition is unknown, so there is no way to spread them across categories,
                    # but
                    # hiding them in a faceless "no category" would also be wrong.
                    add('legacy_bundle' if self._BUNDLE_RE.match(r['product_variant_name'] or '')
                        else 'legacy', amount)

        # Climb up the tree: a node holds the sum of its own and all its descendants.
        sub = defaultdict(float)
        for cid, amount in own.items():
            if not isinstance(cid, int):
                sub[cid] += amount
                continue
            cur = cid
            while cur is not None:
                sub[cur] += amount
                cur = parent.get(cur)

        out = []
        def emit(cid, d):
            if sub.get(cid, 0) <= 0:
                return
            out.append({'name': cname[cid], 'revenue': round(sub[cid], 2), 'depth': d})
            for ch in sorted(kids.get(cid, []), key=lambda c: -sub.get(c, 0)):
                emit(ch, d + 1)
        for cid in sorted(kids.get(None, []), key=lambda c: -sub.get(c, 0)):
            emit(cid, 0)
        for key, label in (('course', 'Courses'),
                           ('legacy_bundle', 'Bundles (no longer in the catalog)'),
                           ('legacy', 'No category (archived orders)')):
            if sub.get(key, 0) > 0:
                out.append({'name': label, 'revenue': round(sub[key], 2), 'depth': 0})
        return out

    def _analytics_data(self, request):
        from django.db.models import Sum, Count, Avg, F, DecimalField, Q
        from django.db.models.functions import TruncDate, Coalesce
        from decimal import Decimal
        from datetime import timedelta

        start, end, prev_start, prev_end, key, label = self._period_bounds(request)
        money = DecimalField(max_digits=14, decimal_places=2)

        # Base QS: only PAID orders (incl. Horoshop history), excluding cancelled.
        base = Order.objects.filter(payment_status='paid')
        qs = base.filter(created_at__gte=start, created_at__lte=end).exclude(status='cancelled')
        prev = base.filter(created_at__gte=prev_start, created_at__lte=prev_end).exclude(status='cancelled')

        def _f(x):
            return float(x) if x is not None else 0.0

        # KPI for the current/previous period.
        def _kpi(q):
            agg = q.aggregate(
                revenue=Coalesce(Sum('total_price'), Decimal('0'), output_field=money),
                orders=Count('id'),
            )
            items = (OrderItem.objects.filter(order__in=q)
                     .aggregate(n=Coalesce(Sum('quantity'), 0))['n'])
            rev, orders = _f(agg['revenue']), agg['orders']
            return {
                'revenue': rev,
                'orders': orders,
                'avg_check': (rev / orders) if orders else 0.0,
                'items_sold': items or 0,
            }

        cur = _kpi(qs)
        pre = _kpi(prev)

        # ── Customers (deduplicated by phone) ──────────────────────────────────────
        # We count PEOPLE who placed and paid. One phone = one customer, even
        # if the person is both a guest and an account holder and has several orders → +1, not +N.
        # Registrations without a paid order are NOT included. Dedup key:
        # phone → email → account → (last resort) the order itself. Import placeholder
        # phones (PLACEHOLDER_PHONES) are NOT a key, otherwise dozens of different
        # people with a fake number would merge into one; they fall back to email/uid/oid.
        from orders.models import PLACEHOLDER_PHONES

        def _ckey(ph, em, uid, oid):
            if ph and ph not in PLACEHOLDER_PHONES:
                return 'p:' + ph
            em = (em or '').strip().lower()
            if em:
                return 'e:' + em
            if uid:
                return 'u:%d' % uid
            return 'o:%d' % oid

        # Each customer's first paid order over all history, to tell
        # "new; first order in the period" from "repeat".
        first_seen, recs = {}, []
        for oid, cat, ph, em, uid, tot in (base.exclude(status='cancelled')
                .order_by('created_at')
                .values_list('id', 'created_at', 'phone_normalized', 'customer_email', 'user_id', 'total_price')
                .iterator()):
            k = _ckey(ph, em, uid, oid)
            if k not in first_seen:
                first_seen[k] = cat
            recs.append((k, cat, float(tot or 0)))

        def _cust(s, e):
            # k -> the customer's order count and revenue within the window.
            cnt, rev = {}, {}
            for k, cat, tot in recs:
                if s <= cat <= e:
                    cnt[k] = cnt.get(k, 0) + 1
                    rev[k] = rev.get(k, 0.0) + tot
            total = len(cnt)
            is_new = {k: (s <= first_seen[k] <= e) for k in cnt}
            new = sum(1 for k in cnt if is_new[k])
            new_rev = sum(rev[k] for k in cnt if is_new[k])
            return {
                'total': total, 'new': new, 'returning': total - new,
                'repeat': sum(1 for c in cnt.values() if c >= 2),
                'new_revenue': new_rev, 'returning_revenue': sum(rev.values()) - new_rev,
                'dist': {
                    'one': sum(1 for c in cnt.values() if c == 1),
                    'two': sum(1 for c in cnt.values() if c == 2),
                    'three_plus': sum(1 for c in cnt.values() if c >= 3),
                },
            }

        cust = _cust(start, end)
        cust_prev = _cust(prev_start, prev_end)
        new_customers = cust['new']
        cur['new_customers'] = new_customers

        # Daily series (helper, reused for the main and the comparison period).
        def _series(q, s_date, e_date):
            pd = (q.annotate(d=TruncDate('created_at')).values('d')
                  .annotate(rev=Coalesce(Sum('total_price'), Decimal('0'), output_field=money),
                            cnt=Count('id')).order_by('d'))
            dm = {row['d'].isoformat(): row for row in pd}
            L, R, O = [], [], []
            c = s_date
            while c <= e_date:
                iso = c.isoformat(); L.append(iso); row = dm.get(iso)
                R.append(_f(row['rev']) if row else 0.0); O.append(row['cnt'] if row else 0)
                c += timedelta(days=1)
            return L, R, O

        labels, rev_series, ord_series = _series(qs, start.date(), end.date())

        # Comparison with another period (optional): the previous equal period / a year ago.
        compare_key = request.GET.get('compare', '')
        compare = None
        if compare_key in ('prev', 'year'):
            if compare_key == 'prev':
                c_start, c_end, c_label = prev_start, prev_end, 'Previous period'
            else:
                c_start, c_end, c_label = start - timedelta(days=365), end - timedelta(days=365), 'A year ago'
            c_qs = base.filter(created_at__gte=c_start, created_at__lte=c_end).exclude(status='cancelled')
            _, c_rev, c_ord = _series(c_qs, c_start.date(), c_end.date())
            n = len(labels)
            ck = _kpi(c_qs)
            ck['new_customers'] = _cust(c_start, c_end)['new']
            ck['avg_items'] = (ck['items_sold'] / ck['orders']) if ck['orders'] else 0.0
            compare = {
                'key': compare_key, 'label': c_label,
                'from': c_start.date().isoformat(), 'to': c_end.date().isoformat(),
                'kpi': ck,
                'series': {'revenue': (c_rev + [0.0] * n)[:n], 'orders': (c_ord + [0] * n)[:n]},
            }

        item_qs = OrderItem.objects.filter(order__in=qs)
        line = F('price') * F('quantity')

        # Items by revenue. We group NOT by the name snapshot (one item over the years
        # had several spellings and several SKUs), but by product group; see
        # _group_items. We show the most recent name. We return the whole list.
        _FIELDS = ('id', 'order_id', 'product_variant_id', 'product_variant_sku',
                   'product_variant_name', 'bundle_id', 'bundle__name',
                   'course_id', 'course__name', 'price', 'quantity')
        rows_cur = self._group_items(list(item_qs.values(*_FIELDS)))
        _agg = {}
        for r in rows_cur:
            amount = _f(r['price']) * (r['quantity'] or 0)
            name = (r['product_variant_name'] or r['bundle__name']
                    or r['course__name'] or 'Deleted item')
            kind = ('Bundle' if (r['bundle_id'] or r['bundle__name']) else
                    'Course' if r['course_id'] else 'Product')
            e = _agg.setdefault(r['_g'], {'name': name, 'kind': kind, 'revenue': 0.0,
                                          'qty': 0, '_oid': r['order_id'] or 0})
            e['revenue'] += amount
            e['qty'] += (r['quantity'] or 0)
            if (r['order_id'] or 0) >= e['_oid']:      # name from the most recent order
                e['_oid'] = r['order_id'] or 0
                e['name'] = name
                e['kind'] = kind
        for e in _agg.values():
            e.pop('_oid', None)
        top_products = sorted(_agg.values(), key=lambda x: -x['revenue'])

        top_categories = self._category_tree(item_qs)

        # Payment method (the only static breakdown we kept; it shows monobank growth).
        pay_labels = dict(Order.PAYMENT_CHOICES)
        by_payment = [
            {'label': pay_labels.get(r['payment_method'], r['payment_method'] or '—'),
             'count': r['cnt'], 'revenue': _f(r['rev'])}
            for r in (qs.values('payment_method')
                .annotate(cnt=Count('id'), rev=Coalesce(Sum('total_price'), Decimal('0'), output_field=money))
                .order_by('-cnt'))
        ]

        # ── Metrics for regular monitoring ──────────────────────────────
        from datetime import datetime, time
        from django.db.models.functions import TruncMonth
        from collections import Counter
        import calendar

        tz = timezone.get_current_timezone()
        today = timezone.localdate()
        now = timezone.now()
        paid_nc = base.exclude(status='cancelled')

        def _sum(q):
            return _f(q.aggregate(s=Coalesce(Sum('total_price'), Decimal('0'), output_field=money))['s'])

        # Monthly trend (last 12 calendar months); does NOT depend on the period.
        mrows = {}
        for r in (paid_nc.annotate(m=TruncMonth('created_at')).values('m')
                  .annotate(rev=Coalesce(Sum('total_price'), Decimal('0'), output_field=money), cnt=Count('id'))):
            if r['m']:
                mrows[timezone.localtime(r['m']).strftime('%Y-%m')] = (_f(r['rev']), r['cnt'])
        new_by_month = Counter()
        for cat in first_seen.values():
            new_by_month[timezone.localtime(cat).strftime('%Y-%m')] += 1
        months, mm = [], today.replace(day=1)
        for _ in range(12):
            months.append(mm)
            mm = (mm - timedelta(days=1)).replace(day=1)
        months.reverse()
        monthly_trend = []
        for mdt in months:
            mkey = mdt.strftime('%Y-%m')
            rev, cnt = mrows.get(mkey, (0.0, 0))
            monthly_trend.append({
                'month': mkey, 'revenue': rev, 'orders': cnt,
                'avg_check': (rev / cnt) if cnt else 0.0,
                'new_customers': new_by_month.get(mkey, 0),
            })

        # Forecast for the current month based on the current pace.
        cur_month = today.replace(day=1)
        month_start = timezone.make_aware(datetime.combine(cur_month, time.min), tz)
        pm_start = timezone.make_aware(datetime.combine((cur_month - timedelta(days=1)).replace(day=1), time.min), tz)
        mtd = _sum(paid_nc.filter(created_at__gte=month_start, created_at__lte=now))
        days_passed, days_in_month = today.day, calendar.monthrange(today.year, today.month)[1]
        run_rate = {
            'mtd': mtd, 'days_passed': days_passed, 'days_in_month': days_in_month,
            'projected': (mtd / days_passed * days_in_month) if days_passed else 0.0,
            'prev_month': _sum(paid_nc.filter(created_at__gte=pm_start, created_at__lt=month_start)),
        }

        # Customer retention (for the selected period); phone dedup is already in cust.
        retention = {
            'total': cust['total'], 'repeat': cust['repeat'],
            'repeat_rate': (cust['repeat'] / cust['total'] * 100) if cust['total'] else 0.0,
            'new_revenue': cust['new_revenue'], 'returning_revenue': cust['returning_revenue'],
            'dist': cust['dist'],
        }

        # Payment funnel + losses (only 'live' orders, excluding the Horoshop import).
        live = Order.objects.filter(is_imported=False, created_at__gte=start, created_at__lte=end)
        live_total = live.count()
        live_paid = live.filter(payment_status='paid').count()
        mono_pend = live.filter(payment_method='monobank', payment_status='pending')
        canc = live.filter(status='cancelled')
        payment_funnel = {
            'orders': live_total, 'paid': live_paid,
            'conversion': (live_paid / live_total * 100) if live_total else 0.0,
            # Loss = an issued monobank invoice without payment (cod/transfer in pending is normal).
            'mono_pending_count': mono_pend.count(), 'mono_pending_sum': _sum(mono_pend),
            'cancelled_count': canc.count(), 'cancelled_loss': _sum(canc.filter(payment_status='pending')),
        }

        # Value of own-stock leftovers (a snapshot right now; dropshipping/hybrid/fallback are not
        # our capital).
        from catalog.models import ProductVariant
        own = ProductVariant.objects.filter(is_dropshipping=False, is_hybrid=False, is_supplier_fallback=False)
        val_expr = F('stock') * F('price')
        sv = own.aggregate(value=Coalesce(Sum(val_expr, output_field=money), Decimal('0'), output_field=money),
                           units=Coalesce(Sum('stock'), 0))
        stock_value = {
            'value': _f(sv['value']), 'units': sv['units'] or 0,
            'positions': own.filter(stock__gt=0).count(),
            # WITHOUT a slice: the table shows the first 10, but the filter and search in it
            # must see all items, not just the top.
            'top': [
                {'name': r['name'] or r['product__name'],
                 'stock': r['stock'], 'value': _f(r['v'])}
                for r in (own.filter(stock__gt=0).annotate(v=val_expr)
                          .values('product__name', 'name', 'stock', 'v').order_by('-v'))
            ],
        }

        # Product movement: what rose / fell vs the previous period (only for a bounded window).
        movers = None
        # For a custom range there is a previous period too (a window of the same length right
        # before it) — previously the block stayed silent and showed "No data". Only
        # for "all time" is there nothing to compare against.
        if key != 'all':
            # Groups are computed over BOTH periods at once — otherwise the same product in the
            # current and previous window would get different keys and would appear in both "Grew"
            # and "Dropped" at the same time.
            rows_prev = list(OrderItem.objects.filter(order__in=prev).values(*_FIELDS))
            both = self._group_items([dict(r) for r in rows_cur] + rows_prev)
            n_cur = len(rows_cur)
            cur_rev, prev_rev, gname, gorder = {}, {}, {}, {}
            for i, r in enumerate(both):
                amount = _f(r['price']) * (r['quantity'] or 0)
                g = r['_g']
                (cur_rev if i < n_cur else prev_rev)[g] = (cur_rev if i < n_cur else prev_rev).get(g, 0.0) + amount
                nm = (r['product_variant_name'] or r['bundle__name'] or r['course__name'] or 'Deleted item')
                if (r['order_id'] or 0) >= gorder.get(g, -1):
                    gorder[g] = r['order_id'] or 0
                    gname[g] = nm
            # Only name+delta: cur/prev are not shown on the page, and there are as many rows here
            # as there are items in sales across both periods.
            diffs = []
            for g in set(cur_rev) | set(prev_rev):
                diffs.append({'name': gname.get(g, 'Deleted item'),
                              'delta': cur_rev.get(g, 0.0) - prev_rev.get(g, 0.0)})
            # NO slicing — the table filter must search the whole movement, not just the top 8.
            movers = {
                'risers': sorted([d for d in diffs if d['delta'] > 0], key=lambda x: -x['delta']),
                'fallers': sorted([d for d in diffs if d['delta'] < 0], key=lambda x: x['delta']),
            }

        # Average number of items per order.
        avg_items = (cur['items_sold'] / cur['orders']) if cur['orders'] else 0.0

        def _delta(cur_v, prev_v):
            if not prev_v:
                return None
            return round((cur_v - prev_v) / prev_v * 100, 1)

        kpi = {
            'revenue': cur['revenue'], 'orders': cur['orders'],
            'avg_check': cur['avg_check'], 'items_sold': cur['items_sold'],
            'new_customers': new_customers, 'avg_items': avg_items,
            'd_revenue': _delta(cur['revenue'], pre['revenue']),
            'd_orders': _delta(cur['orders'], pre['orders']),
            'd_avg': _delta(cur['avg_check'], pre['avg_check']),
            'd_items': _delta(cur['items_sold'], pre['items_sold']),
            'd_new': _delta(cust['new'], cust_prev['new']),
        }
        # For "all time" / a custom range there is no meaningful previous period.
        if key in ('all', 'custom'):
            for dk in ('d_revenue', 'd_orders', 'd_avg', 'd_items', 'd_new'):
                kpi[dk] = None

        return {
            'kpi': kpi,
            'compare': compare, 'compare_key': compare_key,
            'period_key': key, 'period_label': label,
            'from': start.date().isoformat(), 'to': end.date().isoformat(),
            'series': {'labels': labels, 'revenue': rev_series, 'orders': ord_series},
            'top_products': top_products,
            'top_categories': top_categories,
            'by_payment': by_payment,
            'monthly_trend': monthly_trend,
            'run_rate': run_rate,
            'retention': retention,
            'payment_funnel': payment_funnel,
            'stock_value': stock_value,
            'movers': movers,
            'counts': {
                'products': len(top_products), 'categories': len(top_categories),
            },
        }

    # The financial dashboard is visible only to the owner (the same email as OWNER_EMAIL in
    # SiteSettingsAdmin/CourseAdmin). All other staff get 403, even via a direct URL.
    ANALYTICS_OWNER_EMAIL = settings.OWNER_EMAIL

    def analytics_view(self, request):
        from django.shortcuts import render
        from django.core.exceptions import PermissionDenied
        if (getattr(request.user, 'email', '') or '').lower() != self.ANALYTICS_OWNER_EMAIL:
            raise PermissionDenied
        # Computing the full slice takes 0.3–1.5 s, and people click back and forth between periods.
        # A one-minute cache makes going back instant and doesn't lag behind new orders enough to be
        # noticeable.
        import hashlib
        from django.core.cache import cache
        _raw = '|'.join(request.GET.get(k, '') for k in ('period', 'from', 'to', 'compare'))
        ck = 'analytics:' + hashlib.md5(_raw.encode('utf-8')).hexdigest()
        data = cache.get(ck)
        if data is None:
            data = self._analytics_data(request)
            cache.set(ck, data, 60)
        ctx = {
            **self.admin_site.each_context(request),
            'title': 'Analytics',
            'data': data,
            'data_json': js_json(data, ensure_ascii=False),
            'periods': [
                ('month', 'Month'), ('today', 'Today'), ('7d', '7 days'),
                ('30d', '30 days'), ('90d', '90 days'), ('year', 'Year'), ('all', 'All time'),
            ],
            'compare_options': [
                ('', 'No comparison'), ('prev', 'Previous period'), ('year', 'A year ago'),
            ],
        }
        return render(request, 'admin/orders/analytics.html', ctx)

    def changelist_view(self, request, extra_context=None):
        # AJAX mode: return only the current page as JSON.
        if request.GET.get('_json'):
            from django.http import JsonResponse
            return JsonResponse(self._orders_payload(request))
        extra_context = extra_context or {}
        payload = self._orders_payload(request)
        extra_context['orders_json'] = js_json(payload['orders'], ensure_ascii=False)
        extra_context['orders_meta_json'] = js_json({
            'total': payload['total'], 'num_pages': payload['num_pages'],
            'page': payload['page'], 'page_size': payload['page_size'],
        }, ensure_ascii=False)
        extra_context['items_facet_json'] = js_json(self._items_facet(request), ensure_ascii=False)
        return super().changelist_view(request, extra_context)
    def get_fieldsets(self, request, obj=None):
        if obj is None:
            # New order — hide auto-generated readonly fields
            return (
                ('Customer', {
                    'fields': ('customer_name', 'customer_email', 'customer_phone')
                }),
                ('Delivery', {
                    'fields': ('delivery_type', 'delivery_address', 'drop_delivery_address')
                }),
                ('Payment', {
                    'fields': ('payment_method', 'payment_status')
                }),
                ('Comments', {
                    'fields': ('comment', 'manager_comment', 'source')
                }),
            )
        return (
            ('Order', {
                'fields': ('order_number', 'status', 'created_at', 'updated_at')
            }),
            ('Customer', {
                'fields': ('customer_name', 'customer_email', 'customer_phone')
            }),
            ('Delivery', {
                'fields': ('delivery_type', 'delivery_address', 'drop_delivery_address')
            }),
            ('Payment', {
                'fields': ('payment_method', 'payment_status')
            }),
            ('Comments', {
                'fields': ('comment', 'manager_comment', 'source')
            }),
        )

    def save_related(self, request, form, formsets, change):
        # First the standard saving of items (formset), then rebuild the set of shipments by the
        # products' stock composition + apply addresses from the hidden JSON shipments_addr (key
        # 'own' / supplier.code → the address string the manager entered via the pickers).
        super().save_related(request, form, formsets, change)
        order = form.instance
        if not order.is_imported:  # keep old imported orders on the legacy fields
            import json as _json
            try:
                addr_map = _json.loads(request.POST.get('shipments_addr') or '{}')
                if not isinstance(addr_map, dict):
                    addr_map = {}
            except (ValueError, TypeError):
                addr_map = {}
            from .models import recompute_shipments
            recompute_shipments(order, addr_map)

    def _stash_pending_stock_returns(self, request, restored):
        """Stores in the session a list of what has just been automatically returned to stock due to a
        product/bundle replacement in an order item — the order page after the redirect (submit
        "Save" = _continue → the same page) picks this up and shows the manager a modal
        «how much of this to keep in stock» (see /admin/orders/pending-stock-returns/,
        /admin/orders/confirm-stock-returns/)."""
        from decimal import Decimal
        by_variant = {}
        for r in restored:
            v = r.get('variant')
            if v is None or not v.pk:
                continue
            key = v.pk
            amount = Decimal(str(r.get('amount') or 0))
            order_qty = Decimal(str(r.get('order_qty') or 0))
            if key not in by_variant:
                photo = (
                    v.images.order_by('sort_order', 'pk').first()
                    or (v.product.images.filter(variant__isnull=True).order_by('sort_order', 'pk').first()
                        if v.product_id else None)
                    or (v.product.images.order_by('sort_order', 'pk').first() if v.product_id else None)
                )
                is_bag = bool(v.bulk_source_id and v.unit_size and not v.stock_manual)
                unit = (v.bulk_source.unit if (is_bag and v.bulk_source_id) else 'pcs')
                by_variant[key] = {
                    'variant_id': v.pk, 'product_id': v.product_id, 'name': v.name or '', 'sku': v.sku or '',
                    'photo': request.build_absolute_uri(photo.image.url) if photo else None,
                    'bag_id': r.get('bag_id'), 'unit': unit, 'amount': str(amount),
                    'order_qty': str(order_qty),
                }
            else:
                by_variant[key]['amount'] = str(Decimal(by_variant[key]['amount']) + amount)
                by_variant[key]['order_qty'] = str(Decimal(by_variant[key]['order_qty']) + order_qty)
        entries = list(by_variant.values())
        if not entries:
            return
        existing = request.session.get('pending_stock_returns') or []
        request.session['pending_stock_returns'] = existing + entries
        request.session.modified = True

    def save_formset(self, request, form, formset, change):
        # Shipments are saved not by the formset but via save_related (recompute_shipments from the
        # hidden JSON shipments_addr); here only the OrderItem logic (price/stock/bundles).
        if formset.model is not OrderItem:
            return super().save_formset(request, form, formset, change)
        from .models import _restore, _consume, _stock_consumable, _ensure_bundle_components_snapshot
        instances = formset.save(commit=False)
        # Items where the product/bundle was ACTUALLY replaced (not just a quantity change) — a
        # replacement is often made PRECISELY because the old product is no longer in stock, so the
        # automatic return (below, for stock_applied consistency) isn't always right. We collect
        # what was actually returned and, after saving, ask the manager (modal on the order page)
        # how much of it to keep in stock.
        pending_returns = []
        # For existing items we remember the DB state before saving — if the quantity/variant
        # changed, the old stock must be returned and the new one written off (the post_save signal
        # reacts only to created, it doesn't catch changes).
        old_by_pk = {}
        for inst in instances:
            if inst.pk:
                try:
                    old_by_pk[inst.pk] = OrderItem.objects.get(pk=inst.pk)
                except OrderItem.DoesNotExist:
                    pass
        for instance in instances:
            if not instance.price:
                if instance.product_variant_id:
                    try:
                        v = ProductVariant.objects.get(pk=instance.product_variant_id)
                        instance.price = v.final_price_whole
                    except ProductVariant.DoesNotExist:
                        pass
                elif instance.bundle_id:
                    try:
                        b = Bundle.objects.get(pk=instance.bundle_id)
                        instance.price = b.get_price() or 0
                    except Bundle.DoesNotExist:
                        pass
            old = old_by_pk.get(instance.pk)
            if old is not None and (
                old.quantity != instance.quantity
                or old.product_variant_id != instance.product_variant_id
                # bundle_id too — otherwise changing the bundle ITSELF (A→B) would not roll back
                # A's stock. The rebuild block (below) wipes A's components along with
                # stock_consumed anyway → without this rollback A's stock was lost forever and B was
                # not written off at all (double desync). _restore goes BEFORE the components are
                # deleted on the line below — it reads stock_consumed while it still exists.
                or old.bundle_id != instance.bundle_id
            ):
                # An ACTUAL replacement of the product/bundle (not just a quantity change of the
                # same one) — it's often done precisely because the old one isn't in stock, so the
                # full automatic return below is not always right by default.
                is_replacement = bool(
                    (old.product_variant_id or old.bundle_id) and (
                        old.product_variant_id != instance.product_variant_id
                        or old.bundle_id != instance.bundle_id
                    )
                )
                # Return stock according to the old state, then write off according to the new one
                restored = _restore(old)
                if is_replacement and restored:
                    for r in restored:
                        r['order_qty'] = old.quantity or 0
                    pending_returns.extend(restored)
                instance.stock_applied = False
            instance.save()
            # Bundle change or bundle↔product conversion: rebuild the per-order
            # contents snapshot for the new bundle (otherwise stock/printing stay on the
            # old contents); on conversion to a product — remove orphaned components.
            if old is not None and old.bundle_id != instance.bundle_id:
                instance.bundle_components.all().delete()
                if instance.bundle_id:
                    _ensure_bundle_components_snapshot(instance)
            # Write off only if stock should be held now. For an UNPAID monobank order
            # (_stock_consumable=False) stock is held only after payment — otherwise editing an
            # unpaid invoice would reduce the stock level prematurely. _consume is idempotent via
            # the stock_applied flag.
            if old is not None and not instance.stock_applied and _stock_consumable(order=form.instance):
                _consume(instance)
        formset.save_m2m()
        for obj in formset.deleted_objects:
            obj.delete()

        if pending_returns:
            self._stash_pending_stock_returns(request, pending_returns)

        from decimal import Decimal, InvalidOperation
        order = form.instance

        # Per-item editing of bundle composition (replace/delete/quantity/add components in the
        # bundle's composition ONLY in this order). The snapshot is OrderItemBundleComponent; stock
        # is recalculated safely via _restore/_consume.
        try:
            comp_edits = json.loads(request.POST.get('oi_component_edits') or '{}')
        except (ValueError, TypeError):
            comp_edits = {}
        if isinstance(comp_edits, dict) and comp_edits:
            from .models import apply_bundle_component_edits
            # Newly added items didn't have a pk yet at the time of the stock edit, so the frontend
            # sends them under the key new:<form index in the formset>. After formset.save() each
            # form's instance already has a pk — that's how we match them.
            pk_by_row = {}
            for _idx, _f in enumerate(getattr(formset, 'forms', [])):
                _inst = getattr(_f, 'instance', None)
                if _inst is not None and _inst.pk:
                    pk_by_row[str(_idx)] = _inst.pk
            for item_key, comps in comp_edits.items():
                if not isinstance(comps, list):
                    continue
                key = str(item_key)
                if key.startswith('new:'):
                    item_pk = pk_by_row.get(key[4:])
                    if item_pk is None:
                        continue   # row not saved (empty / deleted)
                else:
                    item_pk = key
                try:
                    it = order.items.get(pk=int(item_pk))
                except (ValueError, TypeError, OrderItem.DoesNotExist):
                    continue
                apply_bundle_component_edits(it, comps, order)
                # We immediately put it in the "needs attention" queue, without
                # waiting for export: the manager should see the consequence of the replacement at
                # the moment they make it, not a week later when they get around to printing the
                # sheets.
                try:
                    from catalog.instruction_resolve import resolve_order_item
                    resolve_order_item(it, create_pending=True, user=request.user)
                except Exception:
                    # The queue is a convenience, not a condition for saving the order.
                    log.exception('instruction queue failed for item=%s', it.pk)

        new_total = None
        if request.POST.get('price_is_manual') == '1':
            manual_price = request.POST.get('id_total_price') or request.POST.get('total_price')
            if manual_price:
                try:
                    new_total = Decimal(manual_price)
                except InvalidOperation:
                    new_total = None
        if new_total is None:
            new_total = sum((item.price or 0) * item.quantity for item in order.items.all())
        order.total_price = new_total
        order.save(update_fields=['total_price'])


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    change_form_template = 'admin/orders/invoice/change_form.html'
    change_list_template = 'admin/orders/invoice/change_list.html'
    list_display = ['__str__', 'invoice_date', 'status', 'created_at']
    readonly_fields = ['created_at', 'status', 'invoice_date', 'photo']
    save_on_top = True

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        invoices = (Invoice.objects
                    .prefetch_related(
                        'items__matched_variant__images',
                        'items__matched_variant__product__images',
                        'photos')
                    .order_by('-created_at'))
        data = []
        for inv in invoices:
            items_all = list(inv.items.all())
            items_confirmed = sum(1 for it in items_all if it.confirmed)
            items_total = len(items_all)
            # Invoice products for the item search filter: a matched variant provides the
            # name/SKU/photo from the DB, otherwise the raw name from the invoice.
            inv_items = []
            for it in items_all:
                mv = it.matched_variant
                name = (mv.name if mv else it.invoice_name) or ''
                if not name:
                    continue
                inv_items.append({
                    'name': name,
                    'sku': (mv.sku if mv else '') or '',
                    'photo': self._variant_photo(mv, request) if mv else None,
                    'qty': it.quantity,
                    'is_own': bool(mv and mv.product_id and mv.product.packaging_kind == 'own'),
                })
            photos = []
            if inv.photo:
                photos.append(request.build_absolute_uri(inv.photo.url))
            for p in inv.photos.all():
                if p.photo:
                    photos.append(request.build_absolute_uri(p.photo.url))
            data.append({
                'id': inv.pk,
                'invoice_date': inv.invoice_date.strftime('%d.%m.%Y') if inv.invoice_date else '',
                'status': inv.status,
                'items_confirmed': items_confirmed,
                'items_total': items_total,
                'items': inv_items,
                'confirmed_at': timezone.localtime(inv.confirmed_at).strftime('%d.%m.%Y %H:%M') if inv.confirmed_at else '',
                'action_at': timezone.localtime(inv.reverted_at or inv.confirmed_at).strftime('%d.%m.%Y %H:%M') if (inv.reverted_at or inv.confirmed_at) else '',
                'created_at': timezone.localtime(inv.created_at).strftime('%d.%m.%Y %H:%M') if inv.created_at else '',
                'photos': photos,
            })
        extra_context['invoices_json'] = js_json(data, ensure_ascii=False)
        return super().changelist_view(request, extra_context)

    @staticmethod
    def _variant_photo(mv, request):
        """Photo URL of the matched variant (variant → product); None if there is no photo."""
        img = next(iter(mv.images.all()), None) or (
            next(iter(mv.product.images.all()), None) if mv.product_id else None)
        if img and img.image:
            return request.build_absolute_uri(img.image.url)
        return None

    def change_view(self, request, object_id, form_url='', extra_context=None):
        extra_context = extra_context or {}
        try:
            from .models import bag_item_payload
            from catalog.models import BulkSource
            invoice = Invoice.objects.get(pk=object_id)
            items_qs = (invoice.items
                        .select_related('matched_variant__product', 'matched_variant__bulk_source')
                        .order_by('pk'))
            mapping_names = {
                it.invoice_name.strip().lower()
                for it in items_qs
                if it.invoice_name
            }
            saved_mappings = set(
                InvoiceNameMapping.objects.filter(invoice_name__in=mapping_names)
                .values_list('invoice_name', flat=True)
            )
            items_data = []
            for it in items_qs:
                mv = it.matched_variant
                row = {
                    'id': it.id,
                    'invoice_name': it.invoice_name,
                    'quantity': it.quantity,
                    'website_price': str(it.website_price) if it.website_price else '',
                    'matched_variant_id': mv.id if mv else None,
                    'matched_variant_name': mv.name if mv else '',
                    'similarity': round(it.similarity, 3) if it.similarity is not None else None,
                    'current_stock': mv.stock or 0 if mv else 0,
                    'current_price': str(mv.price) if mv else '',
                    'confirmed': it.confirmed,
                    'original_stock': it.original_stock,
                    'manually_edited': it.manually_edited,
                    'from_mapping': (it.invoice_name or '').strip().lower() in saved_mappings,
                    'product_name': (mv.product.name if (mv and mv.product_id) else (mv.name if mv else '')),
                }
                # Stock receipt of "own packaging": a CONFIRMED item shows the
                # recorded result (weight actually refilled / artificial receipt), WITHOUT a fresh
                # auto-detect — otherwise an artificial own receipt would be wrongly rendered as a
                # bulk bag on reopening. An unconfirmed one gets a fresh auto-detect of the refill
                # plan.
                if it.confirmed:
                    if it.applied_bag_id:
                        bag = BulkSource.objects.filter(pk=it.applied_bag_id).first()
                        row.update({
                            'bag_eligible': True, 'is_bag': True,
                            'bag_amount': float(it.applied_bag_amount or 0),
                            'bag_unit': {'g': 'g', 'ml': 'ml', 'pcs': 'pcs'}.get(bag.unit if bag else 'g', 'g'),
                            # We don't store the historical bag stock level — the live one drifts
                            # with sales, so for a confirmed invoice we don't show it (None → «—»).
                            'bag_current': None,
                            'unit_size': mv.unit_size if mv else None,
                        })
                    else:
                        row.update({'bag_eligible': False, 'is_bag': False, 'bag_amount': None,
                                    'bag_unit': '', 'bag_current': 0,
                                    'unit_size': mv.unit_size if mv else None})
                elif mv:
                    row.update(bag_item_payload(mv, it.invoice_name, it.quantity))
                else:
                    row.update({'bag_eligible': False, 'is_bag': False, 'bag_amount': None,
                                'bag_unit': '', 'bag_current': 0, 'unit_size': None})
                items_data.append(row)
            extra_context['invoice_items_json'] = js_json(items_data, ensure_ascii=False)
            extra_context['invoice_status'] = invoice.status
            extra_context['invoice_date_str'] = invoice.invoice_date.strftime('%d.%m.%Y') if invoice.invoice_date else ''
            extra_context['confirmed_at_str'] = timezone.localtime(invoice.confirmed_at).strftime('%d.%m.%Y %H:%M') if invoice.confirmed_at else ''
            extra_context['reverted_at_str'] = timezone.localtime(invoice.reverted_at).strftime('%d.%m.%Y %H:%M') if invoice.reverted_at else ''
            all_photos = [invoice.photo.url] if invoice.photo else []
            all_photos += [p.photo.url for p in invoice.photos.all() if p.photo]
            extra_context['invoice_photo_urls'] = json.dumps(all_photos)
            extra_context['invoice_photo_url'] = invoice.photo.url if invoice.photo else ''
        except Invoice.DoesNotExist:
            pass
        return super().change_view(request, object_id, form_url, extra_context)

    def add_view(self, request, form_url='', extra_context=None):
        extra_context = extra_context or {}
        extra_context['invoice_items_json'] = '[]'
        extra_context['invoice_status'] = 'new'
        extra_context['invoice_date_str'] = ''
        extra_context['invoice_photo_url'] = ''
        extra_context['invoice_photo_urls'] = '[]'
        return super().add_view(request, form_url, extra_context)
