# SEO — how it works and what to do after deployment

Canonical domain: **https://magiyaroslyn.com** (no www).

## Architecture

The site is a React SPA. So that bots see unique meta tags and structured data, Django
**injects `<head>` on the fly** (server-side injection, no User-Agent sniffing and no
Playwright prerenderer). The page body is rendered by React.

- **nginx** proxies navigation HTML requests to `web` (Django) instead of serving a static
  `index.html`. Assets/api/media bypass it (they have their own `location` blocks).
- **`core/shell.py`** reads `index.html` (from the shared `frontend_dist` volume), substitutes
  `title`/`description`/`canonical`/OG/Twitter + JSON-LD from the DB into `<head>`, and returns
  the correct status (404 for missing products — the soft-404 fix).
- **`core/seo.py`** — JSON-LD generators (Product/AggregateOffer/AggregateRating,
  BreadcrumbList, Article, Course, WebSite+SearchAction, Organization) and the
  canonical/robots logic.
- **`core/sitemaps.py`** — `sitemap.xml` (index + sections products/bundles/categories/
  brands/blog/courses/static). **`core/robots.py`** — `robots.txt`.
- **Client**: `frontend/src/components/Seo.tsx` updates meta tags on SPA navigation
  (upsert, no duplicates). JSON-LD is set only by the shell.

## Delivering index.html to `web` (important)

`web` does not contain the built frontend. The shared `frontend_dist` volume is populated by the nginx container
on startup (`docker-compose.yml`: the nginx `command` does `cp index.html` BEFORE starting).
The SEO-`<head>` cache is versioned by the mtime of index.html → a deploy invalidates the cache automatically.

## Required env (prod `.env` in the root of `/opt/shop`)

```
SITE_URL=https://magiyaroslyn.com        # all canonical/OG/sitemap URLs derive from it (NO www!)
SITE_NAME=Magiya Roslyn
ALLOWED_HOSTS=magiyaroslyn.com,www.magiyaroslyn.com
GOOGLE_SITE_VERIFICATION=                 # optional, fallback verification (see below)
# FRONTEND_DIST is already set in docker-compose (web → /app/frontend_dist), no env needed.
```

If `SITE_URL` is left at its default (`localhost:5173`), canonical/sitemap will
point to localhost. Check before deploying.

## Deployment

```bash
git pull
docker compose up -d --build      # nginx gets rebuilt and refreshes frontend_dist
```

After startup, check:
```bash
curl -s https://magiyaroslyn.com/robots.txt
curl -s https://magiyaroslyn.com/sitemap.xml | head
curl -s https://magiyaroslyn.com/product/<slug> | grep -o '<title>.*</title>'
curl -so /dev/null -w '%{http_code}\n' https://magiyaroslyn.com/product/nonexistent  # → 404
```

## Google Search Console (done manually by the owner — requires a Google login)

1. https://search.google.com/search-console → **Add property** → Domain → `magiyaroslyn.com`.
2. Verification — **DNS TXT** (recommended): add a TXT record to the domain's DNS (the value is
   provided by Search Console). Independent of code/deployment.
   - Fallback: set `GOOGLE_SITE_VERIFICATION=<code>` in `.env`, `docker compose up -d`
     — the shell will add `<meta name="google-site-verification">` to the home page (the "HTML tag" method).
3. After verification: **Sitemaps** → add `https://magiyaroslyn.com/sitemap.xml`.
4. After a few days check **Pages** (indexing coverage) and **Enhancements**
   (Product/Breadcrumb snippets).
5. Check individual pages with the **Rich Results Test**
   (https://search.google.com/test/rich-results) — Product/Article/Breadcrumb/Course without errors.

## Contacts for Organization markup

Filled in in the admin: **Settings → SEO contacts** (phone, email, social networks).
They are pulled into the Organization JSON-LD on the home page.

## SEO fields on content

The product/bundle/category/brand/course/article forms have **SEO Title** and **SEO Description**.
When empty, the name/description is substituted automatically. Fill them in for key pages.
