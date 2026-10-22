import re
import os

from core.jsonutils import js_json
import json
from datetime import datetime, time as dt_time
from django import forms
from django.conf import settings
from django.contrib import admin
from django_summernote.models import Attachment
admin.site.unregister(Attachment)
from django.contrib.admin.models import LogEntry, ADDITION, CHANGE, DELETION
from django.contrib.admin.widgets import AdminDateWidget
from django.db import transaction
from django.db.models import Case, When, Prefetch, Count, Max
from django.forms import CheckboxSelectMultiple
from django.utils import timezone
from django.utils.html import format_html, format_html_join
from django.utils.safestring import mark_safe
from .widgets import QuillWidget
from .printout_thumb import thumb_rel_path
from .models import (
    Category, Label, Attribute, AttributeValue,
    Brand, Product, ProductVariant, ProductImage, ProductCharacteristic, VariantPrintout,
    Bundle, BundleItem, BundleImage, BundleInstructionOverride,
    BulkSource,
    DropshippingVariant, DropshippingProduct, VariantLabel,
    Review, StockNotification, DropSyncEvent, SiteSettings, PaymentCard,
    Favorite, Supplier, StockWriteOff, StockWriteOffItem,
)
from .revert import capture_fields, capture_full_snapshot
from core.admin_filters import IsActiveFilter


@admin.register(Supplier)
class SupplierAdmin(admin.ModelAdmin):
    list_display = ('name', 'code', 'kind', 'requires_cargo_branch', 'is_active')
    list_editable = ('is_active',)
    list_filter = ('kind', 'requires_cargo_branch', 'is_active')


class StockWriteOffItemInline(admin.TabularInline):
    model = StockWriteOffItem
    extra = 0
    readonly_fields = ('variant', 'variant_name', 'variant_sku', 'quantity')
    can_delete = False


@admin.register(StockWriteOff)
class StockWriteOffAdmin(admin.ModelAdmin):
    """View only — audit trail of manual write-offs from the products page.
    No rollback (unlike Invoice) — the write-off itself is one-time."""
    list_display = ('__str__', 'created_by', 'note')
    readonly_fields = ('created_at', 'created_by', 'note', 'source_text')
    inlines = [StockWriteOffItemInline]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


# Global defaults for all tables: 50 per page, "Show all" up to 10 000 records
admin.ModelAdmin.list_per_page = 50
admin.ModelAdmin.list_max_show_all = 10000


class NoEmptyHistoryMixin:
    """Do not write to history and do not save if nothing changed."""
    def log_change(self, request, obj, message):
        if not message:
            return
        try:
            msgs = json.loads(message)
            if not any('changed' in m or 'added' in m or 'deleted' in m for m in msgs):
                return
        except Exception:
            pass
        super().log_change(request, obj, message)

    def save_model(self, request, obj, form, change):
        if change and not form.changed_data:
            return
        super().save_model(request, obj, form, change)


class RevertableAdminMixin:
    """Saves the object's old state in LogEntry so it can be rolled back."""

    def save_model(self, request, obj, form, change):
        if change and obj.pk:
            try:
                old = self.model.objects.get(pk=obj.pk)
                request._revert_snap = capture_fields(old)
            except self.model.DoesNotExist:
                pass
        super().save_model(request, obj, form, change)

    def save_related(self, request, form, formsets, change):
        obj = form.instance
        if change and obj.pk and hasattr(request, '_revert_snap'):
            m2m = {}
            for field in obj._meta.many_to_many:
                m2m[field.name] = list(
                    getattr(obj, field.name).values_list('pk', flat=True)
                )
            request._revert_snap['m2m'] = m2m
        super().save_related(request, form, formsets, change)

    def construct_change_message(self, request, form, formsets, add=False):
        msgs = super().construct_change_message(request, form, formsets, add)
        snap = getattr(request, '_revert_snap', None)
        if snap is not None:
            msgs.append({'revert': snap})
            del request._revert_snap
        return msgs

    def log_deletions(self, request, queryset):
        # Django 6+: called BEFORE the actual deletion,
        # so the objects still exist and we can capture a snapshot of each.
        from django.contrib.contenttypes.models import ContentType
        for obj in queryset:
            snap = capture_full_snapshot(obj)
            LogEntry.objects.create(
                user_id=request.user.pk,
                content_type_id=ContentType.objects.get_for_model(obj).pk,
                object_id=str(obj.pk),
                object_repr=str(obj)[:200],
                action_flag=DELETION,
                change_message=json.dumps([{'deletion_snapshot': snap}]),
            )


def _tree_ordered_category_qs(exclude_pk=None):
    """Queryset of all categories in depth-first tree order (for select widgets)."""
    cats = list(Category.objects.select_related('parent').order_by('sort_order', 'name'))
    if exclude_pk:
        cats = [c for c in cats if c.pk != exclude_pk]
    ordered_pks = []

    def walk(pid):
        for c in sorted(
            [c for c in cats if c.parent_id == pid],
            key=lambda x: (x.sort_order, x.name)
        ):
            ordered_pks.append(c.pk)
            walk(c.pk)

    walk(None)
    for c in cats:
        if c.pk not in ordered_pks:
            ordered_pks.append(c.pk)

    if not ordered_pks:
        return Category.objects.none()
    return (
        Category.objects
        .select_related('parent', 'parent__parent', 'parent__parent__parent')
        .filter(pk__in=ordered_pks)
        .order_by(Case(*[When(pk=pk, then=i) for i, pk in enumerate(ordered_pks)]))
    )


class HierarchicalCategoryChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        depth = 0
        p = obj.parent
        while p is not None:
            depth += 1
            p = p.parent
        return ('— ' * depth) + obj.name


class HierarchicalCategoryMultipleChoiceField(forms.ModelMultipleChoiceField):
    def label_from_instance(self, obj):
        depth = 0
        p = obj.parent
        while p is not None:
            depth += 1
            p = p.parent
        return ('— ' * depth) + obj.name


class CategoryAdminForm(forms.ModelForm):
    discount_start = forms.DateField(
        widget=AdminDateWidget(),
        required=False,
        label='Discount start',
    )
    discount_end = forms.DateField(
        widget=AdminDateWidget(),
        required=False,
        label='Discount end',
    )

    class Meta:
        model = Category
        fields = '__all__'

    def __init__(self, *args, **kwargs):
        instance = kwargs.get('instance')
        if instance:
            initial = kwargs.setdefault('initial', {})
            if instance.discount_start:
                initial.setdefault('discount_start', instance.discount_start.date())
            if instance.discount_end:
                initial.setdefault('discount_end', instance.discount_end.date())
        super().__init__(*args, **kwargs)
        exclude_pk = self.instance.pk if self.instance.pk else None
        self.fields['parent'] = HierarchicalCategoryChoiceField(
            queryset=_tree_ordered_category_qs(exclude_pk=exclude_pk),
            required=False,
            label='Parent category',
            empty_label='—',
        )
        # 0–100 in %, at most 3 digits.
        if 'discount_percent' in self.fields:
            self.fields['discount_percent'].widget.attrs.update({
                'min': '0', 'max': '100', 'step': '0.01', 'size': '3',
                'maxlength': '5',
            })

    def clean_discount_percent(self):
        v = self.cleaned_data.get('discount_percent')
        if v is None:
            return v
        if v < 0:
            raise forms.ValidationError('The discount cannot be negative.')
        if v > 100:
            raise forms.ValidationError('The discount cannot exceed 100%.')
        return v

    def clean_discount_start(self):
        value = self.cleaned_data.get('discount_start')
        return datetime.combine(value, dt_time(0, 0)) if value else None

    def clean_discount_end(self):
        value = self.cleaned_data.get('discount_end')
        return datetime.combine(value, dt_time(23, 59, 59)) if value else None


@admin.register(Category)
class CategoryAdmin(RevertableAdminMixin, NoEmptyHistoryMixin, admin.ModelAdmin):
    form = CategoryAdminForm
    list_display = ['drag_handle', 'display_name', 'show_discount', 'is_active']
    list_display_links = ['display_name']
    list_filter = [IsActiveFilter]
    search_fields = ['name']
    list_editable = ['is_active']
    exclude = ['slug', 'sort_order']
    change_list_template = 'admin/catalog/category/change_list.html'
    change_form_template = 'admin/catalog/category/change_form.html'
    save_on_top = True
    fieldsets = [
        (None, {'fields': ['name', 'parent']}),
        (None, {'fields': ['discount_percent', ('discount_start', 'discount_end'), 'is_active']}),
    ]
    list_per_page = 1000

    def _tree_ordering(self, request):
        if hasattr(request, '_cat_tree_order'):
            return request._cat_tree_order
        cats = list(
            self.model._default_manager.get_queryset()
            .select_related('parent')
            .order_by('sort_order', 'name')
        )
        ordered_pks = []

        def walk(pid):
            for c in sorted(
                [c for c in cats if c.parent_id == pid],
                key=lambda x: (x.sort_order, x.name)
            ):
                ordered_pks.append(c.pk)
                walk(c.pk)

        walk(None)
        for c in cats:
            if c.pk not in ordered_pks:
                ordered_pks.append(c.pk)

        request._cat_tree_order = (
            [Case(*[When(pk=pk, then=i) for i, pk in enumerate(ordered_pks)])]
            if ordered_pks else []
        )
        return request._cat_tree_order

    def get_ordering(self, request):
        return self._tree_ordering(request)

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('parent', 'parent__parent')

    @admin.display(description='')
    def drag_handle(self, obj):
        return format_html(
            '<span class="drag-handle" data-pk="{}" data-parent-pk="{}">⠿</span>',
            obj.pk, obj.parent_id or ''
        )

    @admin.display(description='Name')
    def display_name(self, obj):
        depth = 0
        p = obj.parent
        while p is not None:
            depth += 1
            p = p.parent
        if depth == 0:
            return obj.name
        return format_html(
            '<span style="display:inline-block;padding-left:{}px"></span>{}',
            depth * 20, obj.name
        )

    @admin.display(description='Discount')
    def show_discount(self, obj):
        if obj.discount_percent:
            return f'{obj.discount_percent}%'
        return '—'


@admin.register(Brand)
class BrandAdmin(RevertableAdminMixin, admin.ModelAdmin):
    list_display = ['logo_preview', 'name', 'discount_percent', 'is_active']
    list_editable = ['is_active']
    list_filter = [IsActiveFilter]
    search_fields = ['name']
    exclude = ['slug', 'logo']
    save_on_top = True
    change_list_template = 'admin/catalog/brand/change_list.html'
    change_form_template = 'admin/catalog/brand/change_form.html'
    fieldsets = [
        (None, {'fields': ['name', 'discount_percent', 'is_active']}),
    ]

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        form.base_fields['discount_percent'].widget.attrs.update({
            'id': 'id_discount_percent', 'min': '0', 'max': '100', 'size': '3',
        })
        return form

    def logo_preview(self, obj):
        first = obj.images.first()
        url = first.image.url if first else (obj.logo.url if obj.logo else None)
        if url:
            return format_html('<img src="{}" style="height:32px;border-radius:3px;">', url)
        return '—'
    logo_preview.short_description = 'Logo'

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        temp_key = request.POST.get('_brand_photo_temp_key', '').strip()
        if temp_key:
            from .models import BrandImage
            BrandImage.objects.filter(temp_key=temp_key).update(brand=obj, temp_key='')

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        brands = Brand.objects.all().order_by('name').prefetch_related('images')
        brands_flat = []
        for b in brands:
            first_img = b.images.first()
            logo_url = first_img.image.url if first_img else (b.logo.url if b.logo else '')
            brands_flat.append({
                'id': b.pk,
                'name': b.name,
                'logo': logo_url,
                'discount_percent': str(b.discount_percent) if b.discount_percent else '',
                'is_active': b.is_active,
                'created_at': timezone.localtime(b.created_at).strftime('%d.%m.%Y %H:%M') if b.created_at else '',
                'created_iso': timezone.localtime(b.created_at).strftime('%Y-%m-%d') if b.created_at else '',
            })
        extra_context['brands_json'] = js_json(brands_flat, ensure_ascii=False)
        return super().changelist_view(request, extra_context)


class GroupedAttributeValueWidget(forms.Widget):
    def render(self, name, value, attrs=None, renderer=None):
        if value is None:
            value = []
        ids = [str(v) for v in value if v]
        selected_ids = ','.join(ids)
        # Server-side fallback inputs: if for some reason JS does not initialize the widget,
        # these values still end up in the POST and the M2M is NOT wiped. initAttrWidget
        # removes them on initialization and manages its own checkboxes.
        fallback = ''.join(
            f'<input type="hidden" class="attr-initial-input" name="{name}" value="{vid}">'
            for vid in ids
        )
        return mark_safe(
            f'<div class="attr-widget-root" data-name="{name}" data-selected="{selected_ids}">{fallback}</div>'
        )

    def value_from_datadict(self, data, files, name):
        return data.getlist(name)


class GroupedAttributeValueField(forms.ModelMultipleChoiceField):
    widget = GroupedAttributeValueWidget

    def __init__(self, **kwargs):
        kwargs.setdefault('queryset', AttributeValue.objects.select_related('attribute'))
        kwargs.setdefault('required', False)
        kwargs.setdefault('label', 'Attributes')
        super().__init__(**kwargs)


class AttributeValueInlineForm(forms.ModelForm):
    class Meta:
        model = AttributeValue
        fields = ['value', 'color_hex']
        widgets = {
            # The real UI is the custom .av-color swatches in the template (change_form.html);
            # this formset row is always hidden (.inline-group{display:none}).
            # type=color broke saving here: a non-empty browser validator
            # substitutes #000000 even when the field was untouched, so an untouched
            # "extra" (extra=1) row looked "changed" to the formset — Django
            # validated it in full and required the mandatory value → the whole formset
            # failed, the new value was not saved.
            'color_hex': forms.HiddenInput(attrs={'class': 'av-color-picker'}),
        }


class AttributeValueInline(admin.TabularInline):
    model = AttributeValue
    form = AttributeValueInlineForm
    extra = 1
    fields = ['value', 'color_hex']


@admin.register(Attribute)
class AttributeAdmin(admin.ModelAdmin):
    change_form_template = 'admin/catalog/attribute/change_form.html'
    change_list_template = 'admin/catalog/attribute/change_list.html'
    save_on_top = True
    list_display = ['name', 'display_values']
    list_filter = ['is_color']
    search_fields = ['name']
    inlines = [AttributeValueInline]

    def changelist_view(self, request, extra_context=None):
        """JSON with all attributes and their values for the custom popup."""
        import json
        attrs = []
        for a in Attribute.objects.prefetch_related('values').all():
            attrs.append({
                'id': a.id, 'name': a.name, 'is_color': a.is_color,
                'values': [{'value': v.value, 'color_hex': v.color_hex} for v in a.values.all()],
            })
        ctx = extra_context or {}
        ctx['attributes_json'] = js_json(attrs, ensure_ascii=False)
        return super().changelist_view(request, extra_context=ctx)
    # `is_color` is a system flag. In the DB it is enforced via a partial unique
    # constraint that only one attribute may have is_color=True ("Color").
    # So we hide it from the form — the user must not change it by hand.
    exclude = ['is_color']

    def log_addition(self, request, obj, message): pass
    def log_change(self, request, obj, message): pass
    def log_deletion(self, request, obj, object_repr): pass
    def history_view(self, request, object_id, extra_context=None):
        from django.http import HttpResponseRedirect
        from django.urls import reverse
        return HttpResponseRedirect(reverse('admin:catalog_attribute_changelist'))

    @admin.display(description='Values')
    def display_values(self, obj):
        from django.utils.html import mark_safe
        values = list(obj.values.values_list('value', flat=True))
        if not values:
            return '—'
        tags = ''.join('<span class="av-tag">{}</span>'.format(v) for v in values)
        return mark_safe('<div class="av-wrap">{}</div>'.format(tags))


class ProductImageInline(admin.TabularInline):
    model = ProductImage
    extra = 1
    fields = ['image', 'is_main', 'sort_order']
    verbose_name = 'Photo'
    verbose_name_plural = 'Photos'


def _thumb_url(printout_pk):
    """Preview URL versioned by the file's time.

    nginx serves /media/ with `Cache-Control: immutable` for 30 days on the assumption
    that names there are unique by content. The preview, however, lives under a fixed name and
    is overwritten, so without a version the browser would show the image of the PREVIOUS
    sheet. The version is taken from the preview's own mtime: it changes exactly when
    the image was redrawn.
    """
    rel = thumb_rel_path(printout_pk)
    url = f'{settings.MEDIA_URL}{rel}'
    try:
        stamp = int(os.path.getmtime(os.path.join(settings.MEDIA_ROOT, rel)))
    except OSError:
        return url                      # no preview yet — the frontend will draw the card
    return f'{url}?v={stamp}'


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    """Several files in one input. Django has no such field out of the box."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault('widget', MultipleFileInput())
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        one = super().clean
        if isinstance(data, (list, tuple)):
            return [one(d, initial) for d in data]
        return [one(data, initial)] if data else []


class ProductVariantInlineForm(forms.ModelForm):
    # A packaging can have several printouts, so they live in a separate model rather than a
    # field. Django admin has no nested inlines, so we add two service
    # fields: new files go into one, ids of those marked for deletion into the other.
    # Processed by ProductAdmin.save_formset, so everything is applied on "Save".
    printout_new = MultipleFileField(required=False, label='Printouts')
    printout_delete = forms.CharField(required=False, widget=forms.HiddenInput)

    class Meta:
        model = ProductVariant
        fields = '__all__'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        product = None
        instance = kwargs.get('instance') or self.instance
        if instance and instance.pk and instance.product_id:
            product = instance.product
        if product is None:
            initial = self.initial or {}
            data = self.data or {}
            pid = initial.get('product') or data.get(self.add_prefix('product') if self.prefix else 'product')
            if pid:
                from .models import Product as _Product
                product = _Product.objects.filter(pk=pid).first()
        if product is not None:
            self.fields['bulk_source'].queryset = BulkSource.objects.filter(product=product)


class ProductVariantInline(admin.StackedInline):
    model = ProductVariant
    form = ProductVariantInlineForm
    extra = 0
    min_num = 1
    show_change_link = True
    fields = ['name', 'price', 'discount_percent', 'discount_amount', 'stock', 'stock_manual', 'low_stock_threshold_override', 'sku', 'is_active', 'is_hybrid', 'supplier_url', 'supplier_price', 'attribute_values', 'description', 'raw_text', 'bulk_source', 'unit_size', 'printout_new', 'printout_delete', 'paired_variant']
    verbose_name = 'Variant'
    verbose_name_plural = ''

    def get_formset(self, request, obj=None, **kwargs):
        formset = super().get_formset(request, obj, **kwargs)
        formset.form.base_fields['name'].required = True
        formset.form.base_fields['sku'].required = True
        formset.form.base_fields['attribute_values'] = GroupedAttributeValueField()
        formset.form.base_fields['description'].widget = QuillWidget(label='Description', extract_block='description')
        formset.form.base_fields['description'].required = False
        formset.form.base_fields['raw_text'].widget = QuillWidget(label='Raw variant text', extract_block='product_fill')
        formset.form.base_fields['raw_text'].required = False
        return formset


class ProductAdminForm(forms.ModelForm):
    class Meta:
        model = Product
        fields = '__all__'
        widgets = {
            'packaging_kind': forms.HiddenInput,
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['name'].label = 'General name'
        self.fields['categories'] = HierarchicalCategoryMultipleChoiceField(
            queryset=_tree_ordered_category_qs(),
            widget=CheckboxSelectMultiple,
            required=True,
            label='Categories',
        )


@admin.register(Product)
class ProductAdmin(RevertableAdminMixin, NoEmptyHistoryMixin, admin.ModelAdmin):
    form = ProductAdminForm
    save_on_top = True
    list_display = ['name', 'display_labels', 'is_active', 'created_at']
    list_filter = ['labels']
    exclude = ['slug']
    inlines = [ProductVariantInline]
    change_form_template = 'admin/catalog/product/change_form.html'
    change_list_template = 'admin/catalog/product/change_list.html'
    actions = []
    # Cache of the full packaging snapshot for the changelist (changelist_view below);
    # invalidated by a signal — catalog/signals.py.
    VARIANTS_CL_CACHE_KEY = 'admin:catalog:variants_cl'
    VARIANTS_CL_TTL = 300
    fieldsets = [
        (None, {'fields': ['name', 'brand', 'categories', 'is_active', 'packaging_kind']}),
        ('Description', {'fields': ['description']}),
        ('Application norms', {'fields': ['application_norms']}),
    ]

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related('labels')

    def display_labels(self, obj):
        labels = list(obj.labels.all())
        if not labels:
            return '—'
        return format_html_join(
            '',
            '<span class="app-label" style="--label-color:{}">{}</span>',
            ((l.color, l.name) for l in labels),
        )
    display_labels.short_description = 'Labels'

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        form.base_fields['description'].widget = QuillWidget(label='General description', extract_block='description')
        form.base_fields['application_norms'].widget = QuillWidget(label='Application norms', extract_block='application_norms')
        form.base_fields['application_norms'].label = ''
        form.base_fields['is_active'].label = 'General visibility'
        form.base_fields['is_active'].help_text = ''
        return form

    def render_change_form(self, request, context, add=False, change=False, form_url='', obj=None):
        # Brand discounts — for the live hint "inherited from the brand" in
        # the pack row
        # when it has no discount of its own (see initDiscountUI in JS).
        # Brand.discount_percent is an integer (not a Decimal), so WITHOUT .rstrip('0').
        brand_discounts = {
            b.pk: str(b.discount_percent)
            for b in Brand.objects.exclude(discount_percent__isnull=True).exclude(discount_percent=0)
        }
        context['brand_discounts_json'] = js_json(brand_discounts)
        return super().render_change_form(request, context, add, change, form_url, obj)

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        product = form.instance
        # "General visibility" = bulk toggle: apply to all packagings
        # ONLY if the admin actually touched this checkbox (otherwise the individual
        # checkmarks of the packagings are not overwritten when the form is saved).
        if 'is_active' in form.changed_data:
            product.variants.update(is_active=form.cleaned_data.get('is_active'))
        # Derived product flag — always from the packagings (bulk .update() does not send a signal).
        from .models import sync_product_is_active
        sync_product_is_active(product.pk)

    def changelist_view(self, request, extra_context=None):
        import json
        from django.conf import settings
        from django.db.models import Count
        from django.http import HttpResponseRedirect

        if request.method == 'POST' and request.POST.get('delete_variants'):
            ids = request.POST.getlist('variant_ids')
            if ids:
                from django.contrib.contenttypes.models import ContentType
                from django.contrib import messages
                ct = ContentType.objects.get_for_model(ProductVariant)
                qs = ProductVariant.objects.filter(pk__in=ids)
                for v in qs:
                    snap = capture_full_snapshot(v)
                    LogEntry.objects.create(
                        user_id=request.user.pk,
                        content_type_id=ct.pk,
                        object_id=str(v.pk),
                        object_repr=str(v)[:200],
                        action_flag=DELETION,
                        change_message=json.dumps([{'deletion_snapshot': snap}]),
                    )
                # SET_NULL on OrderItem.product_variant — order items
                # will not block deletion; the name/SKU is kept in the snapshot.
                count = qs.delete()[0]
                messages.success(request, f'Deleted {count} packagings.')
            return HttpResponseRedirect(request.get_full_path())

        extra_context = extra_context or {}

        sort_fields = {
            'name': 'name', 'sku': 'sku', 'price': 'price',
            'stock': 'stock', 'discount': 'discount_percent',
            'created_at': 'created_at', 'updated_at': 'updated_at',
            'is_active': 'is_active', 'brand': 'product__brand__name',
        }
        sort = request.GET.get('sort', 'created_at')
        direction = request.GET.get('dir', 'desc')
        order_field = sort_fields.get(sort, 'created_at')
        if direction == 'desc':
            order_field = '-' + order_field
        # order_field remains only for extra_context['current_sort']/['current_dir']
        # (highlighting the active sort) — the #result_list table is hidden (display:none),
        # the visible list is rendered and sorted by JS from variants_json, so the server-side
        # order_by below does not affect the view and is safely cached as a single snapshot.

        # The full snapshot of all packagings (card/price/photo/labels) weighs ~700KB of JSON and
        # was rebuilt (several SQL queries + serialization of ~1000 objects) on EVERY
        # visit to the page — the main reason Products was slow. We cache it: a signal
        # on Product/ProductVariant save/delete (catalog/signals.py) clears the cache
        # right after a normal edit; the TTL is a fallback for
        # .update() paths that bypass the signal (bulk import, etc.).
        from django.core.cache import cache
        variants_flat = cache.get(self.VARIANTS_CL_CACHE_KEY)
        if variants_flat is None:
            multi_pids = set(
                ProductVariant.objects.values('product_id')
                .annotate(cnt=Count('id')).filter(cnt__gt=1)
                .values_list('product_id', flat=True)
            )

            # Classification of dropshipping products — by the REAL is_dropshipping flag, NOT by the
            # "Made to order" label. A hybrid (50/50) is own stock that, at stock==0,
            # temporarily gets the auto-label "Made to order"; classifying such a variant as
            # dropshipping and hiding the product from Products is wrong — that made 50/50 products
            # vanish from the list.
            # is_hybrid and is_dropshipping are different fields (a hybrid is never
            # is_dropshipping).
            _drop_vids = set(
                ProductVariant.objects.filter(is_dropshipping=True)
                .values_list('id', flat=True)
            )
            # products that have at least one pure-drop variant
            _has_drop_pids = set(
                ProductVariant.objects.filter(id__in=_drop_vids)
                .values_list('product_id', flat=True).distinct()
            )
            # products that have at least one non-drop variant (including hybrids)
            _has_non_drop_pids = set(
                ProductVariant.objects.exclude(id__in=_drop_vids)
                .values_list('product_id', flat=True).distinct()
            )
            pure_drop_pids = _has_drop_pids - _has_non_drop_pids   # all variants are pure dropshipping → hide from Products
            mixed_pids = _has_drop_pids & _has_non_drop_pids        # part dropshipping, part not → show in both

            variants_flat = []
            # Printouts are a separate table; the list only needs yes/no, so
            # we count with a single Count instead of pulling the files.
            # Fixed order_by('id') — independent of sort/dir (see the comment above),
            # so there is one shared cache rather than one per sort variation.
            qs = ProductVariant.objects.select_related(
                'product', 'product__brand', 'product__paired_product', 'bulk_source'
            ).prefetch_related('product__categories', 'variant_labels__label').annotate(
                _printout_count=Count('printouts')
            ).only(
                'id', 'product_id', 'name', 'sku', 'price', 'discount_percent', 'discount_amount',
                'stock', 'stock_manual', 'is_active', 'is_hybrid', 'unit_size',
                'created_at', 'updated_at',
                'bulk_source_id', 'bulk_source__quantity', 'bulk_source__unit', 'bulk_source__waste_percent',
                'product__id', 'product__name', 'product__brand_id', 'product__paired_product_id',
                'product__packaging_kind',
                'product__brand__name', 'product__brand__discount_percent',
            ).order_by('id')

            from .models import BulkSource as _BulkSource
            product_default_bag = {}
            for src in _BulkSource.objects.all().order_by('pk'):
                product_default_bag.setdefault(src.product_id, src)

            _now = timezone.now()
            for v in qs:
                all_cats = list(v.product.categories.all())
                cats = [c.name for c in all_cats]   # all categories; clamp:2 on the frontend will trim the excess
                cats_extra = 0
                # Labels are taken from the variant (VariantLabel), as on the site: only non-expired
                # ones,
                # in the same order (display_rank). Previously the general product.labels were used
                # —
                # because of that the variant's auto-labels (New/Hit) did not show in
                # the table.
                labels = [
                    {'name': l.name, 'color': l.color}
                    for l in sorted(
                        (vl.label for vl in v.variant_labels.all()
                         if vl.label and (vl.expires_at is None or vl.expires_at > _now)),
                        key=lambda l: (Label.display_rank(l.slug), l.name or ''),
                    )
                ]
                variants_flat.append({
                    'id': v.id,
                    'product_id': v.product_id,
                    'name': v.name or '',
                    'product_name': v.product.name if v.product_id else '',
                    'sku': v.sku or '',
                    'price': str(v.price) if v.price is not None else '',
                    'discount_percent': str(v.discount_percent).rstrip('0').rstrip('.') if v.discount_percent else '',
                    'discount_amount': str(v.discount_amount).rstrip('0').rstrip('.') if v.discount_amount else '',
                    # The brand discount applies only if the pack has none of its own
                    # (effective_discount_percent:
                    # the own % takes priority, a UAH discount does not affect this).
                    # Brand.discount_percent is an
                    # integer (not a Decimal), so WITHOUT .rstrip('0') — otherwise «10» becomes «1».
                    'brand_discount_percent': (
                        str(v.product.brand.discount_percent)
                        if not v.discount_percent and v.product.brand_id and v.product.brand.discount_percent else ''
                    ),
                    'stock': v.stock,
                    'stock_manual': v.stock_manual,
                    'is_active': v.is_active,
                    'brand': v.product.brand.name if v.product.brand_id else '',
                    'categories': cats,
                    'categories_extra': cats_extra,
                    'labels': labels,
                    'created_at': timezone.localtime(v.created_at).strftime('%d.%m.%Y %H:%M') if v.created_at else '',
                    'updated_at': timezone.localtime(v.updated_at).strftime('%d.%m.%Y %H:%M') if v.updated_at else '',
                    'created_iso': timezone.localtime(v.created_at).strftime('%Y-%m-%d') if v.created_at else '',
                    'updated_iso': timezone.localtime(v.updated_at).strftime('%Y-%m-%d') if v.updated_at else '',
                    'multi': v.product_id in multi_pids,
                    'is_mixed_product': v.product_id in mixed_pids,
                    'is_hybrid': v.is_hybrid,
                    'is_own': v.product.packaging_kind == 'own',
                    'is_paired': v.product.paired_product_id is not None,
                    'pair_key': min(v.product_id, v.product.paired_product_id) if v.product.paired_product_id else v.product_id,
                    'unit_size': float(v.unit_size) if v.unit_size else None,
                    # the list only needs the fact of presence — the product card pulls the files
                    'printout': bool(getattr(v, '_printout_count', 0)),
                    'bag_quantity': float(v.bulk_source.quantity) if v.bulk_source_id else (
                        float(product_default_bag[v.product_id].quantity)
                        if v.product_id in product_default_bag else None
                    ),
                    'bag_unit': v.bulk_source.unit if v.bulk_source_id else (
                        product_default_bag[v.product_id].unit
                        if v.product_id in product_default_bag else 'g'
                    ),
                    'bag_waste': float(v.bulk_source.waste_percent or 0) if v.bulk_source_id else (
                        float(product_default_bag[v.product_id].waste_percent or 0)
                        if v.product_id in product_default_bag else 1.0
                    ),
                    'photos': [],
                })

            # Hide pure-drop products from "Products" (they live only in "Dropshipping")
            variants_flat = [v for v in variants_flat if v['product_id'] not in pure_drop_pids]

            # Variant photos (up to 3 per variant)
            variant_ids = [v['id'] for v in variants_flat]
            variant_photos = {}
            for img in ProductImage.objects.filter(variant_id__in=variant_ids).order_by('sort_order').values('variant_id', 'image'):
                vid = img['variant_id']
                if vid not in variant_photos:
                    variant_photos[vid] = []
                if len(variant_photos[vid]) < 3:
                    variant_photos[vid].append(settings.MEDIA_URL + img['image'])

            # Fallback 1: product photos (variant=NULL) for variants without own photos
            no_photo_pids = list({v['product_id'] for v in variants_flat if not variant_photos.get(v['id'])})
            if no_photo_pids:
                product_photos = {}
                for img in ProductImage.objects.filter(product_id__in=no_photo_pids, variant_id__isnull=True).order_by('sort_order').values('product_id', 'image'):
                    pid = img['product_id']
                    if pid not in product_photos:
                        product_photos[pid] = []
                    if len(product_photos[pid]) < 3:
                        product_photos[pid].append(settings.MEDIA_URL + img['image'])
                for v in variants_flat:
                    if not variant_photos.get(v['id']) and v['product_id'] in product_photos:
                        variant_photos[v['id']] = product_photos[v['product_id']]

            # Fallback 2: any sibling variant's photos (multi-variant product where
            # only some variants have photos — show neighbours' photos instead of
            # leaving the row empty).
            still_no_photo_pids = list({v['product_id'] for v in variants_flat if not variant_photos.get(v['id'])})
            if still_no_photo_pids:
                sibling_photos = {}  # product_id → first 3 photo urls
                for img in ProductImage.objects.filter(
                    product_id__in=still_no_photo_pids, variant_id__isnull=False,
                ).order_by('variant_id', 'sort_order').values('product_id', 'image'):
                    pid = img['product_id']
                    if pid not in sibling_photos:
                        sibling_photos[pid] = []
                    if len(sibling_photos[pid]) < 3:
                        sibling_photos[pid].append(settings.MEDIA_URL + img['image'])
                for v in variants_flat:
                    if not variant_photos.get(v['id']) and v['product_id'] in sibling_photos:
                        variant_photos[v['id']] = sibling_photos[v['product_id']]

            for v in variants_flat:
                v['photos'] = variant_photos.get(v['id'], [])

            cache.set(self.VARIANTS_CL_CACHE_KEY, variants_flat, self.VARIANTS_CL_TTL)

        extra_context['variants_json'] = js_json(variants_flat, ensure_ascii=False)
        extra_context['labels_json'] = js_json(
            # "Promo" is a derived label (a signal sets it automatically based on the
            # presence of a
            # discount); filtering by it separately from "Discount" would be duplication.
            [{'name': l.name, 'color': l.color}
             for l in Label.objects.exclude(slug=Label.SLUG_AKTSIA).order_by('name')],
            ensure_ascii=False
        )
        extra_context['cats_tree_json'] = js_json(
            [{'id': c.pk, 'name': c.name, 'parent_id': c.parent_id or 0}
             for c in _tree_ordered_category_qs()],
            ensure_ascii=False
        )
        extra_context['current_sort'] = sort
        extra_context['current_dir'] = direction
        return super().changelist_view(request, extra_context)

    def get_urls(self):
        from django.urls import path
        urls = super().get_urls()
        return [
            path('color-value/', self.admin_site.admin_view(self.color_value_view)),
        ] + urls

    def color_value_view(self, request):
        """Create a color attribute (ensure_only) or an AttributeValue with hex+name."""
        from django.http import JsonResponse
        import json, re
        if request.method != 'POST':
            return JsonResponse({'error': 'POST only'}, status=405)
        try:
            payload = json.loads(request.body or '{}')
        except ValueError:
            return JsonResponse({'error': 'bad json'}, status=400)
        color_attr = Attribute.objects.filter(is_color=True).order_by('pk').first()
        if not color_attr:
            color_attr, _ = Attribute.objects.get_or_create(
                # "Color": the canonical color attribute name stored in the catalog DB;
                # the import commands look the attribute up by this exact name.
                name='Color',
                defaults={'is_color': True},
            )
            if not color_attr.is_color:
                color_attr.is_color = True
                color_attr.save(update_fields=['is_color'])
        if payload.get('ensure_only'):
            return JsonResponse({
                'attr_id': color_attr.pk,
                'attr_name': color_attr.name,
                'is_color': True,
            })
        hex_val = (payload.get('hex') or '').strip().lower()
        if not re.match(r'^#[0-9a-f]{6}$', hex_val):
            return JsonResponse({'error': 'bad hex'}, status=400)
        name = (payload.get('name') or '').strip() or hex_val
        av, _ = AttributeValue.objects.get_or_create(
            attribute=color_attr, value=name,
            defaults={'color_hex': hex_val},
        )
        if not av.color_hex:
            av.color_hex = hex_val
            av.save(update_fields=['color_hex'])
        return JsonResponse({
            'attr_id': color_attr.pk,
            'attr_name': color_attr.name,
            'is_color': True,
            'id': av.pk,
            'val': av.value,
            'hex': av.color_hex,
        })

    def _attrs_context(self, extra_context=None):
        import json
        extra_context = extra_context or {}
        data = []
        for attr in Attribute.objects.prefetch_related('values').order_by('-is_color', 'name'):
            values = [
                {'id': av.pk, 'val': av.value, 'hex': av.color_hex or ''}
                for av in attr.values.all().order_by('value')
            ]
            # Always show the color attribute (even without values) — so the palette stays
            # accessible
            if values or attr.is_color:
                data.append({
                    'id': attr.pk,
                    'name': attr.name,
                    'is_color': attr.is_color,
                    'values': values,
                })
        extra_context['attrs_json'] = js_json(data, ensure_ascii=False)
        return extra_context

    def add_view(self, request, form_url='', extra_context=None):
        return super().add_view(request, form_url, self._attrs_context(extra_context))

    def change_view(self, request, object_id, form_url='', extra_context=None):
        ctx = self._attrs_context(extra_context) or {}
        # Existing sheets go into the template, because our JS renders them, not a Django widget.
        try:
            rows = {}
            for pr in VariantPrintout.objects.filter(
                    variant__product_id=int(object_id)).select_related(None):
                rows.setdefault(str(pr.variant_id), []).append({
                    'id': pr.pk,
                    'url': pr.file.url if pr.file else '',
                    'name': pr.basename,
                    'thumb': _thumb_url(pr.pk),
                })
            ctx['printouts_json'] = js_json(rows, ensure_ascii=False)
        except (ValueError, TypeError):
            ctx['printouts_json'] = js_json({}, ensure_ascii=False)
        try:
            obj = Product.objects.get(pk=int(object_id))
            if obj.paired_product_id:
                paired = obj.paired_product
                ctx['paired_variants'] = [
                    {'id': v.pk, 'name': v.name or paired.name, 'kind': paired.packaging_kind}
                    for v in paired.variants.all()
                ]
                ctx['paired_product'] = {'id': paired.pk, 'kind': paired.packaging_kind}
        except (Product.DoesNotExist, ValueError, TypeError):
            pass
        return super().change_view(request, object_id, form_url, ctx)




    def save_model(self, request, obj, form, change):
        # Raw texts are not form fields (rendered custom), read from POST.
        obj.raw_text = request.POST.get('raw_text', '') or ''
        obj.raw_characteristics = request.POST.get('raw_characteristics', '') or ''
        obj.raw_composition = request.POST.get('raw_composition', '') or ''
        if change and obj.pk:
            try:
                old = Product.objects.select_related('brand').get(pk=obj.pk)
                request._prd_old_brand = old.brand
            except Product.DoesNotExist:
                pass
            # Save raw texts explicitly so they are not lost if the rest of the form
            # has not changed (super().save_model may skip save via the mixin).
            Product.objects.filter(pk=obj.pk).update(
                raw_text=obj.raw_text,
                raw_characteristics=obj.raw_characteristics,
                raw_composition=obj.raw_composition)
        super().save_model(request, obj, form, change)
        if not change:
            temp_key = request.POST.get('_photo_temp_key', '').strip()
            if temp_key:
                ProductImage.objects.filter(temp_key=temp_key).update(product=obj, temp_key=None)
            # Characteristics are created later in save_related, because the already
            # saved variants are needed to bind by position 'variant:N'.

    def save_related(self, request, form, formsets, change):
        obj = form.instance
        if change and obj.pk:
            request._prd_old_cats   = set(obj.categories.values_list('pk', flat=True))
            request._prd_old_labels = set(obj.labels.values_list('pk', flat=True))
        super().save_related(request, form, formsets, change)
        if not change:
            # Create general + variant characteristics from the _char-N-* fields.
            char_re = re.compile(r'^_char-(\d+)-name$')
            indices = sorted(
                int(m.group(1))
                for key in request.POST
                for m in [char_re.match(key)]
                if m
            )
            if indices:
                variants_ordered = list(obj.variants.order_by('pk'))
                for idx in indices:
                    name = request.POST.get(f'_char-{idx}-name', '').strip()
                    value = request.POST.get(f'_char-{idx}-value', '').strip()
                    if not (name or value):
                        continue
                    color_hex = request.POST.get(f'_char-{idx}-color', '').strip()
                    target = request.POST.get(f'_char-{idx}-target', 'common').strip()
                    variant = None
                    if target.startswith('variant:'):
                        try:
                            pos = int(target.split(':', 1)[1])
                            if 0 <= pos < len(variants_ordered):
                                variant = variants_ordered[pos]
                        except (ValueError, IndexError):
                            variant = None
                    ProductCharacteristic.objects.create(
                        product=obj, variant=variant,
                        name=name, value=value, color_hex=color_hex, sort_order=idx,
                    )
            return
        details = []
        # Brand
        if hasattr(request, '_prd_old_brand'):
            old_brand = request._prd_old_brand
            old_id = old_brand.pk if old_brand else None
            if old_id != obj.brand_id:
                old_n = old_brand.name if old_brand else '—'
                new_n = obj.brand.name if obj.brand_id else '—'
                details.append(f'Brand: {old_n} → {new_n}')
            del request._prd_old_brand
        # Categories
        if hasattr(request, '_prd_old_cats'):
            new_cats = set(obj.categories.values_list('pk', flat=True))
            added   = new_cats - request._prd_old_cats
            removed = request._prd_old_cats - new_cats
            if added:
                names = list(Category.objects.filter(pk__in=added).values_list('name', flat=True))
                details.append(f'Categories added: {", ".join(names)}')
            if removed:
                names = list(Category.objects.filter(pk__in=removed).values_list('name', flat=True))
                details.append(f'Categories removed: {", ".join(names)}')
            del request._prd_old_cats
        # Labels
        if hasattr(request, '_prd_old_labels'):
            new_labels = set(obj.labels.values_list('pk', flat=True))
            added   = new_labels - request._prd_old_labels
            removed = request._prd_old_labels - new_labels
            if added:
                names = list(Label.objects.filter(pk__in=added).values_list('name', flat=True))
                details.append(f'Labels added: {", ".join(names)}')
            if removed:
                names = list(Label.objects.filter(pk__in=removed).values_list('name', flat=True))
                details.append(f'Labels removed: {", ".join(names)}')
            del request._prd_old_labels
        if details:
            request._prd_extra_details = details

    def construct_change_message(self, request, form, formsets, add=False):
        msgs = super().construct_change_message(request, form, formsets, add)
        if hasattr(request, '_prd_extra_details'):
            for detail in request._prd_extra_details:
                msgs.append({'changed': {'fields': [detail]}})
            del request._prd_extra_details
        return msgs

    def _save_printouts(self, request, formset):
        """Label sheets: add the uploaded ones, remove those marked.

        Django admin has no nested inlines, so the packaging form carries two
        service fields — `printout_new` (several files) and `printout_delete`
        (comma-separated ids). This way everything is applied exactly on "Save", with no
        separate AJAX endpoint, and cancelling the form leaves nothing on disk.
        """
        for f in formset.forms:
            variant = f.instance
            if not variant.pk or f in formset.deleted_forms:
                continue

            raw = (f.cleaned_data or {}).get('printout_delete') or ''
            ids = {int(x) for x in raw.replace(' ', '').split(',') if x.isdigit()}
            if ids:
                # only its own: the id comes from the browser, someone else's row must not be
                # deletable
                for pr in VariantPrintout.objects.filter(pk__in=ids, variant=variant):
                    pr.delete()          # django_cleanup will remove the file and the preview

            files = (f.cleaned_data or {}).get('printout_new') or []
            if not files:
                continue
            start = (VariantPrintout.objects.filter(variant=variant)
                     .aggregate(m=Max('sort_order'))['m'] or 0)
            for i, uploaded in enumerate(files, start=1):
                VariantPrintout.objects.create(
                    variant=variant, file=uploaded, sort_order=start + i)

    def save_formset(self, request, form, formset, change):
        # Remember the old attribute_values for existing variants
        old_attr_vals = {}
        for f in formset.forms:
            if f.instance.pk and 'attribute_values' in (f.changed_data or []):
                old_attr_vals[f.instance.pk] = set(
                    f.instance.attribute_values.values_list('pk', flat=True)
                )

        # Remember the forms of new variants (without pk) before saving
        new_forms = [
            f for f in formset.forms
            if not f.instance.pk
            and f not in formset.deleted_forms
            and f.has_changed()
        ]
        formset.save()

        # Printouts: new files and those marked for deletion. Done AFTER
        # formset.save(), because a new packaging has no pk until then.
        self._save_printouts(request, formset)

        # Log attribute_values changes on existing variants
        for f in formset.forms:
            pk = f.instance.pk
            if pk and pk in old_attr_vals:
                new_av  = set(f.instance.attribute_values.values_list('pk', flat=True))
                added   = new_av - old_attr_vals[pk]
                removed = old_attr_vals[pk] - new_av
                log_parts = []
                if added:
                    avs = AttributeValue.objects.select_related('attribute').filter(pk__in=added)
                    log_parts.append('Attribute added: ' + ', '.join(
                        f'{av.attribute.name} — {av.value}' for av in avs))
                if removed:
                    avs = AttributeValue.objects.select_related('attribute').filter(pk__in=removed)
                    log_parts.append('Attribute removed: ' + ', '.join(
                        f'{av.attribute.name} — {av.value}' for av in avs))
                if log_parts:
                    self.log_change(request, f.instance, '; '.join(log_parts))

        # After saving, every form has a pk — bind the temp data
        for f in new_forms:
            if not f.instance.pk:
                continue
            prefix = f.prefix
            variant = f.instance

            # Bind temp photos
            temp_key = request.POST.get(prefix + '-_photo_temp_key', '').strip()
            if temp_key:
                ProductImage.objects.filter(temp_key=temp_key).update(
                    variant=variant, temp_key=None
                )

            # Create characteristics from the form fields
            char_re = re.compile(r'^' + re.escape(prefix) + r'-_char-(\d+)-name$')
            indices = sorted(
                int(m.group(1))
                for key in request.POST
                for m in [char_re.match(key)]
                if m
            )
            for idx in indices:
                name = request.POST.get(f'{prefix}-_char-{idx}-name', '').strip()
                value = request.POST.get(f'{prefix}-_char-{idx}-value', '').strip()
                color_hex = request.POST.get(f'{prefix}-_char-{idx}-color', '').strip()
                if name or value:
                    ProductCharacteristic.objects.create(
                        variant=variant, name=name, value=value, color_hex=color_hex, sort_order=idx
                    )


@admin.register(ProductVariant)
class ProductVariantAdmin(admin.ModelAdmin):
    """Registered only for redirects. There is no variant page — all editing happens in the product form."""

    def get_model_perms(self, request):
        return {}

    def change_view(self, request, object_id, form_url='', extra_context=None):
        from django.shortcuts import redirect
        try:
            v = ProductVariant.objects.select_related('product').get(pk=object_id)
            return redirect(f'/admin/catalog/product/{v.product.pk}/change/#v{v.pk}')
        except ProductVariant.DoesNotExist:
            return redirect('/admin/catalog/product/')

    def changelist_view(self, request, extra_context=None):
        from django.shortcuts import redirect
        return redirect('/admin/catalog/product/')

    def add_view(self, request, form_url='', extra_context=None):
        from django.shortcuts import redirect
        return redirect('/admin/catalog/product/add/')


class VariantPickerWidget(forms.Widget):
    def render(self, name, value, attrs=None, renderer=None):
        widget_id = (attrs or {}).get('id', f'id_{name}')
        if value:
            try:
                v = ProductVariant.objects.select_related('product').prefetch_related('images', 'product__images').get(pk=value)
                photo = (
                    v.images.order_by('sort_order', 'pk').first() or
                    v.product.images.filter(variant__isnull=True).order_by('sort_order', 'pk').first() or
                    v.product.images.order_by('sort_order', 'pk').first()
                )
                photo_url = photo.image.url if photo else ''
                display_name = v.name or v.product.name
                meta = f'{v.sku} · {v.price} UAH'
            except ProductVariant.DoesNotExist:
                value = ''; photo_url = ''; display_name = ''; meta = ''
        else:
            photo_url = ''; display_name = ''; meta = ''

        show_selected = 'flex' if value else 'none'
        show_search = 'none' if value else 'block'
        return mark_safe(f'''
<div class="vpick" id="{widget_id}_wrap">
  <div class="vpick-selected" style="display:{show_selected}">
    <img class="vpick-photo" src="{photo_url}">
    <div class="vpick-info">
      <span class="vpick-name">{display_name}</span>
      <span class="vpick-meta">{meta}</span>
    </div>
    <button type="button" class="vpick-clear" title="Clear">×</button>
  </div>
  <div class="vpick-search-wrap" style="display:{show_search}">
    <input type="text" class="vpick-search" placeholder="Search by name, SKU or ID…" autocomplete="off">
    <div class="vpick-dropdown"></div>
  </div>
  <input type="hidden" name="{name}" id="{widget_id}" value="{value or ''}">
</div>''')

    def value_from_datadict(self, data, files, name):
        return data.get(name)


class BundleAdminForm(forms.ModelForm):
    class Meta:
        model = Bundle
        fields = '__all__'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['categories'] = HierarchicalCategoryMultipleChoiceField(
            queryset=_tree_ordered_category_qs(),
            widget=CheckboxSelectMultiple,
            required=False,
            label='Categories',
        )
        # description/raw_text/application_norms/instruction_html are rendered by a custom
        # editor in the template (buildVariantEditor), so we do NOT attach QuillWidget here —
        # otherwise its submit handler wipes the textarea (as it did on BundleVariantInline).
        self.fields['raw_text'].required = False
        self.fields['application_norms'].required = False
        self.fields['instruction_html'].required = False
        self.fields['sku'].required = False
        self.fields['price'].required = False
        self.fields['stock'].required = False
        self.fields['discount_percent'].required = False
        self.fields['discount_amount'].required = False


@admin.register(Bundle)
class BundleAdmin(RevertableAdminMixin, NoEmptyHistoryMixin, admin.ModelAdmin):
    form = BundleAdminForm
    list_display = ['name', 'sku', 'display_price', 'display_stock', 'is_active']
    # WITHOUT list_editable: visibility is saved instantly via fetch save-bundle-field
    # (as with products). list_editable rendered a hidden formset, and the "Save" button
    # overwrote is_active back to the page state at load time.
    exclude = ['slug']
    readonly_fields = []
    save_on_top = True
    change_form_template = 'admin/catalog/bundle/change_form.html'
    change_list_template = 'admin/catalog/bundle/change_list.html'
    fieldsets = [
        (None, {'fields': [
            'name', 'is_active', 'sku', 'price', 'stock',
            'discount_percent', 'discount_amount',
            'description', 'application_norms', 'instruction_html',
            'sort_order', 'categories', 'raw_text',
        ]}),
    ]

    def changelist_view(self, request, extra_context=None):
        import json

        # Deleting the selected bundles — a list row = a bundle (variant_id
        # historically, from the time of the separate BundleVariant model; we kept the POST
        # parameter name).
        if request.method == 'POST' and request.POST.get('delete_variants'):
            ids = request.POST.getlist('variant_ids')
            if ids:
                from django.contrib.contenttypes.models import ContentType
                from django.contrib import messages
                ct = ContentType.objects.get_for_model(Bundle)
                qs = Bundle.objects.filter(pk__in=ids)
                for v in qs:
                    snap = capture_full_snapshot(v)
                    LogEntry.objects.create(
                        user_id=request.user.pk,
                        content_type_id=ct.pk,
                        object_id=str(v.pk),
                        object_repr=str(v)[:200],
                        action_flag=DELETION,
                        change_message=json.dumps([{'deletion_snapshot': snap}]),
                    )
                count = qs.delete()[0]
                messages.success(request, f'Deleted {count} bundles.')
            from django.http import HttpResponseRedirect
            return HttpResponseRedirect(request.get_full_path())

        extra_context = extra_context or {}

        sort_fields = {
            'name': 'name', 'sku': 'sku',
            'created_at': 'created_at', 'is_active': 'is_active',
        }
        sort = request.GET.get('sort', 'name')
        direction = request.GET.get('dir', 'asc')
        order_field = sort_fields.get(sort, 'name')
        if direction == 'desc':
            order_field = '-' + order_field

        extra_context['cats_tree_json'] = js_json(
            [{'id': c.pk, 'name': c.name, 'parent_id': c.parent_id or 0}
             for c in _tree_ordered_category_qs()],
            ensure_ascii=False
        )
        extra_context['labels_json'] = js_json(
            [{'name': l.name, 'color': l.color or ''} for l in Label.objects.all().order_by('name')],
            ensure_ascii=False
        )
        extra_context['current_sort'] = sort
        extra_context['current_dir'] = direction
        return super().changelist_view(request, extra_context)

    def get_queryset(self, request):
        # We prefetch items + the needed relations so that display_price/display_stock
        # (get_price()/get_stock()) take the components from the cache rather than doing N+1 over
        # the list.
        return super().get_queryset(request).prefetch_related(
            'items__product_variant__product__brand',
            'items__product_variant__bulk_source',
        )

    @admin.display(description='Price')
    def display_price(self, obj):
        p = obj.price if obj.price is not None else obj.get_price()
        return f'{p} UAH' if p else '—'

    @admin.display(description='Stock')
    def display_stock(self, obj):
        return obj.stock if obj.stock is not None else obj.get_stock()

    def change_view(self, request, object_id, form_url='', extra_context=None):
        extra_context = extra_context or {}
        if object_id:
            def _photo_for(v):
                # Photo selection from the prefetch cache (no N+1): the pack's photo →
                # the product's general photo (variant=NULL) → any photo of the product.
                _vimgs = sorted(v.images.all(), key=lambda i: (i.sort_order, i.pk))
                _pimgs = sorted(v.product.images.all(), key=lambda i: (i.sort_order, i.pk)) if v.product_id else []
                return (
                    (_vimgs[0] if _vimgs else None)
                    or next((i for i in _pimgs if i.variant_id is None), None)
                    or (_pimgs[0] if _pimgs else None)
                )

            def _ser_variant(v):
                photo = _photo_for(v)
                return {
                    'variant_id': v.pk,
                    'sku': v.sku or '',
                    'name': v.name or v.product.name,
                    'price': str(v.price),
                    'stock': v.stock,
                    'photo': request.build_absolute_uri(photo.image.url) if photo else '',
                    'discount_percent': str(v.discount_percent).rstrip('0').rstrip('.') if v.discount_percent else '',
                    'discount_amount': str(v.discount_amount).rstrip('0').rstrip('.') if v.discount_amount else '',
                    'product_id': v.product_id,
                    'packaging_kind': v.product.packaging_kind if v.product_id else '',
                    'site_url': (settings.SITE_URL.rstrip('/') + '/product/' + v.product.slug + '?variant=' + str(v.pk)) if (v.product_id and v.product.slug) else '',
                }

            items_out = []
            bundle_items_qs = BundleItem.objects.filter(bundle_id=object_id).select_related(
                'product_variant__product',
            ).prefetch_related(
                'product_variant__images',
                'product_variant__product__images',
                'product_variant__paired_variant__product',
                'product_variant__paired_variant__images',
                'product_variant__paired_variant__product__images',
            )
            for bi in bundle_items_qs:
                v = bi.product_variant
                d = _ser_variant(v)
                d['qty'] = bi.quantity
                d['apply_discount'] = bi.apply_discount
                # Paired packaging (own ↔ factory) — for the quick-switch button.
                pv = v.paired_variant if v.paired_variant_id else None
                d['paired'] = _ser_variant(pv) if pv else None
                items_out.append(d)
            extra_context['bundle_items_json'] = js_json(items_out, ensure_ascii=False)
            from . import instruction_jobs
            extra_context['instruction_job_json'] = js_json(instruction_jobs.ui_state(object_id), ensure_ascii=False)
        return super().change_view(request, object_id, form_url, extra_context)

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        temp_key = request.POST.get('_bundle_photo_temp_key', '').strip()
        if temp_key:
            BundleImage.objects.filter(temp_key=temp_key).update(bundle=obj, temp_key=None)
        # Instruction markup from the LLM. The field is editable=False, so it is not in the form —
        # it arrives as a hidden input, like the composition below.
        map_raw = request.POST.get('_bundle_instruction_map', '')
        if map_raw:
            import json as _json
            try:
                Bundle.objects.filter(pk=obj.pk).update(instruction_map=_json.loads(map_raw))
            except (ValueError, TypeError):
                pass
        # "Run through AI" on the ADD form hit the limit → the run sits in the
        # queue without a
        # bundle; now the bundle exists, and the queue knows where to write the result.
        job_id = request.POST.get('_bundle_instruction_job', '').strip()
        if job_id:
            from . import instruction_jobs
            instruction_jobs.link(job_id, obj, request.user)

    def save_related(self, request, form, formsets, change):
        obj = form.instance
        if change and obj.pk:
            request._bnd_old_cats = set(obj.categories.values_list('pk', flat=True))
        super().save_related(request, form, formsets, change)
        self._save_bundle_items(request, obj)
        if not change:
            return
        details = list(getattr(request, '_bnd_instr', []))
        if hasattr(request, '_bnd_old_cats'):
            new_cats = set(obj.categories.values_list('pk', flat=True))
            added   = new_cats - request._bnd_old_cats
            removed = request._bnd_old_cats - new_cats
            if added:
                names = list(Category.objects.filter(pk__in=added).values_list('name', flat=True))
                details.append(f'Categories added: {", ".join(names)}')
            if removed:
                names = list(Category.objects.filter(pk__in=removed).values_list('name', flat=True))
                details.append(f'Categories removed: {", ".join(names)}')
            del request._bnd_old_cats
        if details:
            request._bnd_extra_details = details

    def _save_bundle_items(self, request, obj):
        """Recreates the bundle composition (BundleItem) from the hidden _bundle_items field."""
        import json as _json
        if not obj.pk:
            return
        raw = request.POST.get('_bundle_items', '')
        if not raw:
            return
        try:
            items_data = _json.loads(raw)
        except (ValueError, TypeError):
            return
        obj.items.all().delete()
        for it in items_data:
            try:
                BundleItem.objects.create(
                    bundle=obj,
                    product_variant_id=int(it['variant_id']),
                    quantity=max(1, int(it.get('qty', 1))),
                    apply_discount=bool(it.get('apply_discount', True)),
                )
            except Exception:
                pass
        # The bundle composition has changed — we rewrite the preparation name in the instruction
        # text
        # ONCE. Without this, printing would substitute it anew on every
        # export, while the CRM would keep the old preparation forever, and whoever
        # opens the instruction would see something untrue.
        request._bnd_instr = getattr(request, '_bnd_instr', [])
        note = _resync_instruction_after_swap(obj)
        if note:
            request._bnd_instr.append(note)

    def construct_change_message(self, request, form, formsets, add=False):
        msgs = super().construct_change_message(request, form, formsets, add)
        if hasattr(request, '_bnd_extra_details'):
            for detail in request._bnd_extra_details:
                msgs.append({'changed': {'fields': [detail]}})
            del request._bnd_extra_details
        return msgs




@admin.register(LogEntry)
class LogEntryAdmin(admin.ModelAdmin):
    list_display = ['formatted_time', 'user', 'content_type', 'object_repr', 'action_name']
    date_hierarchy = 'action_time'
    list_per_page = 50
    change_list_template = 'admin/admin/logentry/change_list.html'
    change_form_template = 'admin/admin/logentry/change_form.html'

    def changelist_view(self, request, extra_context=None):
        import json as _json
        app = request.GET.get('app')
        model = request.GET.get('model')
        if app or model:
            request._history_app = app
            request._history_model = model
            get = request.GET.copy()
            get.pop('app', None)
            get.pop('model', None)
            request.GET = get

        extra_context = extra_context or {}
        qs = self.get_queryset(request).select_related('content_type', 'user').order_by('-action_time')
        entries_flat = []
        for e in qs:
            entries_flat.append({
                'id': e.pk,
                'action_time': timezone.localtime(e.action_time).strftime('%d.%m.%Y %H:%M'),
                'user': e.user.email if e.user else '—',
                'model': e.content_type.model if e.content_type else '—',
                'model_label': str(e.content_type) if e.content_type else '—',
                'object_repr': e.object_repr,
                'object_id': str(e.object_id),
                'action_flag': e.action_flag,
            })
        extra_context['entries_json'] = js_json(entries_flat, ensure_ascii=False)
        return super().changelist_view(request, extra_context)

    def get_queryset(self, request):
        qs = super().get_queryset(request).select_related('content_type', 'user')
        app = getattr(request, '_history_app', None)
        model = getattr(request, '_history_model', None)
        if app and model:
            qs = qs.filter(content_type__app_label=app, content_type__model=model)
        return qs

    def change_view(self, request, object_id, form_url='', extra_context=None):
        """Pass the object's current state and a map verbose_name → attname,
        so the template can show a "was → now" diff for specific fields."""
        import json as _json
        from django.urls import reverse
        from django.conf import settings as _settings
        from .revert import capture_full_snapshot
        extra_context = extra_context or {}
        try:
            entry = LogEntry.objects.select_related('content_type').get(pk=object_id)
        except LogEntry.DoesNotExist:
            return super().change_view(request, object_id, form_url, extra_context)

        current_snap = None
        label_map = {}
        obj_info = {'edit_url': None, 'photo': None, 'sku': None, 'name': None}

        if entry.content_type:
            model_class = entry.content_type.model_class()
            if model_class is not None:
                obj = None
                # Current state (for the "Deleted" action the object may be missing)
                if entry.action_flag != DELETION:
                    obj = model_class.objects.filter(pk=entry.object_id).first()
                    if obj is not None:
                        current_snap = capture_full_snapshot(obj)
                        # URL for editing the object (for a clickable link)
                        try:
                            obj_info['edit_url'] = reverse(
                                f'admin:{entry.content_type.app_label}_'
                                f'{entry.content_type.model}_change',
                                args=[obj.pk],
                            )
                        except Exception:
                            obj_info['edit_url'] = None
                        # Name, SKU
                        obj_info['name'] = (
                            getattr(obj, 'name', None)
                            or getattr(obj, 'title', None)
                            or str(obj)
                        )
                        obj_info['sku'] = getattr(obj, 'sku', None) or None
                        # Photo: try .images / .photo / .image / .cover
                        photo_url = None
                        try:
                            imgs = getattr(obj, 'images', None)
                            if imgs is not None and hasattr(imgs, 'all'):
                                first_img = imgs.all().first()
                                if first_img is not None:
                                    img_field = (
                                        getattr(first_img, 'image', None)
                                        or getattr(first_img, 'photo', None)
                                    )
                                    if img_field and getattr(img_field, 'name', None):
                                        photo_url = _settings.MEDIA_URL + img_field.name
                        except Exception:
                            pass
                        if not photo_url:
                            for attr in ('photo', 'image', 'cover', 'preview_image'):
                                f = getattr(obj, attr, None)
                                if f and getattr(f, 'name', None):
                                    photo_url = _settings.MEDIA_URL + f.name
                                    break
                        # If this is a variant — try the photos of product.images
                        if not photo_url:
                            parent = getattr(obj, 'product', None)
                            if parent is not None:
                                try:
                                    p_imgs = getattr(parent, 'images', None)
                                    if p_imgs is not None and hasattr(p_imgs, 'all'):
                                        first_img = p_imgs.all().first()
                                        if first_img is not None:
                                            img_field = (
                                                getattr(first_img, 'image', None)
                                                or getattr(first_img, 'photo', None)
                                            )
                                            if img_field and getattr(img_field, 'name', None):
                                                photo_url = _settings.MEDIA_URL + img_field.name
                                except Exception:
                                    pass
                        obj_info['photo'] = photo_url
                # verbose_name → attname (for matching against the list of changed fields,
                # where Django stores verbose_name in Ukrainian)
                for f in model_class._meta.concrete_fields:
                    if getattr(f, 'verbose_name', None):
                        label_map[str(f.verbose_name).lower()] = f.attname
                    label_map[f.name.lower()] = f.attname
                    label_map[f.attname.lower()] = f.attname
                for f in model_class._meta.many_to_many:
                    if getattr(f, 'verbose_name', None):
                        label_map[str(f.verbose_name).lower()] = f.name
                    label_map[f.name.lower()] = f.name

        extra_context['le_current_snapshot_json'] = js_json(
            current_snap, ensure_ascii=False, default=str)
        extra_context['le_label_map_json'] = js_json(label_map, ensure_ascii=False)
        extra_context['le_obj_info'] = obj_info
        return super().change_view(request, object_id, form_url, extra_context)

    @admin.display(description='Time', ordering='action_time')
    def formatted_time(self, obj):
        return timezone.localtime(obj.action_time).strftime('%d.%m.%Y %H:%M')

    @admin.display(description='Action')
    def action_name(self, obj):
        return {ADDITION: 'Added', CHANGE: 'Changed', DELETION: 'Deleted'}.get(obj.action_flag, '—')

    def has_add_permission(self, request): return False
    def has_change_permission(self, request, obj=None): return False
    def log_deletions(self, request, queryset): pass  # do not log deletions of log records


# ---------------------------------------------------------------------------
# Dropshipping: packaging inline for DropshippingProductAdmin
# ---------------------------------------------------------------------------

class DropshippingVariantInlineForm(forms.ModelForm):
    class Meta:
        model = ProductVariant
        fields = ['name', 'sku', 'supplier_price', 'price_auto', 'price',
                  'discount_percent', 'discount_amount',
                  'stock_auto', 'stock', 'is_active', 'attribute_values', 'description', 'raw_text']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['description'].widget = QuillWidget(label='Description', extract_block='description')
        self.fields['description'].required = False
        self.fields['raw_text'].widget = QuillWidget(label='Raw variant text', extract_block='product_fill')
        self.fields['raw_text'].required = False
        self.fields['sku'].required = False
        self.fields['price_auto'].label = 'Auto'
        self.fields['stock_auto'].label = 'Auto'
        self.fields['supplier_price'].required = False
        self.fields['supplier_price'].widget.attrs['readonly'] = True
        self.fields['supplier_price'].widget.attrs['data-supplier-price'] = True


class DropshippingVariantInline(admin.StackedInline):
    model = ProductVariant
    form = DropshippingVariantInlineForm
    extra = 0
    min_num = 0
    show_change_link = False
    fields = ['name', 'sku', 'supplier_price', 'price_auto', 'price',
              'discount_percent', 'discount_amount',
              'stock_auto', 'stock', 'is_active', 'attribute_values', 'description', 'raw_text']

    def get_formset(self, request, obj=None, **kwargs):
        formset = super().get_formset(request, obj, **kwargs)
        formset.form.base_fields['attribute_values'] = GroupedAttributeValueField()
        return formset
    verbose_name = 'Variant'
    verbose_name_plural = ''

    def get_queryset(self, request):
        return super().get_queryset(request).filter(is_dropshipping=True)

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(DropshippingProduct)
class DropshippingProductAdmin(ProductAdmin):
    change_form_template = 'admin/catalog/dropshippingproduct/change_form.html'
    inlines = [DropshippingVariantInline]
    fieldsets = [
        (None, {'fields': ['name', 'categories', 'is_active', 'description']}),
    ]
    actions = []

    def get_form(self, request, obj=None, **kwargs):
        # Call admin.ModelAdmin.get_form directly to avoid
        # setting QuillWidget for fields that are not in fieldsets.
        form = admin.ModelAdmin.get_form(self, request, obj, **kwargs)
        if 'is_active' in form.base_fields:
            form.base_fields['is_active'].label = 'General visibility'
        if 'categories' in form.base_fields:
            form.base_fields['categories'].required = False
        if 'description' in form.base_fields:
            form.base_fields['description'].widget = QuillWidget(label='General description', extract_block='description')
            form.base_fields['description'].required = False
        return form

    def get_queryset(self, request):
        qs = admin.ModelAdmin.get_queryset(self, request)
        return qs.filter(variants__is_dropshipping=True).distinct()

    def has_add_permission(self, request):
        return False

    def get_model_perms(self, request):
        return {}  # hidden from the menu — opens only via redirect

    def change_view(self, request, object_id, form_url='', extra_context=None):
        from django.urls import reverse
        from urllib.parse import unquote
        extra_context = extra_context or {}
        # Where the user came from (cookie set by the changelist JS before
        # they clicked into this product).  Falls back to the plain list URL.
        back = request.COOKIES.get('drops_back')
        if back:
            try:
                back = unquote(back)
            except Exception:
                back = None
        extra_context['exit_url'] = back or reverse('admin:catalog_dropshippingvariant_changelist')
        return super().change_view(request, object_id, form_url, extra_context)

    def response_post_save_change(self, request, obj):
        """After Save, return the user to the same filtered list view they came
        from (URL stored in the `drops_back` cookie by the changelist JS)."""
        from django.http import HttpResponseRedirect
        from urllib.parse import unquote
        back = request.COOKIES.get('drops_back')
        if back:
            try:
                resp = HttpResponseRedirect(unquote(back))
                resp.delete_cookie('drops_back', path='/admin/')
                return resp
            except Exception:
                pass
        return super().response_post_save_change(request, obj)


# ---------------------------------------------------------------------------
# Dropshipping: variants list (changelist) + redirect to DropshippingProduct
# ---------------------------------------------------------------------------

@admin.register(DropshippingVariant)
class DropshippingVariantAdmin(admin.ModelAdmin):
    change_list_template = 'admin/catalog/dropshipping/change_list.html'
    save_on_top = True

    list_display = (
        'photo_thumb', 'sku_display', 'variant_name', 'our_price',
        'is_active', 'created_display', 'updated_display', 'stock',
    )
    list_display_links = ('variant_name',)
    list_editable = ('is_active',)
    list_per_page = 25
    list_filter = (IsActiveFilter, 'is_supplier_fallback', 'product__categories')
    search_fields = ('product__name', 'name', 'sku', 'supplier_sku')
    ordering = ('-stock', 'product__name', 'name')

    def get_list_per_page(self, request):
        per = request.GET.get('per_page')
        if per and per.isdigit() and int(per) in (25, 50, 100):
            return int(per)
        return self.list_per_page

    def get_queryset(self, request):
        from django.db.models import Q
        qs = super().get_queryset(request).select_related('product').prefetch_related('images', 'product__images')
        return qs.filter(Q(is_dropshipping=True) | Q(is_hybrid=True)).distinct()

    def has_add_permission(self, request):
        return False

    def get_urls(self):
        from django.urls import path
        urls = super().get_urls()
        return [
            path('import/', self.admin_site.admin_view(self.import_view), name='catalog_dropshipping_import'),
        ] + urls

    def import_view(self, request):
        from django.shortcuts import redirect
        from django.contrib import messages
        from django.core.management import call_command
        if request.method == 'POST':
            try:
                # --skip-fallback: products that do not pass the SKU format are not created
                # (otherwise they clutter the catalog as inactive).
                call_command('sync_dropshipping', skip_fallback=True)
                messages.success(request, 'Synchronization with bradas.ua is complete. Details are in "Sync events".')
            except Exception as e:
                messages.error(request, f'Sync error: {e}')
        return redirect('..')

    def change_view(self, request, object_id, form_url='', extra_context=None):
        from django.shortcuts import redirect
        obj = self.get_object(request, object_id)
        if obj:
            # Edit happens on the parent Product page (variants are inlines).
            # The Product change_form's JS reads "#v<id>" and activates that
            # variant's tab automatically.
            return redirect(
                f'/admin/catalog/dropshippingproduct/{obj.product_id}/change/'
                f'#v{obj.pk}'
            )
        return super().change_view(request, object_id, form_url, extra_context)

    @admin.display(description='Photo')
    def photo_thumb(self, obj):
        # Priority: packaging photo → general product photos (variant=NULL) → any product photo
        variant_imgs = sorted(obj.images.all(), key=lambda x: (x.sort_order, x.pk))
        if variant_imgs:
            sources = variant_imgs[:3]
        else:
            product_imgs = [i for i in obj.product.images.all() if i.variant_id is None]
            product_imgs.sort(key=lambda x: (x.sort_order, x.pk))
            if not product_imgs:
                product_imgs = sorted(obj.product.images.all(), key=lambda x: (x.sort_order, x.pk))
            sources = product_imgs[:3]
        if not sources:
            return '—'
        parts = []
        for img in sources:
            url = img.image.url
            parts.append(
                f'<span class="pv-photo-wrap">'
                f'<img src="{url}" class="pv-thumb">'
                f'<span class="pv-photo-preview"><img src="{url}"></span>'
                f'</span>'
            )
        return mark_safe(f'<span class="pv-photos">{"".join(parts)}</span>')

    @admin.display(description='SKU', ordering='sku')
    def sku_display(self, obj):
        return obj.sku or '—'

    @admin.display(description='Name', ordering='name')
    def variant_name(self, obj):
        return obj.name or obj.product.name

    @admin.display(description='Price', ordering='price')
    def our_price(self, obj):
        return f'{obj.price} UAH'

    @admin.display(description='Added', ordering='created_at')
    def created_display(self, obj):
        return timezone.localtime(obj.created_at).strftime('%d.%m.%Y %H:%M') if obj.created_at else '—'

    @admin.display(description='Updated', ordering='updated_at')
    def updated_display(self, obj):
        return timezone.localtime(obj.updated_at).strftime('%d.%m.%Y %H:%M') if obj.updated_at else '—'

    def changelist_view(self, request, extra_context=None):
        from django.conf import settings
        extra_context = extra_context or {}

        from django.db.models import Q as _Q
        qs = (
            ProductVariant.objects.filter(_Q(is_dropshipping=True) | _Q(is_hybrid=True))
            .distinct()
            .select_related('product')
            .prefetch_related('product__categories', 'variant_labels__label')
            .order_by('-stock', 'product__name', 'name')
        )

        variant_ids = [v.id for v in qs]
        variant_photos = {}
        for img in ProductImage.objects.filter(
            variant_id__in=variant_ids
        ).order_by('sort_order', 'pk').values('variant_id', 'image'):
            vid = img['variant_id']
            if vid not in variant_photos:
                variant_photos[vid] = []
            if len(variant_photos[vid]) < 3:
                variant_photos[vid].append(settings.MEDIA_URL + img['image'])

        no_photo_pids = list({v.product_id for v in qs if not variant_photos.get(v.id)})
        product_photos = {}
        if no_photo_pids:
            for img in ProductImage.objects.filter(
                product_id__in=no_photo_pids, variant_id__isnull=True
            ).order_by('sort_order', 'pk').values('product_id', 'image'):
                pid = img['product_id']
                if pid not in product_photos:
                    product_photos[pid] = []
                if len(product_photos[pid]) < 3:
                    product_photos[pid].append(settings.MEDIA_URL + img['image'])

        # Mixed products: have both drop and non-drop variants (via label)
        from .models import Label as _Label
        _dl = _Label.objects.filter(slug=_Label.SLUG_PID_ZAMOVLENNYA).first()
        if _dl:
            _ds_drop_vids = set(VariantLabel.objects.filter(label=_dl).values_list('variant_id', flat=True))
            _ds_has_drop = set(ProductVariant.objects.filter(id__in=_ds_drop_vids).values_list('product_id', flat=True).distinct())
            _ds_has_non_drop = set(ProductVariant.objects.exclude(id__in=_ds_drop_vids).values_list('product_id', flat=True).distinct())
            ds_mixed_pids = _ds_has_drop & _ds_has_non_drop
        else:
            ds_mixed_pids = set()

        variants_flat = []
        _now = timezone.now()
        for v in qs:
            photos = variant_photos.get(v.id) or product_photos.get(v.product_id, [])
            cats = [c.name for c in v.product.categories.all()]
            # Labels — as on the site: non-expired variant labels in display_rank order.
            labels = [
                {'name': l.name, 'color': l.color or ''}
                for l in sorted(
                    (vl.label for vl in v.variant_labels.all()
                     if vl.label and (vl.expires_at is None or vl.expires_at > _now)),
                    key=lambda l: (Label.display_rank(l.slug), l.name or ''),
                )
            ]
            variants_flat.append({
                'id': v.id,
                'product_id': v.product_id,
                'sku': v.sku or '',
                'name': v.name or (v.product.name if v.product_id else ''),
                'product_name': v.product.name if v.product_id else '',
                'price': str(v.price) if v.price is not None else '',
                'stock': v.stock if v.stock is not None else 0,
                'stock_manual': v.stock_manual,
                'is_active': v.is_active,
                'is_mixed': v.product_id in ds_mixed_pids,
                'is_dropshipping': v.is_dropshipping,
                'is_hybrid': v.is_hybrid,
                'created_at': timezone.localtime(v.created_at).strftime('%d.%m.%Y %H:%M') if v.created_at else '',
                'updated_at': timezone.localtime(v.updated_at).strftime('%d.%m.%Y %H:%M') if v.updated_at else '',
                'photos': photos,
                'categories': cats,
                'labels': labels,
            })

        extra_context['ds_variants_json'] = js_json(variants_flat, ensure_ascii=False)
        extra_context['ds_cats_tree_json'] = js_json(
            [{'id': c.pk, 'name': c.name, 'parent_id': c.parent_id or 0}
             for c in _tree_ordered_category_qs()],
            ensure_ascii=False
        )
        extra_context['ds_labels_json'] = js_json(
            [{'name': l.name, 'color': l.color or ''} for l in Label.objects.all().order_by('name')],
            ensure_ascii=False
        )
        return super().changelist_view(request, extra_context=extra_context)


# ---------------------------------------------------------------------------
# Reviews and Questions
# ---------------------------------------------------------------------------

@admin.register(Review)
class ReviewAdmin(admin.ModelAdmin):
    list_display = ('author_name', 'rating', 'product', 'bundle', 'is_approved', 'moderation_verdict', 'moderation_reason', 'created_at')
    list_filter = ('is_approved', 'moderation_verdict', 'rating')
    list_editable = ('is_approved',)
    search_fields = ('author_name', 'author_email', 'text', 'moderation_reason')
    readonly_fields = ('created_at', 'moderation_verdict', 'moderation_reason')
    ordering = ('-created_at',)
    change_list_template = 'admin/catalog/review/change_list.html'

    def changelist_view(self, request, extra_context=None):
        import json
        items = []
        qs = (Review.objects.select_related('product', 'bundle')
              .order_by('-created_at'))
        for r in qs:
            target_name = ''
            target_id = ''
            target_kind = ''
            if r.product_id:
                target_name = r.product.name if r.product else ''
                target_id = r.product_id; target_kind = 'product'
            elif r.bundle_id:
                target_name = r.bundle.name if r.bundle else ''
                target_id = r.bundle_id; target_kind = 'bundle'
            items.append({
                'id': r.id,
                'author_name': r.author_name,
                'author_email': r.author_email or '',
                'rating': r.rating,
                'target_name': target_name,
                'target_id': target_id,
                'target_kind': target_kind,
                'is_approved': r.is_approved,
                'is_rejected': r.is_rejected,
                'moderation_verdict': r.moderation_verdict or '',
                'moderation_reason': r.moderation_reason or '',
                'created_at': timezone.localtime(r.created_at).strftime('%d.%m.%Y %H:%M'),
                'created_at_iso': timezone.localtime(r.created_at).strftime('%Y-%m-%d'),
                'text': r.text[:200],
            })
        ctx = extra_context or {}
        ctx['reviews_json'] = js_json(items, ensure_ascii=False)
        return super().changelist_view(request, extra_context=ctx)


@admin.register(StockNotification)
class StockNotificationAdmin(admin.ModelAdmin):
    list_display = ('email', 'variant', 'bundle', 'created_at', 'is_notified')
    list_filter = ('is_notified',)
    list_editable = ('is_notified',)
    search_fields = ('email', 'variant__sku', 'variant__product__name',
                     'bundle__sku', 'bundle__name')
    readonly_fields = ('created_at',)
    raw_id_fields = ('variant', 'bundle')
    ordering = ('-created_at',)
    change_list_template = 'admin/catalog/stocknotification/change_list.html'

    def changelist_view(self, request, extra_context=None):
        items = []
        qs = (StockNotification.objects
              .select_related('variant', 'variant__product', 'bundle')
              .order_by('-created_at'))
        for n in qs:
            v = n.variant
            if v:
                name = (v.product.name + (' — ' + v.name if v.name else '')) if v.product else v.name
            else:
                name = 'Bundle: ' + n.target_name
            items.append({
                'id': n.id,
                'email': n.email,
                'variant_name': name,
                'variant_sku': n.target_sku,
                'created_at': timezone.localtime(n.created_at).strftime('%d.%m.%Y %H:%M'),
                'created_at_iso': timezone.localtime(n.created_at).strftime('%Y-%m-%d'),
                'is_notified': n.is_notified,
            })
        ctx = extra_context or {}
        ctx['notifications_json'] = js_json(items, ensure_ascii=False)
        return super().changelist_view(request, extra_context=ctx)


@admin.register(Favorite)
class FavoriteAdmin(admin.ModelAdmin):
    """Favorites from the site (demand). Read-only — records are created by the site, the manager only
    views what people added to favorites (and whether it is in stock)."""
    list_display = ('target_name', 'kind_display', 'availability', 'user', 'device_id', 'created_at')
    search_fields = ('product_variant__name', 'product_variant__sku',
                     'bundle__name', 'course__name', 'device_id')
    readonly_fields = ('user', 'device_id', 'product_variant', 'bundle', 'course', 'created_at')
    ordering = ('-created_at',)
    list_select_related = ('user', 'product_variant', 'product_variant__product',
                           'bundle', 'course')

    def has_add_permission(self, request):
        return False

    @admin.display(description='Type')
    def kind_display(self, obj):
        if obj.product_variant_id:
            return 'Product'
        if obj.bundle_id:
            return 'Bundle'
        if obj.course_id:
            return 'Course'
        return '—'

    @admin.display(description='Availability')
    def availability(self, obj):
        v = obj.product_variant
        if v is None:
            return '—'  # bundle/course/deleted product
        if getattr(v, 'is_hybrid', False):
            return 'in stock'
        return 'in stock' if (v.stock or 0) > 0 else 'out of stock'


# DropSyncEvent — we do not register a dedicated admin page.
# Sync events are managed via /admin/notifications/dropsync/.


# ---------------------------------------------------------------------------
# Site settings (singleton)
# ---------------------------------------------------------------------------

class PaymentCardInline(admin.TabularInline):
    model = PaymentCard
    extra = 0
    fields = ['threshold', 'bank', 'card_number', 'holder', 'sort_order', 'is_active']


@admin.register(SiteSettings)
class SiteSettingsAdmin(admin.ModelAdmin):
    """Singleton: always a single record (pk=1). The list immediately redirects to the form.

    Access — owner only (OWNER_EMAIL): the "Settings" item is hidden in the
    sidebar for everyone else, and a direct URL returns 403.
    """

    OWNER_EMAIL = settings.OWNER_EMAIL

    save_on_top = True
    inlines = [PaymentCardInline]

    fieldsets = (
        (None, {'fields': ('maintenance_mode', 'min_order_amount')}),
        ('Contacts for SEO (Organization markup on the home page)', {
            'fields': ('org_phone', 'org_email', 'org_address',
                       'org_instagram_url', 'org_facebook_url', 'org_telegram_url'),
            'classes': ('collapse',),
        }),
    )

    def _is_owner(self, request):
        u = getattr(request, 'user', None)
        return bool(u and u.is_authenticated and (u.email or '').lower() == self.OWNER_EMAIL)

    def has_module_permission(self, request):
        return self._is_owner(request)

    def has_view_permission(self, request, obj=None):
        return self._is_owner(request)

    def has_change_permission(self, request, obj=None):
        return self._is_owner(request)

    def has_add_permission(self, request):
        # Owner only, and only if the record does not exist yet (singleton).
        return self._is_owner(request) and not SiteSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        # We do not show the list — we send straight to the pk=1 form (creating it if needed).
        from django.shortcuts import redirect
        obj = SiteSettings.get()
        return redirect(f'../sitesettings/{obj.pk}/change/')


@admin.register(BundleInstructionOverride)
class BundleInstructionOverrideAdmin(admin.ModelAdmin):
    """Queue "instruction needs a human look".

    A bundle lands here when a preparation in it was swapped: the machine filled in the name itself,
    but a person has to check the dose — 50 g of granules and 10 ml of liquid cannot be converted
    into one another. While the record is in the "Needs a look" state, the sheet is
    not sent to print: nothing is printed in its place, and the bundle itself shows up in the export
    warnings.

    A confirmed record is cached by the substitution signature, so one review closes ALL orders with
    the same substitution, not each one separately.
    """
    list_display = ('summary_col', 'bundle_col', 'status', 'hits', 'stale_col', 'created_at')
    list_filter = ('status', 'is_stale')
    search_fields = ('summary', 'bundle__name')
    readonly_fields = ('bundle', 'signature', 'summary', 'composition_col',
                       'auto_substituted', 'is_stale', 'hits', 'base_stamp',
                       'created_at', 'updated_at', 'resolved_by', 'resolved_at')
    fields = ('bundle', 'summary', 'composition_col', 'status',
              'auto_substituted', 'is_stale', 'hits',
              'instruction_html', 'signature', 'base_stamp',
              'resolved_by', 'resolved_at', 'created_at', 'updated_at')
    ordering = ('-is_stale', 'status', '-created_at')

    @admin.display(description='Substitution', ordering='summary')
    def summary_col(self, obj):
        return obj.summary or '—'

    @admin.display(description='Bundle')
    def bundle_col(self, obj):
        b = obj.bundle
        return b.name if b else '—'

    @admin.display(description='Stale', boolean=True)
    def stale_col(self, obj):
        return obj.is_stale

    @admin.display(description='Composition the correction was made for')
    def composition_col(self, obj):
        rows = obj.composition or []
        if not rows:
            return '—'
        return format_html_join(
            mark_safe('<br>'), '{} × {}',
            ((r.get('name', ''), r.get('qty', 1)) for r in rows))

    def has_add_permission(self, request):
        # Records are created by the resolver; there is no point in creating them by hand.
        return False

    def save_model(self, request, obj, form, change):
        # Who confirmed and when is recorded automatically; it is the trail for later review.
        if obj.status == BundleInstructionOverride.READY and not obj.resolved_at:
            obj.resolved_by = request.user
            obj.resolved_at = timezone.now()
        if obj.status != BundleInstructionOverride.PENDING:
            # This very version of the base instruction was confirmed — clear the "stale"
            # flag.
            obj.is_stale = False
            if obj.bundle_id:
                obj.base_stamp = obj.bundle.instruction_stamp
        super().save_model(request, obj, form, change)


def _resync_instruction_after_swap(bundle):
    """Rewrite the preparation name in the instruction after a bundle's composition changes.

    Called from `BundleAdmin.save_related` right after BundleItem is recreated. The difference from
    a per-order substitution is fundamental: there the template must not be touched (only one order
    changed), but here the bundle's composition has changed PERMANENTLY and the text must reflect
    that — otherwise every print would substitute the name anew, and a manager opening the
    instruction would read the old preparation.

    We rewrite ONLY an unambiguous substitution "one disappeared —
    one appeared", and the packaging moves along with the name. Returns a string for the change
    history, or None if there was nothing to rewrite.
    """
    from . import instruction_resolve as ir
    from . import instruction_llm as il

    if not (bundle.instruction_html or '').strip() or not bundle.instruction_map:
        return None

    surfaces = ir.surfaces_of(bundle)
    if not surfaces:
        return None

    template = ir.components_of_template(bundle)
    expected = ir._expected_components(bundle, template, surfaces)
    removed, added, _qty, material = ir.diff_components(
        expected, template, bundle.instruction_html, surfaces)
    if not removed and not added:
        return None
    # The text does not name this product (respirator, bag, gloves) — there is nothing to fix in the
    # instruction, and no reason to alarm the manager with "fix the instruction".
    if not material:
        return None

    new_html, n, dose_note, dose_ok = ir.auto_substitute(bundle.instruction_html, removed,
                                                         added, expected, surfaces)
    if n and not dose_ok:
        # We can substitute the name, but not the dose. Rewriting the text halfway is worse
        # than not touching it: the field would keep the new name with the old dose, and nobody
        # would ever
        # see what was there. We put it in the queue for LLM resolve (no human involved —
        # resolve_pending_instructions picks up the PENDING row on its own).
        ir.resolve_bundle(bundle, components=template, create_pending=True)
        return (f'Composition changed ({ir.human_summary(removed, added)}) — {dose_note}')
    if not n:
        # Ambiguous replacement — there is nothing to substitute, it goes to the queue for LLM
        # resolve.
        ir.resolve_bundle(bundle, components=template, create_pending=True)
        return (f'Composition changed ({ir.human_summary(removed, added)}), '
                f'automatic renaming is impossible — the instruction is queued for AI resolve')

    # The markup must also be moved to the new product, otherwise the next change would be compared
    # against a composition that no longer exists.
    old_to_new = {removed[0].variant.product_id: added[0]} if (
        len(removed) == 1 and len(added) == 1 and removed[0].variant) else {}
    m = dict(bundle.instruction_map)
    mentions = []
    for row in m.get('mentions', []):
        row = dict(row)
        repl = old_to_new.get(row.get('product'))
        if repl is not None and repl.variant:
            row['product'] = repl.variant.product_id
            row['surface'] = repl.label
        mentions.append(row)

    bundle.instruction_html = new_html
    bundle.instruction_map = il.build_map(new_html, mentions, template)
    bundle.save(update_fields=['instruction_html', 'instruction_map',
                               'instruction_stamp', 'instruction_updated_at'])
    return (f'Instruction: {ir.human_summary(removed, added)} — '
            f'name replaced in {n} places'
            + (f', {dose_note}' if dose_note else ', check the dose'))
