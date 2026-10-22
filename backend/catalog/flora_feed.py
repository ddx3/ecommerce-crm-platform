"""Shared feed composition for the Flora plant-assistant (MyPlantApp).

Flora attaches product/bundle cards to its answers and searches a local vector index
built from the shop's TEXT. Two consumers need the SAME composition, so it lives here once:

  * the read-only API endpoints (`flora_catalog_batch` / `flora_catalog_delta` in
    api_views.py) — realtime price/stock/image + text for incremental re-embedding;
  * the Flora-side full export (`rag/ingest/catalog.py`), which imports this module when
    it runs under the shop venv, so a full rebuild and an incremental update produce the
    BYTE-IDENTICAL embed_text/card — otherwise their vectors would diverge.

Card fields (price/old_price/in_stock/image) reuse the existing serializer logic
(ProductListSerializer / BundleListSerializer) so there is a single source of truth for
"current price" and "in stock" — no duplicated pricing rules.

`request` is optional: with a DRF request we build absolute media URLs via
`request.build_absolute_uri`; without one (standalone export) we fall back to
settings.SITE_URL / BACKEND_URL.
"""
from __future__ import annotations

import html
import re

from django.conf import settings

from .serializers import ProductListSerializer, BundleListSerializer

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


# ── text helpers (identical to rag/ingest/catalog.py so exports stay byte-identical) ──

def strip_html(text) -> str:
    if not text:
        return ""
    return _WS_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", text))).strip()


def _clean(*parts: str) -> str:
    # No length cap: embed the FULL product text. The embedder applies its own limit
    # downstream (embed.py: OpenAI ~8000 chars, local MiniLM ~1200), so this never blows
    # past the model — but it no longer drops norms/composition/specs that sat past an
    # arbitrary 1500 chars in products with a long description.
    return _WS_RE.sub(" ", " ".join(p for p in parts if p and p.strip())).strip()


def _context_block(fields) -> str:
    # No length cap — the FULL grounding text (description/norms/composition/specs) reaches the
    # model. NB: with several products in context this makes the prompt larger; reinstate a
    # cap here if the Opus prompt gets too heavy.
    lines = [f"{label}: {_WS_RE.sub(' ', val).strip()}" for label, val in fields if val and val.strip()]
    return "\n".join(lines)


def _abs_url(field, request) -> str | None:
    """Absolute media URL for an ImageFieldFile. With a request use build_absolute_uri;
    otherwise prefix settings.SITE_URL/BACKEND_URL so the standalone export still works."""
    if not field:
        return None
    if request is not None:
        return request.build_absolute_uri(field.url)
    base = (getattr(settings, "SITE_URL", "") or getattr(settings, "BACKEND_URL", "")).rstrip("/")
    return f"{base}{field.url}"


def _abs_path(path: str, request) -> str:
    if request is not None:
        return request.build_absolute_uri(path)
    base = (getattr(settings, "SITE_URL", "") or getattr(settings, "BACKEND_URL", "")).rstrip("/")
    return f"{base}{path}"


# ── product packaging ────────────────────────────────────────────────
# Flora recommends HOW MUCH to buy: from the product's dose × the client's scale it
# works out the amount needed, then picks the packaging that covers it. So the feed
# exposes each active variant's size + price + live stock (not just the product-level
# min-price/any-in-stock aggregate, which hides that a specific size is out).

# Base units so different packs of one product are comparable/sortable (volume→ml, mass→g).
# LANGUAGE DATA: the unit words below are Ukrainian abbreviations (л = l, мл = ml, мкл = µl, кг = kg,
# г = g, мг = mg, шт = pcs, таб = tablets, капс = capsules, доз = doses) because the variant names
# in the catalog are typed in Ukrainian.
_UNIT_BASE = {"л": ("мл", 1000.0), "мл": ("мл", 1.0), "мкл": ("мл", 0.001),
              "кг": ("г", 1000.0), "г": ("г", 1.0), "мг": ("г", 0.001),
              "шт": ("шт", 1.0), "таб": ("шт", 1.0), "капс": ("шт", 1.0), "доз": ("шт", 1.0)}
_PACK_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(мл|мкл|л|кг|мг|г|шт|таб|капс|доз)\b", re.IGNORECASE)


def _parse_pack(name: str):
    """(size_label, amount, unit, base_amount, base_unit) parsed from a variant name's
    trailing size (e.g. '…, 100 мл' = '…, 100 ml' → 100 ml). None if no size token — some products
    (seeds packs, tools) carry no dosable size."""
    m = None
    for m in _PACK_RE.finditer(name or ""):
        pass  # keep the LAST match — the size is at the end ('Plant Booster, 100 мл')
    if not m:
        return None
    amt = float(m.group(1).replace(",", "."))
    unit = m.group(2).lower()
    base_unit, k = _UNIT_BASE.get(unit, (unit, 1.0))
    return f"{m.group(1)} {unit}", amt, unit, amt * k, base_unit


def product_variants(p) -> list:
    """Active variants of a product with size + live price + stock, smallest pack first.
    Lets Flora pick the right packaging for the client's need and be honest per-size about
    availability (product 'in stock' can be true while the asked size is out)."""
    out = []
    for v in p.variants.all():
        if not getattr(v, "is_active", True):
            continue
        pack = _parse_pack(v.name)
        row = {
            "size": pack[0] if pack else (v.name or "").strip(),
            "price": float(v.effective_price) if v.effective_price is not None else None,
            "in_stock": bool((v.stock or 0) > 0),
        }
        if pack:
            row.update(amount=pack[1], unit=pack[2], base_amount=pack[3], base_unit=pack[4])
        out.append(row)
    # smallest pack first (within a unit family); packs without a parsed size go LAST
    # (￿ sorts after the Cyrillic unit letters г/мл/шт, unlike "~" which sorts before them)
    out.sort(key=lambda r: (r.get("base_unit") or "￿", r.get("base_amount") if r.get("base_amount") is not None else 1e18))
    return out


# ── card fields (reuse serializer logic; instances are method-only, no full serialization) ──

def _card_price(v):
    """(price to pay, struck-through old price) of the chosen packaging — or (None, None)."""
    if v is None or v.price is None:
        return None, None
    base = float(v.price)
    effective = v.effective_price
    price = float(effective) if effective is not None else base
    explicit_old = float(v.old_price) if v.old_price else None
    if explicit_old and explicit_old > price:
        return price, explicit_old
    return price, (base if price < base else None)


def product_card_fields(p, request) -> dict:
    """Product card for chat/feed.

    The price here is the ACTUAL one, discounted, and the base price goes as `old_price`.
    On the storefront the frontend computes the discount (computeDiscount) from the packaging's
    fields, but the chat card does not carry such fields: it used to show the base price and put
    the base price into the cart too, i.e. more expensive than the same product from the catalog
    page."""
    s = ProductListSerializer(context={"request": request})
    img = s._pick_main_image(p)
    price, old_price = _card_price(s.min_price_variant(p))
    return {
        "price": price,
        "old_price": old_price,
        "in_stock": bool(s._has_in_stock_variant(p)),
        "image": _abs_url(img.image if img else None, request),
        "variants": product_variants(p),
    }


def bundle_card_fields(b, request) -> dict:
    s = BundleListSerializer(context={"request": request})
    return {
        "price": s.get_price(b),
        "old_price": s.get_old_price(b),
        "in_stock": bool(s.get_stock(b) > 0),
        "image": _abs_url(s._pick_main_image(b), request),
    }


# ── embed_text / context / categories (moved verbatim from rag/ingest/catalog.py) ──

def product_embed_text(p) -> str:
    comp = "; ".join(f"{c.name}: {c.value}".strip(": ") for c in p.compositions.all())
    char = "; ".join(f"{c.name}: {c.value}" for c in p.characteristics.all())
    cats = [c.name for c in p.categories.all()]
    return _clean(
        p.name, strip_html(p.description), strip_html(p.application_norms),
        comp, char, ("Categories: " + ", ".join(cats)) if cats else "",
    )


def product_context(p) -> str:
    comp = "; ".join(f"{c.name}: {c.value}".strip(": ") for c in p.compositions.all())
    char = "; ".join(f"{c.name}: {c.value}" for c in p.characteristics.all())
    return _context_block([
        ("Description", strip_html(p.description)),
        ("Application norms", strip_html(p.application_norms)),
        ("Composition", comp),
        ("Characteristics", char),
    ])


def _bundle_sklad(b) -> str:
    items = [f"{it.product_variant.name} x{it.quantity}" if it.quantity and it.quantity > 1
             else it.product_variant.name
             for it in b.items.all() if it.product_variant_id]
    return ", ".join(items)


def bundle_embed_text(b) -> str:
    norms = strip_html(b.application_norms) if b.application_norms else ""
    return _clean(b.name, strip_html(b.description), _bundle_sklad(b), norms)


def bundle_context(b) -> str:
    norms = strip_html(b.application_norms) if b.application_norms else ""
    return _context_block([
        ("Description", strip_html(b.description)),
        ("Composition", _bundle_sklad(b)),
        ("Application scheme", norms),
    ])


# ── unified payload (one row of the endpoint / one line of catalog.jsonl) ──

def unit_payload(obj, kind: str, request, include_text: bool = False) -> dict:
    """One catalog unit for Flora. `include_text=False` → lean card-only (realtime);
    `True` → also the text fields needed to (re)build the vector."""
    if kind == "product":
        card = product_card_fields(obj, request)
        row = {"id": f"prod-{obj.id}", "kind": "product", "active": bool(obj.is_active),
               "url": _abs_path(f"/product/{obj.slug}", request),
               "packaging_kind": obj.packaging_kind,
               "updated_at": obj.updated_at.isoformat() if obj.updated_at else None, **card}
        # Paired product (own ↔ factory packaging): lean card of the partner so Flora can
        # fall back to it when THIS product is removed or fully out of stock.
        partner = getattr(obj, "paired_product", None)
        if partner is not None:
            pc = product_card_fields(partner, request)
            row["paired"] = {
                "id": f"prod-{partner.id}", "active": bool(partner.is_active),
                "packaging_kind": partner.packaging_kind, "name": partner.name,
                "url": _abs_path(f"/product/{partner.slug}", request),
                "price": pc["price"], "in_stock": pc["in_stock"], "image": pc["image"],
            }
        if include_text:
            row.update(name=obj.name, embed_text=product_embed_text(obj),
                       context=product_context(obj),
                       categories=[c.name for c in obj.categories.all()])
        return row
    card = bundle_card_fields(obj, request)
    row = {"id": f"bundle-{obj.id}", "kind": "bundle", "active": bool(obj.is_active),
           "url": _abs_path(f"/bundle/{obj.slug}", request),
           "updated_at": obj.updated_at.isoformat() if obj.updated_at else None, **card}
    if include_text:
        row.update(name=obj.name, embed_text=bundle_embed_text(obj),
                   context=bundle_context(obj),
                   categories=[c.name for c in obj.categories.all()])
    return row
