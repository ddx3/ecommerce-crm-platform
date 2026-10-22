#!/bin/sh
set -e

echo "==> Waiting for database..."
until python -c "import os, psycopg2; psycopg2.connect(dbname=os.environ['DB_NAME'], user=os.environ['DB_USER'], password=os.environ['DB_PASSWORD'], host=os.environ['DB_HOST'], port=os.environ.get('DB_PORT','5432'))" 2>/dev/null; do
  sleep 1
done

# Auxiliary services (bot, scheduler) share the image with web, but migrations/collectstatic
# are run ONLY by web. They just wait until web applies the migrations (migrate --check
# fails if there are unapplied ones) and then start — so there is no race between several migrate
# runs.
case "$*" in
  *run_telegram_bot*|*run_scheduler*|*run_analyst*)
    echo "==> Worker service: waiting for migrations (applied by web)..."
    until python manage.py migrate --check >/dev/null 2>&1; do
      sleep 2
    done
    echo "==> Starting worker: $@"
    exec "$@"
    ;;
esac

echo "==> Applying migrations..."
python manage.py migrate --noinput

echo "==> Collecting static files..."
python manage.py collectstatic --noinput

echo "==> Starting: $@"
exec "$@"
