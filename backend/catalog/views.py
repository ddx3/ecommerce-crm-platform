import json
import logging
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.contrib import admin
from django.contrib.admin.views.decorators import staff_member_required
from .models import Product, ProductVariant, ProductImage, ProductCharacteristic, ProductComposition, Category, BundleImage, Bundle, BundleItem, BrandImage, Brand, Review, StockNotification, BulkSource
from .revert import apply_snapshot, restore_deleted

log = logging.getLogger(__name__)


@staff_member_required
def generate_sku(request):
    return JsonResponse({"sku": ProductVariant.generate_unique_sku(also_check_bundle=True)})


@staff_member_required
def check_sku(request):
    sku = request.GET.get("sku", "").strip()
    exclude_id = request.GET.get("exclude_id")
    exclude_bundle_id = request.GET.get("exclude_bundle_id")
    qs_v = ProductVariant.objects.filter(sku=sku)
    if exclude_id:
        qs_v = qs_v.exclude(pk=exclude_id)
    qs_b = Bundle.objects.filter(sku=sku)
    if exclude_bundle_id:
        qs_b = qs_b.exclude(pk=exclude_bundle_id)
    return JsonResponse({"exists": qs_v.exists() or qs_b.exists()})


@staff_member_required
def upload_product_photo(request):
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)
    image = request.FILES.get("image")
    if not image:
        return JsonResponse({"error": "No image"}, status=400)
    product_id = request.POST.get("product_id")
    variant_id = request.POST.get("variant_id")
    temp_key = request.POST.get("temp_key")
    is_main = request.POST.get("is_main") == "1"
    obj = ProductImage(image=image, is_main=is_main)
    if product_id:
        obj.product_id = product_id
    elif variant_id:
        # Invariant: a variant's photo is also linked to the product (product_id),
        # so that the combined obj.images list includes both shared and variant photos.
        v = ProductVariant.objects.filter(pk=variant_id).only('product_id').first()
        if not v:
            return JsonResponse({"error": "Variant not found"}, status=400)
        obj.variant_id = variant_id
        obj.product_id = v.product_id
    elif temp_key:
        obj.temp_key = temp_key
    else:
        return JsonResponse({"error": "No target"}, status=400)

    # A new photo goes to the end of the list.
    if obj.product_id:
        from django.db.models import Max
        max_so = ProductImage.objects.filter(product_id=obj.product_id).aggregate(m=Max('sort_order'))['m'] or 0
        obj.sort_order = max_so + 1

    obj.save()
    if is_main:
        qs = ProductImage.objects.exclude(pk=obj.pk)
        if product_id:
            qs = qs.filter(product_id=product_id)
        elif variant_id:
            qs = qs.filter(variant_id=variant_id)
        qs.update(is_main=False)
    return JsonResponse({"id": obj.pk, "url": obj.image.url})


@staff_member_required
def cleanup_temp_photos(request):
    """Deletes 'orphaned' ProductImage rows with a temp_key left over from a previous
    attempt to create a product (the user closed the form without saving)."""
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)
    import json as _json
    try:
        data = _json.loads(request.body or '{}')
    except Exception:
        data = {}
    temp_key = (data.get('temp_key') or '').strip()
    if not temp_key:
        return JsonResponse({"ok": True, "deleted": 0})
    qs = ProductImage.objects.filter(temp_key=temp_key, product__isnull=True, variant__isnull=True)
    count = 0
    for p in qs:
        try:
            p.image.delete(save=False)
        except Exception:
            pass
        p.delete()
        count += 1
    return JsonResponse({"ok": True, "deleted": count})


@staff_member_required
def get_temp_photos(request):
    temp_key = request.GET.get("temp_key", "").strip()
    if not temp_key:
        return JsonResponse({"photos": []})
    photos = ProductImage.objects.filter(temp_key=temp_key).order_by("sort_order", "pk")
    return JsonResponse({"photos": [
        {"id": p.pk, "url": p.image.url, "is_main": p.is_main, "variant_id": p.variant_id}
        for p in photos
    ]})


@staff_member_required
def get_variant_photos(request):
    """Kept for backward compatibility — returns the photos linked to a specific variant."""
    variant_id = request.GET.get("variant_id", "").strip()
    if not variant_id:
        return JsonResponse({"photos": []})
    photos = ProductImage.objects.filter(variant_id=variant_id).order_by("sort_order", "pk")
    return JsonResponse({"photos": [
        {"id": p.pk, "url": p.image.url, "is_main": p.is_main, "variant_id": p.variant_id}
        for p in photos
    ]})


@staff_member_required
def get_product_photos(request):
    """All product photos in the unified order: both shared and variant ones."""
    product_id = request.GET.get("product_id", "").strip()
    if not product_id:
        return JsonResponse({"photos": []})
    photos = ProductImage.objects.filter(product_id=product_id).order_by("sort_order", "pk")
    return JsonResponse({"photos": [
        {"id": p.pk, "url": p.image.url, "is_main": p.is_main, "variant_id": p.variant_id}
        for p in photos
    ]})


def _auto_promote_main_photo(product_id):
    """When hiding the main photo, automatically assign a new main one using the same
    priority as on the product page (_pick_main_image in the serializers): the first
    variant's photo → the first common photo → any visible one. Otherwise no photo in
    the CRM is marked "Main", even though the site shows some other one — an
    inconsistency between the admin and the storefront."""
    if not product_id:
        return
    visible = list(ProductImage.objects.filter(product_id=product_id, is_hidden=False))
    if not visible or any(i.is_main for i in visible):
        return None
    key = lambda i: (i.sort_order, i.pk)
    candidate = None
    if any(i.variant_id for i in visible):
        first_variant = ProductVariant.objects.filter(product_id=product_id).order_by('pk').first()
        if first_variant:
            candidate = min((i for i in visible if i.variant_id == first_variant.pk), key=key, default=None)
    if not candidate:
        candidate = min((i for i in visible if i.variant_id is None), key=key, default=None)
    if not candidate:
        candidate = min(visible, key=key, default=None)
    if candidate:
        candidate.is_main = True
        candidate.save(update_fields=['is_main'])
        return candidate.pk
    return None


@staff_member_required
def set_photo_assignment(request):
    """Single endpoint for reassigning a photo:
        mode='hidden'        — the photo exists but is not shown anywhere on the site;
        mode='main'          — the product's main photo (shown in the catalog card);
        mode='common'        — an ordinary common product photo;
        mode='variant'       — a specific variant's photo (variant_id is required);
        mode='toggle_hidden' — quick «eye» toggle in pg-actions: only
            is_hidden (and is_main, if hiding) — does NOT touch the binding
            (variant/common), unlike mode='hidden'.
    """
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)
    import json as _json
    data = _json.loads(request.body)
    photo_id = data.get("id")
    mode = (data.get("mode") or "").strip()
    variant_id = data.get("variant_id")
    try:
        photo = ProductImage.objects.get(pk=photo_id)
    except ProductImage.DoesNotExist:
        return JsonResponse({"error": "Not found"}, status=404)

    was_main = photo.is_main

    if mode == "hidden":
        photo.is_hidden = True
        photo.is_main = False
    elif mode == "main":
        # On the product creation page the photo isn't linked to a product_id yet —
        # it lies under a temporary temp_key (it gets linked in save_model). Previously
        # product_id was required here, so "Main" before saving the product silently
        # did nothing (the JS swallowed {"error": ...} without ok — no sign at all
        # for the user).
        if not photo.product_id and not photo.temp_key:
            return JsonResponse({"error": "Photo not bound to product"}, status=400)
        # Main = one per product. Clear the flag on the rest — within the same
        # product, or the same temp_key while the product doesn't exist yet.
        if photo.product_id:
            ProductImage.objects.filter(product_id=photo.product_id).exclude(pk=photo.pk).update(is_main=False)
        else:
            ProductImage.objects.filter(temp_key=photo.temp_key).exclude(pk=photo.pk).update(is_main=False)
        photo.is_hidden = False
        photo.is_main = True
        # If variant_id is passed, link the photo to the variant; otherwise it stays shared.
        if variant_id:
            v = ProductVariant.objects.filter(pk=variant_id).only('product_id').first()
            if not v:
                return JsonResponse({"error": "Variant not found"}, status=400)
            if photo.product_id and photo.product_id != v.product_id:
                return JsonResponse({"error": "Variant from another product"}, status=400)
            photo.variant_id = v.pk
        else:
            photo.variant_id = None
    elif mode == "variant":
        if not variant_id:
            return JsonResponse({"error": "variant_id required"}, status=400)
        v = ProductVariant.objects.filter(pk=variant_id).only('product_id').first()
        if not v:
            return JsonResponse({"error": "Variant not found"}, status=400)
        if photo.product_id and photo.product_id != v.product_id:
            return JsonResponse({"error": "Variant from another product"}, status=400)
        photo.is_hidden = False
        photo.is_main = False
        photo.variant_id = v.pk
        photo.product_id = v.product_id
    elif mode == "common":
        photo.is_hidden = False
        photo.is_main = False
        photo.variant_id = None
    elif mode == "toggle_hidden":
        photo.is_hidden = not photo.is_hidden
        if photo.is_hidden:
            photo.is_main = False
    else:
        return JsonResponse({"error": "Invalid mode"}, status=400)

    photo.save(update_fields=['is_hidden', 'is_main', 'variant_id', 'product_id'])
    promoted_id = _auto_promote_main_photo(photo.product_id) if (was_main and photo.is_hidden) else None
    return JsonResponse({
        "ok": True,
        "is_hidden": photo.is_hidden,
        "is_main": photo.is_main,
        "variant_id": photo.variant_id,
        "promoted_main_id": promoted_id,
    })


# Backwards-compatible aliases — we keep the old endpoints as a wrapper, since cached
# admin pages may still call them.
@staff_member_required
def set_photo_main(request):
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)
    import json as _json
    data = _json.loads(request.body)
    request._body = _json.dumps({"id": data.get("id"), "mode": "main"}).encode()
    request._read_started = False
    return _set_assignment_compat(data.get("id"), "main", None)


@staff_member_required
def set_photo_variant(request):
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)
    import json as _json
    data = _json.loads(request.body)
    vid = data.get("variant_id")
    mode = "variant" if vid else "common"
    return _set_assignment_compat(data.get("id"), mode, vid)


def _set_assignment_compat(photo_id, mode, variant_id):
    try:
        photo = ProductImage.objects.get(pk=photo_id)
    except ProductImage.DoesNotExist:
        return JsonResponse({"error": "Not found"}, status=404)
    if mode == "main":
        if photo.variant_id or not photo.product_id:
            return JsonResponse({"error": "Cannot set main"}, status=400)
        ProductImage.objects.filter(
            product_id=photo.product_id, variant__isnull=True
        ).exclude(pk=photo.pk).update(is_main=False)
        photo.is_main = True
        photo.is_hidden = False
        photo.variant_id = None
    elif mode == "variant":
        v = ProductVariant.objects.filter(pk=variant_id).only('product_id').first()
        if not v:
            return JsonResponse({"error": "Variant not found"}, status=400)
        photo.is_hidden = False
        photo.is_main = False
        photo.variant_id = v.pk
        photo.product_id = v.product_id
    else:
        photo.is_hidden = False
        photo.is_main = False
        photo.variant_id = None
    photo.save(update_fields=['is_hidden', 'is_main', 'variant_id', 'product_id'])
    return JsonResponse({"ok": True, "variant_id": photo.variant_id})


@staff_member_required
def delete_product_photo(request):
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)
    import json as _json
    data = _json.loads(request.body)
    photo_id = data.get("id")
    try:
        photo = ProductImage.objects.get(pk=photo_id)
        photo.image.delete(save=False)
        photo.delete()
        return JsonResponse({"ok": True})
    except ProductImage.DoesNotExist:
        return JsonResponse({"error": "Not found"}, status=404)


@staff_member_required
def replace_product_photo(request):
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)
    photo_id = request.POST.get("id")
    image = request.FILES.get("image")
    if not photo_id or not image:
        return JsonResponse({"error": "Missing data"}, status=400)
    try:
        photo = ProductImage.objects.get(pk=photo_id)
        photo.image.delete(save=False)
        photo.image = image
        photo.save()
        return JsonResponse({"ok": True, "url": photo.image.url})
    except ProductImage.DoesNotExist:
        return JsonResponse({"error": "Not found"}, status=404)


@staff_member_required
def process_product_photo(request):
    """Prepares the storefront-standard preview (1080×1080, background removed, object centered
    on a white background) and returns it as base64 — WITHOUT writing to a file/DB. The actual
    overwrite happens only on form submit via apply_product_photo_std."""
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)
    try:
        body = json.loads(request.body or "{}")
    except ValueError:
        body = {}
    photo_id = body.get("id") or request.POST.get("id")
    if not photo_id:
        return JsonResponse({"error": "Missing id"}, status=400)
    try:
        photo = ProductImage.objects.get(pk=photo_id)
    except ProductImage.DoesNotExist:
        return JsonResponse({"error": "Not found"}, status=404)

    try:
        from .image_standardize import standardize
    except ImportError:
        return JsonResponse(
            {"error": "rembg is not installed on the server (pip install rembg onnxruntime)"},
            status=500,
        )
    try:
        with photo.image.open("rb") as fh:
            data = fh.read()
        processed = standardize(data)
    except ImportError:
        return JsonResponse(
            {"error": "rembg is not installed on the server (pip install rembg onnxruntime)"},
            status=500,
        )
    except Exception as e:
        return JsonResponse({"error": f"Could not process: {e}"}, status=500)

    import base64
    preview = "data:image/jpeg;base64," + base64.b64encode(processed).decode("ascii")
    return JsonResponse({"ok": True, "preview": preview})


@staff_member_required
def apply_product_photo_std(request):
    """Saves the standardized photo (the preview from process_product_photo) to a file.
    Called only on product form submit — until then the original is untouched."""
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)
    try:
        body = json.loads(request.body or "{}")
    except ValueError:
        body = {}
    photo_id = body.get("id")
    image_b64 = body.get("image") or ""
    if not photo_id or not image_b64:
        return JsonResponse({"error": "Missing data"}, status=400)
    try:
        photo = ProductImage.objects.get(pk=photo_id)
    except ProductImage.DoesNotExist:
        return JsonResponse({"error": "Not found"}, status=404)

    import os
    import io
    import base64
    from django.core.files.base import ContentFile
    if image_b64.startswith("data:"):
        image_b64 = image_b64.split(",", 1)[-1]
    try:
        processed = base64.b64decode(image_b64)
    except Exception:
        return JsonResponse({"error": "Bad image"}, status=400)
    # Validate that it's a real image before writing to disk.
    try:
        from PIL import Image
        Image.open(io.BytesIO(processed)).verify()
    except Exception:
        return JsonResponse({"error": "Bad image"}, status=400)

    base = os.path.splitext(os.path.basename(photo.image.name))[0]
    if base.endswith("_std"):
        base = base[:-4]
    photo.image.delete(save=False)
    photo.image.save(f"{base}_std.jpg", ContentFile(processed), save=True)
    return JsonResponse({"ok": True, "url": photo.image.url})


@staff_member_required
def reorder_product_photos(request):
    """Reorder photos in the product's unified gallery.

    JS passes a list of ids (or {id, sort_order} objects) in the desired order.
    We store exactly the order the admin set — with no auto-normalization:
    the catalog card takes the first photo in the list, so the admin fully controls
    what is the product's 'face' (the variant packaging or an instruction).
    """
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)
    import json as _json
    data = _json.loads(request.body)
    raw = data.get("order") or data.get("photos") or []
    ids = []
    for item in raw:
        if isinstance(item, dict):
            ids.append(item.get("id"))
        else:
            ids.append(item)
    ids = [int(i) for i in ids if i]
    if not ids:
        return JsonResponse({"ok": True})
    for i, photo_id in enumerate(ids):
        ProductImage.objects.filter(pk=photo_id).update(sort_order=i + 1)
    return JsonResponse({"ok": True})


@staff_member_required
def get_characteristics(request):
    """Returns characteristics:
    - if variant_id is passed → only that variant's (old behavior for new variants);
    - if only product_id → ALL of the product's characteristics (shared + variant), each with a
      variant_id field so the client-side filter can split them across tabs.
    """
    product_id = request.GET.get("product_id")
    variant_id = request.GET.get("variant_id")
    if variant_id:
        items = ProductCharacteristic.objects.filter(variant_id=variant_id).order_by("sort_order", "pk")
    elif product_id:
        items = ProductCharacteristic.objects.filter(product_id=product_id).order_by("sort_order", "pk")
    else:
        return JsonResponse({"items": []})
    return JsonResponse({"items": [
        {"id": i.pk, "name": i.name, "value": i.value, "color_hex": i.color_hex, "variant_id": i.variant_id}
        for i in items
    ]})


@staff_member_required
def save_characteristic(request):
    """Create/update a characteristic.
    On update it does NOT change the link (variant_id) — that is controlled by
    set_characteristic_assignment.
    Returns {id, variant_id} so the frontend knows the current state.
    """
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)
    import json as _json
    data = _json.loads(request.body)
    char_id = data.get("id")
    name = data.get("name", "").strip()
    value = data.get("value", "").strip()
    color_hex = (data.get("color") or "").strip()
    product_id = data.get("product_id")
    variant_id = data.get("variant_id")
    sort_order = data.get("sort_order", 0)
    if char_id:
        try:
            char = ProductCharacteristic.objects.get(pk=char_id)
            char.name = name
            char.value = value
            char.color_hex = color_hex
            char.sort_order = sort_order
            char.save(update_fields=['name', 'value', 'color_hex', 'sort_order'])
            return JsonResponse({"id": char.pk, "variant_id": char.variant_id})
        except ProductCharacteristic.DoesNotExist:
            return JsonResponse({"error": "Not found"}, status=404)
    # Invariant: a variant characteristic is also linked to the product (product_id).
    if variant_id and not product_id:
        v = ProductVariant.objects.filter(pk=variant_id).only('product_id').first()
        if v:
            product_id = v.product_id
    char = ProductCharacteristic.objects.create(
        name=name, value=value, color_hex=color_hex, sort_order=sort_order,
        product_id=product_id or None,
        variant_id=variant_id or None,
    )
    return JsonResponse({"id": char.pk, "variant_id": char.variant_id})


@staff_member_required
def set_characteristic_assignment(request):
    """Reassign a characteristic: 'common' (shared, variant=NULL) or 'variant' (linked to a variant)."""
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)
    import json as _json
    data = _json.loads(request.body)
    char_id = data.get("id")
    mode = (data.get("mode") or "").strip()
    variant_id = data.get("variant_id")
    try:
        char = ProductCharacteristic.objects.get(pk=char_id)
    except ProductCharacteristic.DoesNotExist:
        return JsonResponse({"error": "Not found"}, status=404)

    if mode == "common":
        char.variant_id = None
    elif mode == "variant":
        if not variant_id:
            return JsonResponse({"error": "variant_id required"}, status=400)
        v = ProductVariant.objects.filter(pk=variant_id).only('product_id').first()
        if not v:
            return JsonResponse({"error": "Variant not found"}, status=400)
        if char.product_id and char.product_id != v.product_id:
            return JsonResponse({"error": "Variant from another product"}, status=400)
        char.variant_id = v.pk
        char.product_id = v.product_id
    else:
        return JsonResponse({"error": "Invalid mode"}, status=400)

    char.save(update_fields=['variant_id', 'product_id'])
    return JsonResponse({"ok": True, "variant_id": char.variant_id})


@staff_member_required
def delete_characteristic(request):
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)
    import json as _json
    data = _json.loads(request.body)
    char_id = data.get("id")
    try:
        ProductCharacteristic.objects.get(pk=char_id).delete()
        return JsonResponse({"ok": True})
    except ProductCharacteristic.DoesNotExist:
        return JsonResponse({"error": "Not found"}, status=404)


@staff_member_required
def reorder_characteristics(request):
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)
    import json as _json
    data = _json.loads(request.body)
    order = data.get("order", [])
    for i, char_id in enumerate(order):
        ProductCharacteristic.objects.filter(pk=char_id).update(sort_order=i)
    return JsonResponse({"ok": True})


@staff_member_required
def get_composition(request):
    product_id = request.GET.get("product_id")
    if not product_id:
        return JsonResponse({"items": []})
    items = ProductComposition.objects.filter(product_id=product_id).order_by("sort_order", "pk")
    return JsonResponse({"items": [{"id": i.pk, "name": i.name, "value": i.value} for i in items]})


@staff_member_required
def save_composition_item(request):
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)
    import json as _json
    data = _json.loads(request.body)
    item_id = data.get("id")
    name = data.get("name", "").strip()
    value = data.get("value", "").strip()
    product_id = data.get("product_id")
    sort_order = data.get("sort_order", 0)
    if item_id:
        try:
            item = ProductComposition.objects.get(pk=item_id)
            item.name = name
            item.value = value
            item.sort_order = sort_order
            item.save()
            return JsonResponse({"id": item.pk})
        except ProductComposition.DoesNotExist:
            return JsonResponse({"error": "Not found"}, status=404)
    item = ProductComposition.objects.create(
        name=name, value=value, sort_order=sort_order,
        product_id=product_id or None,
    )
    return JsonResponse({"id": item.pk})


@staff_member_required
def delete_composition_item(request):
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)
    import json as _json
    data = _json.loads(request.body)
    item_id = data.get("id")
    try:
        ProductComposition.objects.get(pk=item_id).delete()
        return JsonResponse({"ok": True})
    except ProductComposition.DoesNotExist:
        return JsonResponse({"error": "Not found"}, status=404)


@staff_member_required
def reorder_composition(request):
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)
    import json as _json
    data = _json.loads(request.body)
    order = data.get("order", [])
    for i, item_id in enumerate(order):
        ProductComposition.objects.filter(pk=item_id).update(sort_order=i)
    return JsonResponse({"ok": True})


@staff_member_required
def reorder_categories(request):
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)
    import json as _json
    data = _json.loads(request.body)
    order = data.get("order", [])
    for item in order:
        Category.objects.filter(pk=item["id"]).update(
            sort_order=item["sort_order"],
            parent_id=item.get("parent_id") or None,
        )
    return JsonResponse({"ok": True})


@staff_member_required
def get_variant_price_stock(request):
    ids = request.GET.getlist("id")
    if not ids:
        return JsonResponse({"variants": {}, "bags": {}})
    variants = list(ProductVariant.objects.filter(pk__in=ids).values(
        "id", "price", "stock", "name", "bulk_source_id", "unit_size"
    ))
    bag_ids = {v["bulk_source_id"] for v in variants if v["bulk_source_id"]}
    bags = {}
    if bag_ids:
        from .models import BulkSource
        for b in BulkSource.objects.filter(pk__in=bag_ids).values("id", "quantity", "name"):
            bags[str(b["id"])] = {"quantity": str(b["quantity"]) if b["quantity"] is not None else None, "name": b["name"]}
    out = {}
    for v in variants:
        out[str(v["id"])] = {
            "price": str(v["price"]) if v["price"] is not None else None,
            "stock": v["stock"],
            "name": v["name"],
            "bulk_source_id": v["bulk_source_id"],
            "unit_size": str(v["unit_size"]) if v["unit_size"] is not None else None,
        }
    return JsonResponse({"variants": out, "bags": bags})


def _bundle_items_data(bundle, request):
    items = []
    for bi in bundle.items.select_related('product_variant__product').prefetch_related('product_variant__images', 'product_variant__product__images'):
        v = bi.product_variant
        vphoto = (
            v.images.order_by('sort_order', 'pk').first() or
            v.product.images.filter(variant__isnull=True).order_by('sort_order', 'pk').first() or
            v.product.images.order_by('sort_order', 'pk').first()
        )
        items.append({
            'variant_id': v.pk,
            'product_id': v.product_id,
            'name': v.name or v.product.name,
            'sku': v.sku or '',
            'qty': bi.quantity,
            'stock': v.stock,
            'price': str(v.price) if v.price is not None else '',
            'discount_percent': str(v.discount_percent).rstrip('0').rstrip('.') if v.discount_percent else '',
            'discount_amount': str(v.discount_amount).rstrip('0').rstrip('.') if v.discount_amount else '',
            'apply_discount': bi.apply_discount,
            'photo': request.build_absolute_uri(vphoto.image.url) if vphoto else None,
            'site_url': (settings.SITE_URL.rstrip('/') + '/product/' + v.product.slug + '?variant=' + str(v.pk)) if (v.product_id and v.product.slug) else '',
        })
    return items


@staff_member_required
def bundle_details(request):
    ids = [x.strip() for x in request.GET.get('ids', '').split(',') if x.strip().isdigit()]
    if not ids:
        return JsonResponse({'bundles': {}})
    qs = Bundle.objects.filter(pk__in=ids).prefetch_related(
        'images',
        'items__product_variant__product',
        'items__product_variant__images',
        'items__product_variant__product__images',
    )
    result = {}
    for b in qs:
        photo = b.images.order_by('sort_order', 'pk').first()
        # CRM sees everything, including hidden bundles.
        _price = b.get_price() or 0
        _stock = b.get_stock() or 0
        result[str(b.pk)] = {
            'id': b.pk, 'sku': b.sku or '', 'name': b.name,
            'price': str(_price), 'stock': _stock,
            'photo': request.build_absolute_uri(photo.image.url) if photo else None,
            'items': _bundle_items_data(b, request),
            'site_url': (settings.SITE_URL.rstrip('/') + '/bundle/' + b.slug) if b.slug else '',
        }
    return JsonResponse({'bundles': result})


@staff_member_required
def variant_details(request):
    from catalog.serializers import _supplier_dict as _sup_dict
    ids = [x.strip() for x in request.GET.get('ids', '').split(',') if x.strip().isdigit()]
    if not ids:
        return JsonResponse({'variants': {}})
    qs = ProductVariant.objects.filter(pk__in=ids).select_related('product__brand', 'supplier').prefetch_related('images', 'product__images')
    result = {}
    for v in qs:
        photo = (
            v.images.order_by('sort_order', 'pk').first() or
            v.product.images.filter(variant__isnull=True).order_by('sort_order', 'pk').first() or
            v.product.images.order_by('sort_order', 'pk').first()
        )
        name = v.name or v.product.name
        result[str(v.pk)] = {
            'id': v.pk, 'product_id': v.product_id, 'sku': v.sku, 'name': name,
            'price': str(v.price), 'final_price': str(v.final_price_whole), 'stock': v.stock,
            'packaging_kind': v.product.packaging_kind if v.product_id else '',
            'is_dropshipping': bool(getattr(v, 'is_dropshipping', False)),
            'is_hybrid': bool(getattr(v, 'is_hybrid', False)),
            'supplier': _sup_dict(v.supplier) if v.supplier_id else None,
            'photo': request.build_absolute_uri(photo.image.url) if photo else None,
            'discount_percent': str(v.discount_percent).rstrip('0').rstrip('.') if v.discount_percent else '',
            'discount_amount': str(v.discount_amount).rstrip('0').rstrip('.') if v.discount_amount else '',
            'site_url': (settings.SITE_URL.rstrip('/') + '/product/' + v.product.slug + '?variant=' + str(v.pk)) if (v.product_id and v.product.slug) else '',
        }
    return JsonResponse({'variants': result})


@staff_member_required
def variant_search(request):
    q = request.GET.get("q", "").strip()
    include_bundles = request.GET.get("include_bundles") == "1"
    if not q:
        return JsonResponse({"results": []})
    qs = ProductVariant.objects.select_related("product__brand", "supplier").prefetch_related("images", "product__images")
    if q.isdigit():
        qs = qs.filter(Q(pk=q) | Q(sku__icontains=q) | Q(name__icontains=q) | Q(product__name__icontains=q))
    else:
        qs = qs.filter(Q(sku__icontains=q) | Q(name__icontains=q) | Q(product__name__icontains=q))
    results = []
    # CRM search: the manager sees EVERYTHING, including hidden items (is_active=False).
    # Hiding applies only to customers on the frontend, not in the admin.
    for v in qs[:20]:
        photo = (
            v.images.order_by('sort_order', 'pk').first() or
            v.product.images.filter(variant__isnull=True).order_by('sort_order', 'pk').first() or
            v.product.images.order_by('sort_order', 'pk').first()
        )
        results.append({
            "type": "variant",
            "id": v.pk,
            "product_id": v.product_id,
            "name": v.name or v.product.name,
            "product_name": v.product.name,
            "sku": v.sku,
            "price": str(v.price),
            "final_price": str(v.final_price_whole),
            "stock": v.stock,
            "photo": request.build_absolute_uri(photo.image.url) if photo else None,
            "packaging_kind": v.product.packaging_kind if v.product_id else "",
            "is_dropshipping": bool(getattr(v, "is_dropshipping", False)),
            "is_hybrid": bool(getattr(v, "is_hybrid", False)),
            "supplier": {"code": v.supplier.code, "name": v.supplier.name} if v.supplier_id else None,
            "site_url": (settings.SITE_URL.rstrip('/') + '/product/' + v.product.slug + '?variant=' + str(v.pk)) if (v.product_id and v.product.slug) else '',
        })
    if include_bundles:
        bqs = Bundle.objects.prefetch_related(
            "images",
            "items__product_variant__product",
            "items__product_variant__images",
            "items__product_variant__product__images",
        )
        if q.isdigit():
            bqs = bqs.filter(Q(pk=q) | Q(sku__icontains=q) | Q(name__icontains=q))
        else:
            bqs = bqs.filter(Q(sku__icontains=q) | Q(name__icontains=q))
        for b in bqs[:10]:
            photo = b.images.order_by('sort_order', 'pk').first()
            _price = b.get_price() or 0
            _stock = b.get_stock() or 0
            results.append({
                "type": "bundle",
                "id": b.pk,
                "product_id": None,
                "name": b.name,
                "product_name": b.name,
                "sku": b.sku or "",
                "price": str(_price),
                "stock": _stock,
                "photo": request.build_absolute_uri(photo.image.url) if photo else None,
                "items": _bundle_items_data(b, request),
                "site_url": (settings.SITE_URL.rstrip('/') + '/bundle/' + b.slug) if b.slug else '',
            })
    return JsonResponse({"results": results})


@staff_member_required
def save_variant_field(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    import json as _json
    data = _json.loads(request.body)
    vid = data.get('id')
    field = data.get('field')
    value = data.get('value')
    allowed = {'price', 'stock', 'is_active', 'manual_stock', 'low_stock_threshold'}
    if field not in allowed:
        return JsonResponse({'error': 'forbidden'}, status=403)
    try:
        v = ProductVariant.objects.get(pk=vid)
        if field == 'is_active':
            setattr(v, field, bool(value))
            v.save(update_fields=[field])
        elif field == 'manual_stock':
            # value=None or '' → reset manual mode, recompute from the bag
            # value=number   → enable manual mode, store the given number
            if value is None or value == '':
                v.stock_manual = False
                update_fields = ['stock_manual']
                if v.bulk_source_id and v.unit_size:
                    eu = v.bulk_source.effective_unit(v.unit_size)
                    v.stock = int(v.bulk_source._dec(v.bulk_source.quantity) // eu) if eu > 0 else 0
                    update_fields.append('stock')
                v.save(update_fields=update_fields)
            else:
                try:
                    v.stock = int(value)
                except (ValueError, TypeError):
                    return JsonResponse({'error': 'invalid'}, status=400)
                v.stock_manual = True
                v.save(update_fields=['stock', 'stock_manual'])
            return JsonResponse({'ok': True, 'stock': v.stock, 'stock_manual': v.stock_manual})
        elif field == 'low_stock_threshold':
            # value=None or '' → back to auto (Croston calculation)
            # value=number   → manual threshold, ignores both the sales rate and the history gate
            if value is None or value == '':
                v.low_stock_threshold_override = None
            else:
                try:
                    v.low_stock_threshold_override = max(0, int(value))
                except (ValueError, TypeError):
                    return JsonResponse({'error': 'invalid'}, status=400)
            v.save(update_fields=['low_stock_threshold_override'])
            return JsonResponse({'ok': True, 'low_stock_threshold_override': v.low_stock_threshold_override})
        elif field in ('price', 'stock'):
            # The same trap as with the bag: a raw JSON string ('12') in a model field is
            # coerced only when written to the DB, while computations in save/signals see a str.
            try:
                v.stock = int(value) if field == 'stock' else v.stock
                v.price = BulkSource._dec(value) if field == 'price' else v.price
            except (ValueError, TypeError):
                return JsonResponse({'error': 'invalid'}, status=400)
            v.save(update_fields=[field])
        return JsonResponse({'ok': True})
    except ProductVariant.DoesNotExist:
        return JsonResponse({'error': 'not found'}, status=404)


@staff_member_required
def variant_fresh_demand(request):
    """How many units of the variant have already been written off by fresh orders (new/processing) —
    to warn during a stock revision. Counts both direct lines and bundle components.
    Does not count: bag-based variants (derived stock) and dropshipping (nothing was written off
    from stock)."""
    vid = request.GET.get('id')
    if not vid or not str(vid).isdigit():
        return JsonResponse({'error': 'bad id'}, status=400)
    # Bag-based variant (not manual mode): stock is computed from BulkSource — we touch nothing.
    v = ProductVariant.objects.filter(pk=vid).values('bulk_source_id', 'stock_manual').first()
    if v is None or (v['bulk_source_id'] and not v['stock_manual']):
        return JsonResponse({'ok': True, 'held': 0})
    from orders.models import OrderItem, OrderItemBundleComponent
    FRESH = ('new', 'processing')

    def _pieces(consumed, nominal):
        # Units actually written off; legacy (stock_consumed IS NULL) — by nominal.
        return nominal if consumed is None else int(round(float(consumed)))

    held = 0
    # Direct lines (stock is kept; not dropshipping; piece-based write-off — bag_id IS NULL).
    direct = (OrderItem.objects
              .filter(product_variant_id=vid, bundle__isnull=True, stock_applied=True,
                      order__status__in=FRESH, stock_consumed_bag_id__isnull=True)
              .exclude(was_dropshipped=True)
              .values_list('quantity', 'stock_consumed'))
    for qty, consumed in direct:
        held += _pieces(consumed, qty)
    # Bundle components: the variant is a component of a bundle in a fresh order.
    comps = (OrderItemBundleComponent.objects
             .filter(product_variant_id=vid, stock_consumed_bag_id__isnull=True,
                     order_item__stock_applied=True, order_item__order__status__in=FRESH)
             .values_list('quantity', 'order_item__quantity', 'stock_consumed'))
    for cqty, oqty, consumed in comps:
        held += _pieces(consumed, (cqty or 0) * (oqty or 0))

    return JsonResponse({'ok': True, 'held': max(0, held)})


@staff_member_required
def save_bundle_field(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    data = json.loads(request.body)
    bid = data.get('id')
    field = data.get('field')
    value = data.get('value')
    allowed = {'price', 'stock', 'is_active'}
    if field not in allowed:
        return JsonResponse({'error': 'forbidden'}, status=403)
    try:
        b = Bundle.objects.get(pk=bid)
        if field == 'is_active':
            b.is_active = bool(value)
            b.save(update_fields=['is_active'])
            return JsonResponse({'ok': True})
        if field == 'price':
            from decimal import Decimal, InvalidOperation
            try:
                b.price = Decimal(str(value)) if value != '' else None
            except InvalidOperation:
                return JsonResponse({'error': 'invalid'}, status=400)
        elif field == 'stock':
            try:
                b.stock = int(value) if value != '' else None
            except (ValueError, TypeError):
                return JsonResponse({'error': 'invalid'}, status=400)
        b.save(update_fields=[field])
        return JsonResponse({'ok': True})
    except Bundle.DoesNotExist:
        return JsonResponse({'error': 'not found'}, status=404)


@staff_member_required
def save_brand_field(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    data = json.loads(request.body)
    bid = data.get('id')
    field = data.get('field')
    value = data.get('value')
    if field not in {'is_active'}:
        return JsonResponse({'error': 'forbidden'}, status=403)
    try:
        b = Brand.objects.get(pk=bid)
        b.is_active = bool(value)
        b.save(update_fields=[field])
        return JsonResponse({'ok': True})
    except Brand.DoesNotExist:
        return JsonResponse({'error': 'not found'}, status=404)


@staff_member_required
def save_category_field(request):
    """Instant toggle of is_active in the category list (the bottom submit-row is hidden,
    so the standard list_editable has nothing to save with — we save immediately on click)."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    data = json.loads(request.body)
    cid = data.get('id')
    field = data.get('field')
    value = data.get('value')
    if field not in {'is_active'}:
        return JsonResponse({'error': 'forbidden'}, status=403)
    try:
        c = Category.objects.get(pk=cid)
        c.is_active = bool(value)
        c.save(update_fields=[field])
        return JsonResponse({'ok': True})
    except Category.DoesNotExist:
        return JsonResponse({'error': 'not found'}, status=404)


@staff_member_required
def upload_bundle_photo(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    image = request.FILES.get('image')
    if not image:
        return JsonResponse({'error': 'no image'}, status=400)
    bundle_id = request.POST.get('bundle_id')
    temp_key = request.POST.get('temp_key', '').strip()
    kwargs = {'image': image}
    if bundle_id:
        kwargs['bundle_id'] = int(bundle_id)
    elif temp_key:
        kwargs['temp_key'] = temp_key
    else:
        return JsonResponse({'error': 'no bundle_id or temp_key'}, status=400)
    photo = BundleImage.objects.create(**kwargs)
    return JsonResponse({'id': photo.id, 'url': request.build_absolute_uri(photo.image.url)})


@staff_member_required
def get_bundle_photos(request):
    bundle_id = request.GET.get('bundle_id')
    if not bundle_id:
        return JsonResponse({'photos': []})
    photos = BundleImage.objects.filter(bundle_id=bundle_id).order_by('sort_order', 'pk')
    return JsonResponse({'photos': [{
        'id': p.id,
        'url': request.build_absolute_uri(p.image.url),
        'is_main': p.is_main,
        'is_hidden': p.is_hidden,
    } for p in photos]})


@staff_member_required
def delete_bundle_photo(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    data = json.loads(request.body)
    BundleImage.objects.filter(pk=data.get('id')).delete()
    return JsonResponse({'ok': True})


@staff_member_required
def replace_bundle_photo(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    photo_id = request.POST.get('photo_id')
    image = request.FILES.get('image')
    if not photo_id or not image:
        return JsonResponse({'error': 'missing data'}, status=400)
    photo = BundleImage.objects.get(pk=int(photo_id))
    photo.image.delete(save=False)
    photo.image = image
    photo.save()
    return JsonResponse({'url': request.build_absolute_uri(photo.image.url)})


def _auto_promote_bundle_main_photo(bundle_id):
    """The same auto-pick of a new main photo as _auto_promote_main_photo
    for products, but simpler — no variants (priority of BundleListSerializer
    _pick_main_image: sort_order, pk)."""
    if not bundle_id:
        return None
    visible = list(BundleImage.objects.filter(bundle_id=bundle_id, is_hidden=False))
    if not visible or any(i.is_main for i in visible):
        return None
    candidate = min(visible, key=lambda i: (i.sort_order, i.pk))
    candidate.is_main = True
    candidate.save(update_fields=['is_main'])
    return candidate.pk


@staff_member_required
def toggle_bundle_photo_hidden(request):
    """The «eye» toggle in pg-actions — only is_hidden (and is_main, if hiding),
    like toggle_hidden in set_photo_assignment for products."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    data = json.loads(request.body)
    try:
        photo = BundleImage.objects.get(pk=data.get('id'))
    except BundleImage.DoesNotExist:
        return JsonResponse({'error': 'Not found'}, status=404)
    was_main = photo.is_main
    photo.is_hidden = not photo.is_hidden
    if photo.is_hidden:
        photo.is_main = False
    photo.save(update_fields=['is_hidden', 'is_main'])
    promoted_id = _auto_promote_bundle_main_photo(photo.bundle_id) if (was_main and photo.is_hidden) else None
    return JsonResponse({'ok': True, 'is_hidden': photo.is_hidden, 'is_main': photo.is_main, 'promoted_main_id': promoted_id})


@staff_member_required
def reorder_bundle_photos(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    data = json.loads(request.body)
    for item in data.get('photos', []):
        BundleImage.objects.filter(pk=item['id']).update(sort_order=item['sort_order'])
    return JsonResponse({'ok': True})


@staff_member_required
def get_bundle_temp_photos(request):
    temp_key = request.GET.get('temp_key', '').strip()
    if not temp_key:
        return JsonResponse({'photos': []})
    photos = BundleImage.objects.filter(temp_key=temp_key).order_by('sort_order')
    return JsonResponse({'photos': [{'id': p.id, 'url': request.build_absolute_uri(p.image.url)} for p in photos]})



@staff_member_required
def revert_history(request, log_id):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)

    from django.contrib.admin.models import LogEntry, DELETION, ADDITION
    try:
        entry = LogEntry.objects.select_related('content_type').get(pk=log_id)
    except LogEntry.DoesNotExist:
        return JsonResponse({'error': 'Record not found'}, status=404)

    if entry.action_flag == ADDITION:
        return JsonResponse({'error': 'Rolling back an addition is not supported'}, status=400)

    try:
        msgs = json.loads(entry.change_message) if entry.change_message else []
    except (json.JSONDecodeError, TypeError):
        msgs = []

    warnings = []

    if entry.action_flag == DELETION:
        snap = next((m.get('deletion_snapshot') for m in msgs if 'deletion_snapshot' in m), None)
        if not snap:
            return JsonResponse(
                {'error': 'There is no data to restore — this event predates the rollback feature.'},
                status=400
            )
        ok, warnings = restore_deleted(entry, snap)
        if not ok:
            return JsonResponse({'error': warnings[0] if warnings else 'Restore error'}, status=400)
        msg = f'Object "{entry.object_repr}" restored.'

    else:  # CHANGE
        snap = next((m.get('revert') for m in msgs if 'revert' in m), None)
        if not snap:
            return JsonResponse(
                {'error': 'There is no data to roll back — this event predates the rollback feature.'},
                status=400
            )
        model_class = entry.content_type.model_class()
        if model_class is None:
            return JsonResponse({'error': 'Model not found'}, status=400)

        try:
            obj = model_class.objects.get(pk=entry.object_id)
        except model_class.DoesNotExist:
            cascade = request.GET.get('cascade') == '1'
            # Find the record of this object's deletion
            del_entry = LogEntry.objects.filter(
                content_type=entry.content_type,
                object_id=entry.object_id,
                action_flag=DELETION,
            ).order_by('-action_time').first()

            if not cascade:
                if del_entry:
                    try:
                        del_msgs = json.loads(del_entry.change_message) if del_entry.change_message else []
                    except Exception:
                        del_msgs = []
                    del_snap = next((m.get('deletion_snapshot') for m in del_msgs if 'deletion_snapshot' in m), None)
                    if del_snap:
                        return JsonResponse({
                            'needs_cascade': True,
                            'id': log_id,
                            'message': (
                                f'Object "{entry.object_repr}" was deleted. '
                                f'To roll back this change, the object must be restored first. Continue?'
                            ),
                        }, status=409)
                return JsonResponse(
                    {'error': f'Object "{entry.object_repr}" was deleted and there is no data to restore it.'},
                    status=400
                )

            # cascade=1: restore the object first
            if not del_entry:
                return JsonResponse({'error': 'Deletion record not found'}, status=400)
            try:
                del_msgs = json.loads(del_entry.change_message) if del_entry.change_message else []
            except Exception:
                del_msgs = []
            del_snap = next((m.get('deletion_snapshot') for m in del_msgs if 'deletion_snapshot' in m), None)
            if not del_snap:
                return JsonResponse({'error': 'There is no data to restore the object'}, status=400)
            ok, w = restore_deleted(del_entry, del_snap)
            if not ok:
                return JsonResponse({'error': f'Could not restore the object: {w[0]}'}, status=400)
            warnings.extend(w)
            try:
                obj = model_class.objects.get(pk=entry.object_id)
            except model_class.DoesNotExist:
                return JsonResponse({'error': 'Object restore error'}, status=400)

        ok, w = apply_snapshot(obj, snap)
        warnings.extend(w)
        if not ok:
            return JsonResponse({'error': warnings[0] if warnings else 'Rollback error'}, status=400)
        msg = f'Changes to "{entry.object_repr}" rolled back.'

    if warnings:
        msg += ' Warnings: ' + '; '.join(warnings)

    # Record the rollback in the log
    from django.contrib.admin.models import CHANGE, LogEntry
    LogEntry.objects.create(
        user_id=request.user.pk,
        content_type_id=entry.content_type_id,
        object_id=entry.object_id,
        object_repr=entry.object_repr,
        action_flag=CHANGE,
        change_message=json.dumps([{'reverted': {'log_id': log_id, 'original_action': entry.action_flag}}]),
    )

    return JsonResponse({'ok': True, 'message': msg, 'warnings': warnings})


@staff_member_required
def upload_brand_photo(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    image = request.FILES.get('image')
    if not image:
        return JsonResponse({'error': 'No image'}, status=400)
    brand_id = request.POST.get('brand_id')
    temp_key = request.POST.get('temp_key')
    obj = BrandImage(image=image)
    if brand_id:
        obj.brand_id = brand_id
    elif temp_key:
        obj.temp_key = temp_key
    else:
        return JsonResponse({'error': 'No target'}, status=400)
    obj.save()
    return JsonResponse({'id': obj.pk, 'url': obj.image.url})


@staff_member_required
def get_brand_photos(request):
    brand_id = request.GET.get('brand_id', '').strip()
    if not brand_id:
        return JsonResponse({'photos': []})
    photos = list(BrandImage.objects.filter(brand_id=brand_id).order_by('sort_order', 'pk'))
    if not photos:
        try:
            brand = Brand.objects.get(pk=brand_id)
            if brand.logo:
                img = BrandImage.objects.create(brand=brand, image=brand.logo.name)
                photos = [img]
        except Brand.DoesNotExist:
            pass
    return JsonResponse({'photos': [{'id': p.pk, 'url': p.image.url} for p in photos]})


@staff_member_required
def delete_brand_photo(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    data = json.loads(request.body)
    photo_id = data.get('id')
    try:
        photo = BrandImage.objects.get(pk=photo_id)
        photo.image.delete(save=False)
        photo.delete()
        return JsonResponse({'ok': True})
    except BrandImage.DoesNotExist:
        return JsonResponse({'error': 'Not found'}, status=404)


@staff_member_required
def replace_brand_photo(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    photo_id = request.POST.get('photo_id')
    image = request.FILES.get('image')
    if not photo_id or not image:
        return JsonResponse({'error': 'Missing data'}, status=400)
    try:
        photo = BrandImage.objects.get(pk=photo_id)
        photo.image.delete(save=False)
        photo.image = image
        photo.save()
        return JsonResponse({'ok': True, 'url': photo.image.url})
    except BrandImage.DoesNotExist:
        return JsonResponse({'error': 'Not found'}, status=404)


@staff_member_required
def reorder_brand_photos(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    data = json.loads(request.body)
    for i, photo_id in enumerate(data.get('order', [])):
        BrandImage.objects.filter(pk=photo_id).update(sort_order=i)
    return JsonResponse({'ok': True})


def _load_prompt(name):
    try:
        path = settings.BASE_DIR / 'catalog' / 'prompts' / name
        return open(path, encoding='utf-8').read()
    except Exception:
        return ''


def _match_bundle_items_to_variants(items, gemini_client):
    """For each {name, qty} pick the best ProductVariant in the DB by cosine
    similarity of embeddings.

    Catalog name vectors come from the DB index (core.embeddings) — the free Gemini quota
    doesn't allow computing them on the fly across a thousand variants. We embed only
    the queries themselves here — that's a handful of calls.
    """
    if not items:
        return []
    from core import embeddings as emb
    from .models import ProductImage

    variants = list(ProductVariant.objects.select_related('product').only(
        'id', 'name', 'sku', 'price', 'stock', 'product_id'
    ).order_by('id'))
    if not variants:
        return [dict(it, variant_id=None, similarity=0.0, _err='') for it in items]

    db_matrix, missing = emb.variant_matrix(gemini_client, [v.id for v in variants])
    index_err = ''
    if missing:
        index_err = (
            f'The name index is incomplete: {missing} of {len(variants)} variants are not '
            f'indexed yet. Run "python manage.py warm_embeddings".'
        )
    if missing == len(variants):
        return [dict(it, variant_id=None, similarity=0.0, _err=index_err) for it in items]

    try:
        # A couple of short retries: there are only a few requests, but the shared free pool
        # sometimes refuses out of the blue. Waiting 8 seconds on the button is more
        # acceptable than "stock not matched".
        query_vecs = emb.embed_texts(
            gemini_client, [it['name'] for it in items], 'RETRIEVAL_QUERY',
            retries=2, retry_wait=4,
        )
        query_err = ''
    except Exception as e:
        query_vecs = None
        query_err = str(e)[:200]

    def _photo_url(variant):
        img = ProductImage.objects.filter(
            product_id=variant.product_id
        ).order_by('sort_order', 'id').first()
        if img and img.image:
            try:
                return img.image.url
            except Exception:
                return ''
        return ''

    def _payload(variant):
        return {
            'variant_id': variant.id,
            'product_id': variant.product_id,
            'name': variant.name,
            'sku': variant.sku or '',
            'price': str(variant.price) if variant.price is not None else '',
            'stock': variant.stock or 0,
            'photo': _photo_url(variant),
            'packaging_kind': variant.product.packaging_kind if variant.product_id else '',
        }

    out = []
    for i, it in enumerate(items):
        best = None
        score = 0.0
        err = query_err or index_err
        if query_vecs is not None:
            sims = emb.cosine(query_vecs[i], db_matrix)
            best_idx = int(sims.argmax())
            score = float(sims[best_idx])
            # A zero row (a variant with no vector) gives exactly 0 — that's not a match
            # but emptiness: better 'not found' than a random first variant.
            if score > 0:
                best = variants[best_idx]

        paired = None
        if best:
            # We take exactly the variant that won the cosine. The paired packaging (if one exists)
            # is returned alongside as an alternative — a human chooses, not the machine.
            full = (ProductVariant.objects
                    .select_related('product', 'paired_variant__product')
                    .filter(pk=best.pk).first())
            if full:
                best = full
                partner = best.paired_variant if best.paired_variant_id else None
                paired = _payload(partner) if partner else None

        row = {
            'name': it['name'],
            'qty': it['qty'],
            'variant_id': None,
            'product_id': None,
            'variant_name': '',
            'sku': '',
            'price': '',
            'stock': 0,
            'photo': '',
            'packaging_kind': '',
            'paired': None,
            'similarity': round(score, 3),
            '_err': err,
        }
        if best:
            # `name` in the response is the raw name from the text, so we assign the fields one by
            # one
            # rather than update()-ing the whole payload.
            p = _payload(best)
            row.update({
                'variant_id': p['variant_id'],
                'product_id': p['product_id'],
                'variant_name': p['name'],
                'sku': p['sku'],
                'price': p['price'],
                'stock': p['stock'],
                'photo': p['photo'],
                'packaging_kind': p['packaging_kind'],
                'paired': paired,
            })
        out.append(row)
    return out


@staff_member_required
def generate_description(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    data = json.loads(request.body)
    text = data.get('text', '').strip()
    prompt_key = data.get('prompt', 'product')
    object_id = data.get('object_id')  # product id, to mark it as 'processed by AI'
    if not text:
        return JsonResponse({'error': 'No text'}, status=400)
    prompt_files = {
        'product': 'product.txt',
        'description': 'description.txt',
        'characteristics': 'characteristics.txt',
        'application_norms': 'application_norms.txt',
        'edit': 'edit.txt',
        'clean': 'clean.txt',
        'extract': 'extract_blocks.txt',
        'bundle_items': 'bundle_items.txt',
    }
    prompt = _load_prompt(prompt_files.get(prompt_key, 'product.txt'))

    # For bundles, dynamically inject the list of real categories from the DB
    # so the LLM picks one or more of the categories that actually exist.
    db_categories = []
    if prompt_key == 'bundle_items':
        db_categories = list(
            Category.objects.values('id', 'name', 'parent_id').order_by('name')
        )
        cat_lines = []
        for c in db_categories:
            cat_lines.append('- ' + c['name'])
        prompt = (
            prompt
            + '\n\n═══════════════════════════════════════════════════\n'
            + 'AVAILABLE CATEGORIES (choose ONE or SEVERAL exactly as written):\n'
            + '═══════════════════════════════════════════════════\n'
            + '\n'.join(cat_lines)
            + '\n\nReturn the "category" field as an ARRAY of strings (even if there is only one).'
        )

    # Photos in a bundle draft: illustrations stay, tables and text from photos become
    # text for the model (see description_images.py). Without this a base64 photo would
    # go to the model as is, and the response would lose it.
    prepared = None
    if prompt_key == 'bundle_items':
        from . import description_images
        try:
            prepared = description_images.prepare(text)
        except description_images.DescriptionImageError as e:
            return JsonResponse({'error': str(e)}, status=500)
        text = prepared.text
        if prepared.drafts and not description_images.has_text(text):
            return JsonResponse({'error': 'Nothing is left in the text after processing the photos'}, status=400)

    try:
        from core import llm
        try:
            data = llm.text_to_json(f"{prompt}\n\n---\n\n{text}")
        except Exception as e:
            return JsonResponse({'error': f'Generation error: {e}'}, status=500)
        # Any successful magic button marks the product as processed by AI
        # (the frontend uses this flag to ask for confirmation before a re-run).
        if object_id:
            from .models import Product
            Product.objects.filter(pk=object_id).update(llm_processed=True)
        try:
            with open(settings.BASE_DIR / 'llm_last_output.txt', 'w', encoding='utf-8') as f:
                f.write(json.dumps(data, ensure_ascii=False, indent=2))
        except Exception:
            pass

        if 'html' in data:
            return JsonResponse({'html': data['html']})
        # Bundle items extraction: return {name, items[], category}.
        # We do NOT change the description in the editor.
        if prompt_key == 'bundle_items':
            items = data.get('items') or []
            cleaned = []
            for it in items:
                if not isinstance(it, dict):
                    continue
                iname = (it.get('name') or '').strip()
                if not iname:
                    continue
                qty = it.get('qty', 1)
                try:
                    qty = max(1, int(qty))
                except (TypeError, ValueError):
                    qty = 1
                cleaned.append({'name': iname, 'qty': qty})
            # Matching bundle products to variants goes through Gemini embeddings
            # (Claude has no embedding model), hence the separate genai client here.
            from google import genai
            client = genai.Client(api_key=settings.GEMINI_API_KEY)
            matched = _match_bundle_items_to_variants(cleaned, client)
            # An embeddings failure (429 Gemini quota / network) used to look like
            # 'product not found': the manager saw an empty stock and did not understand
            # why. Now the reason is sent to the frontend in a separate field.
            match_error = next((it.get('_err') for it in matched if it.get('_err')), '')
            # Categories
            raw_cats = data.get('category')
            if isinstance(raw_cats, str):
                raw_cats = [raw_cats] if raw_cats.strip() else []
            elif not isinstance(raw_cats, list):
                raw_cats = []
            cat_by_name = {c['name'].strip().lower(): c for c in db_categories}
            matched_categories = []
            for nm in raw_cats:
                if not isinstance(nm, str):
                    continue
                key = nm.strip().lower()
                if key in cat_by_name:
                    c = cat_by_name[key]
                    matched_categories.append({'id': c['id'], 'name': c['name'], 'parent_id': c.get('parent_id')})
            # Drop parent categories if at least one of their children is already
            # in the list: the system picks up the parent automatically.
            child_parents = {c['parent_id'] for c in matched_categories if c.get('parent_id')}
            matched_categories = [
                c for c in matched_categories if c['id'] not in child_parents
            ]
            # parent_id is unnecessary for the frontend
            matched_categories = [{'id': c['id'], 'name': c['name']} for c in matched_categories]

            # Does a Bundle with this general_name already exist? ONLY an exact name match
            # inside the «...» quotes, with no broad icontains fallback, which caught
            # any partial overlap and gave a false duplicate warning.
            general_name = (data.get('general_name') or '').strip()
            existing_bundle = None
            if general_name:
                from .models import Bundle
                first = Bundle.objects.filter(name__icontains=f'«{general_name}»').first()
                if first:
                    existing_bundle = {
                        'id': first.id,
                        'name': first.name,
                        'url': f'/admin/catalog/bundle/{first.id}/change/',
                    }

            description, application_norms, image_notes = description_images.finish(
                prepared,
                (data.get('description') or '').strip(),
                (data.get('application_norms') or '').strip(),
            )
            return JsonResponse({
                'general_name': general_name,
                'name': (data.get('name') or '').strip(),
                'variant_name': (data.get('variant_name') or '').strip(),
                'items': matched,
                'match_error': match_error,
                'categories': matched_categories,
                'existing_bundle': existing_bundle,
                'description': description,
                'application_norms': application_norms,
                'image_notes': image_notes,
            })
        chars = data.get('characteristics', []) or []
        comp = data.get('composition', []) or []
        if len(chars) < 2:
            chars = []
        if len(comp) < 2:
            comp = []
        return JsonResponse({
            'description_html': data.get('description', ''),
            'characteristics': chars,
            'composition': comp,
            'application_norms_html': data.get('application_norms', ''),
        })
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


@staff_member_required
def run_parse_dropshipping(request):
    """Runs sync_dropshipping (idempotent sync with bradas.ua).
    The view name is kept for historical compatibility, so links don't need to change."""
    from django.contrib import messages
    from django.shortcuts import redirect
    from django.core.management import call_command
    from io import StringIO
    if request.method != 'POST':
        from django.http import HttpResponseNotAllowed
        return HttpResponseNotAllowed(['POST'])
    out = StringIO()
    try:
        call_command('sync_dropshipping', skip_fallback=True, stdout=out)
        msg = 'Synchronization with bradas.ua is complete. The events are in "Sync events".'
        messages.success(request, msg)
    except Exception as e:
        messages.error(request, f'Import error: {e}')
    return redirect('/admin/catalog/dropshippingvariant/')




@staff_member_required
def bundles_data_view(request):
    """Returns a flat list of Bundles (one row = one bundle).

    The fields variant_id/multi/multi_count are kept for compatibility with the old JS in
    templates/admin/catalog/bundle/change_list.html: variant_id == id,
    multi is always False (multi-variant bundles no longer exist).
    """
    from django.utils import timezone as tz
    from django.conf import settings as conf

    bundles_qs = list(Bundle.objects.only(
        'id', 'name', 'sku', 'price', 'stock', 'discount_percent', 'discount_amount',
        'is_active', 'created_at', 'updated_at'
    ).order_by('-created_at'))
    bundle_ids = [b.id for b in bundles_qs]
    bundles_by_id = {b.id: b for b in bundles_qs}

    # categories
    cats_map = {}
    for row in Bundle.categories.through.objects.filter(
        bundle_id__in=bundle_ids
    ).select_related('category').values('bundle_id', 'category__name'):
        cats_map.setdefault(row['bundle_id'], []).append(row['category__name'])

    # items per bundle
    items_by_variant = {}
    for row in (BundleItem.objects
                .filter(bundle_id__in=bundle_ids)
                .values('bundle_id', 'quantity',
                        'product_variant_id',
                        'product_variant__price',
                        'product_variant__stock',
                        'product_variant__sku',
                        'product_variant__name',
                        'product_variant__product_id',
                        'product_variant__product__name',
                        'product_variant__product__packaging_kind')):
        items_by_variant.setdefault(row['bundle_id'], []).append(row)

    # Bundle labels are the bundle's OWN labels (Bundle.labels), NOT inherited from the
    # component products. A bundle is "Hit" only if the bundle ITSELF is a hit (khit is
    # set by recompute_khit from its own sales); "Promo" is added by the
    # auto_apply_bundle_aktsia signal based on an actual discount.
    labels_map = {}
    for row in (Bundle.labels.through.objects
                .filter(bundle_id__in=bundle_ids)
                .select_related('label')
                .values('bundle_id', 'label__name', 'label__color')):
        labels_map.setdefault(row['bundle_id'], []).append({
            'name': row['label__name'],
            'color': row['label__color'] or '',
        })

    bundle_photos = {}
    for img in (BundleImage.objects
                .filter(bundle_id__in=bundle_ids)
                .order_by('bundle_id', 'sort_order', 'id')
                .values('bundle_id', 'image')):
        lst = bundle_photos.setdefault(img['bundle_id'], [])
        if len(lst) < 3:
            lst.append(conf.MEDIA_URL + img['image'])

    # variant thumbs (ProductVariant in BundleItem) for items_search
    all_pv_ids = list({row['product_variant_id']
                       for rows in items_by_variant.values() for row in rows})
    variant_thumb = {}
    for img in (ProductImage.objects
                .filter(variant_id__in=all_pv_ids)
                .order_by('variant_id', 'sort_order')
                .values('variant_id', 'image')):
        if img['variant_id'] not in variant_thumb:
            variant_thumb[img['variant_id']] = conf.MEDIA_URL + img['image']
    no_photo = [vid for vid in all_pv_ids if vid not in variant_thumb]
    if no_photo:
        pid_map = dict(ProductVariant.objects.filter(id__in=no_photo).values_list('id', 'product_id'))
        prod_thumb = {}
        for img in (ProductImage.objects
                    .filter(product_id__in=pid_map.values(), variant__isnull=True)
                    .order_by('product_id', 'sort_order')
                    .values('product_id', 'image')):
            if img['product_id'] not in prod_thumb:
                prod_thumb[img['product_id']] = conf.MEDIA_URL + img['image']
        for vid in no_photo:
            pid = pid_map.get(vid)
            if pid and pid in prod_thumb:
                variant_thumb[vid] = prod_thumb[pid]

    # Build result — flat list per bundle (variant_id == id, kept for
    # compatibility with the old JS of the change_list.html template)
    result = []
    for b in bundles_qs:
        items = items_by_variant.get(b.id, [])
        auto_price = sum(float(i['product_variant__price'] or 0) * i['quantity'] for i in items)
        stocks = [i['product_variant__stock'] // i['quantity']
                  for i in items if i['quantity'] and i['product_variant__stock'] is not None]
        auto_stock = min(stocks) if stocks else 0

        all_cats = cats_map.get(b.id, [])
        # send all categories; the frontend decides how many fit
        def _full_name(i):
            vn = i['product_variant__name'] or ''
            return vn or (i['product_variant__product__name'] or '')
        items_search = [{
            'name': _full_name(i).lower(),
            'sku': (i['product_variant__sku'] or '').lower(),
            'display_name': _full_name(i),
            'display_sku': i['product_variant__sku'] or '',
            'stock': i['product_variant__stock'] or 0,
            'quantity': i['quantity'],
            'photo': variant_thumb.get(i['product_variant_id'], ''),
            'product_id': i['product_variant__product_id'],
            'variant_id': i['product_variant_id'],
            'is_own': i['product_variant__product__packaging_kind'] == 'own',
        } for i in items]

        photos = bundle_photos.get(b.id, [])

        result.append({
            # bundle-level (for grouping)
            'bundle_id': b.id,
            'id': b.id,            # for compatibility with old code
            'bundle_name': b.name or '',
            'bundle_sku': b.sku or '',
            'multi': False,
            'multi_count': 1,
            'categories': all_cats,

            # variant-level (== bundle-level: a bundle is a flat entity)
            'variant_id': b.id,
            'name': b.name or '',
            'sku': b.sku or '',
            'labels': labels_map.get(b.id, []),
            'photos': photos,
            'items_search': items_search,
            'price_manual': b.price is not None,
            'price': str(b.price) if b.price is not None else '',
            'auto_price': '{:.2f}'.format(auto_price),
            'discount_percent': str(b.discount_percent).rstrip('0').rstrip('.') if b.discount_percent else '',
            'discount_amount': str(b.discount_amount).rstrip('0').rstrip('.') if b.discount_amount else '',
            'stock_manual': b.stock is not None,
            'stock': b.stock,
            'auto_stock': auto_stock,
            'is_active': b.is_active,
            'created_at': tz.localtime(b.created_at).strftime('%d.%m.%Y %H:%M') if b.created_at else '',
            'updated_at': tz.localtime(b.updated_at).strftime('%d.%m.%Y %H:%M') if b.updated_at else '',
        })

    return JsonResponse(result, safe=False)


def _notification_steps(section, ids):
    """{object_id: step} for the section; 0 by default."""
    from .models import NotificationState
    if not ids:
        return {}
    return dict(NotificationState.objects
                .filter(section=section, object_id__in=ids)
                .values_list('object_id', 'step'))


def _safe_json(data):
    """JSON for embedding into an inline <script>. json.dumps does not escape
    '<', '>', '&', so useful input (reviews/questions) could break out of the
    string context via </script> and execute as XSS in the manager's session.
    We escape it the way django's json_script does."""
    return (json.dumps(data, ensure_ascii=False)
            .replace('<', '\\u003c')
            .replace('>', '\\u003e')
            .replace('&', '\\u0026')
            .replace(' ', '\\u2028')
            .replace(' ', '\\u2029'))


def _variant_photo(v):
    """First photo of the packaging, otherwise of the product. Reads the 'images'/'product__images'
prefetch."""
    try:
        imgs = list(v.images.all())
        if imgs and imgs[0].image:
            return imgs[0].image.url
        if v.product_id:
            pimgs = list(v.product.images.all())
            if pimgs and pimgs[0].image:
                return pimgs[0].image.url
    except Exception:
        pass
    return ''


def _variant_title(v):
    base = v.product.name if v.product_id else ''
    vname = v.name or ''
    if not base or not vname:
        return base or vname
    # The packaging name usually already contains the product name ("Salto" + "Salto,
    # 100 ml") or equals it up to an asterisk/case, so there is
    # no point showing the same thing twice.
    def norm(t):
        return t.strip().strip('*').casefold()
    nb, nv = norm(base), norm(vname)
    if nb == nv or nb in nv:
        return vname
    if nv in nb:
        return base
    return f'{base}, {vname}'


def _stock_demand_items(subs, steps):
    """"Subscriptions" section by item: for each packaging of a product or bundle,
    the pending subscriptions and likes ("Favorites", Favorites) on unavailable items.

    An item with subscriptions is always shown (the email is waiting for exactly the subscriber).
    Likes count only while the item is not on the storefront
    (catalog.notifications.unavailable_likes):
    a like on something that can be bought is a shopping list, not demand for a restock."""
    from collections import defaultdict
    from .notifications import unavailable_likes

    def _dt(dt):
        return timezone.localtime(dt).strftime('%d.%m.%Y %H:%M')

    subs_by = defaultdict(list)
    for s in subs:
        subs_by[f'v{s.variant_id}' if s.variant_id else f'b{s.bundle_id}'].append(s)
    likes_by = unavailable_likes()

    keys = set(subs_by) | set(likes_by)
    variants = {
        v.pk: v for v in ProductVariant.objects
        .filter(pk__in=[int(k[1:]) for k in keys if k[0] == 'v'])
        .select_related('product').prefetch_related('images', 'product__images')
    }
    bundles = {
        b.pk: b for b in Bundle.objects
        .filter(pk__in=[int(k[1:]) for k in keys if k[0] == 'b'])
        .prefetch_related('images')
    }

    items = []
    for key in keys:
        if key[0] == 'v':
            v = variants.get(int(key[1:]))
            if v is None:
                continue
            target, sku, photo = _variant_title(v), v.sku or '', _variant_photo(v)
            url = f'/admin/catalog/product/{v.product_id}/change/#v{v.pk}'
        else:
            b = bundles.get(int(key[1:]))
            if b is None:
                continue
            target = b.name
            sku = b.sku or ''
            img = next(iter(b.images.all()), None)
            photo = img.image.url if img and img.image else ''
            url = f'/admin/catalog/bundle/{b.pk}/change/'
        pending = subs_by.get(key, [])
        likes = likes_by.get(key, [])
        items.append({
            'key': key,
            'kind': 'bundle' if key[0] == 'b' else 'variant',
            'target': target,
            'sku': sku,
            'photo': photo,
            'target_url': url,
            'subs': [{'id': s.id, 'email': s.email, 'step': steps.get(s.id, 0), 'date': _dt(s.created_at)}
                     for s in pending],
            'likes': [{'ids': r['ids'], 'who': r['who'], 'step': r['step'], 'date': _dt(r['at'])} for r in likes],
            '_last': max([s.created_at for s in pending] + [r['at'] for r in likes]),
        })
    # A more recent interaction (subscription or like) goes higher: the order matches the "Date"
    # column.
    items.sort(key=lambda it: it['_last'], reverse=True)
    for it in items:
        del it['_last']
    return items


@staff_member_required
def notifications_view(request, section):
    """"Notifications" page, one section per page.

    section ∈ {payments, reviews-products, reviews-site, stock, dropsync}.
    Reviews have two separate sections: about products and about the site (previously sub-tabs).
    """
    valid_sections = {'payments', 'requisites', 'reviews-products', 'reviews-site', 'stock', 'stock_out', 'stock_low', 'dropsync'}
    # Legacy: /admin/notifications/questions/ → redirect to reviews-site.
    if section == 'questions':
        from django.shortcuts import redirect
        return redirect('/admin/notifications/reviews-site/')
    # Legacy: /admin/notifications/reviews/ → redirect to reviews-products ("Product reviews").
    if section == 'reviews':
        from django.shortcuts import redirect
        return redirect('/admin/notifications/reviews-products/')
    if section not in valid_sections:
        from django.http import Http404
        raise Http404(f'Unknown section: {section}')
    # Feature flag: DROPSYNC_ENABLED=False hides the section from the UI entirely.
    from django.conf import settings as _settings
    if section == 'dropsync' and not getattr(_settings, 'DROPSYNC_ENABLED', True):
        from django.http import Http404
        raise Http404('The section is temporarily disabled')

    from orders.models import Order, format_phone
    from orders.admin import _order_item_data
    from .models import NotificationState as _NSP
    # step 1/2 (done/dismissed) stay visible; step 3 (deleted) does not.
    _payments_processed_ids = list(
        _NSP.objects.filter(section='payments', step__in=(1, 2)).values_list('object_id', flat=True)
    )
    payments = (
        Order.objects
        .filter(payment_method='bank_transfer')   # bank transfer payments only
        .filter(
            Q(payment_notification=True)
            | Q(bank_matches__transaction__status='manual_review',
                bank_matches__is_active=False)
            | Q(id__in=_payments_processed_ids)
        )
        .exclude(is_imported=True)   # OLD (historical) orders are not shown in "Payments"
        .distinct()
        .prefetch_related(
            'items__product_variant__images',
            'items__product_variant__product__images',
            # _variant_images_cached (via _order_item_data → _variant_photo) reads
            # variant.attribute_values.all() + av.attribute. Without this prefetch
            # it is an N+1 on EVERY packaging in every order item (measured: 682
            # items → 924+629 extra queries, ~1.4s of extra page time).
            'items__product_variant__attribute_values__attribute',
            'items__bundle__images',
            'items__bundle__items__product_variant__images',
            'items__bundle__items__product_variant__product__images',
            'items__bundle__items__product_variant__attribute_values__attribute',
            'items__bundle_components__product_variant__images',
            'items__bundle_components__product_variant__product__images',
            'items__bundle_components__product_variant__attribute_values__attribute',
            'bank_matches__transaction__account',
        )
        .order_by('-updated_at')
    )
    import os
    def _receipt_info(o):
        if not o.payment_screenshot:
            return {'url': '', 'name': '', 'kind': '', 'ext': ''}
        url = o.payment_screenshot.url
        name = os.path.basename(o.payment_screenshot.name)
        ext = os.path.splitext(name)[1].lower().lstrip('.')
        if ext in ('jpg', 'jpeg', 'png', 'webp', 'gif', 'bmp', 'heic'):
            kind = 'image'
        elif ext == 'pdf':
            kind = 'pdf'
        else:
            kind = ''
        return {'url': url, 'name': name, 'kind': kind, 'ext': ext}

    def _tx_data(tx):
        if tx is None:
            return None
        return {
            'id': tx.id,
            'source': tx.source,
            'source_display': tx.get_source_display(),
            'account': tx.account.name if tx.account_id else '',
            'amount': str(tx.amount),
            'currency_code': tx.currency_code,
            'transacted_at': timezone.localtime(tx.transacted_at).strftime('%d.%m.%Y %H:%M') if tx.transacted_at else '',
            'counterparty': tx.counterparty_name or '',
            'description': tx.description or '',
            'comment': tx.comment or '',
            'status': tx.status,
        }

    def _match_data(m):
        return {
            'match_id': m.id,
            'transaction': _tx_data(m.transaction),
            'confidence': m.confidence,
            'reasons': m.reasons or [],
            'is_active': m.is_active,
            'auto_confirmed': m.auto_confirmed,
            'confirmed_at': timezone.localtime(m.confirmed_at).strftime('%d.%m.%Y %H:%M') if m.confirmed_at else '',
        }

    def _has_mismatch(reasons):
        return any(r.get('type') == 'order_number_amount_mismatch' for r in (reasons or []))

    def _compute_state(o, active_match, pending_matches):
        if active_match is not None:
            return 'paid_auto' if active_match.auto_confirmed else 'paid'
        if o.payment_status == 'paid':
            return 'paid'
        if pending_matches:
            best = pending_matches[0]
            if _has_mismatch(best.reasons):
                return 'mismatch'
            return 'pending_confirm'
        return 'pending'

    # The heavy block (deep prefetch over bundle/variant for every order item)
    # runs ONLY for the active section: we iterate an empty list instead of
    # payments when that section is not active. The other pages ("Low stock",
    # "Out of stock", etc.) would otherwise pay for it every time for nothing.
    payments_steps = _notification_steps('payments', [o.id for o in payments]) if section == 'payments' else {}
    payments_data = []
    for o in (payments if section == 'payments' else []):
        matches = list(o.bank_matches.all())
        active_match = next((m for m in matches if m.is_active), None)
        # For the manual queue: only unresolved candidates, sorted by confidence.
        pending = sorted(
            [m for m in matches if not m.is_active and m.transaction.status == 'manual_review'],
            key=lambda m: m.confidence, reverse=True,
        )
        state = _compute_state(o, active_match, pending)
        step = payments_steps.get(o.id, 0)
        # The payment status changed ON ITS OWN (bank_matcher / screenshot OCR): we add
        # the row to the list but without a notification: no "new" marker, the badge doesn't
        # grow
        # (mirrors auto_paid in notifications.get_notification_counts).
        # If candidates for manual review remain, we keep the notification.
        if step == 0 and state == 'paid_auto' and not pending:
            step = 1
        payments_data.append({
            'id': o.id,
            'order_number': o.order_number,
            'customer': o.customer_name,
            'phone': format_phone(o.customer_phone),
            'email': o.customer_email,
            'total': str(o.total_price) if o.total_price else '',
            'items': [_order_item_data(it, request) for it in o.items.all()],
            'payment_status': o.payment_status,
            'payment_method': o.payment_method,
            'receipt': _receipt_info(o),
            'order_url': f'/admin/orders/order/{o.id}/change/',
            'updated_at': timezone.localtime(o.updated_at).strftime('%d.%m.%Y %H:%M') if o.updated_at else '',
            'created_at': timezone.localtime(o.created_at).strftime('%d.%m.%Y %H:%M') if o.created_at else '',
            'payment_changed_at': timezone.localtime(o.payment_status_changed_at).strftime('%d.%m.%Y %H:%M') if o.payment_status_changed_at else '',
            'state': state,
            'active_match': _match_data(active_match) if active_match else None,
            'pending_matches': [_match_data(m) for m in pending],
            'step': step,
        })

    # ── "Payment details": orders where the customer asked for the details to be sent
    # to their phone ──
    # The queue's composition is requisites_orders() (shared source with the counter): cancelled
    # and re-placed-and-paid rows are not shown at all. Paid ones stay as history
    # but fade out on their own: step 1 below, with no «new» marker and no badge.
    from .notifications import requisites_orders
    requisites = (
        requisites_orders()
        .distinct()
        .prefetch_related(
            'items__product_variant__images',
            'items__product_variant__product__images',
            'items__product_variant__attribute_values__attribute',
            'items__bundle__images',
            'items__bundle__items__product_variant__images',
            'items__bundle__items__product_variant__product__images',
            'items__bundle__items__product_variant__attribute_values__attribute',
            'items__bundle_components__product_variant__images',
            'items__bundle_components__product_variant__product__images',
            'items__bundle_components__product_variant__attribute_values__attribute',
        )
        .order_by('-created_at')
    )
    requisites_steps = _notification_steps('requisites', [o.id for o in requisites]) if section == 'requisites' else {}
    requisites_data = []
    for o in (requisites if section == 'requisites' else []):
        requisites_data.append({
            'id': o.id,
            'order_number': o.order_number,
            'customer': o.customer_name,
            'phone': format_phone(o.customer_phone),
            'email': o.customer_email,
            'total': str(o.total_price) if o.total_price else '',
            'items': [_order_item_data(it, request) for it in o.items.all()],
            'payment_status': o.payment_status,
            'payment_method': o.payment_method,
            'comment': o.comment or '',
            'order_url': f'/admin/orders/order/{o.id}/change/',
            'created_at': timezone.localtime(o.created_at).strftime('%d.%m.%Y %H:%M') if o.created_at else '',
            'step': 1 if (requisites_steps.get(o.id, 0) == 0 and o.payment_status == 'paid')
                    else requisites_steps.get(o.id, 0),
        })

    # In "Notifications" we pull everything that went through LLM moderation (verdict
    # != '')
    # OR is not yet approved/replied to manually. Old pre-existing approved reviews without a
    # verdict are not shown.
    # Processed ones (NotificationState.step>0) are NOT hidden: they stay in the CRM
    # struck through (for both the site and products). There is no deletion here.
    # "Reviews": all Review rows with a rating (rated), including old approved ones.
    reviews = (
        Review.objects
        .filter(parent__isnull=True, rating__isnull=False)
        .select_related('product', 'bundle', 'variant')
        .prefetch_related('product__images', 'variant__images', 'bundle__images')
        .order_by('-created_at')
    )
    # "Questions/comments": Review.rating=None, also all of them.
    questions = (
        Review.objects
        .filter(parent__isnull=True, rating__isnull=True)
        .select_related('product', 'bundle', 'variant')
        .prefetch_related('product__images', 'variant__images', 'bundle__images')
        .order_by('-created_at')
    )
    stock_notifications = (
        StockNotification.objects
        .filter(is_notified=False)
        .select_related('variant__product', 'bundle')
        .order_by('-created_at')
    )

    def fmt(dt):
        return timezone.localtime(dt).strftime('%d.%m.%Y %H:%M') if dt else ''

    # Same reasoning as for "Payments" above: heavy sections are computed only
    # for the active one, the rest get {}/[] and don't pay for someone else's prefetch.
    reviews_steps   = _notification_steps('reviews',   [r.id for r in reviews]) if section == 'reviews-products' else {}
    questions_steps = _notification_steps('questions', [q.id for q in questions]) if section == 'reviews-site' else {}
    stock_steps     = _notification_steps('stock',     [s.id for s in stock_notifications]) if section == 'stock' else {}

    # Fetch ALL replies (recursively, with no restriction to the root parent)
    # so that deep chains (manager → client → manager) are included too.
    _all_replies = list(
        Review.objects
        .filter(parent__isnull=False)
        .order_by('created_at')
        .values('id', 'parent_id', 'author_name', 'text', 'created_at', 'user_id')
    ) if section in ('reviews-products', 'reviews-site') else []
    from collections import defaultdict as _dd
    _replies_by_parent = _dd(list)
    for rep in _all_replies:
        _replies_by_parent[rep['parent_id']].append({
            'id': rep['id'],
            'parent_id': rep['parent_id'],
            'author': rep['author_name'],
            'text': rep['text'],
            'created_at': fmt(rep['created_at']),
        })

    def _build_thread(root_id):
        """Recursively collect all nested replies (BFS, with a depth level)."""
        thread = []
        stack = [(root_id, 0)]
        while stack:
            pid, depth = stack.pop(0)
            for rep in _replies_by_parent.get(pid, []):
                rep_with_depth = dict(rep, depth=depth)
                thread.append(rep_with_depth)
                stack.append((rep['id'], depth + 1))
        return thread

    def _review_status(r):
        """Mapping of Review fields → status for the UI:
        rejected: admin rejected OR LLM flag (NOT approved)
        approved: is_approved=True
        pending:  LLM error (needs an admin decision)
        none:     the rest (new, not yet processed)
        """
        if r.is_rejected:
            return 'rejected'
        if r.moderation_verdict == 'flag' and not r.is_approved:
            return 'rejected'
        if r.is_approved:
            return 'approved'
        if r.moderation_verdict == 'error':
            return 'pending'
        return 'none'

    def _target_name(r):
        # Full name: for a product review, "Product name, Packaging" or just
        # "Product name"; for a bundle, the bundle name.
        if r.product:
            base = r.product.name
            if r.variant and r.variant.name:
                return f'{base}, {r.variant.name}'
            return base
        if r.bundle:
            return r.bundle.name
        return ''

    def _target_photo(r):
        """Thumbnail URL for a product/bundle review. Takes the first image of the
        variant → product → bundle."""
        try:
            if r.variant:
                imgs = list(r.variant.images.all())
                if imgs and imgs[0].image:
                    return imgs[0].image.url
            if r.product:
                imgs = list(r.product.images.all())
                if imgs and imgs[0].image:
                    return imgs[0].image.url
            if r.bundle:
                imgs = list(r.bundle.images.all())
                if imgs and imgs[0].image:
                    return imgs[0].image.url
        except Exception:
            pass
        return ''

    def _target_sku(r):
        if r.variant and r.variant.sku:
            return r.variant.sku
        if r.bundle and r.bundle.sku:
            return r.bundle.sku
        return ''

    def _row(r):
        return {
            'id': r.id,
            'step': (reviews_steps if r.rating is not None else questions_steps).get(r.id, 0),
            'author': r.author_name,
            'rating': r.rating,
            'text': r.text,
            'target': _target_name(r),
            'target_url': f'/admin/catalog/product/{r.product_id}/change/' if r.product_id else (f'/admin/catalog/bundle/{r.bundle_id}/change/' if r.bundle_id else ''),
            'photo': _target_photo(r),
            'sku':   _target_sku(r),
            'is_site': not (r.product_id or r.bundle_id),
            'status': _review_status(r),
            'created_at': fmt(r.created_at),
            'moderation_verdict': r.moderation_verdict,
            'moderation_reason': r.moderation_reason,
            'replies': _build_thread(r.id),
        }

    reviews_data   = [_row(r) for r in (reviews if section == 'reviews-products' else [])]
    questions_data = [_row(q) for q in (questions if section == 'reviews-site' else [])]

    stock_data = _stock_demand_items(stock_notifications, stock_steps) if section == 'stock' else []

    # "Out of stock" is a derived list: packagings with zero stock that have a date
    # set.
    # There is no separate table of rows, so nothing to sync.
    # Unlike out_of_stock_variants() (the counter of unprocessed ones), here we
    # take ALL stock=0: "Done" on a row only dismisses the marker, not the row itself,
    # as in the other notification sections: the packaging has still run out. Rows
    # closed permanently by migration 0118 (step 2, old backlog) are an exception:
    # oos_row_step() hides them until they run out again.
    from .notifications import oos_dismissed_map, oos_row_step
    out_of_stock = list(
        ProductVariant.objects
        # lte, not exact: a write-off against a future delivery (allow_negative_stock) can
        # push stock below zero, skipping exactly 0.
        .filter(stock__lte=0, out_of_stock_at__isnull=False)
        .select_related('product', 'bulk_source')
        .prefetch_related('images', 'product__images')
        .order_by('-out_of_stock_at')[:500]
    ) if section == 'stock_out' else []
    _oos_seen = oos_dismissed_map() if section == 'stock_out' else {}
    stock_out_data = []
    for v in out_of_stock:
        _step = oos_row_step(v.id, v.out_of_stock_at, _oos_seen)
        if _step is None:
            continue   # closed permanently by migration 0118 (old backlog), does not resurface
        stock_out_data.append({
            'id': v.id,
            'step': _step,
            'target': _variant_title(v),
            'sku': v.sku or '',
            'photo': _variant_photo(v),
            'target_url': f'/admin/catalog/product/{v.product_id}/change/#v{v.id}' if v.product_id else '',
            'product_id': v.product_id,
            'product_name': (v.product.name if v.product_id else ''),
            'is_active': v.is_active,
            'price': str(v.price) if v.price is not None else '',
            'stock': v.stock or 0,
            # Fields for the stock widget are the same as in the product list, so the widget
            # is identical: an own packaging is edited via the bulk bag weight, not via
            # variant.stock (otherwise you get 'bag is full, no product').
            'stock_manual': v.stock_manual,
            'from_bag': bool(v.bulk_source_id),
            'unit_size': float(v.unit_size) if v.unit_size else None,
            'bag_id': v.bulk_source_id,
            'bag_quantity': float(v.bulk_source.quantity) if v.bulk_source_id else None,
            'bag_unit': v.bulk_source.unit if v.bulk_source_id else 'g',
            'bag_waste': float(v.bulk_source.waste_percent or 0) if v.bulk_source_id else 0.0,
            'created_at': fmt(v.out_of_stock_at),
        })

    # "Low stock" is a derived list: packagings whose stock has dropped below the dynamic
    # threshold (sales rate × coverage days) but is not yet zero. Dismissal uses the
    # same technique as "Out of stock": NotificationState + comparing the date with
    # low_stock_at, so a repeat drop below the threshold AFTER dismissal resurfaces
    # as a new event (low_stock_at itself is maintained by low_stock_variants()).
    from .notifications import low_stock_variants, lowstock_dismissed_map, lowstock_row_step
    _low_seen = lowstock_dismissed_map() if section == 'stock_low' else {}
    stock_low_data = []
    for v, rate, threshold in (low_stock_variants() if section == 'stock_low' else []):
        stock_low_data.append({
            'id': v.id,
            'step': lowstock_row_step(v.id, v.low_stock_at, _low_seen),
            'target': _variant_title(v),
            'sku': v.sku or '',
            'photo': _variant_photo(v),
            'target_url': f'/admin/catalog/product/{v.product_id}/change/#v{v.id}' if v.product_id else '',
            'product_id': v.product_id,
            'product_name': (v.product.name if v.product_id else ''),
            'is_active': v.is_active,
            'price': str(v.price) if v.price is not None else '',
            'stock': v.stock or 0,
            'stock_manual': v.stock_manual,
            'from_bag': bool(v.bulk_source_id),
            'unit_size': float(v.unit_size) if v.unit_size else None,
            'bag_id': v.bulk_source_id,
            'bag_quantity': float(v.bulk_source.quantity) if v.bulk_source_id else None,
            'bag_unit': v.bulk_source.unit if v.bulk_source_id else 'g',
            'bag_waste': float(v.bulk_source.waste_percent or 0) if v.bulk_source_id else 0.0,
            'daily_rate': round(rate, 3) if rate is not None else None,
            'threshold': threshold,
            'created_at': fmt(v.low_stock_at),
        })

    from .models import DropSyncEvent
    from django.conf import settings as _settings
    # If the feature is disabled, we pull nothing from the DB; JS works with [].
    if not getattr(_settings, 'DROPSYNC_ENABLED', True) or section != 'dropsync':
        dropsync = []
        dropsync_steps = {}
    else:
        dropsync = (
            DropSyncEvent.objects
            .filter(is_seen=False)
            .select_related('variant__product')
            .order_by('-created_at')
        )
        dropsync_steps = _notification_steps('dropsync', [e.id for e in dropsync])
    dropsync_data = [{
        'id': e.id,
        'step': dropsync_steps.get(e.id, 0),
        'kind': e.kind,
        'kind_display': e.get_kind_display(),
        'supplier_name': e.variant.name if e.variant else (e.message or e.supplier_sku),
        'supplier_url':  e.variant.supplier_url if e.variant else '',
        'created_at': fmt(e.created_at),
    } for e in dropsync]

    section_titles = {
        'payments':  'Payments',
        'requisites':         'Payment details',
        'reviews':            'Reviews',
        'reviews-products':   'Product reviews',
        'reviews-site':       'Site reviews',
        'stock':              'Awaiting stock',
        'stock_out':          'Sold out',
        'stock_low':          'Low',
        'dropsync':           'Supplier changes',
    }
    page_title = section_titles.get(section, 'Notifications')

    return render(request, 'admin/notifications.html', {
        **admin.site.each_context(request),
        'title': page_title,
        'active_section': section,
        'payments_json': _safe_json(payments_data),
        'requisites_json': _safe_json(requisites_data),
        'reviews_json': _safe_json(reviews_data),
        'questions_json': _safe_json(questions_data),
        'stock_json': _safe_json(stock_data),
        'stock_out_json': _safe_json(stock_out_data),
        'stock_low_json': _safe_json(stock_low_data),
        'dropsync_json': _safe_json(dropsync_data),
        'payments_count': len(payments_data),
        'requisites_count': len(requisites_data),
        'reviews_count': len(reviews_data),
        'questions_count': len(questions_data),
        'stock_count': len(stock_data),
        'stock_out_count': len(stock_out_data),
        'stock_low_count': len(stock_low_data),
        'dropsync_count': len(dropsync_data),
    })


@staff_member_required
def notification_review_action(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)
    data = json.loads(request.body)
    action = data.get('action')
    pk = data.get('id')
    try:
        review = Review.objects.get(pk=pk)
    except Review.DoesNotExist:
        return JsonResponse({'error': 'Not found'}, status=404)
    if action == 'approve':
        review.is_approved = True
        review.is_rejected = False
        review.save(update_fields=['is_approved', 'is_rejected'])
    elif action == 'reject':
        review.is_rejected = True
        review.is_approved = False
        review.save(update_fields=['is_approved', 'is_rejected'])
    elif action == 'set_status':
        # Universal handler for the picker button: status ∈ {approved, rejected, pending, none}.
        status = data.get('status')
        if status == 'approved':
            review.is_approved = True
            review.is_rejected = False
        elif status == 'rejected':
            review.is_approved = False
            review.is_rejected = True
        else:
            # pending / none: reset both flags; the status will follow from moderation_verdict.
            review.is_approved = False
            review.is_rejected = False
        review.save(update_fields=['is_approved', 'is_rejected'])
    elif action == 'reset_status':
        review.is_approved = False
        review.is_rejected = False
        review.save(update_fields=['is_approved', 'is_rejected'])
    elif action == 'delete':
        review.delete()
    elif action == 'reply':
        # Create a nested Review as the manager's reply. parent_id can refer
        # to the root or to another reply, in which case the hierarchy gets deeper.
        parent_id = data.get('parent_id') or pk
        try:
            parent = Review.objects.get(pk=parent_id)
        except Review.DoesNotExist:
            return JsonResponse({'error': 'parent not found'}, status=404)
        text = (data.get('text') or '').strip()
        if not text:
            return JsonResponse({'error': 'empty text'}, status=400)
        name = ' '.join(filter(None, [getattr(request.user, 'first_name', ''),
                                       getattr(request.user, 'last_name', '')])).strip() or 'Shop'
        reply = Review.objects.create(
            parent=parent,
            product=parent.product, bundle=parent.bundle,
            user=request.user, author_name=name, author_email=request.user.email or '',
            text=text, is_approved=True,
        )
        from .notifications import broadcast_notification_counts
        broadcast_notification_counts()
        return JsonResponse({
            'ok': True, 'reply_id': reply.id, 'parent_id': parent.id,
            'author': name, 'text': text,
            'created_at': timezone.localtime(reply.created_at).strftime('%d.%m.%Y %H:%M'),
        })
    from .notifications import broadcast_notification_counts
    broadcast_notification_counts()
    return JsonResponse({'ok': True})


@staff_member_required
def notification_question_action(request):
    """A question is a Review with rating=None. Delegates to review-action."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)
    data = json.loads(request.body)
    action = data.get('action')
    pk = data.get('id')
    try:
        review = Review.objects.get(pk=pk, rating__isnull=True)
    except Review.DoesNotExist:
        return JsonResponse({'error': 'Not found'}, status=404)
    if action == 'answer':
        # Reply to a question: create a Review reply with a parent.
        Review.objects.create(
            parent=review,
            product=review.product, bundle=review.bundle,
            user=request.user if request.user.is_authenticated else None,
            author_name='Shop',
            text=data.get('answer', ''),
            is_approved=True,
        )
    elif action == 'delete':
        review.delete()
    from .notifications import broadcast_notification_counts
    broadcast_notification_counts()
    return JsonResponse({'ok': True})


@staff_member_required
def notification_stock_action(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)
    data = json.loads(request.body)
    action = data.get('action')
    from .notifications import broadcast_notification_counts
    if action == 'delete_likes':
        from .models import Favorite
        ids = [int(i) for i in (data.get('ids') or []) if str(i).isdigit()]
        Favorite.objects.filter(pk__in=ids).delete()
        broadcast_notification_counts()
        return JsonResponse({'ok': True})
    pk = data.get('id')
    try:
        notif = StockNotification.objects.get(pk=pk)
    except StockNotification.DoesNotExist:
        return JsonResponse({'error': 'Not found'}, status=404)
    if action == 'dismiss':
        notif.is_notified = True
        notif.save()
        try:
            from core.email import send_stock_notification
            send_stock_notification(notif)
        except Exception:
            pass
    elif action == 'delete':
        notif.delete()
    broadcast_notification_counts()
    return JsonResponse({'ok': True})


@staff_member_required
def notification_payment_action(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)
    from datetime import timedelta
    from django.db import transaction as db_transaction
    from django.utils import timezone
    from orders.models import Order, PaymentMatch, BankTransaction
    from orders.bank_matcher import confirm_match_manually
    from .notifications import broadcast_notification_counts
    data = json.loads(request.body)
    action = data.get('action', 'dismiss')

    # ── Actions on matches (key: match_id) ──────────────────────────────
    if action == 'confirm_match':
        try:
            match = PaymentMatch.objects.select_related('order', 'transaction').get(
                pk=data.get('match_id'))
        except PaymentMatch.DoesNotExist:
            return JsonResponse({'error': 'Match not found'}, status=404)
        confirm_match_manually(match, request.user)
        broadcast_notification_counts()
        return JsonResponse({'ok': True, 'order_id': match.order_id})

    if action == 'reject_match':
        try:
            match = PaymentMatch.objects.select_related('transaction').get(
                pk=data.get('match_id'))
        except PaymentMatch.DoesNotExist:
            return JsonResponse({'error': 'Match not found'}, status=404)
        tx = match.transaction
        with db_transaction.atomic():
            match.delete()
            # If the transaction has no live candidates left,
            # mark it as ignored (the manager said "none of these are mine").
            if not tx.order_matches.exists():
                tx.status = 'ignored'
                tx.save(update_fields=['status', 'updated_at'])
        broadcast_notification_counts()
        return JsonResponse({'ok': True})

    if action == 'unlink_match':
        try:
            match = PaymentMatch.objects.select_related('order', 'transaction').get(
                pk=data.get('match_id'))
        except PaymentMatch.DoesNotExist:
            return JsonResponse({'error': 'Match not found'}, status=404)
        with db_transaction.atomic():
            match.is_active = False
            match.confirmed_by = None
            match.confirmed_at = None
            match.save(update_fields=['is_active', 'confirmed_by', 'confirmed_at'])
            tx = match.transaction
            tx.status = 'unmatched'
            tx.save(update_fields=['status', 'updated_at'])
            order = match.order
            order.payment_status = 'pending'
            order.payment_notification = True
            order.save(update_fields=['payment_status', 'payment_notification', 'updated_at'])
        broadcast_notification_counts()
        return JsonResponse({'ok': True})

    if action == 'find_transactions':
        try:
            order = Order.objects.get(pk=data.get('id'))
        except Order.DoesNotExist:
            return JsonResponse({'error': 'Order not found'}, status=404)
        if order.total_price is None:
            return JsonResponse({'transactions': []})
        # Look for incoming transactions with the same amount within a ±48 h window.
        anchor = order.created_at or timezone.now()
        window_from = anchor - timedelta(hours=48)
        window_to = anchor + timedelta(hours=48)
        qs = (BankTransaction.objects
              .filter(amount=order.total_price,
                      status__in=['unmatched', 'manual_review'],
                      transacted_at__gte=window_from,
                      transacted_at__lte=window_to)
              .select_related('account')
              .order_by('-transacted_at')[:20])
        result = [{
            'id': tx.id,
            'source': tx.source,
            'source_display': tx.get_source_display(),
            'account': tx.account.name if tx.account_id else '',
            'amount': str(tx.amount),
            'transacted_at': timezone.localtime(tx.transacted_at).strftime('%d.%m.%Y %H:%M') if tx.transacted_at else '',
            'counterparty': tx.counterparty_name or '',
            'description': tx.description or '',
            'comment': tx.comment or '',
            'status': tx.status,
        } for tx in qs]
        return JsonResponse({'transactions': result})

    if action == 'link_transaction':
        try:
            order = Order.objects.get(pk=data.get('id'))
            tx = BankTransaction.objects.get(pk=data.get('tx_id'))
        except (Order.DoesNotExist, BankTransaction.DoesNotExist):
            return JsonResponse({'error': 'Not found'}, status=404)
        with db_transaction.atomic():
            match, _created = PaymentMatch.objects.update_or_create(
                order=order, transaction=tx,
                defaults={
                    'confidence': 100,
                    'reasons': [{'type': 'manual_link', 'weight': 100,
                                 'detail': f'Linked manually by user {request.user}'}],
                    'is_active': True,
                    'auto_confirmed': False,
                    'confirmed_by': request.user,
                    'confirmed_at': timezone.now(),
                },
            )
            # Deactivate the other candidates of the same transaction.
            PaymentMatch.objects.filter(transaction=tx).exclude(pk=match.pk).update(is_active=False)
            tx.status = 'matched'
            tx.save(update_fields=['status', 'updated_at'])
            if order.payment_status != 'paid':
                order.payment_status = 'paid'
                order.save(update_fields=['payment_status', 'updated_at'])
        broadcast_notification_counts()
        return JsonResponse({'ok': True, 'match_id': match.id})

    # ── Actions on an order (key: id) ────────────────────────────────
    pk = data.get('id')
    try:
        order = Order.objects.get(pk=pk)
    except Order.DoesNotExist:
        return JsonResponse({'error': 'Not found'}, status=404)

    from .models import NotificationState as _NSPM
    def _mark_processed(order_id):
        """Mark as "struck through, step=2": the request doesn't disappear from the list,
        but is shown with struck-through text and is filtered via "Type"."""
        _NSPM.objects.update_or_create(
            section='payments', object_id=int(order_id), defaults={'step': 2}
        )

    if action == 'set_status':
        new_status = data.get('status')
        if new_status not in ('pending', 'paid'):
            return JsonResponse({'error': 'Invalid status'}, status=400)
        # Direct UPDATE: bypasses the validation in Order.save() (paid→pending is forbidden).
        # In notifications the manager is allowed to move the status in both directions.
        from django.utils import timezone as _tz
        prev_pay = order.payment_status
        Order.objects.filter(pk=order.pk).update(
            payment_status=new_status,
            payment_status_changed_at=_tz.now(),
        )
        # queryset.update() does NOT emit post_save, so the _order_status_changed signal won't
        # run the stock logic. For monobank, stock is held only AFTER payment
        # (written off by this signal), so without this block payment='paid' would leave stock
        # unreduced → silent overselling. We mirror the signal manually (idempotent via
        # stock_applied). cod/bank_transfer are written off at creation, so we don't touch them.
        if (order.payment_method == 'monobank' and order.status != 'cancelled'
                and prev_pay != new_status):
            from orders.models import _consume, _restore
            order.payment_status = new_status
            items = list(order.items.select_related(
                'product_variant', 'bundle').all())
            if new_status == 'paid':
                for it in items:
                    _consume(it)
            elif new_status == 'pending':
                for it in items:
                    _restore(it)
        # Courses also live on post_save (courses/signals.py), which doesn't fire here.
        # Without this, the manager confirmed payment but the buyer got no access
        # and not even the "awaiting payment" badge (it also hangs on
        # payment_status='pending').
        # We mirror the payment rollback the same way: otherwise the manager removed the payment,
        # but the course
        # stayed open forever (stock is rolled back here, but access is not).
        if new_status == 'paid' and prev_pay != 'paid':
            from courses.signals import grant_course_access
            order.payment_status = 'paid'
            grant_course_access(order)
        elif new_status == 'pending' and prev_pay == 'paid':
            from courses.signals import revoke_course_access
            order.payment_status = 'pending'
            revoke_course_access(order)
        broadcast_notification_counts()
        return JsonResponse({'ok': True})

    if action == 'force_paid':
        order.payment_status = 'paid'
        order.payment_notification = False
        order.save(update_fields=['payment_status', 'payment_notification', 'updated_at'])
        _mark_processed(order.id)
        broadcast_notification_counts()
        return JsonResponse({'ok': True})

    if action == 'delete':
        # "Delete selected": remove completely from the payments list (step=3 is
        # excluded
        # from the query, unlike step=1/2, which stay visible).
        order.payment_notification = False
        order.save(update_fields=['payment_notification'])
        _NSPM.objects.update_or_create(
            section='payments', object_id=int(order.id), defaults={'step': 3}
        )
        broadcast_notification_counts()
        return JsonResponse({'ok': True})

    # default action == 'dismiss': hide from the notifications list.
    order.payment_notification = False
    order.save(update_fields=['payment_notification'])
    _mark_processed(order.id)
    broadcast_notification_counts()
    return JsonResponse({'ok': True})


@staff_member_required
def notification_dropsync_action(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)
    from .models import DropSyncEvent
    data = json.loads(request.body)
    action = data.get('action')
    if action == 'mark_seen':
        pk = data.get('id')
        DropSyncEvent.objects.filter(pk=pk).update(is_seen=True)
    elif action == 'mark_all_seen':
        DropSyncEvent.objects.filter(is_seen=False).update(is_seen=True)
    else:
        return JsonResponse({'error': 'Unknown action'}, status=400)
    from .notifications import broadcast_notification_counts
    broadcast_notification_counts()
    return JsonResponse({'ok': True})


@staff_member_required
def notification_requisites_action(request):
    """Actions on the "Payment details" queue. 'delete' removes the notification row:
    we clear the payment_contact_requested flag. We don't touch the order itself;
    if the customer clicks "send me the payment details" again, the row comes
    back."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)
    from orders.models import Order
    from .models import NotificationState
    data = json.loads(request.body)
    if data.get('action') != 'delete':
        return JsonResponse({'error': 'Unknown action'}, status=400)
    order = Order.objects.filter(pk=data.get('id')).first()
    if not order:
        return JsonResponse({'error': 'Not found'}, status=404)
    if order.payment_contact_requested:
        order.payment_contact_requested = False
        order.save(update_fields=['payment_contact_requested'])
    # Reset the card state so that after a repeat request it starts from "new".
    NotificationState.objects.filter(section='requisites', object_id=order.id).delete()
    from .notifications import broadcast_notification_counts
    broadcast_notification_counts()
    return JsonResponse({'ok': True})


@staff_member_required
def notification_set_step(request):
    """Save the step (0=new, 1=viewed, 2=dismissed) for any tab."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)
    from .models import NotificationState
    data = json.loads(request.body)
    section = data.get('section')
    object_id = data.get('id')
    step = int(data.get('step', 0))
    valid_sections = {'payments', 'requisites', 'reviews', 'questions', 'stock', 'stock_like', 'stock_out', 'stock_low', 'dropsync'}
    if section not in valid_sections:
        return JsonResponse({'error': 'Invalid section'}, status=400)
    if step not in (0, 1, 2):
        return JsonResponse({'error': 'Invalid step'}, status=400)
    if not object_id:
        return JsonResponse({'error': 'id required'}, status=400)
    NotificationState.objects.update_or_create(
        section=section, object_id=int(object_id),
        defaults={'step': step},
    )
    # The sidebar counter is updated live via WebSocket, so we send a broadcast.
    from .notifications import broadcast_notification_counts
    broadcast_notification_counts()
    return JsonResponse({'ok': True})


@staff_member_required
def notifications_count(request):
    from analyst.access import is_owner
    from .notifications import get_notification_counts
    counts = get_notification_counts()
    if not is_owner(request.user):
        counts['analyst'] = 0   # "Analyst" — owner only
    return JsonResponse(counts)


# ── BulkSource (bulk bags) ──

@staff_member_required
def bulk_sources_list(request):
    product_id = request.GET.get('product_id')
    if not product_id:
        return JsonResponse({'error': 'product_id required'}, status=400)
    sources = BulkSource.objects.filter(product_id=product_id).order_by('pk')
    return JsonResponse({'sources': [
        {'id': s.pk, 'name': s.name, 'quantity': float(s.quantity), 'unit': s.unit,
         'waste_percent': float(s.waste_percent or 0)}
        for s in sources
    ]})


@staff_member_required
def bulk_source_save(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    data = json.loads(request.body)
    pk = data.get('id')
    product_id = data.get('product_id')
    name = data.get('name', '').strip()
    quantity = data.get('quantity')
    unit = data.get('unit', 'g')
    waste_percent = data.get('waste_percent')
    if not product_id or not name or quantity is None:
        return JsonResponse({'error': 'product_id, name, quantity required'}, status=400)
    if pk:
        try:
            src = BulkSource.objects.get(pk=pk, product_id=product_id)
        except BulkSource.DoesNotExist:
            return JsonResponse({'error': 'Not found'}, status=404)
        src.name = name
        src.quantity = quantity
        src.unit = unit
        if waste_percent is not None:
            try:
                src.waste_percent = max(0, min(100, float(waste_percent)))
            except (ValueError, TypeError):
                pass
        src.save()
    else:
        src = BulkSource.objects.create(product_id=product_id, name=name, quantity=quantity, unit=unit)
    return JsonResponse({'id': src.pk, 'name': src.name, 'quantity': float(src.quantity), 'unit': src.unit,
                         'waste_percent': float(src.waste_percent or 0)})


@staff_member_required
def bulk_source_delete(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    data = json.loads(request.body)
    pk = data.get('id')
    try:
        BulkSource.objects.get(pk=pk).delete()
    except BulkSource.DoesNotExist:
        return JsonResponse({'error': 'Not found'}, status=404)
    return JsonResponse({'ok': True})


@staff_member_required
def bulk_source_link_variant(request):
    """Link/unlink a variant to a bulk bag + set unit_size + update the bag quantity."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    data = json.loads(request.body)
    variant_id = data.get('variant_id')
    unit_size = data.get('unit_size')        # null = unlink
    bag_quantity = data.get('bag_quantity')  # new quantity in the bag
    waste_percent = data.get('waste_percent')  # packaging tolerance, % (None = leave untouched)
    product_id = data.get('product_id')
    try:
        variant = ProductVariant.objects.get(pk=variant_id)
    except ProductVariant.DoesNotExist:
        return JsonResponse({'error': 'Variant not found'}, status=404)
    if unit_size is not None and product_id:
        # Find or create an auto bag for the product
        src = BulkSource.objects.filter(product_id=product_id).order_by('pk').first()
        if src is None:
            src = BulkSource.objects.create(product_id=product_id, name='Bag', quantity=0, unit='g')
        src.quantity = BulkSource._dec(bag_quantity) if bag_quantity is not None else 0
        if waste_percent is not None:
            try:
                src.waste_percent = max(0, min(100, float(waste_percent)))
            except (ValueError, TypeError):
                pass
        # Atomic: otherwise a failed recalculation left the bag filled while the packagings'
        # stock was zero ('the bag is there, but the product is out of stock').
        with transaction.atomic():
            src.save()
            src.sync_variants()  # will skip variants with stock_manual=True (see the model)
            variant.bulk_source = src
            variant.unit_size = unit_size
            variant.save(update_fields=['bulk_source', 'unit_size'])
            # Recalculate stock ONLY if NOT in manual mode; otherwise leave it as is.
            if float(unit_size) > 0 and not variant.stock_manual:
                eu = src.effective_unit(unit_size)
                variant.stock = int(BulkSource._dec(src.quantity) // eu) if eu > 0 else 0
                variant.save(update_fields=['stock'])
        return JsonResponse({'stock': variant.stock, 'stock_manual': variant.stock_manual, 'bag_id': src.pk,
                             'bag_quantity': float(src.quantity), 'unit': src.unit,
                             'waste_percent': float(src.waste_percent or 0)})
    elif bag_quantity is not None and product_id:
        # Only update the bag weight (for variants without unit_size)
        with transaction.atomic():
            src = BulkSource.objects.filter(product_id=product_id).order_by('pk').first()
            if src is None:
                src = BulkSource.objects.create(
                    product_id=product_id, name='Bag', quantity=BulkSource._dec(bag_quantity), unit='g')
            else:
                src.quantity = BulkSource._dec(bag_quantity)
                src.save()
            src.sync_variants()
        return JsonResponse({'ok': True, 'bag_id': src.pk, 'bag_quantity': float(src.quantity), 'unit': src.unit})
    else:
        variant.bulk_source = None
        variant.unit_size = None
        variant.save(update_fields=['bulk_source', 'unit_size'])
        return JsonResponse({'ok': True})


@staff_member_required
def goto_paired_product(request, product_id):
    """Switch to the paired product.
    If ?variant=<id> is passed, a pair is created specifically for that variant
    and linked via ProductVariant.paired_variant. Otherwise, a full copy of the whole Product."""
    from django.db import transaction
    from django.shortcuts import redirect, get_object_or_404
    src = get_object_or_404(Product, pk=product_id)
    src_variant_id = request.GET.get('variant')
    src_variant = None
    if src_variant_id:
        try:
            src_variant = ProductVariant.objects.get(pk=int(src_variant_id), product=src)
        except (ProductVariant.DoesNotExist, ValueError):
            src_variant = None

    counter_kind = Product.PACKAGING_OWN if src.packaging_kind == Product.PACKAGING_FACTORY else Product.PACKAGING_FACTORY

    # If this specific variant already has a pair, switch to it
    if src_variant and src_variant.paired_variant_id:
        partner = src_variant.paired_variant
        return redirect(f'/admin/catalog/product/{partner.product_id}/change/#v{partner.pk}')

    # If there is no paired Product at all, clone the whole Product
    if not src.paired_product_id:
        with transaction.atomic():
            new_product, new_variants_map = _clone_product_for_pair(src, counter_kind)
        if src_variant and src_variant.pk in new_variants_map:
            return redirect(f'/admin/catalog/product/{new_product.pk}/change/#v{new_variants_map[src_variant.pk]}')
        return redirect(f'/admin/catalog/product/{new_product.pk}/change/')

    # A paired Product exists, but this variant has no pair: create only a new variant in the paired
    # Product
    pair_product = src.paired_product
    if src_variant:
        with transaction.atomic():
            new_v = _clone_variant_into(pair_product, src_variant)
        return redirect(f'/admin/catalog/product/{pair_product.pk}/change/#v{new_v.pk}')

    return redirect(f'/admin/catalog/product/{pair_product.pk}/change/')


def _clone_variant_into(target_product, src_variant):
    """Clone a single ProductVariant into target_product, linking paired_variant."""
    from catalog.models import VariantLabel
    sku = ProductVariant.generate_unique_sku()
    new_v = ProductVariant.objects.create(
        product=target_product, name=src_variant.name, description=src_variant.description,
        price=src_variant.price, old_price=src_variant.old_price,
        discount_percent=src_variant.discount_percent, discount_amount=src_variant.discount_amount,
        stock=src_variant.stock, sku=sku, is_active=src_variant.is_active,
        is_dropshipping=src_variant.is_dropshipping, is_hybrid=src_variant.is_hybrid,
        unit_size=src_variant.unit_size, paired_variant=src_variant,
    )
    new_v.attribute_values.set(src_variant.attribute_values.all())
    # Copy the photos: the new link goes via color_value (not to the variant).
    from catalog.serializers import _variant_images_qs
    new_color_av = new_v.attribute_values.filter(attribute__is_color=True).order_by('pk').first()
    for img in _variant_images_qs(src_variant):
        ProductImage.objects.create(
            product=target_product,
            variant=None if new_color_av else new_v,  # legacy fallback only for colorless ones
            color_value=new_color_av,
            image=img.image,
            sort_order=img.sort_order, is_main=img.is_main,
        )
    for ch in src_variant.characteristics.all():
        ProductCharacteristic.objects.create(
            variant=new_v, name=ch.name, value=ch.value, sort_order=ch.sort_order,
        )
    for vl in src_variant.variant_labels.all():
        VariantLabel.objects.update_or_create(
            variant=new_v, label=vl.label,
            defaults={'expires_at': vl.expires_at, 'is_auto': vl.is_auto},
        )
    return new_v


def _clone_product_for_pair(src, counter_kind):
    """Full copy of a Product with all its variants. Returns (new_product, dict src_variant_id ->
new_variant_id)."""
    from django.db import transaction
    from catalog.models import VariantLabel
    with transaction.atomic():
        new_product = Product.objects.create(
            name=src.name,
            description=src.description,
            application_norms=src.application_norms,
            brand=src.brand,
            is_active=False,
            packaging_kind=counter_kind,
            paired_product=src,
            meta_title=src.meta_title,
            meta_description=src.meta_description,
        )
        new_product.categories.set(src.categories.all())
        new_product.labels.set(src.labels.all())

        for img in src.images.filter(variant__isnull=True):
            ProductImage.objects.create(
                product=new_product, image=img.image,
                sort_order=img.sort_order, is_main=img.is_main,
            )
        for ch in src.characteristics.filter(variant__isnull=True):
            ProductCharacteristic.objects.create(
                product=new_product, name=ch.name, value=ch.value, sort_order=ch.sort_order,
            )
        for comp in src.compositions.all():
            ProductComposition.objects.create(
                product=new_product, name=comp.name, value=comp.value, sort_order=comp.sort_order,
            )

        variants_map = {}
        for v in src.variants.all():
            sku = ProductVariant.generate_unique_sku()
            new_v = ProductVariant.objects.create(
                product=new_product, name=v.name, description=v.description,
                price=v.price, old_price=v.old_price,
                discount_percent=v.discount_percent, discount_amount=v.discount_amount,
                stock=v.stock, sku=sku, is_active=v.is_active,
                is_dropshipping=v.is_dropshipping, is_hybrid=v.is_hybrid,
                unit_size=v.unit_size, paired_variant=v,
            )
            variants_map[v.pk] = new_v.pk
            new_v.attribute_values.set(v.attribute_values.all())
            for img in v.images.all():
                ProductImage.objects.create(
                    product=new_product, variant=new_v, image=img.image,
                    sort_order=img.sort_order, is_main=img.is_main,
                )
            for ch in v.characteristics.all():
                ProductCharacteristic.objects.create(
                    variant=new_v, name=ch.name, value=ch.value, sort_order=ch.sort_order,
                )
            for vl in v.variant_labels.all():
                VariantLabel.objects.update_or_create(
                    variant=new_v, label=vl.label,
                    defaults={'expires_at': vl.expires_at, 'is_auto': vl.is_auto},
                )

    return new_product, variants_map



def _components_from_form(items):
    """Contents from the form [{variant_id, qty}] → [Comp]. Same as components_of_template,
    but without a bundle in the DB: unknown ids are skipped."""
    from . import instruction_resolve as ir
    rows = []
    for it in items or []:
        try:
            rows.append((int(it['variant_id']), max(1, int(it.get('qty') or 1))))
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
    variants = ProductVariant.objects.select_related(
        'product', 'paired_variant__product').in_bulk([vid for vid, _ in rows])
    return [ir.Comp(variants[vid], qty) for vid, qty in rows if vid in variants]


@staff_member_required
def normalize_bundle_instruction(request):
    """Run a bundle's instruction through the LLM: clean it up and mark up the preparations.

    SAVES NOTHING. Returns the result next to the original — a human applies it with a
    button in the form. This is deliberate: the model makes substantive decisions (e.g.
    fills in a preparation name based on the contents), and such decisions must not be
    written into the field silently — the sheet in the box carries pesticide doses.

    POST {"text": "<text from the editor>", "name": "<bundle name>",
          "items": [{"variant_id": 1, "qty": 2}, ...], "bundle_id": <pk or null>}

    AI limit exhausted → 202 {"queued": true}: a snapshot of the form is put in the queue
    (InstructionJob), the scheduler will finish it on its own and write the result into the
    saved bundle.

    The bundle does NOT need to be in the DB: the model sees only the name, text and contents,
    and all of that is in the form even before the first save. The contents are taken from
    the form, not from the DB — the DB may hold a previous version if the person has just
    changed it.
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    try:
        payload = json.loads(request.body)
    except (ValueError, TypeError):
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    from core.limits import LimitExceeded
    from core.llm import SubscriptionLimit
    from . import instruction_jobs as ij
    from . import instruction_llm as il

    text = (payload.get('text') or '').strip()
    if not text:
        return JsonResponse({'error': 'Paste the instruction text first'}, status=400)
    bundle = Bundle(name=(payload.get('name') or '').strip(), instruction_html=text)  # only a carrier for normalize, not saved
    comps = _components_from_form(payload.get('items'))
    try:
        # Agent (step by step, each step checked by code) or script — per INSTRUCTION_MODE.
        from . import instruction_agent as ia
        res = ia.normalize_by_mode(bundle, comps, getattr(settings, 'INSTRUCTION_MODE', 'agent'))
    except il.Rejected as e:
        # A rejection is not a server error but a normal result of validation.
        #
        # But the text arrives here from the editor and is stored NOWHERE, so without
        # this record the reason for the rejection can't be found: the DB holds the previous
        # version, not the one the person pressed the button on.
        try:
            log.warning(
                'instruction normalize REJECTED «%s»: %s\n'
                '  treatment program doses: %s\n'
                '  contents list doses    : %s\n'
                '  contents list detected : %s\n'
                '--- source (2000) ---\n%s\n--- end ---',
                bundle.name, e,
                sorted(il.dose_set(il.cut_composition(bundle.instruction_html, comps))),
                sorted(il.dose_set(il.composition_region(bundle.instruction_html, comps))),
                bool(il.composition_region(bundle.instruction_html, comps)),
                (bundle.instruction_html or '')[:2000])
        except Exception:
            log.exception('could not log the reason for the rejection')
        return JsonResponse({'ok': False, 'rejected': str(e)}, status=200)
    except (SubscriptionLimit, LimitExceeded):
        # AI limit: not a refusal but a queue. The scheduler will finish the run when the limit
        # recovers and will write the result into the bundle itself (catalog/instruction_jobs.py).
        job = ij.enqueue(text=text, name=bundle.name, items=payload.get('items'),
                         bundle_id=payload.get('bundle_id'), user=request.user)
        return JsonResponse({'queued': True, 'job_id': job.pk, 'linked': bool(job.bundle_id),
                             'message': ij.queued_message(job)}, status=202)
    except Exception as e:
        return JsonResponse({'error': f'Generation error: {e}'}, status=500)
    ij.cancel_pending(payload.get('bundle_id'))      # done manually in the meantime — the old queue entry is redundant

    # The model's remarks are not shown to the manager (the form only says "Done"), but
    # they
    # are not lost either: if a question about the text comes up later, the reason is visible here.
    if res['warnings']:
        log.info('instruction normalize warnings «%s»: %s', bundle.name, res['warnings'])

    return JsonResponse({
        'ok': True,
        'html': res['html'],
        'mentions': res['mentions'],
        'changes': res['changes'],
        'warnings': res['warnings'],
        # We must pass the composition: without it the markup would have neither the list of
        # components nor their packagings, and the dose note on substitution would quote the
        # packaging of an arbitrarily picked variant of the same product.
        'map': il.build_map(res['html'], res['mentions'], comps),
    })


@staff_member_required
def bundle_instruction_docx(request, bundle_id):
    """Download the .docx with the printed instruction of ONE bundle."""
    from urllib.parse import quote

    from django.http import HttpResponse

    from orders.instruction_docx import build_single_bundle

    bundle = Bundle.objects.filter(pk=bundle_id).first()
    if bundle is None:
        return JsonResponse({'error': 'Bundle not found'}, status=404)

    data = build_single_bundle(bundle)
    if data is None:
        return JsonResponse({'error': 'There is no instruction'}, status=404)

    resp = HttpResponse(
        data,
        content_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    )
    resp['Content-Disposition'] = ("attachment; filename*=UTF-8''"
                                   + quote(f'{bundle.name} — instructions.docx', safe=''))
    return resp
