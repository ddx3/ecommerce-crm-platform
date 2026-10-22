"""Own-packaging stock invariant — in one place.

For a variant on bulk-bag accounting (bulk_source + unit_size, not a manual stock level)
variant.stock is NOT a fact: it is derived from the bag's remaining quantity —

    stock == floor(bag.quantity / bag.effective_unit(unit_size))

That is exactly how BulkSource.sync_variants and ProductVariant.save compute it. Any
mismatch = drift: the DB holds a lie, and the storefront shows it to the customer
(typical symptom — the bag is full but the product shows as out of stock).

This is only the invariant SCANNER. Both sides use it:
  - catalog/management/commands/audit_stock.py    — READ-ONLY reconciliation
  - catalog/management/commands/heal_stock_drift.py — self-healing (daily from the scheduler)
"""
def expected_stock(bag, unit_size):
    """How many unit_size packagings actually come out of the bag. None if there is nothing to compute
from."""
    if bag is None or bag.quantity is None:
        return None
    eu = bag.effective_unit(unit_size)
    if eu <= 0:
        return 0
    return int(bag._dec(bag.quantity) // eu)


def scan_bag_drift():
    """Scans all bulk-bag packagings. Returns (drift, no_bag, checked):

      drift  — [(variant, expected, actual, bag)] invariant mismatches;
      no_bag — [variant] unit_size is set but there is no accounting bag (stock with no source of
               truth — informational, not a write-off defect);
      checked — how many variants were checked.
    """
    from .models import ProductVariant

    drift, no_bag, checked = [], [], 0
    qs = (ProductVariant.objects
          .filter(unit_size__isnull=False, stock_manual=False)
          .exclude(unit_size=0)
          .select_related('bulk_source')
          .iterator())
    for v in qs:
        bag = v.bulk_source if v.bulk_source_id else None
        if bag is None:
            no_bag.append(v)
            continue
        checked += 1
        exp = expected_stock(bag, v.unit_size)
        if exp is None:
            continue
        if exp != (v.stock or 0):
            drift.append((v, exp, v.stock or 0, bag))
    return drift, no_bag, checked


def split_by_direction(drift):
    """Split drift into safe and risky for self-healing.

    DOWN (expected < actual) — the storefront currently shows MORE than the bag provides. This is
    a live oversell risk already, and the recompute removes it. We always heal automatically.

    UP (expected > actual) — the recompute will PUT the product on sale. That is right only if
    the bag's quantity is the truth. If the bag itself is inflated (see
    [[project_bag_restore_inflation]]),
    the auto-raise creates an oversell out of thin air. A machine cannot tell these two cases apart,
    so we don't heal upward without an explicit human decision (--allow-raise).
    """
    down = [row for row in drift if row[1] < row[2]]
    up = [row for row in drift if row[1] > row[2]]
    return down, up


def heal_bag_drift(rows):
    """Bring the stock of the given variants to the value derived from the bag. Safe: the stock of a
    bag-based packaging is derived, so the recompute loses no information. Returns the number of
    variants fixed."""
    fixed = 0
    for v, exp, _actual, _bag in rows:
        v.stock = exp
        v.save(update_fields=['stock', 'updated_at'])
        v.refresh_from_db(fields=['stock'])
        if (v.stock or 0) == exp:
            fixed += 1
    return fixed
