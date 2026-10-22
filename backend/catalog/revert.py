"""
Utilities for reverting actions from history (LogEntry).
"""
from decimal import Decimal, InvalidOperation
from datetime import datetime, date


def _serialize(val):
    from django.db.models.fields.files import FieldFile
    if isinstance(val, FieldFile):
        return val.name if val else None
    if isinstance(val, Decimal):
        return str(val)
    if hasattr(val, 'isoformat'):
        return val.isoformat()
    return val


def capture_fields(obj):
    """Snapshot of plain fields (without M2M) for writing into change_message."""
    snap = {'fields': {}}
    for field in obj._meta.concrete_fields:
        if field.primary_key:
            continue
        snap['fields'][field.attname] = _serialize(getattr(obj, field.attname))
    return snap


def capture_full_snapshot(obj):
    """Full snapshot of an object (fields + M2M) for restoring after deletion."""
    snap = capture_fields(obj)
    snap['pk'] = obj.pk
    snap['m2m'] = {}
    for field in obj._meta.many_to_many:
        snap['m2m'][field.name] = list(
            getattr(obj, field.name).values_list('pk', flat=True)
        )
    return snap


def _coerce(field, val):
    from django.db import models
    if val is None or val == '':
        # Empty value: for NOT NULL text fields we return '' (not None), otherwise save() fails on
        # the null constraint and the WHOLE revert breaks — no field (including the description)
        # gets restored.
        if not field.null and isinstance(field, (models.CharField, models.TextField)):
            return ''
        return None
    if isinstance(field, models.DecimalField):
        try:
            return Decimal(str(val))
        except InvalidOperation:
            return None
    if isinstance(field, (models.IntegerField, models.AutoField)):
        try:
            return int(val)
        except (ValueError, TypeError):
            return None
    if isinstance(field, models.BooleanField):
        if isinstance(val, bool):
            return val
        return str(val).lower() in ('true', '1', 'yes')
    if isinstance(field, models.DateTimeField):
        try:
            return datetime.fromisoformat(str(val))
        except (ValueError, AttributeError):
            return None
    if isinstance(field, models.DateField):
        try:
            return date.fromisoformat(str(val)[:10])
        except (ValueError, AttributeError):
            return None
    return val


def apply_snapshot(obj, snap):
    """
    Apply a snapshot to obj and save.
    Returns (ok: bool, warnings: list[str]).
    """
    warnings = []
    attname_map = {f.attname: f for f in obj._meta.concrete_fields}
    update_fields = []

    for attname, val in snap.get('fields', {}).items():
        field = attname_map.get(attname)
        if field is None or field.primary_key:
            continue
        try:
            setattr(obj, attname, _coerce(field, val))
            update_fields.append(attname)
        except Exception as e:
            warnings.append(f'Field {attname}: {e}')

    if update_fields:
        try:
            obj.save(update_fields=update_fields)
        except Exception as e:
            return False, [f'Save error: {e}']

    for field_name, pks in snap.get('m2m', {}).items():
        try:
            getattr(obj, field_name).set(pks)
        except Exception as e:
            warnings.append(f'{field_name}: {e}')

    return True, warnings


def restore_deleted(entry, snap):
    """
    Restore a deleted object from its snapshot.
    Returns (ok: bool, warnings: list[str]).
    """
    model_class = entry.content_type.model_class()
    if model_class is None:
        return False, ['Model not found']

    pk = snap.get('pk') or entry.object_id
    if model_class.objects.filter(pk=pk).exists():
        return False, [f'An object with this ID ({pk}) already exists — it cannot be restored']

    attname_map = {f.attname: f for f in model_class._meta.concrete_fields}
    create_kwargs = {'pk': pk}
    warnings = []

    for attname, val in snap.get('fields', {}).items():
        field = attname_map.get(attname)
        if field is None or field.primary_key:
            continue
        try:
            create_kwargs[attname] = _coerce(field, val)
        except Exception as e:
            warnings.append(f'Field {attname}: {e}')

    try:
        obj = model_class(**create_kwargs)
        obj.save()
    except Exception as e:
        return False, [f'Restore error: {e}']

    for field_name, pks in snap.get('m2m', {}).items():
        try:
            getattr(obj, field_name).set(pks)
        except Exception as e:
            warnings.append(f'{field_name}: {e}')

    return True, warnings
