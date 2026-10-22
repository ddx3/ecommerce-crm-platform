"""Daily stock snapshot: stock history cannot be reconstructed retroactively."""
from __future__ import annotations

from analyst.models import StockSnapshot


def take_stock_snapshot(date) -> int:
    """Snapshot of the stock level of every packaging the shop holds itself (pure dropshipping and raw
    supplier import are not snapshotted — their stock mirrors someone else's warehouse).
    Idempotent: re-running for the same date duplicates nothing."""
    from catalog.models import ProductVariant
    rows = [
        StockSnapshot(
            date=date, variant_id=v.pk, stock=v.stock or 0,
            bag_id=v.bulk_source_id,
            bag_quantity=v.bulk_source.quantity if v.bulk_source_id else None,
        )
        for v in (ProductVariant.objects
                  .filter(is_dropshipping=False, is_supplier_fallback=False)
                  .select_related('bulk_source').iterator())
    ]
    StockSnapshot.objects.bulk_create(rows, batch_size=500, ignore_conflicts=True)
    return len(rows)
