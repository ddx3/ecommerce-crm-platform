"""Vector index of variant names - shared by bundle matching and supplier invoice matching.

Gemini embeddings are the only paid calls and are strictly rate-limited. On the free tier the quota
(global_embed_content_requests_per_minute_per_base_model) is a pool shared by all users of the
model: sometimes it lets a hundred names through at once, sometimes it refuses even ten. The catalog
has about a thousand names.

Previously the catalog matrix was computed on the fly and cached in process memory. Consequence: any
restart or change in the number of variants dropped the cache, the next request tried to push the
WHOLE catalog through in one go, got a 429 on the second batch - and matching silently returned
'nothing found'.

Now each name's vector is stored in the DB (catalog.VariantEmbedding). Recomputation is needed only
for new or renamed variants - a handful per day, which fits the quota with room to spare. Initial
fill is the `warm_embeddings` command.

IMPORTANT: all catalog vectors are computed with task_type=RETRIEVAL_DOCUMENT, queries with
RETRIEVAL_QUERY. Changing the model or task_type makes the stored vectors incompatible - then the
table must be cleared and re-indexed.
"""
from __future__ import annotations

import time

import numpy as np

EMBED_MODEL = 'gemini-embedding-001'

# Ceiling of batchEmbedContents is 100 contents per call. That was also the largest batch that ever
# fit into the free quota; background indexing uses a smaller one to hit the windows more often when
# the shared pool gives anything.
QUOTA_PER_MINUTE = 100


def _pack(values) -> bytes:
    return np.asarray(values, dtype=np.float32).tobytes()


def _unpack(blob) -> np.ndarray:
    return np.frombuffer(bytes(blob), dtype=np.float32)


def is_quota_error(exc) -> bool:
    text = str(exc)
    return '429' in text or 'RESOURCE_EXHAUSTED' in text or 'Quota exceeded' in text


def embed_texts(client, texts, task_type='RETRIEVAL_DOCUMENT', *,
                batch_size=QUOTA_PER_MINUTE, retries=0, retry_wait=60):
    """Embeds a list of texts in batches of `batch_size`.

    `retries` > 0 - on a 429 wait `retry_wait` seconds and retry the batch (for background
    indexing). In interactive requests we leave it at 0: an honest error is better than a request
    that hangs for a minute.
    """
    from google.genai import types as gtypes
    from core.limits import enforce_gemini

    if not texts:
        return np.zeros((0, 0), dtype=np.float32)

    enforce_gemini(len(texts))  # embeddings — the only real money spent
    out = []
    for start in range(0, len(texts), batch_size):
        batch = texts[start:start + batch_size]
        attempt = 0
        while True:
            try:
                res = client.models.embed_content(
                    model=EMBED_MODEL,
                    contents=batch,
                    config=gtypes.EmbedContentConfig(task_type=task_type),
                )
                out.extend(e.values for e in res.embeddings)
                break
            except Exception as e:
                if attempt >= retries or not is_quota_error(e):
                    raise
                attempt += 1
                time.sleep(retry_wait)
        if start + batch_size < len(texts):
            time.sleep(retry_wait if retries else 0.4)
    return np.asarray(out, dtype=np.float32)


def stale_variant_ids():
    """ids of variants that have no vector or whose vector was computed for an old name."""
    from catalog.models import ProductVariant, VariantEmbedding

    stored = dict(VariantEmbedding.objects.values_list('variant_id', 'name'))
    stale = []
    for vid, name in ProductVariant.objects.values_list('id', 'name'):
        if stored.get(vid) != name:
            stale.append(vid)
    return stale


def sync_embeddings(client, *, limit=None, batch_size=QUOTA_PER_MINUTE,
                    retries=0, retry_wait=60, progress=None):
    """Tops up vectors for new/renamed variants.

    Writes to the DB incrementally, after each batch - an interrupted run is not lost, the command
    can simply be run again. Returns (computed, remaining); `limit` caps the work of a single call.
    """
    from catalog.models import ProductVariant, VariantEmbedding

    ids = stale_variant_ids()
    total = len(ids)
    if not total:
        return 0, 0

    todo = ids[:limit] if limit else ids
    done = 0
    for start in range(0, len(todo), batch_size):
        chunk_ids = todo[start:start + batch_size]
        rows = list(
            ProductVariant.objects.filter(id__in=chunk_ids).values_list('id', 'name')
        )
        vectors = embed_texts(
            client, [name for _, name in rows], 'RETRIEVAL_DOCUMENT',
            batch_size=batch_size, retries=retries, retry_wait=retry_wait,
        )
        VariantEmbedding.objects.bulk_create(
            [
                VariantEmbedding(variant_id=vid, name=name, vector=_pack(vec))
                for (vid, name), vec in zip(rows, vectors)
            ],
            update_conflicts=True,
            update_fields=['name', 'vector', 'updated_at'],
            unique_fields=['variant'],
        )
        done += len(rows)
        if progress:
            progress(done, total)
        if start + batch_size < len(todo):
            time.sleep(retry_wait if retries else 0.4)
    return done, total - done


def variant_matrix(client, variant_ids, *, fill_limit=50):
    """Vector matrix for `variant_ids` (order preserved).

    Rows of variants without a vector are zero: cosine with them is always 0, so they never win
    matching. Before assembling we top up small gaps - so a just-added product is picked up without
    a separate command.

    Returns (matrix, missing_count) - missing_count > 0 means the index is incomplete and a
    `warm_embeddings` run is needed.
    """
    from catalog.models import VariantEmbedding

    if fill_limit:
        try:
            pending = stale_variant_ids()
            # Top up on the fly ONLY small gaps. If the index is mostly empty, that is a job for
            # warm_embeddings: otherwise we would eat the per-minute quota on the catalog and starve
            # embedding of the queries themselves.
            if pending and len(pending) <= fill_limit:
                sync_embeddings(client, limit=fill_limit)
        except Exception:
            # Quota/network is no reason to break matching: work with what is already indexed, and
            # missing_count will report the incompleteness.
            pass

    stored = {
        vid: _unpack(blob)
        for vid, blob in VariantEmbedding.objects.filter(
            variant_id__in=variant_ids
        ).values_list('variant_id', 'vector')
    }
    dim = len(next(iter(stored.values()))) if stored else 0
    if not dim:
        return np.zeros((len(variant_ids), 0), dtype=np.float32), len(variant_ids)

    matrix = np.zeros((len(variant_ids), dim), dtype=np.float32)
    missing = 0
    for i, vid in enumerate(variant_ids):
        vec = stored.get(vid)
        if vec is None or len(vec) != dim:
            missing += 1
            continue
        matrix[i] = vec
    return matrix, missing


def cosine(query_vec, matrix):
    """Cosine similarity of the query vector with each row of the matrix."""
    q = np.asarray(query_vec, dtype=np.float32)
    q = q / (np.linalg.norm(q) + 1e-10)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True) + 1e-10
    return (matrix / norms) @ q
