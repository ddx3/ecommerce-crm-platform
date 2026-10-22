from pathlib import Path
import os
import sys
from datetime import timedelta
from dotenv import load_dotenv

# override=False: variables from the real environment/Docker env_file take precedence
# over the .env file. In the container .env does not make it into the image (.dockerignore), and
# config arrives via compose env_file; a leftover file must not override it.
# Locally (no env in the environment) .env is read as usual.
load_dotenv(override=False)

# The Windows console (cp1252) breaks on Ukrainian/Cyrillic characters in `manage.py`
# command output. Switch stdout/stderr to UTF-8 to avoid UnicodeEncodeError.
if sys.platform == 'win32':
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding='utf-8', errors='replace')  # type: ignore[attr-defined]
        except Exception:
            pass

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.getenv('SECRET_KEY')
if not SECRET_KEY:
    if os.getenv('DEBUG', 'False') == 'True':
        SECRET_KEY = 'django-insecure-dev-only-key-do-not-use-in-production'
    else:
        from django.core.exceptions import ImproperlyConfigured
        raise ImproperlyConfigured('SECRET_KEY environment variable is required in production')
ANTHROPIC_API_KEY = os.getenv('ANTHROPIC_API_KEY', '')
NOVA_POSHTA_API_KEY = os.getenv('NOVA_POSHTA_API_KEY', '')

# Monobank Acquiring (card payments on the site: invoice creation)
MONOBANK_TOKEN = os.getenv('MONOBANK_TOKEN', '')
MONOBANK_API_BASE = os.getenv('MONOBANK_API_BASE', 'https://api.monobank.ua')
# Monobank Personal API (statements for personal accounts / sole proprietor: a separate token!)
MONOBANK_PERSONAL_TOKEN = os.getenv('MONOBANK_PERSONAL_TOKEN', '')
# Secret in the Mono Personal API webhook URL (protection against stray POSTs).
MONOBANK_PERSONAL_WEBHOOK_SECRET = os.getenv('MONOBANK_PERSONAL_WEBHOOK_SECRET', '')
# Privat24 for business: sole-proprietor statements token. No webhooks, polling only.
PRIVAT_BUSINESS_TOKEN = os.getenv('PRIVAT_BUSINESS_TOKEN', '')
PRIVAT_BUSINESS_CLIENT_ID = os.getenv('PRIVAT_BUSINESS_CLIENT_ID', '')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY', '')
# Shared secret with the MyPlantApp/Flora app: create_order requests from a trusted
# first-party backend (server, not browser) carry the X-Flora-Order-Secret header and
# bypass the browser antibot token. Empty = bypass disabled (behavior unchanged).
FLORA_ORDER_SECRET = os.getenv('FLORA_ORDER_SECRET', '')

# LLM for the admin text buttons (see core/llm.py).
# Main path: subscription via the local claude CLI. Fallback (CLI unavailable
# or failed, e.g. in production): the regular Claude API billed per token (ANTHROPIC_API_KEY).
LLM_PREFER_API = os.getenv('LLM_PREFER_API', 'False') == 'True'  # straight to the API, bypassing the CLI
CLAUDE_MODEL = os.getenv('CLAUDE_MODEL', 'sonnet')               # alias for the CLI
CLAUDE_API_MODEL = os.getenv('CLAUDE_API_MODEL', 'claude-sonnet-4-6')  # full id for the API
CLAUDE_BIN = os.getenv('CLAUDE_BIN', '')  # explicit path to claude (for production/Docker)
# Bundle instruction processing (catalog/instruction_agent.py::normalize_by_mode):
#   script — a single call (instruction_llm.py): fast and cheap, but a rejection = start over;
#   agent  — always the agent (step by step, with tools): ×2–3 in time, ×3–10 in tokens,
#            fixes validation rejections itself;
#   auto   — script first, and ONLY when its result is rejected by the dose check does the agent
#   finish the job.
# INSTRUCTION_MODE — the "Run through AI" button (a human waits and sees the
# result): agent.
# INSTRUCTION_BACKGROUND_MODE — scheduler and one-off commands (no human, dozens of rows in a row):
# auto, otherwise the queue of contents swaps would eat the daily token limit (3M) that the buttons
# depend on.
INSTRUCTION_MODE = os.getenv('INSTRUCTION_MODE', 'agent')
INSTRUCTION_BACKGROUND_MODE = os.getenv('INSTRUCTION_BACKGROUND_MODE', 'auto')

# AI usage limits (protection against loop bugs/spam; see core/limits.py).
# They do not interfere with normal work; they catch only anomalies (thousands of calls).
LLM_RATE_LIMIT = int(os.getenv('LLM_RATE_LIMIT', '20'))            # subscription: requests per window (loop protection)
LLM_RATE_WINDOW = int(os.getenv('LLM_RATE_WINDOW', '300'))         # rate window, sec (300 = 5 min)
LLM_TOKENS_PER_DAY = int(os.getenv('LLM_TOKENS_PER_DAY', '3000000'))  # Claude subscription: tokens/day (in+out)
GEMINI_LIMIT_PER_DAY = int(os.getenv('GEMINI_LIMIT_PER_DAY', '5000'))  # embeddings/day (the only real money)
TG_MSGS_PER_HOUR = int(os.getenv('TG_MSGS_PER_HOUR', '20'))        # bot: messages/hour per chat

# ── Analyst (analyst app): a daily analyst, the "Analyst" section in the CRM inbox ──
# Does not write to shop data (does not touch orders, stock, prices); changes its own analyst_*
# tables
# and, through the helper on the VPS, the server (disk, backup, scheduler restart).
ANALYST_RUN_HOUR = int(os.getenv('ANALYST_RUN_HOUR', '8'))                    # launch hour, Europe/Kyiv
# Who fixes the server: ask — buttons (the owner presses "Apply"); auto — the Analyst
# performs the allowed
# actions itself, no buttons; off — cards only. Kill switch: set back to ask/off and restart
# analyst.
ANALYST_AUTOFIX = os.getenv('ANALYST_AUTOFIX', 'ask')
ANALYST_AUTOFIX_MAX_PER_DAY = int(os.getenv('ANALYST_AUTOFIX_MAX_PER_DAY', '3'))   # identical actions per day
ANALYST_AUTOFIX_COOLDOWN_HOURS = int(os.getenv('ANALYST_AUTOFIX_COOLDOWN_HOURS', '6'))   # pause before retrying
# The "Analyst" section is visible only to this user (by email). All other staff get
# a 403 on the page, no row in the sidebar and no number in the badge.
ANALYST_OWNER_EMAIL = os.getenv('ANALYST_OWNER_EMAIL', os.getenv('OWNER_EMAIL', 'owner@example.com'))
# Shared queue folder with the helper on the VPS (ops/shop_helper.py): the buttons "free up space",
# "backup now", "restart the scheduler". Empty = channel
# disabled, no buttons.
ANALYST_OPS_DIR = os.getenv('ANALYST_OPS_DIR', '')

# Purchasing: how many days a delivery takes and how many days of sales after it we need stock for.
# Forecast horizon = LEAD + COVER. The owner did not specify a delivery time — 14 days is an
# assumption;
# it can be changed without a deploy (env) or later separately per supplier.
ANALYST_LEAD_DAYS = int(os.getenv('ANALYST_LEAD_DAYS', '14'))
ANALYST_COVER_DAYS = int(os.getenv('ANALYST_COVER_DAYS', '45'))

# LLM layer of the Analyst (purchasing agent): disabled until explicitly enabled. Its own token
# budget — so the analyst doesn't eat the subscription limit that the CRM buttons depend on.
ANALYST_AGENT_ENABLED = os.getenv('ANALYST_AGENT_ENABLED', 'False') == 'True'
ANALYST_MODEL = os.getenv('ANALYST_MODEL', 'opus')                           # purchasing decision: stronger model
ANALYST_THINKING_TOKENS = int(os.getenv('ANALYST_THINKING_TOKENS', '8000'))   # «thinking» budget (0 = disabled)
# Noise cut-off before the agent: a purchase for less than ANALYST_MIN_REVENUE UAH of revenue over
# the horizon,
# or an item that sold in fewer than ANALYST_MIN_SALES_MONTHS months out of the year (one-off sales
# = clearing leftover stock), is not shown.
ANALYST_MIN_REVENUE = int(os.getenv('ANALYST_MIN_REVENUE', '1000'))
ANALYST_MIN_SALES_MONTHS = int(os.getenv('ANALYST_MIN_SALES_MONTHS', '2'))
ANALYST_LLM_RATE_LIMIT = int(os.getenv('ANALYST_LLM_RATE_LIMIT', '12'))         # requests/window
ANALYST_LLM_RATE_WINDOW = int(os.getenv('ANALYST_LLM_RATE_WINDOW', '600'))      # window, sec
ANALYST_LLM_TOKENS_PER_DAY = int(os.getenv('ANALYST_LLM_TOKENS_PER_DAY', '1500000'))
# Directory with DB dumps, mounted into the container read-only (/opt/backups on the VPS).
# An empty string disables the check.
ANALYST_BACKUP_DIR = os.getenv('ANALYST_BACKUP_DIR', '/backups')

# Auto-fix of code errors (analyst/codefix): off | propose (prepares a fix and waits for the owner's
# button) |
# auto (after the sandbox check and the reviewer's approval, rolls it out itself, at night). File
# policy — analyst/codefix_policy.py.
ANALYST_CODEFIX = os.getenv('ANALYST_CODEFIX', 'off')
ANALYST_CODEFIX_MIN_REPEATS = int(os.getenv('ANALYST_CODEFIX_MIN_REPEATS', '3'))            # number of repeats per day before taking it on
# Signing key for auto-fix tasks: the file exists only in analyst (docker-compose) and in the helper
# on the VPS; without it the pipeline does not work.
ANALYST_CODEFIX_KEY_FILE = os.getenv('ANALYST_CODEFIX_KEY_FILE', '')
ANALYST_SESSION_MAX_USD = float(os.getenv('ANALYST_SESSION_MAX_USD', '8'))                      # ceiling on the cost of a single LLM session
ANALYST_CODEFIX_MAX_DRAFTS = int(os.getenv('ANALYST_CODEFIX_MAX_DRAFTS', '3'))               # agent attempts per error
ANALYST_CODEFIX_MAX_STARTS_PER_DAY = int(os.getenv('ANALYST_CODEFIX_MAX_STARTS_PER_DAY', '3'))
ANALYST_CODEFIX_RETRY_HOURS = int(os.getenv('ANALYST_CODEFIX_RETRY_HOURS', '24'))
ANALYST_CODEFIX_WATCH_HOURS = int(os.getenv('ANALYST_CODEFIX_WATCH_HOURS', '24'))             # monitoring after rollout


# ── Telegram bot (telegrambot app) ──────────────────────────────────────────
# The bot = a Claude agent: accepts commands from Telegram and executes them through a
# limited set of tools (catalog/orders ORM). Launch:
# `python manage.py run_telegram_bot` (long-polling, separate process/container).
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
# Whitelist by Telegram user id (comma-separated). Commands from others are ignored.
# EMPTY = the bot answers no one (protection against an accidental launch without configuration).
TELEGRAM_ALLOWED_USER_IDS = {
    int(x) for x in os.getenv('TELEGRAM_ALLOWED_USER_IDS', '').split(',') if x.strip()
}
# In groups react only to a reply to the bot / mention / prefix command,
# so the bot does not grab every chat message.
TELEGRAM_REQUIRE_MENTION_IN_GROUP = os.getenv('TELEGRAM_REQUIRE_MENTION_IN_GROUP', 'True') == 'True'
# Safety fuse: maximum write operations (create/modify) per command.
# Protection against an ambiguous request like "zero out all prices".
TELEGRAM_MAX_WRITES_PER_COMMAND = int(os.getenv('TELEGRAM_MAX_WRITES_PER_COMMAND', '10'))

DEBUG = os.getenv('DEBUG', 'False') == 'True'
ALLOWED_HOSTS = [h for h in os.getenv('ALLOWED_HOSTS', '').split(',') if h]

# Feature flags. COURSES_ENABLED=False hides the Courses section in the Django admin
# (models stay, migrations/API/signals work; only the UI in /admin/ is disabled).
COURSES_ENABLED = os.getenv('COURSES_ENABLED', 'True') == 'True'

# DROPSYNC_ENABLED=False hides the "Supplier changes" section in the inbox
# (sidebar,
# tab and the /admin/notifications/dropsync/ page). Sync tasks keep writing
# DropSyncEvent to the DB; only the viewing UI is disabled.
DROPSYNC_ENABLED = os.getenv('DROPSYNC_ENABLED', 'True') == 'True'

INSTALLED_APPS = [
    # daphne must come before staticfiles to override runserver with ASGI
    'daphne',
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    # sitemaps: templates + the sitemap framework only. Does NOT require django.contrib.sites
    # (domain/protocol are pinned in core.sitemaps via a fake site).
    'django.contrib.sitemaps',
    'channels',
    'rest_framework',
    'rest_framework_simplejwt.token_blacklist',
    'corsheaders',
    'django_summernote',
    'easy_thumbnails',
    'users',
    'catalog',
    'courses',
    'orders',
    'chat',
    'blog',
    'analyst',
    # 'telegrambot',  # not deploying yet: the app is kept locally, not committed to git
    # Must be LAST: it cleans up FileField/ImageField files from disk when a record
    # is deleted and when a file is replaced (post_delete / pre_save). Without it orphan files
    # accumulated (product photos, course videos, receipts, etc.).
    'django_cleanup.apps.CleanupConfig',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.middleware.gzip.GZipMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    # Maintenance mode: after Auth, so request.user.is_staff is visible.
    'catalog.middleware.MaintenanceModeMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        # Django 6 enables cached.Loader UNCONDITIONALLY (the old 'do not cache under DEBUG'
        # behavior was removed from the engine). Together with runserver --noreload this meant that
        # a
        # template was read from disk once per process lifetime, and every edit of
        # admin templates required a full server restart. Under DEBUG we set the
        # loaders explicitly, without a cache; APP_DIRS has to be turned off because Django forbids
        # setting it together with loaders. In production (DEBUG=False) everything is as before.
        'APP_DIRS': not DEBUG,
        'OPTIONS': {
            **({'loaders': [
                'django.template.loaders.filesystem.Loader',
                'django.template.loaders.app_directories.Loader',
            ]} if DEBUG else {}),
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'catalog.context_processors.sidebar_counts',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'
ASGI_APPLICATION = 'config.asgi.application'

# Channels: Redis for prod, InMemory fallback for local dev
_REDIS_URL = os.getenv('REDIS_URL', '')
if _REDIS_URL:
    CHANNEL_LAYERS = {
        'default': {
            'BACKEND': 'channels_redis.core.RedisChannelLayer',
            'CONFIG': {'hosts': [_REDIS_URL]},
        },
    }
else:
    CHANNEL_LAYERS = {
        'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'},
    }

# Cache: Redis for prod (shared between uvicorn workers — critical for
# DRF throttling and Monobank pubkey cache). LocMemCache fallback for local dev.
if _REDIS_URL:
    CACHES = {
        'default': {
            'BACKEND': 'django.core.cache.backends.redis.RedisCache',
            'LOCATION': _REDIS_URL,
        },
    }
    # Sessions go to the Redis cache so as not to load Postgres with extra queries.
    SESSION_ENGINE = 'django.contrib.sessions.backends.cache'
else:
    CACHES = {
        'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'},
    }

if os.getenv('DB_ENGINE', 'sqlite') == 'postgresql':
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.postgresql',
            'NAME': os.getenv('DB_NAME'),
            'USER': os.getenv('DB_USER'),
            'PASSWORD': os.getenv('DB_PASSWORD'),
            'HOST': os.getenv('DB_HOST', 'localhost'),
            'PORT': os.getenv('DB_PORT', '5432'),
            # Persistent connections: reuse the socket between requests instead of TCP+auth every
            # time.
            # CONN_HEALTH_CHECKS is mandatory: reconnect if the socket died (DB restart / idle
            # timeout).
            'CONN_MAX_AGE': int(os.getenv('DB_CONN_MAX_AGE', '60')),
            'CONN_HEALTH_CHECKS': True,
        }
    }
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'db.sqlite3',
        }
    }

AUTH_USER_MODEL = 'users.User'

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = 'uk'
TIME_ZONE = 'Europe/Kyiv'
USE_I18N = True
USE_TZ = True

FORMAT_MODULE_PATH = ['formats']

DATE_FORMAT          = 'd.m.Y'
DATETIME_FORMAT      = 'd.m.Y H:i'
TIME_FORMAT          = 'H:i'
SHORT_DATE_FORMAT    = 'd.m.Y'
SHORT_DATETIME_FORMAT = 'd.m.Y H:i'

STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_DIRS = [BASE_DIR / 'static']

MEDIA_URL = 'media/'
MEDIA_ROOT = BASE_DIR / 'media'

# Paid course videos are served via X-Accel-Redirect to an internal nginx location
# (see courses/video_access.py, frontend/nginx.conf). There is no nginx locally, so
# Django itself streams the file via FileResponse.
COURSES_X_ACCEL = not DEBUG

# Built frontend (dist) for the shell view: we read index.html from here and inject the SEO <head>.
# Production: shared frontend_dist volume (filled by the nginx container). Locally (after
# `npm run build`) it is frontend/dist. Overridable via env FRONTEND_DIST.
FRONTEND_DIST = os.getenv('FRONTEND_DIST', str(BASE_DIR.parent / 'frontend' / 'dist'))

# easy-thumbnails: reduced versions of photos for listings (catalog/home/search).
# The original is untouched; thumbnails are generated on demand with a disk cache.
# crop=False: fit into the box preserving proportions (like object-contain on the frontend).
THUMBNAIL_ALIASES = {
    '': {
        'catalog': {'size': (400, 400), 'quality': 80, 'crop': False, 'upscale': False},
        'thumb':   {'size': (150, 150), 'quality': 80, 'crop': False, 'upscale': False},
        # Large version for the product page gallery (LCP): webp, lighter than the original.
        # The original stays for zoom in the lightbox.
        'large':   {'size': (1000, 1000), 'quality': 82, 'crop': False, 'upscale': False},
    },
}
# Thumbnails are served as WebP: ~50-70% lighter than JPG at the same quality (faster LCP, a
# ranking factor). Transparent ones (PNG with alpha) stay PNG (transparency_extension).
# The extension determines the encoding format; the alias sizes/quality themselves do not change.
THUMBNAIL_EXTENSION = 'webp'
# If thumbnail generation fails, do not fail the request; serve the original (see serializers).
THUMBNAIL_DEBUG = False

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Email
EMAIL_BACKEND = os.getenv('EMAIL_BACKEND', 'django.core.mail.backends.console.EmailBackend')
EMAIL_HOST = os.getenv('EMAIL_HOST', '')
EMAIL_PORT = int(os.getenv('EMAIL_PORT', '465'))
EMAIL_USE_SSL = os.getenv('EMAIL_USE_SSL', 'True') == 'True'
EMAIL_USE_TLS = os.getenv('EMAIL_USE_TLS', 'False') == 'True'
EMAIL_HOST_USER = os.getenv('EMAIL_HOST_USER', '')
EMAIL_HOST_PASSWORD = os.getenv('EMAIL_HOST_PASSWORD', '')
DEFAULT_FROM_EMAIL = os.getenv('DEFAULT_FROM_EMAIL', 'noreply@example.com')
SITE_NAME = os.getenv('SITE_NAME', 'Magiya Roslyn')
SITE_URL = os.getenv('SITE_URL', 'http://localhost:5173')

# Shop owner's e-mail: only this account sees the owner-only CRM sections
# (analytics dashboard, courses, site settings).
OWNER_EMAIL = os.getenv('OWNER_EMAIL', 'owner@example.com').strip().lower()
BACKEND_URL = os.getenv('BACKEND_URL', 'http://localhost:8000')

# Google Search Console: fallback domain verification method (meta tag on the home page).
# The main one is DNS TXT. If set, the shell adds <meta name="google-site-verification">.
GOOGLE_SITE_VERIFICATION = os.getenv('GOOGLE_SITE_VERIFICATION', '')

CORS_ALLOWED_ORIGINS = [o.strip() for o in os.getenv(
    'CORS_ALLOWED_ORIGINS',
    'http://localhost:5173,http://localhost:3000'
).split(',') if o.strip()]

REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'rest_framework_simplejwt.authentication.JWTAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticatedOrReadOnly',
    ],
    'DEFAULT_THROTTLE_CLASSES': [
        'rest_framework.throttling.AnonRateThrottle',
        'rest_framework.throttling.UserRateThrottle',
        'rest_framework.throttling.ScopedRateThrottle',
    ],
    'DEFAULT_THROTTLE_RATES': {
        # In DEBUG mode we raise the limits a lot: Vite hot-reload + StrictMode double the
        # requests, and Catalog fetches categories / products / bundles in parallel; the usual
        # 120/min is quickly exhausted during active development.
        'anon': '10000/min' if DEBUG else '120/min',
        'user': '10000/min' if DEBUG else '300/min',
        'login': '10/min',
        'register': '5/min',
        'password_reset': '5/min',
        'review_create': '5/hour',
        'question_create': '10/hour',
        'stock_notify': '20/hour',
        'np_proxy': '60/min',
        'order_create': '20/hour',
        'antibot_token': '120/min',
        'payment_action': '10000/hour' if DEBUG else '40/hour',
        # Each call launches Chromium, which is expensive, so a separate scope below 'user'.
        'instruction_pdf': '10000/hour' if DEBUG else '30/hour',
    },
}

# Invisible anti-bot protection for public forms (honeypot + signed time-token).
# Disabled in tests/debugging via .env so you don't have to fiddle with the token.
ANTIBOT_ENABLED = os.getenv('ANTIBOT_ENABLED', 'True') == 'True'

SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(minutes=int(os.getenv('JWT_ACCESS_MIN', '15'))),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=int(os.getenv('JWT_REFRESH_DAYS', '7'))),
    'ROTATE_REFRESH_TOKENS': True,
    'BLACKLIST_AFTER_ROTATION': True,
}

# Upload limits: protection against DoS on upload
DATA_UPLOAD_MAX_MEMORY_SIZE = 25 * 1024 * 1024   # 25 MB form-data
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024    # 5 MB before disk
DATA_UPLOAD_MAX_NUMBER_FILES = 30
DATA_UPLOAD_MAX_NUMBER_FIELDS = 5000

# Password reset token lives 1 hour (Django default is 3 days)
PASSWORD_RESET_TIMEOUT = 60 * 60

# Production security headers
if not DEBUG:
    SECURE_SSL_REDIRECT = os.getenv('SECURE_SSL_REDIRECT', 'True') == 'True'
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
    # HTTPS-dependent settings are enabled only when SSL is really present (SECURE_SSL_REDIRECT).
    # Otherwise (deploy over HTTP on an IP before the domain is connected) Secure cookies break
    # login: the browser does not send a Secure cookie over HTTP -> CSRF 403.
    _HTTPS_ON = SECURE_SSL_REDIRECT
    SECURE_HSTS_SECONDS = int(os.getenv('SECURE_HSTS_SECONDS', '31536000')) if _HTTPS_ON else 0
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    # strict-origin-when-cross-origin (not same-origin): third-party embeds (YouTube)
    # without a Referer throw Error 153. In sync with nginx.conf.
    SECURE_REFERRER_POLICY = 'strict-origin-when-cross-origin'
    SESSION_COOKIE_SECURE = _HTTPS_ON
    CSRF_COOKIE_SECURE = _HTTPS_ON
    CSRF_TRUSTED_ORIGINS = [o.strip() for o in os.getenv('CSRF_TRUSTED_ORIGINS', '').split(',') if o.strip()]
    X_FRAME_OPTIONS = 'DENY'

SUMMERNOTE_CONFIG = {
    'summernote': {
        'width': '100%',
        'height': '320px',
        'toolbar': [
            ['style',   ['style']],
            ['font',    ['bold', 'italic', 'underline', 'strikethrough', 'superscript', 'subscript', 'clear']],
            ['fontsize',['fontsize']],
            ['color',   ['color']],
            ['para',    ['ul', 'ol', 'paragraph']],
            ['table',   ['table']],
            ['insert',  ['link', 'picture', 'hr']],
            ['view',    ['fullscreen', 'codeview']],
        ],
        'styleTags': ['p', 'h1', 'h2', 'h3', 'h4', 'blockquote', 'pre'],
        'fontSizes': ['10', '11', '12', '13', '14', '16', '18', '20', '24', '28', '36'],
    },
    'attachment_require_authentication': True,
}

# Logging: everything to stdout; Docker/systemd pick up the stream. Payment events
# (orders, monobank) are logged at INFO level regardless of the overall level.
LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO').upper()
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '{asctime} [{levelname}] {name}: {message}',
            'style': '{',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'verbose',
        },
    },
    'root': {
        'handlers': ['console'],
        'level': LOG_LEVEL,
    },
    'loggers': {
        'django': {
            'handlers': ['console'],
            'level': LOG_LEVEL,
            'propagate': False,
        },
        'orders': {
            'handlers': ['console'],
            'level': 'INFO',
            'propagate': False,
        },
        'courses': {
            'handlers': ['console'],
            'level': 'INFO',
            'propagate': False,
        },
    },
}

# Code errors (ERROR with a traceback) are collected into analyst.ErrorEvent — the input for the
# Analyst's auto-fixes.
# The handler never raises and never slows down the request (see analyst/errorlog.py). Disabled
# during tests.
if os.getenv('ANALYST_ERRORLOG', 'on').lower() not in ('off', '0', 'false') and 'test' not in sys.argv:
    LOGGING['handlers']['analyst_errors'] = {'class': 'analyst.errorlog.ErrorEventHandler', 'level': 'ERROR'}
    LOGGING['root']['handlers'].append('analyst_errors')
    for _name in ('django', 'orders', 'courses'):
        LOGGING['loggers'][_name]['handlers'].append('analyst_errors')
    # Exceptions in WebSocket consumers (chat) are logged by uvicorn.error, not django: without this
    # they are invisible.
    LOGGING['loggers']['uvicorn.error'] = {'handlers': ['console', 'analyst_errors'], 'level': 'INFO', 'propagate': False}

# Safety check: in production with the console backend, emails (order confirmations,
# password reset, availability notifications) are silently NOT sent. We warn loudly
# to stderr at startup so this does not go unnoticed.
if not DEBUG and EMAIL_BACKEND.endswith('console.EmailBackend'):
    print(
        'WARNING: EMAIL_BACKEND=console in production — order e-mails are NOT being sent. '
        'Set EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend and EMAIL_* in .env',
        file=sys.stderr,
    )
