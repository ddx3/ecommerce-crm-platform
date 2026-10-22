FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/root \
    DISABLE_AUTOUPDATER=1

WORKDIR /app

# postgresql-client — for pg_dump/restore (data transfer, backups) inside the container.
# Node + claude CLI — the text LLM buttons in the admin go through the Claude subscription
# (core/llm.py runs `claude` as a subprocess). The subscription token is passed via the env var
# CLAUDE_CODE_OAUTH_TOKEN (`claude setup-token`). Fallback when the limit is hit — the Claude API.
# libgomp1 — OpenMP runtime for onnxruntime (background removal in product photos).
RUN apt-get update && apt-get install -y --no-install-recommends \
        postgresql-client curl ca-certificates gnupg libgomp1 \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @anthropic-ai/claude-code \
    && npm cache clean --force \
    && apt-get purge -y curl gnupg && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

# Chromium for order PDF receipts (orders/chromium_pdf.py via Playwright).
# --with-deps pulls in the browser's system libraries; fonts-dejavu-core — Cyrillic in the PDF
# (a slim image without fonts would otherwise render squares). The browser is installed into
# /root/.cache —
# the container runs as root, so the runtime process will find it.
RUN apt-get update \
    && python -m playwright install --with-deps chromium \
    && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# Bake the background-removal ML model (u2net, ~170MB) into the image — so the first click on
# "bring to standard" does not wait for a download from GitHub and the model
# is not lost on rebuild.
# Via fetch_u2net.py, because the GitHub releases CDN breaks a continuous transfer of this size
# (Range requests do work) — details in the script header.
COPY fetch_u2net.py /tmp/fetch_u2net.py
RUN python /tmp/fetch_u2net.py && rm /tmp/fetch_u2net.py

COPY . .

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["/entrypoint.sh"]
# uvicorn with several workers serves HTTP and WebSocket (chat). Several processes
# (WEB_CONCURRENCY) — so that one long sync view (LLM/OCR/rembg in staff actions) does not
# block the whole site. Shell-form CMD — so that ${WEB_CONCURRENCY} gets expanded.
# --proxy-headers/--forwarded-allow-ips: we trust X-Forwarded-Proto from nginx.
CMD uvicorn config.asgi:application --host 0.0.0.0 --port 8000 \
    --workers ${WEB_CONCURRENCY:-3} --proxy-headers --forwarded-allow-ips '*'
