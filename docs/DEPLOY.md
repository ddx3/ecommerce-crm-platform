# Server deployment

Docker stack: **nginx** (frontend + proxy) → **web** (Django/daphne) → **db** (PostgreSQL) + **redis**.

## 0. What the server (VPS) needs
- Ubuntu 22.04/24.04 (or another Linux)
- Docker + Docker Compose plugin:
  ```bash
  curl -fsSL https://get.docker.com | sh
  ```
- Ports 80 and 443 open

## 1. Put the project on the server
```bash
git clone <repo-url> /opt/shop
cd /opt/shop
```

## 2. Configure .env
```bash
cp .env.example .env
nano .env
```
Fill in:
- `SECRET_KEY` — generate one: `python3 -c "import secrets; print(secrets.token_urlsafe(64))"`
- `DB_PASSWORD` — a new strong password
- `ALLOWED_HOSTS`, `CORS_ALLOWED_ORIGINS`, `CSRF_TRUSTED_ORIGINS`, `SITE_URL`, `BACKEND_URL` — your domain
- Monobank / Nova Poshta tokens, SMTP credentials
- **While there is no SSL** — set `SECURE_SSL_REDIRECT=False` (otherwise you get a redirect loop)

## 3. First start
```bash
docker compose up -d --build
```
`web` applies the migrations and collects static files by itself. Check:
```bash
docker compose ps
docker compose logs -f web
```
The site will open at `http://<server-ip>`.

## 4. Create an administrator
```bash
docker compose exec web python manage.py createsuperuser
```

## 5. Migrate data from the working database
On the local machine (where the data currently lives — 462 products, users, etc.):
```bash
# dump of the local Postgres
pg_dump -U postgres -d shop_db -F c -f shop_dump.dump
# copy to the server
scp shop_dump.dump root@<ip>:/opt/shop/
```
On the server:
```bash
docker compose cp shop_dump.dump db:/tmp/shop_dump.dump
docker compose exec db pg_restore -U $DB_USER -d $DB_NAME --clean --if-exists /tmp/shop_dump.dump
```
Media files (product photos) from the local `backend/media/`:
```bash
scp -r backend/media/* root@<ip>:/opt/shop/_media/
docker compose cp _media/. web:/app/media/
```

## 6. HTTPS (after the domain points to the server)
```bash
sudo apt install certbot
docker compose stop nginx
sudo certbot certonly --standalone -d your-domain.com -d www.your-domain.com
docker compose start nginx
```
Next — add a 443 block with the certificates to nginx and mount `/etc/letsencrypt`
into the `nginx` service (we'll do it together once there is a domain). After that, in `.env`:
`SECURE_SSL_REDIRECT=True` and `docker compose restart web`.

## LLM buttons in the admin (Claude subscription + fallback)

The text buttons in the editors (description, characteristics, cleanup, block extraction,
bundle composition) work through a **Claude subscription**: the image contains the `claude`
CLI (installed in the Dockerfile), and Django runs it as a subprocess
(`core/llm.py`). If the CLI fails or the subscription limit is exhausted, there is an automatic
**fallback via the Claude API** (billed per token).

In `backend/.env` on the server:
```env
# Claude subscription token (Max/Pro). Generate it on a machine where you are logged in
# to the subscription:  claude setup-token  — and paste the resulting token here.
# It is what gives subscription billing in the container (not API funds).
CLAUDE_CODE_OAUTH_TOKEN=

# Fallback: the regular Claude API. Kicks in only when the subscription is unavailable
# (limit/CLI failure). Without it, when the subscription limit is hit the buttons return an error.
ANTHROPIC_API_KEY=

# (optional) force the API, bypassing the CLI:  LLM_PREFER_API=True
# (optional) models:  CLAUDE_MODEL=sonnet  (CLI),  CLAUDE_API_MODEL=claude-sonnet-4-6
```

The subscription token expires/gets revoked — then the buttons switch to the fallback
API on their own (if `ANTHROPIC_API_KEY` is set); to bring the subscription back, regenerate with
`claude setup-token` and update `.env` + `docker compose restart web`.

> Embeddings (matching invoices and bundle products) are separate, always via
> Gemini (`GEMINI_API_KEY`): Claude has no embedding models.

## Feature flags (env switches)

In `backend/.env` we keep switches that hide admin sections which are not ready yet.
By default (no line in .env) they are `True`, so a local machine
sees everything. On prod we set `False` so the manager doesn't see a raw UI.
When a section is ready → change it to `True` and `docker compose restart` (no
code changes needed, no re-upload needed).

ℹ️ `COURSES_ENABLED` applies ONLY to the admin (CRM): it hides the Courses section
(models are not registered — courses/admin.py) and the CRM tab 'Courses' (base_site.html
via a context_processor). The storefront ALWAYS shows courses — the frontend does not
read this flag. The change is runtime, `docker compose restart web` is enough.

```env
# CRM only: the Courses section + the 'Courses' tab. Courses are always visible on the storefront.
COURSES_ENABLED=False

# The Label section in Django Admin. The labels on products themselves (M2M, system labels,
# serialization to the frontend, auto_days) work independently.
LABELS_ADMIN_ENABLED=False

# The 'Supplier changes' section in the inbox (sidebar + tab + page
# /admin/notifications/dropsync/). DropSyncEvent records keep accumulating
# in the DB via the sync commands.
DROPSYNC_ENABLED=False
```

Comments for each flag are in [backend/config/settings.py](backend/config/settings.py)
next to the definitions.

### Checklist when deploying a new version

0. **Analyst auto-fixes** (see [docs/ANALYST.md](docs/ANALYST.md)): `touch /opt/shop-autofix/PAUSE`; check
   whether the server has auto-commits that are not yet in git (`git log origin/<branch>..HEAD`) — if there are, move them over
   first (`git fetch ssh://root@<VPS>/opt/shop <branch>:refs/heads/vps-autofix` → `git merge --ff-only vps-autofix` → push);
   run all the steps below under `flock /opt/shop-autofix/deploy.lock`; at the end `rm /opt/shop-autofix/PAUSE`.
   If the release changes `ops/*` or `backend/analyst/codefix_policy.py` — also `sh ops/install.sh` (from a full copy of the project).
1. `git push` locally (from master).
2. `ssh root@<server-ip> && cd /opt/shop && git pull`.
3. **Compare `backend/.env`** with the block above — if the commit introduced a new
   flag, add the missing line (otherwise the default `True` will show a raw section
   on prod).
4. `docker compose up -d --build`.
5. Smoke-check `/admin/`: the hidden sections are absent, the main ones (orders,
   products, chat, reviews, payments) work.

### Enable a hidden feature on prod

1. In `backend/.env` change the flag value from `False` to `True`.
2. `docker compose restart` (no rebuild if the code wasn't changed).
3. Verify that the section appeared.

## Useful commands
```bash
docker compose logs -f web          # backend logs
docker compose restart web          # restart after changing .env
docker compose down                 # stop everything
docker compose up -d --build        # update after git pull

# Database backup
docker compose exec db pg_dump -U $DB_USER $DB_NAME -F c -f /tmp/backup.dump
docker compose cp db:/tmp/backup.dump ./backup-$(date +%F).dump
```

## Regular backups (cron on the server)
```cron
0 3 * * * cd /opt/shop && docker compose exec -T db pg_dump -U shop_user shop_db -F c > /opt/backups/db-$(date +\%F).dump
```
