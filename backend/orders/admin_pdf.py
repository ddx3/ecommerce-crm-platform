"""PDF receipt of an order for the admin.

The "PDF receipt" button on the order page opens this endpoint in a new tab. The PDF is
rendered with THE SAME template as the confirmation email (email/order_confirmation.html), via
Chromium (Playwright) — so it looks 1:1 like the email the customer receives (rounded corners,
borders, fonts). The rendering is moved into a separate process (chromium_pdf.py) to avoid
conflicting with the Django event loop under ASGI.

Photos are embedded as data: URIs (compressed thumbnails) — cid: doesn't work in standalone HTML,
while Chromium renders data: without problems (this isn't email).
"""
from __future__ import annotations

import base64
import logging
import os
import subprocess
import sys
import tempfile

from django.conf import settings
from django.contrib.admin.views.decorators import staff_member_required
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.template.loader import render_to_string
from django.views.decorators.http import require_GET

from .models import Order

log = logging.getLogger(__name__)

_RENDER_SCRIPT = os.path.join(os.path.dirname(__file__), 'chromium_pdf.py')


@staff_member_required
@require_GET
def order_receipt_pdf(request, order_id: int):
    order = get_object_or_404(Order, pk=order_id)

    from core.email import build_order_confirmation_context, _media_file_path, _thumbnail_bytes
    ctx = build_order_confirmation_context(order)
    # Photo → compressed thumbnail data-URI (full-size 1080×1080 would bloat the HTML/PDF).
    for it in ctx['items']:
        path = _media_file_path(it.get('photo_url'))
        thumb = _thumbnail_bytes(path) if path else None
        it['photo_url'] = (
            f'data:image/{thumb[1]};base64,' + base64.b64encode(thumb[0]).decode('ascii')
        ) if thumb else ''
    ctx.update({
        'pdf': True,  # base.html: white background for printing instead of gray
        'site_name': settings.SITE_NAME,
        'site_url': settings.SITE_URL,
        'backend_url': getattr(settings, 'BACKEND_URL', ''),
    })
    html = render_to_string('email/order_confirmation.html', ctx)

    with tempfile.TemporaryDirectory() as tmp:
        html_path = os.path.join(tmp, 'receipt.html')
        pdf_path = os.path.join(tmp, 'receipt.pdf')
        with open(html_path, 'w', encoding='utf-8') as fh:
            fh.write(html)
        try:
            subprocess.run(
                [sys.executable, _RENDER_SCRIPT, html_path, pdf_path],
                check=True, timeout=90, capture_output=True,
            )
            with open(pdf_path, 'rb') as fh:
                pdf = fh.read()
        except subprocess.CalledProcessError as e:
            log.error('Chromium PDF render failed order=%s: %s', order.order_number,
                      e.stderr.decode('utf-8', 'ignore')[-2000:])
            return HttpResponse('Could not generate the PDF', status=500)
        except Exception:
            log.exception('Chromium PDF render error order=%s', order.order_number)
            return HttpResponse('Could not generate the PDF', status=500)

    resp = HttpResponse(pdf, content_type='application/pdf')
    # inline — opens a preview in the tab (from where it can be saved/printed).
    resp['Content-Disposition'] = f'inline; filename="order-{order.order_number}.pdf"'
    return resp
