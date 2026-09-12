# Search and agent discovery

`pnpm --dir apps/web build` generates the client bundle, then renders the actual React content for `/`, `/docs`, `/privacy`, `/status`, and `/stats`. `/app` and the missing-page shell receive `noindex`. The renderer reads no credentials or live network data. Stats remain unavailable placeholders until the browser loads a validated snapshot; service health remains unchecked.

The route metadata lives in `src/seo/metadata.ts`. It generates one canonical, descriptive title, social preview, and JSON-LD block per public page. The browser updates that same head after client navigation. The schema describes the published free beta and optional encryption, without reviews, user counts, or adoption claims. The social image is the checked-in 1200×630 PNG rendered from `public/social-card.svg`; no image service is required at build time.

Deployment must serve `/docs`, `/privacy`, `/status`, `/stats`, and `/app` from their matching flat `.html` files. Serve unknown paths with the generated `404.html` **and HTTP 404**, rather than the home page with HTTP 200. Redirect explicit `.html` and trailing-slash variants of known pages to their canonical routes. Assets, Markdown guides, discovery, and API routes keep their existing handlers. Website readiness must compare `/stats` with `stats.html`, not `index.html`. No new browser CSP exception is needed for the non-executable JSON-LD block.

The generated sitemap lists only the five canonical public HTML pages. It omits timestamps because build time is not a truthful content modification date. Robots rules allow public documents and aggregate stats while avoiding API crawl traps. The console remains crawlable so its `noindex` instruction can be read; robots rules are not an access-control boundary.

Agent entry points are the existing `/llms.txt`, `/install.md`, `/skill.md`, `/protocol.md`, and `/.well-known/zavliq`. Keep their links absolute and their claims aligned with the release and visible pages. These support agent consumption; an AI-specific index file is not a ranking guarantee or a replacement for useful HTML and links.

Validation: `pnpm --dir apps/web exec vitest run src/seo/metadata.test.tsx scripts/prerender-seo.test.mjs`, then the full build. Browser QA should check direct route loads, normal/back/modified-click navigation, a single updated canonical/title, no console errors, and the unchanged device console. No search-engine submission or tracking is performed by this code.

Primary guidance consulted on 2026-09-12:

- [Google: JavaScript SEO](https://developers.google.com/search/docs/crawling-indexing/javascript/javascript-seo-basics): useful initial HTML, crawlable anchors, unique metadata, and correct error handling.
- [Google: AI features and websites](https://developers.google.com/search/docs/appearance/ai-features): standard SEO foundations still apply; no special AI markup is required.
- [Google: canonical URLs](https://developers.google.com/search/docs/crawling-indexing/consolidate-duplicate-urls): consistent canonical links and sitemap URLs.
- [Google: structured data](https://developers.google.com/search/docs/appearance/structured-data/intro-structured-data): describe the actual visible content.
- [Bing webmaster guidelines](https://www.bing.com/webmasters/help/webmaster-guidelines-30fba23a): clear content, internal links, and accurate sitemaps also support AI grounding eligibility.
- [Schema.org SoftwareApplication](https://schema.org/SoftwareApplication): application description vocabulary. This implementation does not claim rich-result eligibility.
