import logging
import re
import uuid
from decimal import Decimal
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.conf import settings
from django.utils import timezone
from catalog.models import ProductVariant, Bundle

log = logging.getLogger(__name__)


def normalize_phone(p):
    """Last 9 digits of the number (format/spaces/+38/0 do not matter) — a shared key
    for matching phones. '' if there are fewer than 9 digits."""
    d = re.sub(r'\D', '', p or '')
    return d[-9:] if len(d) >= 9 else ''


# Placeholder phones from the Horoshop import — NOT real numbers: the same
# number marks dozens of DIFFERENT people (the import did not carry over the real phones). They
# must not be used as a dedup/linking key — otherwise different people would merge
# into one customer. Values are normalized (last 9 digits).
PLACEHOLDER_PHONES = {'670000000'}


def format_phone(p):
    """Single display format: '+380000000000'. If the number is not recognized
    (not 9 digits), return it as is."""
    n = normalize_phone(p)
    if len(n) != 9:
        return p or ''
    return f'+38 (0{n[:2]}) {n[2:5]}-{n[5:7]}-{n[7:9]}'


# Allowed order status transitions.
# Protection against accidental rollbacks (e.g. 'delivered' → 'new' from the admin).
# Everything is allowed except: restoring from 'delivered', and the reverse 'paid' → 'pending'.
class OrderNumberCounter(models.Model):
    """Single counter row for sequential order numbers (see Order._assign_sequential_number).
    Incremented only on commit, so it does not 'burn' on failed checkout attempts —
    unlike pk, which Postgres hands out immediately on INSERT and does not take back."""
    last_number = models.PositiveIntegerField(default=0)

    class Meta:
        verbose_name = 'Order number counter'
        verbose_name_plural = 'Order number counter'


class Order(models.Model):
    STATUS_CHOICES = [
        ('new', 'New'),
        ('processing', 'Processing'),
        ('shipped', 'Shipped'),
        ('delivered', 'Completed'),
        ('cancelled', 'Cancelled'),
    ]

    DELIVERY_CHOICES = [
        ('nova_poshta', 'Nova Poshta'),
        ('pickup', 'Pickup'),
    ]
    # Orders placed before Ukrposhta was dropped keep 'ukr_poshta'.
    # The pair remains ONLY so that the CRM and the account page show them a human-readable label —
    # it is not part of the choices for a new order (DELIVERY_CHOICES).
    LEGACY_DELIVERY_CHOICES = [
        ('ukr_poshta', 'Ukrposhta'),
    ]
    ALL_DELIVERY_CHOICES = DELIVERY_CHOICES + LEGACY_DELIVERY_CHOICES

    PAYMENT_CHOICES = [
        ('monobank', 'Monobank'),
        ('cod', 'Cash on delivery'),
        ('bank_transfer', 'Card transfer'),
        ('cash', 'Cash'),
        ('own_expense', 'On the house'),
    ]

    PAYMENT_STATUS_CHOICES = [
        ('pending', 'Unpaid'),
        ('paid', 'Paid'),
    ]

    order_number = models.CharField(max_length=20, unique=True, blank=True, verbose_name='Order number')
    # Unpredictable token for guest access to payment actions (screenshot/payment details).
    # order_number is a sequential counter (OrderNumberCounter), not a secret by itself — protection
    # against IDOR.
    public_token = models.CharField(max_length=32, blank=True, default='', verbose_name='Access token (guest)')
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='orders',
        verbose_name='User'
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='new', verbose_name='Status')
    customer_name = models.CharField(max_length=200, verbose_name='Customer name')
    customer_phone = models.CharField(max_length=20, verbose_name='Phone')
    # Last 9 digits of the phone (formats with spaces / without +38 are reduced to a common
    # form) — for reliably linking guest orders to an account by number.
    phone_normalized = models.CharField(max_length=9, blank=True, default='', db_index=True, verbose_name='Phone (normalized)')
    customer_email = models.EmailField(blank=True, verbose_name='Email')
    delivery_type = models.CharField(max_length=20, choices=ALL_DELIVERY_CHOICES, verbose_name='Delivery type')
    # Empty for pickup — there is no delivery address in that case.
    delivery_address = models.TextField(blank=True, default='', verbose_name='Delivery address')
    # Second parcel: made-to-order (dropship) items ship separately — Bradas sends
    # directly to the customer. Filled only in a mixed order (dropship + own items),
    # when the own part goes to a different branch or by courier to an address.
    drop_delivery_address = models.TextField(blank=True, default='', verbose_name='Delivery address (made to order, separate Nova Poshta parcel)')
    payment_method = models.CharField(max_length=20, choices=PAYMENT_CHOICES, verbose_name='Payment method')
    payment_status = models.CharField(max_length=10, choices=PAYMENT_STATUS_CHOICES, default='pending', verbose_name='Payment status')
    total_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, verbose_name='Amount')
    comment = models.TextField(blank=True, verbose_name='Customer comment')
    manager_comment = models.TextField(blank=True, verbose_name='Manager comment')
    source = models.CharField(max_length=255, blank=True, default='', verbose_name='Source')
    payment_screenshot = models.FileField(upload_to='payment_screenshots/', null=True, blank=True, verbose_name='Payment receipt')
    payment_notification = models.BooleanField(default=False, verbose_name='New payment (notification)')
    # The customer chose "send the payment details to my phone" at
    # checkout (bank transfer payment).
    # The manager has to get in touch and send the details — a separate CRM queue "Payment details".
    payment_contact_requested = models.BooleanField(
        default=False, db_index=True, verbose_name='Asks for payment details by phone',
    )
    monobank_invoice_id = models.CharField(max_length=64, blank=True, db_index=True, verbose_name='Monobank invoiceId')
    payment_status_changed_at = models.DateTimeField(null=True, blank=True, verbose_name='Last payment status update')
    # Old order imported from the old site (not placed on our site).
    # A technical flag for the CRM only (OLD badge + filter); the customer does not see it.
    is_imported = models.BooleanField(default=False, db_index=True, verbose_name='Old (imported)')

    created_at = models.DateTimeField(auto_now_add=True, verbose_name='Created')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='Updated')

    class Meta:
        verbose_name = 'Order'
        verbose_name_plural = 'Orders'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['status', '-created_at']),
            models.Index(fields=['payment_status']),
            models.Index(fields=['-created_at']),
        ]

    def __str__(self):
        return self.order_number

    @property
    def has_drop_items(self):
        """Whether the order has items sold as dropship (a separate NP parcel is needed)."""
        if not self.pk:
            return False
        return self.items.filter(was_dropshipped=True).exists()

    def save(self, *args, **kwargs):
        # Normalized phone — always kept up to date.
        self.phone_normalized = normalize_phone(self.customer_phone)
        # State machine: check that the status / payment_status transition is allowed
        payment_changed = False
        if self.pk:
            prev = Order.objects.filter(pk=self.pk).values('status', 'payment_status').first()
            if prev:
                old_payment = prev['payment_status']
                if old_payment != self.payment_status:
                    payment_changed = True
                    self.payment_status_changed_at = timezone.now()
                    update_fields = kwargs.get('update_fields')
                    if update_fields is not None and 'payment_status_changed_at' not in update_fields:
                        kwargs['update_fields'] = list(update_fields) + ['payment_status_changed_at']

        if not self.pk and not self.order_number:
            if not self.public_token:
                self.public_token = uuid.uuid4().hex[:16]
            self.order_number = uuid.uuid4().hex[:12]  # temp unique placeholder
            super().save(*args, **kwargs)
            # We issue the real number ONLY on commit — pk comes from the DB sequence right
            # at INSERT and is 'burned' forever if checkout then fails (not in stock/invalid data)
            # and the transaction rolls back: the pk is already spent, and order_number = str(pk)
            # left a
            # visible gap in the numbering with no order behind it. OrderNumberCounter is
            # incremented only for orders that
            # are actually saved — it leaves no gaps.
            transaction.on_commit(self._assign_sequential_number)
        else:
            super().save(*args, **kwargs)

    def _assign_sequential_number(self):
        with transaction.atomic():
            counter, _ = OrderNumberCounter.objects.select_for_update().get_or_create(pk=1)
            counter.last_number += 1
            counter.save(update_fields=['last_number'])
            self.order_number = str(counter.last_number)
            Order.objects.filter(pk=self.pk).update(order_number=self.order_number)
        # NOTE: write-off from the bag/stock is done by the _orderitem_consume_stock signal
        # on OrderItem post_save (via _consume → _consume_variant). Previously there was a repeated
        # call of
        # _deduct_bulk_sources here on the transition to shipped — this led to a double
        # bag write-off and a repeated one on the shipped→new→shipped cycle.


class OrderItem(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='items', verbose_name='Order')
    # SET_NULL — so that a product can be deleted without breaking order history.
    # The name and SKU are kept in the snapshot fields below.
    product_variant = models.ForeignKey(ProductVariant, null=True, blank=True, on_delete=models.SET_NULL, verbose_name='Product variant')
    bundle = models.ForeignKey(Bundle, null=True, blank=True, on_delete=models.SET_NULL, related_name='order_items', verbose_name='Bundle')
    # A course is an intangible item. Stock is not written off. Access is granted via
    # CoursePurchase after the order is paid (see courses/signals.py).
    course = models.ForeignKey(
        'courses.Course', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='order_items', verbose_name='Course',
    )
    # Snapshot of names/SKUs — filled when the OrderItem is created, needed
    # so that the order history stays readable after a product is deleted.
    product_variant_name = models.CharField(max_length=300, blank=True, default='', verbose_name='Name (snapshot)')
    product_variant_sku = models.CharField(max_length=20, blank=True, default='', verbose_name='SKU (snapshot)')
    quantity = models.PositiveIntegerField(verbose_name='Quantity')
    price = models.DecimalField(max_digits=10, decimal_places=2, verbose_name='Price')
    old_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, verbose_name='Old price')
    stock_applied = models.BooleanField(default=False, verbose_name='Stock written off')
    # How much was ACTUALLY written off stock at _consume (accounting for clamping to 0 on
    # overselling). A return (_restore) adds back EXACTLY this number — so that the bag/stock level
    # does not 'inflate' beyond what was actually written off. Units: grams/ml if it was written off
    # from a bag
    # (stock_consumed_bag_id is set), otherwise pieces (variant.stock).
    # NULL — the item was created BEFORE exact accounting was introduced (legacy): the return goes
    # by
    # nominal (unit_size*qty / qty), as the old code did.
    stock_consumed = models.DecimalField(
        max_digits=12, decimal_places=3, null=True, blank=True, verbose_name='Actually written off')
    stock_consumed_bag_id = models.PositiveIntegerField(
        null=True, blank=True, verbose_name='Write-off bag (id)',
        help_text='id of the BulkSource it was written off from; NULL means it was written off from variant.stock. '
                  'Stored separately so that stock can be returned even if the variant was deleted.')
    # True — the item was sold as dropshipping (is_dropshipping, or hybrid without available stock
    # at the time the order was created). Fixed once, when the OrderItem is created.
    was_dropshipped = models.BooleanField(default=False, verbose_name='Sold as dropship')
    # The manager confirmed when adding the item: write off beyond the stock level for a planned
    # delivery (an order for a product that is not available now, but the supplier will bring it).
    # True — _consume() does not clamp stock/bag to 0, but writes off exactly quantity,
    # so stock/BulkSource.quantity may go negative until the next supplier invoice.
    allow_negative_stock = models.BooleanField(
        default=False, blank=True, verbose_name='Allow negative stock',
        help_text='Confirmed by the manager when adding the item: write off beyond the '
                  'stock level for a planned delivery instead of clamping to 0.')
    # Frozen bundle instruction (heading + text) as of purchase time, FOREVER.
    # If the bundle template (Bundle.instruction_html) is edited later or the bundle is renamed,
    # the customer's "My orders" is no longer affected.
    # Empty means: either not a bundle, or the bundle had no instruction at purchase time, or
    # (old orders) the snapshot has not been filled by the backfill yet — then instruction_print
    # resolves live, as before. Filled in _ensure_instruction_snapshot.
    instruction_snapshot = models.TextField(
        blank=True, default='', editable=False, verbose_name='Instruction (frozen)')

    class Meta:
        verbose_name = 'Order item'
        verbose_name_plural = 'Order items'
        # Without an explicit order Postgres returns rows as they lie in the heap, and after
        # an item is edited its row goes to the end — so the order card,
        # the print stack and the docx export showed items in DIFFERENT orders,
        # and sheets came out from the last item to the first.
        # pk = order of addition, i.e. 'as in the order'.
        ordering = ['pk']

    def __str__(self):
        return f'{self.display_name} x{self.quantity}'

    @property
    def display_name(self):
        """Item name with a fallback to the snapshot if the product was deleted."""
        if self.product_variant_id and self.product_variant:
            return self.product_variant.name
        if self.bundle_id and self.bundle:
            return self.bundle.name
        if self.course_id and self.course:
            return f'Course: {self.course.name}'
        return self.product_variant_name or 'Deleted item'

    @property
    def display_sku(self):
        if self.product_variant_id and self.product_variant:
            return self.product_variant.sku or ''
        if self.bundle_id and self.bundle:
            return self.bundle.sku or ''
        if self.course_id and self.course:
            return f'COURSE-{self.course_id}'
        return self.product_variant_sku or ''

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        # On first save we fix the name/SKU in the snapshot.
        if not self.product_variant_name:
            if self.product_variant_id and self.product_variant:
                self.product_variant_name = self.product_variant.name or ''
                self.product_variant_sku = self.product_variant.sku or ''
            elif self.bundle_id and self.bundle:
                self.product_variant_name = self.bundle.name
                self.product_variant_sku = self.bundle.sku or ''
            elif self.course_id and self.course:
                self.product_variant_name = f'Course: {self.course.name}'
                self.product_variant_sku = f'COURSE-{self.course_id}'
        # We fix was_dropshipped only when the item is created — so that history is not 'repainted'
        # when the variant later gets or runs out of stock.
        if is_new and self.bundle_id is None and self.product_variant_id and self.product_variant:
            v = self.product_variant
            if getattr(v, 'is_dropshipping', False):
                self.was_dropshipped = True
            elif getattr(v, 'is_hybrid', False) and (v.stock or 0) < (self.quantity or 0):
                self.was_dropshipped = True
            else:
                self.was_dropshipped = False
        super().save(*args, **kwargs)

    def get_total(self):
        return self.price * self.quantity


class Shipment(models.Model):
    """One physical parcel of an order. An order = 1 'own' parcel (own products +
    the own part of a hybrid) + one 'dropship' parcel per supplier (Bradas, ARS…).
    Own — NP (branch/address); dropship — always an NP branch.
    On pickup there is NO own parcel (see recompute_shipments) — the customer collects
    the goods themselves; dropship items of the same order still go by NP separately.
    For NEW orders only; old ones are read via Order.delivery_address/drop_delivery_address."""
    KIND_CHOICES = [('own', 'From us'), ('drop', 'Dropship')]
    # 'pickup' is deliberately NOT added here: pickup is not a carrier, and there is no parcel
    # at all in that case. Shipment remains a record of a real shipment.
    CARRIER_CHOICES = [('nova_poshta', 'Nova Poshta')]
    # Parcels of old orders (before Ukrposhta was dropped) — just for the label.
    LEGACY_CARRIER_CHOICES = [('ukr_poshta', 'Ukrposhta')]
    ALL_CARRIER_CHOICES = CARRIER_CHOICES + LEGACY_CARRIER_CHOICES
    ADDRESS_KIND_CHOICES = [('warehouse', 'Branch'), ('address', 'Address (courier)')]

    order = models.ForeignKey('Order', on_delete=models.CASCADE, related_name='shipments', verbose_name='Order')
    kind = models.CharField(max_length=8, choices=KIND_CHOICES, verbose_name='Parcel type')
    supplier = models.ForeignKey('catalog.Supplier', null=True, blank=True, on_delete=models.PROTECT,
                                 related_name='shipments', verbose_name='Supplier')
    carrier = models.CharField(max_length=20, choices=ALL_CARRIER_CHOICES, default='nova_poshta', verbose_name='Carrier')
    address_kind = models.CharField(max_length=10, choices=ADDRESS_KIND_CHOICES, default='warehouse', verbose_name='Address type')
    address = models.TextField(blank=True, default='', verbose_name='Address')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Parcel'
        verbose_name_plural = 'Parcels'
        ordering = ['kind', 'id']

    def __str__(self):
        who = self.supplier.name if self.supplier_id else 'From us'
        return f'{who}: {self.get_carrier_display()} — {self.address[:40]}'


def recompute_shipments(order, addr_map=None):
    """Idempotently rebuilds the order's parcels by item composition and applies addresses
    from addr_map (key: 'own' for own, supplier.code for dropship).
    1 'own' parcel (if there are non-dropship items) + one 'dropship' parcel per EACH supplier
    of dropship items (was_dropshipped + variant.supplier). Dropship — always an NP branch.
    Called when an order is saved in the CRM; old imported orders are not touched."""
    from collections import OrderedDict
    addr_map = addr_map or {}
    items = list(order.items.select_related('product_variant__supplier').all())
    need_own = False
    drop_suppliers = OrderedDict()  # supplier_id -> supplier, in order of appearance
    for it in items:
        v = it.product_variant if it.product_variant_id else None
        if it.was_dropshipped and v and v.supplier_id:
            drop_suppliers[v.supplier_id] = v.supplier
        else:
            need_own = True  # own, bundles, courses, in-stock hybrid
    # Pickup — there is no physical parcel from us, the customer collects the goods themselves.
    # This does not cancel dropship items: the supplier still ships them by NP separately.
    if order.delivery_type == 'pickup':
        need_own = False
    # existing shipments by key (kind, supplier_id)
    existing = {}
    for s in order.shipments.all():
        existing[('own', None) if s.kind == 'own' else ('drop', s.supplier_id)] = s
    wanted = ([('own', None)] if need_own else []) + [('drop', sid) for sid in drop_suppliers]
    wanted_set = set(wanted)
    # remove the extras (an item was deleted / the supplier was changed)
    for key, s in list(existing.items()):
        if key not in wanted_set:
            s.delete()

    legacy_own = order.delivery_address or ''
    # Marker written into delivery_address by the checkout form. Legacy orders carry the Ukrainian
    # marker "адресна доставка" ("address delivery"), so both spellings are accepted.
    own_is_addr = legacy_own.lower().startswith(('address delivery', 'адресна доставка'))
    for key in wanted:
        kind, sid = key
        supplier = None if kind == 'own' else drop_suppliers[sid]
        if kind == 'own':
            # own parcel — from the legacy fields (full post choice: NP branch/address, Ukrposhta)
            new_addr, carrier, akind = legacy_own, (order.delivery_type or 'nova_poshta'), ('address' if own_is_addr else 'warehouse')
        else:
            # dropship — from addr_map (my pickers), always an NP branch
            new_addr, carrier, akind = addr_map.get(supplier.code), 'nova_poshta', 'warehouse'
        s = existing.get(key)
        if s is not None:
            if kind == 'own':
                # own is always in sync with legacy delivery_type/address
                if s.address != new_addr or s.carrier != carrier or s.address_kind != akind:
                    s.address, s.carrier, s.address_kind = new_addr, carrier, akind
                    s.save()
            elif new_addr is not None and new_addr != s.address:
                s.address = new_addr
                s.save()
        else:
            Shipment.objects.create(
                order=order, kind=kind, supplier=supplier, carrier=carrier,
                address_kind=akind, address=(new_addr if new_addr is not None else ''))


class OrderItemBundleComponent(models.Model):
    """
    Snapshot of a bundle component at the OrderItem level.
    Created when a bundle OrderItem is created, by copying from Bundle.items.
    Allows editing the bundle composition in one specific order without changing the
    global Bundle template (which would affect all other orders).
    """
    order_item = models.ForeignKey(
        'OrderItem', on_delete=models.CASCADE, related_name='bundle_components',
        verbose_name='Order item',
    )
    product_variant = models.ForeignKey(
        ProductVariant, null=True, blank=True, on_delete=models.SET_NULL,
        verbose_name='Product variant',
    )
    quantity = models.PositiveIntegerField(default=1, verbose_name='Quantity per 1 bundle')
    # Snapshot of name/SKU, so the document stays readable even if the variant is deleted
    product_variant_name = models.CharField(max_length=300, blank=True, default='', verbose_name='Name (snapshot)')
    product_variant_sku = models.CharField(max_length=20, blank=True, default='', verbose_name='SKU (snapshot)')
    # Actually written off for this component (see OrderItem.stock_consumed). NULL means legacy.
    stock_consumed = models.DecimalField(
        max_digits=12, decimal_places=3, null=True, blank=True, verbose_name='Actually written off')
    stock_consumed_bag_id = models.PositiveIntegerField(
        null=True, blank=True, verbose_name='Write-off bag (id)')

    class Meta:
        verbose_name = 'Order bundle component'
        verbose_name_plural = 'Order bundle components'
        ordering = ['pk']

    def __str__(self):
        return f'{self.display_name} x{self.quantity}'

    @property
    def display_name(self):
        if self.product_variant_id and self.product_variant:
            return self.product_variant.name
        return self.product_variant_name or 'Deleted component'

    def save(self, *args, **kwargs):
        if not self.product_variant_name and self.product_variant_id and self.product_variant:
            self.product_variant_name = self.product_variant.name or ''
            self.product_variant_sku = self.product_variant.sku or ''
        super().save(*args, **kwargs)


class Invoice(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Awaiting confirmation'),
        ('confirmed', 'Confirmed'),
        ('reverted', 'Reverted'),
    ]
    photo = models.ImageField(upload_to='invoices/', verbose_name='Invoice photo')
    invoice_date = models.DateField(null=True, blank=True, verbose_name='Invoice date')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending', verbose_name='Status')
    confirmed_at = models.DateTimeField(null=True, blank=True, verbose_name='Confirmation date')
    reverted_at = models.DateTimeField(null=True, blank=True, verbose_name='Revert date')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Invoice'
        verbose_name_plural = 'Invoices'
        ordering = ['-created_at']

    def __str__(self):
        return f'Invoice dated {self.invoice_date or self.created_at.date()}'


class InvoicePhoto(models.Model):
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name='photos')
    photo = models.ImageField(upload_to='invoices/')

    class Meta:
        verbose_name = 'Invoice photo'
        verbose_name_plural = 'Invoice photos'


class InvoiceNameMapping(models.Model):
    invoice_name = models.CharField(max_length=500, unique=True, verbose_name='Name from invoice')
    variant = models.ForeignKey(
        ProductVariant, on_delete=models.CASCADE,
        verbose_name='Product in DB'
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Invoice name mapping'
        verbose_name_plural = 'Invoice name mappings'

    def __str__(self):
        return f'{self.invoice_name} → {self.variant.name}'


class InvoiceItem(models.Model):
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name='items', verbose_name='Invoice')
    invoice_name = models.CharField(max_length=500, verbose_name='Name from invoice')
    quantity = models.PositiveIntegerField(verbose_name='Quantity')
    website_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, verbose_name='Handwritten price')
    matched_variant = models.ForeignKey(
        ProductVariant, null=True, blank=True,
        on_delete=models.SET_NULL,
        verbose_name='Product in DB'
    )
    similarity = models.FloatField(null=True, blank=True, verbose_name='Similarity')
    confirmed = models.BooleanField(default=False, verbose_name='Confirmed')
    manually_edited = models.BooleanField(default=False, verbose_name='Edited manually')
    original_stock = models.IntegerField(null=True, blank=True, verbose_name='Stock before confirmation')
    original_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, verbose_name='Price before confirmation')
    # How much the confirmation actually raised variant.stock (stock_after - stock_before).
    # Revert subtracts exactly this instead of restoring the absolute original_stock, so sales
    # made between confirmation and revert are not wiped out. NULL means legacy (before the fix).
    applied_delta = models.IntegerField(null=True, blank=True, verbose_name='Applied stock delta')
    # Own-packaging stock receipt: the line added weight to a BULK BAG (BulkSource), not to
    # variant.stock.
    # applied_bag_id is the bag that was topped up; applied_bag_amount is how much (in bag units,
    # g/ml).
    # Revert subtracts exactly applied_bag_amount from that bag. Both NULL means a regular per-piece
    # receipt.
    applied_bag_id = models.PositiveIntegerField(null=True, blank=True, verbose_name='Receipt bag')
    applied_bag_amount = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True, verbose_name='Topped up into bag')

    class Meta:
        verbose_name = 'Invoice item'
        verbose_name_plural = 'Invoice items'

    def __str__(self):
        return self.invoice_name


# ─── Own-packaging stock receipt: parsing the pack weight from the supplier invoice + bag top-up
# plan ───

# Weight token in a name: number + unit. "pcs" is not counted (it is a quantity, not a weight).
_PACK_WEIGHT_RE = re.compile(r'(\d+(?:[.,]\d+)?)\s*(кг|г|мл|л)\b', re.IGNORECASE)  # language data: Ukrainian units kg / g / ml / l


def parse_pack_weight(name):
    """Last weight token in the invoice line name → (amount_in_base_units, 'g'|'ml').
    kg→g×1000, g→g, l→ml×1000, ml→ml. None if there is no weight (e.g. «NPK 20-20-20»)."""
    matches = _PACK_WEIGHT_RE.findall(name or '')
    if not matches:
        return None
    num_str, unit = matches[-1]
    val = Decimal(num_str.replace(',', '.'))
    unit = unit.lower()
    # Language data: Ukrainian unit abbreviations as they appear in invoice names (кг = kg, г = g, л = l, мл = ml).
    if unit == 'кг':
        return (val * 1000, 'g')
    if unit == 'г':
        return (val, 'g')
    if unit == 'л':
        return (val * 1000, 'ml')
    if unit == 'мл':
        return (val, 'ml')
    return None


def bag_refill_plan(variant, invoice_name, quantity):
    """Whether a supplier invoice line is a receipt into a BAG of own packaging. Rule:
    the product is own, and the pack weight from the invoice does NOT equal the packaging size
    (unit_size) —
    meaning a larger wholesale pack (a bag) arrived, not a ready-made pack.

    Returns a dict or None (regular per-piece receipt):
      {'eligible': bool,       # the product can be poured into a bag at all (own)
       'is_bag': bool,         # auto-detect: this is a receipt into a bag
       'amount': Decimal,      # how much to top up (weight×quantity, base units)
       'unit': 'g'|'ml',       # bag unit
       'bag': BulkSource|None, # the variant's existing bag (None → created on confirmation)
       'bag_current': Decimal} # current bag stock level
    """
    if not variant or not getattr(variant, 'product_id', None):
        return None
    if getattr(variant.product, 'packaging_kind', None) != 'own':
        return None
    bag = variant.bulk_source
    parsed = parse_pack_weight(invoice_name)
    us = variant.unit_size or 0
    unit = (bag.unit if bag else (parsed[1] if parsed else 'g'))
    # Packaging size in the same units — for the 'received ≠ packaging' comparison.
    pack_amount = parsed[0] if parsed else None
    pack_unit = parsed[1] if parsed else None
    # Variant with bulk-bag accounting: a receipt ALWAYS goes into the bag (per-piece stock won't
    # hold,
    # ProductVariant.save recomputes it from the bag) — mirrors _bag_variant in confirm_invoice.
    bag_linked = bool(bag and variant.unit_size and not variant.stock_manual)
    # qty=0 → 0 (don't inflate it to 1 pack); the bag unit must match the weight unit.
    amt = (Decimal(str(pack_amount)) * Decimal(str(quantity or 0))
           if (pack_amount is not None and (not bag or bag.unit == pack_unit)) else Decimal('0'))
    is_bag = False
    amount = Decimal('0')
    if bag_linked:
        is_bag = True
        amount = amt
    elif not variant.stock_manual and pack_amount is not None and (not bag or bag.unit == pack_unit):
        # own without a bag (or a bag in the same unit), NOT manual stock, wholesale weight ≠
        # packaging size
        # → receipt into a bag (we'll create the bag). Manual (stock_manual) own is per-piece
        # accounting.
        if not us or Decimal(str(pack_amount)) != Decimal(str(us)):
            is_bag = True
            amount = amt
    return {
        'eligible': True,
        'is_bag': is_bag,
        'amount': amount,
        'unit': unit,
        'bag': bag,
        'bag_current': (bag.quantity if bag else Decimal('0')),
    }


_BAG_UNIT_LABEL = {'g': 'g', 'ml': 'ml', 'pcs': 'pcs'}


def bag_item_payload(variant, invoice_name, quantity):
    """JSON fields of a supplier invoice line for the form: whether it is a receipt into a bag + how
    much to top up.
    Shared shape for process_invoice and InvoiceAdmin.change_view."""
    plan = bag_refill_plan(variant, invoice_name, quantity)
    if not plan:
        return {'bag_eligible': False, 'is_bag': False, 'bag_amount': None,
                'bag_unit': '', 'bag_current': 0, 'unit_size': None}
    return {
        'bag_eligible': True,
        'is_bag': plan['is_bag'],
        'bag_amount': float(plan['amount']) if plan['amount'] else None,
        'bag_unit': _BAG_UNIT_LABEL.get(plan['unit'], 'g'),
        'bag_current': float(plan['bag_current']),
        'unit_size': variant.unit_size,
    }


# ─── Stock write-off on OrderItem creation / return on deletion ───
from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver


def _bundle_components(bundle):
    """Returns the bundle components."""
    if not bundle:
        return []
    return list(bundle.items.select_related('product_variant', 'product_variant__bulk_source').all())


def _recompute_stock_labels(variant):
    """After variant.stock is changed via queryset .update()/F() (own write-off/return branches)
    the post_save signal does NOT fire, so we recompute stock-dependent things explicitly:
    auto-labels (the hybrid's "made to order" label) and the "sold out" mark.
    The bag branch does this itself via sync_variants→save()."""
    if variant is None:
        return
    try:
        from catalog.signals import recompute_stock_labels
        recompute_stock_labels(variant)
    except Exception:
        pass
    try:
        from catalog.models import sync_out_of_stock_mark
        variant.refresh_from_db(fields=['stock'])
        sync_out_of_stock_mark(variant, commit=True)
    except Exception:
        pass


def _consume_variant(variant, units, allow_negative=False):
    """Writes off `units` pieces of the variant's packaging from stock. Returns (consumed, bag_id,
    oversold):
      consumed — the amount ACTUALLY written off (Decimal): grams/ml if from a bag, otherwise
      pieces;
      bag_id   — id of the bag it was written off from (or None if from variant.stock);
      oversold — True if more was needed than is in stock (clamped to 0).
    The bag/variant row is locked with select_for_update, so both oversell detection and the clamp
    are exact even with concurrent orders (e.g. two monobank payments for the last unit).
    For own packagings with stock_manual=True we don't touch the bag (manual stock is detached).
    allow_negative — the manager confirmed a write-off beyond stock against a planned delivery
    (OrderItem.allow_negative_stock): the clamp to 0 is not applied, stock/bag goes negative."""
    if not variant or units <= 0:
        return Decimal('0'), None, False
    # Pure dropshipping (is_dropshipping, NOT hybrid): variant.stock is a MIRROR of the supplier's
    # (Bradas) stock level, not our warehouse; dropshipping is fulfilled 100% by the supplier and
    # sold beyond stock. We don't touch the mirror — otherwise it would drop to 0 and the product
    # would wrongly vanish from the storefront until the next daily sync_dropshipping. Return
    # consumed=0 (not NULL) → _restore also becomes a no-op (symmetry). Hybrid (real own
    # stock) is written off as usual.
    if getattr(variant, 'is_dropshipping', False) and not getattr(variant, 'is_hybrid', False):
        return Decimal('0'), None, False
    from django.db.models import F, Value
    from django.db.models.functions import Greatest
    from catalog.models import BulkSource
    if variant.bulk_source_id and variant.unit_size and not variant.stock_manual:
        bag = BulkSource.objects.select_for_update().filter(pk=variant.bulk_source_id).first()
        # need — REAL g/ml for `units` packs, accounting for packaging tolerance (effective_unit).
        eff = bag.effective_unit(variant.unit_size) if bag else Decimal(str(variant.unit_size))
        need = Decimal(str(units)) * eff
        cur = Decimal(str(bag.quantity)) if (bag and bag.quantity is not None) else Decimal('0')
        oversold = need > cur
        consumed = need if (allow_negative or not oversold) else cur
        BulkSource.apply_delta(variant.bulk_source_id, -consumed, allow_negative=allow_negative)
        if oversold:
            if allow_negative:
                log.info('BACKORDER variant=%s need=%s bag_had=%s — bag written off below zero for the incoming delivery', variant.pk, need, cur)
            else:
                log.warning('OVERSELL variant=%s need=%s bag_had=%s — bag clamped to 0', variant.pk, need, cur)
        return consumed, variant.bulk_source_id, (oversold and not allow_negative)
    cur = ProductVariant.objects.select_for_update().filter(
        pk=variant.pk).values_list('stock', flat=True).first() or 0
    oversold = units > cur
    consumed_units = units if (allow_negative or not oversold) else cur
    if allow_negative:
        ProductVariant.objects.filter(pk=variant.pk).update(stock=F('stock') - consumed_units)
    else:
        ProductVariant.objects.filter(pk=variant.pk).update(
            stock=Greatest(F('stock') - consumed_units, Value(0)))
    if oversold:
        if allow_negative:
            log.info('BACKORDER variant=%s need=%s stock_had=%s — stock written off below zero for the incoming delivery', variant.pk, units, cur)
        else:
            log.warning('OVERSELL variant=%s need=%s stock_had=%s — stock clamped to 0', variant.pk, units, cur)
    _recompute_stock_labels(variant)
    return Decimal(str(consumed_units)), None, (oversold and not allow_negative)


def _restore_amount(bag_id, variant, amount):
    """Returns EXACTLY `amount` (what was actually written off earlier) to stock — with no upper clamp,
    so the bag/stock level isn't inflated beyond what was written off. Target: bag bag_id if set
    (survives variant deletion); otherwise variant.stock."""
    if amount is None:
        return
    amount = Decimal(str(amount))
    if amount <= 0:
        return
    from django.db.models import F
    from catalog.models import BulkSource
    if bag_id:
        BulkSource.apply_delta(bag_id, amount)
    elif variant is not None:
        ProductVariant.objects.filter(pk=variant.pk).update(stock=F('stock') + int(amount))
        _recompute_stock_labels(variant)


def _restore_nominal(variant, units):
    """Legacy return for lines created BEFORE exact accounting (stock_consumed IS NULL).

    For a non-bulk variant we return units to variant.stock — a regular product wrote off exactly
    stock back then, so the return is symmetric.

    For own packaging (bulk_source) we DO NOT TOUCH THE BAG: it is unknown whether anything was ever
    written off from the bag. Many legacy lines were sold when the product was a regular one (on
    variant.stock) and only later switched to bag accounting. Returning the nominal to the bag for
    such
    lines INFLATED the bag weight (kg in the CRM grew without any write-off). So for legacy own
    packagings
    we skip the return to the bag — exact accounting (stock_consumed) works as before via
    _restore_amount and is unaffected."""
    if not variant or units <= 0:
        return
    if variant.bulk_source_id and variant.unit_size and not variant.stock_manual:
        return  # don't inflate the bag with a legacy nominal
    _restore_amount(None, variant, Decimal(str(units)))


def _iter_bundle_targets(item):
    """Yields (record, variant, units) for each stock target of a bundle item:
      record  — OrderItemBundleComponent (where to store/read what was actually written off), or
      None
                for the legacy fallback without a snapshot (then the restore is always nominal);
      variant — the component's ProductVariant (may be None if the variant was deleted);
      units   — how many pieces of packaging to write off/restore (quantity per bundle × number of
      bundles).
    For bundle items it reads the OrderItemBundleComponent snapshot — the stock accounts for the
    locally
    edited bundle composition, not the current Bundle template."""
    qty = item.quantity or 0
    # When a line is deleted, the cascade removes the components EARLIER than OrderItem's
    # post_delete
    # arrives, so a live query here returns empty and _restore would fall into the legacy fallback —
    # returning the bundle template's NOMINAL instead of what was actually written off (inflating
    # stock
    # for every component removed from this order). The snapshot is taken by pre_delete, like
    # _fresh_stock.
    comps = getattr(item, '_fresh_components', None)
    if comps is None:
        comps = list(item.bundle_components.select_related('product_variant').all())
    if comps:
        for c in comps:
            yield c, c.product_variant, c.quantity * qty
        return
    # Fallback for very old lines without a snapshot.
    bundle = item.bundle if item.bundle_id else None
    for bi in _bundle_components(bundle):
        yield None, bi.product_variant, bi.quantity * qty


def _consume(item):
    """Writes off stock for a line exactly once (idempotent via stock_applied) and stores the ACTUALLY
    written-off amount in stock_consumed (+ bag) on the OrderItem or on each component, so the
    return
    is exact. Returns True on oversell of OWN stock; for was_dropshipped we don't signal oversell
    (the excess is deliberately dropshipped)."""
    if item.course_id:
        return False
    if item.stock_applied:
        return False
    qty = item.quantity or 0
    oversold = False
    neg = bool(getattr(item, 'allow_negative_stock', False))
    if qty:
        if item.bundle_id:
            for record, variant, units in _iter_bundle_targets(item):
                if not variant:
                    continue
                consumed, bag_id, ovs = _consume_variant(variant, units, allow_negative=neg)
                if record is not None:
                    OrderItemBundleComponent.objects.filter(pk=record.pk).update(
                        stock_consumed=consumed, stock_consumed_bag_id=bag_id)
                if ovs:
                    oversold = True
        elif item.product_variant_id and item.product_variant:
            consumed, bag_id, ovs = _consume_variant(item.product_variant, qty, allow_negative=neg)
            OrderItem.objects.filter(pk=item.pk).update(
                stock_consumed=consumed, stock_consumed_bag_id=bag_id)
            if ovs:
                oversold = True
        # else: the product/bundle was deleted — nothing to write off.
    OrderItem.objects.filter(pk=item.pk).update(stock_applied=True)
    item.stock_applied = True
    if item.was_dropshipped:
        return False
    return oversold


def _order_is_imported(item):
    """Whether the line belongs to an imported OLD order (is_imported=True)."""
    return bool(item.order_id) and Order.objects.filter(
        pk=item.order_id, is_imported=True).exists()


def _imported_phantom(item):
    """True — an item of an imported OLD order that was NEVER actually written off.
    The legacy order import sets stock_applied=True as a HISTORICAL marker (stock is not moved
    during import, stock_consumed=NULL). For these, _restore must be a no-op: otherwise the legacy
    branch of _restore_nominal would return the nominal amount = INFLATING the stock by a quantity
    that was never taken off (cumulative drift on every cancellation/deletion of an OLD order).
    Items added later in the CRM have a real stock_consumed (variant) or consumed components
    (bundle) — they are restored normally."""
    if not _order_is_imported(item):
        return False
    if item.bundle_id:
        try:
            return not item.bundle_components.filter(stock_consumed__isnull=False).exists()
        except Exception:
            return True
    return item.stock_consumed is None and item.stock_consumed_bag_id is None


def _restore(item):
    """Returns EXACTLY what was written off (stock_consumed) to stock. For legacy lines
    (stock_consumed IS NULL) — the nominal. Idempotent via stock_applied. The bag it was written off
    from is taken from stock_consumed_bag_id, so the return works even if the variant was deleted
    later (SET_NULL).

    Returns a list of {'variant', 'amount', 'bag_id'} — the exact accounting of what was actually
    returned (legacy nominal is NOT included: there is no exact figure that could later be partially
    rolled back). Called by the admin when replacing a product in an order, to offer the manager to
    confirm the return to stock (replacement often happens PRECISELY BECAUSE the product is out of
    stock anyway)."""
    restored = []
    if item.course_id:
        return restored
    if not item.stock_applied:
        return restored
    # Imported OLD line without a real write-off → nothing to return. We leave the stock_applied
    # flag
    # True (untouched) so that a repeated _consume when a cancellation is lifted also stays a no-op
    # —
    # import doesn't touch stock at all. The value can be taken from the pre_delete cache
    # (post_delete
    # arrives during a cascade when the Order/components are already gone), otherwise computed live
    # (cancellation path — the order still exists).
    phantom = getattr(item, '_imported_phantom_cached', None)
    if phantom is None:
        phantom = _imported_phantom(item)
    if phantom:
        return restored
    if item.bundle_id:
        for record, variant, units in _iter_bundle_targets(item):
            if record is not None and record.stock_consumed is not None:
                _restore_amount(record.stock_consumed_bag_id, variant, record.stock_consumed)
                if variant is not None:
                    restored.append({'variant': variant, 'amount': record.stock_consumed,
                                      'bag_id': record.stock_consumed_bag_id})
                OrderItemBundleComponent.objects.filter(pk=record.pk).update(
                    stock_consumed=None, stock_consumed_bag_id=None)
            else:
                _restore_nominal(variant, units)
    elif item.product_variant_id or item.stock_consumed_bag_id:
        if item.stock_consumed is not None:
            _restore_amount(item.stock_consumed_bag_id, item.product_variant, item.stock_consumed)
            if item.product_variant is not None:
                restored.append({'variant': item.product_variant, 'amount': item.stock_consumed,
                                  'bag_id': item.stock_consumed_bag_id})
        else:
            _restore_nominal(item.product_variant, item.quantity or 0)
        OrderItem.objects.filter(pk=item.pk).update(stock_consumed=None, stock_consumed_bag_id=None)
    OrderItem.objects.filter(pk=item.pk).update(stock_applied=False)
    item.stock_applied = False
    return restored


def _ensure_bundle_components_snapshot(item):
    """Creates the component snapshot for a bundle OrderItem if not created yet.
    Together with it, freezes instruction_snapshot (see _ensure_instruction_snapshot) —
    REGARDLESS of whether components already existed: old orders already have a contents
    snapshot, but their instruction_snapshot is still empty (new field)."""
    if not item.bundle_id:
        return
    bundle = item.bundle

    if not item.bundle_components.exists() and bundle is not None:
        snapshots = []
        for bi in bundle.items.select_related('product_variant').all():
            pv = bi.product_variant
            snapshots.append(OrderItemBundleComponent(
                order_item=item,
                product_variant=pv,
                quantity=bi.quantity,
                product_variant_name=(pv.name or '') if pv else '',
                product_variant_sku=(pv.sku or '') if pv else '',
            ))
        if snapshots:
            OrderItemBundleComponent.objects.bulk_create(snapshots)

    _ensure_instruction_snapshot(item, bundle)


def _ensure_instruction_snapshot(item, bundle=None):
    """Freezes the FULL instruction (heading + text) for this item — forever.

    bundle.instruction_html is taken as it is RIGHT NOW: for a freshly created OrderItem the item's
    composition was just copied one-to-one from the template, so there is no divergence yet and
    no diff needs to be computed. For old orders (backfill or first edit in the CRM) the current
    text
    is used — the best available approximation, since no historical snapshot from purchase time
    exists.

    Idempotent: if a snapshot already exists, leave it alone (even if the bundle template or the
    instruction text itself is edited later — the customer sees what they always saw)."""
    if not item.bundle_id or item.instruction_snapshot:
        return
    if bundle is None:
        bundle = item.bundle
    if bundle is None or not (bundle.instruction_html or '').strip():
        return
    from django.utils.html import escape
    from .instruction_docx import strip_emoji

    title = strip_emoji(bundle.name)
    html = f'<h1>{escape(title)}</h1>{bundle.instruction_html}'
    OrderItem.objects.filter(pk=item.pk).update(instruction_snapshot=html)
    item.instruction_snapshot = html


def _stock_consumable_state(status, payment_method, payment_status):
    """Whether stock should be held for the given state (status/method/payment status).
    Online prepayment (monobank) holds stock only AFTER payment — otherwise abandoned unpaid
    invoices
    would block stock for ~24h. For cod/bank_transfer — from creation. Cancelled — never. The single
    source of truth for checking transitions in the signal."""
    if status == 'cancelled':
        return False
    if payment_method == 'monobank' and payment_status != 'paid':
        return False
    return True


def _stock_consumable(order):
    """Whether stock should be written off for this order right now."""
    if order is None:
        return False
    return _stock_consumable_state(order.status, order.payment_method, order.payment_status)




def apply_bundle_component_edits(item, comps, order):
    """Applies per-line editing of the bundle contents to OrderItemBundleComponent
    (variant replacement / removal / quantity / addition) with a safe stock recalculation.

    `comps` is the desired full contents of the line: a list of {comp_id|None, variant_id|None,
    qty}.
    Stock logic is safe thanks to reusing _restore/_consume:
      • if stock is currently held (stock_applied) → _restore (return the old contents),
        apply the snapshot edits, _consume (write off the new contents);
      • for an unpaid monobank / cancelled order (stock_applied=False) we don't move stock —
        we only edit the snapshot.
    Called in save_formset AFTER top-level lines are saved/deleted."""
    if not item.bundle_id:
        return
    # Guarantee the snapshot BEFORE _restore, so that both restore and write-off read the same
    # per-order snapshot, not the current Bundle template (for legacy items without a snapshot
    # the sources would otherwise diverge).
    _ensure_bundle_components_snapshot(item)
    was_applied = item.stock_applied
    if was_applied:
        _restore(item)  # returns the old stock, stock_applied→False, clears consumed
    existing = {c.pk: c for c in item.bundle_components.all()}
    keep = set()
    for c in (comps or []):
        if not isinstance(c, dict):
            continue
        try:
            qty = min(1000, max(1, int(c.get('qty') or 1)))  # clamp as in create_order
        except (TypeError, ValueError):
            qty = 1
        vid = c.get('variant_id')
        vid = int(vid) if str(vid).isdigit() else None  # robust to garbage in POST
        pv = ProductVariant.objects.filter(pk=vid).first() if vid else None
        # Service supplier-fallback variant (dropshipping stub) — not a stock component.
        if pv is not None and getattr(pv, 'is_supplier_fallback', False):
            pv = None
        comp_id = c.get('comp_id')
        if comp_id and comp_id in existing:
            rec = existing[comp_id]
            rec.product_variant = pv
            rec.quantity = qty
            if pv:
                rec.product_variant_name = pv.name or ''
                rec.product_variant_sku = pv.sku or ''
            rec.stock_consumed = None
            rec.stock_consumed_bag_id = None
            rec.save()
            keep.add(comp_id)
        else:
            rec = OrderItemBundleComponent.objects.create(
                order_item=item, product_variant=pv, quantity=qty,
                product_variant_name=(pv.name or '') if pv else '',
                product_variant_sku=(pv.sku or '') if pv else '',
            )
            keep.add(rec.pk)
    for cid, rec in existing.items():
        if cid not in keep:
            rec.delete()
    # The frozen instruction_snapshot (if one existed) described the OLD contents of the line —
    # after a manual component replacement it would lie to the customer (wrong product/dose).
    # Reset it: the next display ("My orders") recomputes it via
    # instruction_resolve, which knows how to carry the PACKAGING over to the new replacement.
    if item.instruction_snapshot:
        item.instruction_snapshot = ''
        OrderItem.objects.filter(pk=item.pk).update(instruction_snapshot='')
    if _stock_consumable(order):
        item.refresh_from_db()
        _consume(item)


@receiver(post_save, sender=OrderItem)
def _orderitem_consume_stock(sender, instance, created, **kwargs):
    if created:
        # First create the component snapshot — _consume for a bundle reads it
        _ensure_bundle_components_snapshot(instance)
        if _stock_consumable(instance.order):
            _consume(instance)


@receiver(models.signals.pre_delete, sender=OrderItem)
def _orderitem_capture_fresh_stock(sender, instance, **kwargs):
    """Fresh (from DB) stock state at deletion time — so post_delete doesn't rely on
    a stale in-memory value of the formset instance.

    Double-return case: in ONE CRM submit the manager sets status='cancelled' AND deletes a line.
    Django first runs save_model → Order.save → _order_status_changed, which already returns stock
    and
    zeroes stock_applied/stock_consumed in the DB. But the formset instance, loaded BEFORE the save,
    holds stock_applied=True/stock_consumed=X in memory, so post_delete _restore would return stock
    a SECOND time (inflating the bag). Here we read the actual state from the DB (the record still
    exists)."""
    fresh = OrderItem.objects.filter(pk=instance.pk).values(
        'stock_applied', 'stock_consumed', 'stock_consumed_bag_id').first()
    if fresh:
        instance._fresh_stock = fresh
    # Whether this is a 'phantom' imported line (stock was never written off) — computed NOW,
    # while the Order and bundle components still exist (post_delete arrives during the order
    # deletion
    # cascade, when these rows may already be gone).
    instance._imported_phantom_cached = _imported_phantom(instance)
    # For the same reason — a snapshot of the bundle components: post_delete would see them already
    # removed by the cascade and would return the template's nominal instead of what was actually
    # written off.
    if instance.bundle_id:
        instance._fresh_components = list(
            instance.bundle_components.select_related('product_variant').all())


@receiver(post_delete, sender=OrderItem)
def _orderitem_restore_stock(sender, instance, **kwargs):
    # Use the FRESH state from pre_delete instead of the stale in-memory one: if stock has already
    # been returned (e.g. cancellation in the same submit) — stock_applied in the DB was False,
    # so _restore correctly does nothing (no double return).
    fresh = getattr(instance, '_fresh_stock', None)
    if fresh is not None:
        instance.stock_applied = fresh['stock_applied']
        instance.stock_consumed = fresh['stock_consumed']
        instance.stock_consumed_bag_id = fresh['stock_consumed_bag_id']
    _restore(instance)


@receiver(models.signals.pre_save, sender=Order)
def _order_capture_prev_status(sender, instance, **kwargs):
    if instance.pk:
        try:
            prev = Order.objects.only(
                'status', 'payment_status', 'payment_method').get(pk=instance.pk)
            instance._prev_status = prev.status
            instance._prev_payment_status = prev.payment_status
            instance._prev_payment_method = prev.payment_method
        except Order.DoesNotExist:
            instance._prev_status = None
            instance._prev_payment_status = None
            instance._prev_payment_method = None
    else:
        instance._prev_status = None
        instance._prev_payment_status = None
        instance._prev_payment_method = None


@receiver(post_save, sender=Order)
def _order_status_changed(sender, instance, created, **kwargs):
    """Reconcile stock to the required state based on a PURE transition of 'whether stock should be
    held'.
    Previously there were two separate handlers (status + monobank paid) — they didn't see a
    payment_method change, so switching an unpaid monobank order to cod did NOT write off stock
    (the order shipped without decreasing the stock level). Now we compare _stock_consumable_state
    BEFORE and AFTER (accounts for status + method + payment status) — one path covers
    cancellation/return, monobank payment and payment method change.
    _consume/_restore are idempotent via stock_applied, so repeated triggers are safe."""
    if created:
        return
    prev = getattr(instance, '_prev_status', None)
    prev_pay = getattr(instance, '_prev_payment_status', None)
    prev_method = getattr(instance, '_prev_payment_method', None)
    new, new_pay, new_method = instance.status, instance.payment_status, instance.payment_method
    if prev == new and prev_pay == new_pay and prev_method == new_method:
        return

    was = prev is not None and _stock_consumable_state(prev, prev_method, prev_pay)
    now = _stock_consumable_state(new, new_method, new_pay)
    if was == now:
        return

    items = list(instance.items.select_related(
        'product_variant', 'bundle').all())
    with transaction.atomic():
        if now:
            # Became 'hold stock': cancellation lifted / monobank paid / unpaid monobank
            # switched to cod/transfer.
            for it in items:
                _consume(it)
        else:
            # Stopped: cancellation or monobank returned to the unpaid state.
            for it in items:
                _restore(it)


# ─── Bank statements (Mono Personal / Privat Business) ──────────────────
# Universal models for monitoring incoming transfers. They allow auto-confirming
# "bank transfer" payments without a manager.
#
# Flows:
#   1. Polling: a management command pulls the statement → BankTransaction.
#   2. Webhook (mono, in production): POST /api/webhooks/monobank/ → BankTransaction.
#   3. Matcher: for each unmatched transaction looks for candidate orders
#      → creates PaymentMatch with confidence; if score ≥ threshold — Order.paid.

class BankAccount(models.Model):
    """Bank account/card being monitored."""

    SOURCE_CHOICES = [
        ('mono', 'Monobank Personal'),
        ('privat', 'PrivatBank Business'),
    ]

    source = models.CharField(max_length=10, choices=SOURCE_CHOICES, db_index=True, verbose_name='Source')
    name = models.CharField(max_length=100, verbose_name='Name (internal)',
                            help_text='E.g. "Sole proprietor Jane / IBAN" or "Mono black 0000"')
    # Mono: accountId (base64 id from client-info). Privat: account IBAN.
    external_id = models.CharField(max_length=100, db_index=True, verbose_name='Bank ID')
    iban = models.CharField(max_length=34, blank=True, verbose_name='IBAN')
    masked_pan = models.CharField(max_length=64, blank=True, verbose_name='Card mask')
    currency_code = models.IntegerField(default=980, verbose_name='Currency (ISO 4217)',
                                        help_text='980 = UAH, 840 = USD, 978 = EUR')
    is_active = models.BooleanField(default=True, db_index=True, verbose_name='Active')

    # Polling cursor: the last synced moment.
    last_synced_at = models.DateTimeField(null=True, blank=True, verbose_name='Last sync')
    # Last seen transaction external_id (for dedup at the window boundary).
    last_transaction_external_id = models.CharField(max_length=100, blank=True)

    # Raw metadata from the API (cashbackType, type, sendId, etc.). For debugging.
    metadata = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Bank account'
        verbose_name_plural = 'Bank accounts'
        constraints = [
            models.UniqueConstraint(fields=['source', 'external_id'], name='bankaccount_uniq_source_extid'),
        ]
        ordering = ['source', 'name']

    def __str__(self):
        return f'{self.get_source_display()}: {self.name}'


class BankTransaction(models.Model):
    """Statement transaction. Dedup by (source, external_id)."""

    SOURCE_CHOICES = BankAccount.SOURCE_CHOICES

    STATUS_CHOICES = [
        ('unmatched', 'Not linked'),
        ('matched', 'Linked to an order'),
        ('manual_review', 'Needs manual review'),
        ('ignored', 'Ignore'),
    ]

    source = models.CharField(max_length=10, choices=SOURCE_CHOICES, db_index=True, verbose_name='Source')
    account = models.ForeignKey(BankAccount, on_delete=models.PROTECT, related_name='transactions',
                                verbose_name='Account')
    external_id = models.CharField(max_length=100, verbose_name='Bank transaction ID')

    transacted_at = models.DateTimeField(db_index=True, verbose_name='Transaction time')
    # Always in the account's main currency. Sign: + incoming, − outgoing.
    amount = models.DecimalField(max_digits=12, decimal_places=2, verbose_name='Amount',
                                 help_text='+ incoming, − outgoing (in kopecks, converted to UAH)')
    currency_code = models.IntegerField(default=980, verbose_name='Currency')

    # Mono: description ("From: <payer name>" or "Credit from card ..." — the bank's own wording, in Ukrainian).
    # Privat: AUT_CNTR_NAM / full counterparty name.
    description = models.CharField(max_length=500, blank=True, verbose_name='Description')
    # Mono: comment (what the sender wrote). Privat: OSND (payment purpose).
    comment = models.CharField(max_length=500, blank=True, verbose_name='Comment / payment purpose')
    # Extracted full name of the counterparty (if it could be parsed from description).
    counterparty_name = models.CharField(max_length=200, blank=True, db_index=True, verbose_name='Counterparty full name')

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='unmatched',
                              db_index=True, verbose_name='Status')

    # Full raw API response — for debugging and further parsing.
    raw_data = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Bank transaction'
        verbose_name_plural = 'Bank transactions'
        constraints = [
            models.UniqueConstraint(fields=['source', 'external_id'], name='banktransaction_uniq_source_extid'),
        ]
        indexes = [
            models.Index(fields=['status', '-transacted_at'], name='banktx_status_time_idx'),
            models.Index(fields=['amount', '-transacted_at'], name='banktx_amount_time_idx'),
        ]
        ordering = ['-transacted_at']

    def __str__(self):
        sign = '+' if self.amount >= 0 else ''
        label = self.counterparty_name or self.description[:40] or self.comment[:40]
        return f'{sign}{self.amount} ₴ {self.transacted_at:%d.%m %H:%M} {label}'

    @property
    def is_credit(self) -> bool:
        """True for incoming payments (those are the ones we care about)."""
        return self.amount > 0


class PaymentMatch(models.Model):
    """Link Order ↔ BankTransaction with a confidence score and the match source.

    One Order can have several candidate matches (especially when confidence
    is low — the manager picks manually). Only one can be is_active=True.
    """

    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='bank_matches',
                              verbose_name='Order')
    transaction = models.ForeignKey(BankTransaction, on_delete=models.CASCADE,
                                    related_name='order_matches', verbose_name='Transaction')

    # 0–100: 100 = exact match of the order number in the comment;
    # 95 = unique amount; 70–80 = full name; below — weak hypotheses.
    confidence = models.IntegerField(verbose_name='Confidence (0–100)')
    # List of match factors: [{'type':'order_number_in_comment','weight':100,'detail':'...'}].
    reasons = models.JSONField(default=list, verbose_name='Match reasons')

    is_active = models.BooleanField(default=True, db_index=True,
                                    help_text='Active match: affects the payment_status of the order',
                                    verbose_name='Active')
    auto_confirmed = models.BooleanField(default=False, verbose_name='Confirmed automatically')

    confirmed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                     on_delete=models.SET_NULL, related_name='+',
                                     verbose_name='Confirmed by (manual)')
    confirmed_at = models.DateTimeField(null=True, blank=True, verbose_name='Confirmation time')

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Payment match'
        verbose_name_plural = 'Payment matches'
        constraints = [
            models.UniqueConstraint(fields=['order', 'transaction'], name='paymentmatch_uniq_order_tx'),
        ]
        ordering = ['-confidence', '-created_at']

    def __str__(self):
        return f'#{self.order.order_number} ↔ tx {self.transaction.external_id[:12]} ({self.confidence}%)'
