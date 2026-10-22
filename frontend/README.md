# Storefront (React SPA)

React 19 + TypeScript + Vite + Tailwind CSS 4. Part of the [Magiya Roslyn platform](../README.md).

```bash
npm ci
npm run dev        # http://localhost:5173 — proxies /api, /media and /ws to Django on :8000
npm run build      # tsc -b && vite build
npm run lint
```

```
src/
  api/          Axios client (JWT with single-flight silent refresh) and per-domain API modules
  pages/        route-level pages: Home, Catalog, Product, Bundle, Checkout, Profile, Courses, Blog, …
  components/   shared UI (cart/liked drawers, chat widget, stock-notify modal, SEO head updater, …)
  context/      Auth, Cart, Liked, SiteSettings, UI providers
  hooks/ lib/ utils/ types/
```

The production build is served by nginx; navigation requests are proxied to Django, which injects per-page
SEO tags into `index.html` (see [../docs/SEO.md](../docs/SEO.md)).
