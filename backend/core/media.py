# -*- coding: utf-8 -*-
"""Serving media files with HTTP Range request support.

The standard `django.views.static.serve` returns the whole file (200 OK) and does not understand the
`Range` header. Without Range the browser cannot seek in a video - `<video>` plays only sequentially
from the start. This view answers Range requests with `206 Partial Content`, which enables seeking.

Used only in DEBUG (dev). In production media must be served by nginx/CDN.
"""
import mimetypes
import os
import re

from django.http import FileResponse, Http404, StreamingHttpResponse
from django.utils._os import safe_join

_RANGE_RE = re.compile(r'bytes=(\d+)-(\d*)', re.IGNORECASE)


def _chunks(file_obj, length, chunk_size=8192):
    remaining = length
    try:
        while remaining > 0:
            data = file_obj.read(min(chunk_size, remaining))
            if not data:
                break
            remaining -= len(data)
            yield data
    finally:
        file_obj.close()


def serve_media(request, path, document_root):
    full_path = safe_join(document_root, path)
    if not os.path.isfile(full_path):
        raise Http404(path)

    size = os.path.getsize(full_path)
    content_type = mimetypes.guess_type(full_path)[0] or 'application/octet-stream'
    range_header = request.headers.get('Range', '')
    match = _RANGE_RE.match(range_header)

    if match:
        start = int(match.group(1))
        end = int(match.group(2)) if match.group(2) else size - 1
        end = min(end, size - 1)
        if start > end:
            start = 0
        length = end - start + 1
        f = open(full_path, 'rb')
        f.seek(start)
        response = StreamingHttpResponse(
            _chunks(f, length), status=206, content_type=content_type,
        )
        response['Content-Length'] = str(length)
        response['Content-Range'] = f'bytes {start}-{end}/{size}'
        response['Accept-Ranges'] = 'bytes'
        return response

    response = FileResponse(open(full_path, 'rb'), content_type=content_type)
    response['Content-Length'] = str(size)
    response['Accept-Ranges'] = 'bytes'
    return response
