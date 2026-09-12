import { afterEach, describe, expect, it, vi } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import App from '../App';
import { headMarkup, jsonLdText, pageMetadata, PUBLIC_ROUTES, robotsTxt, sitemapXml, structuredData } from './metadata';

afterEach(() => vi.unstubAllGlobals());

describe('public search and agent discovery', () => {
  it('renders actual public content and crawlable routes without browser globals, credentials, or fetches', () => {
    const fetch = vi.fn(() => { throw Error('NETWORK_FORBIDDEN'); });
    vi.stubGlobal('fetch', fetch);
    vi.stubGlobal('location', undefined);
    vi.stubGlobal('window', undefined);
    vi.stubGlobal('document', undefined);
    vi.stubGlobal('indexedDB', undefined);
    for (const route of PUBLIC_ROUTES) {
      const html = renderToStaticMarkup(<App initialPath={route} />);
      expect(html).toContain('<h1');
      expect(html).toContain('href="/docs"');
      expect(html).toContain('href="/stats"');
      expect(html).toContain('href="https://github.com/IsmailKharoub/zavliq"');
    }
    expect(fetch).not.toHaveBeenCalled();
  });

  it('never prerenders a fabricated live count, freshness timestamp, or online service result', () => {
    const stats = renderToStaticMarkup(<App initialPath="/stats" />);
    expect(stats).toContain('Numbers appear after a verified snapshot loads.');
    expect(stats.match(/aria-label="Not available"/g)).toHaveLength(4);
    expect(stats).not.toContain('Snapshot current');
    expect(stats).not.toContain('<time');
    expect(stats).not.toContain('stats-day-value');
    const status = renderToStaticMarkup(<App initialPath="/status" />);
    expect(status).toContain('Checking network');
    expect(status).not.toContain('Service reachable');
  });

  it('provides distinct canonical titles and social previews for all public routes', () => {
    const titles = new Set();
    for (const route of PUBLIC_ROUTES) {
      const metadata = pageMetadata(route);
      const html = headMarkup(route);
      titles.add(metadata.title);
      expect(metadata.canonical).toBe('https://zavliq.com' + route);
      expect(metadata.robots).toContain('index, follow');
      expect(html.match(/rel="canonical"/g)).toHaveLength(1);
      expect(html.match(/<title>/g)).toHaveLength(1);
      expect(html).toContain('https://zavliq.com/social-card.png');
      expect(html).toContain('summary_large_image');
    }
    expect(titles.size).toBe(PUBLIC_ROUTES.length);
  });

  it('keeps private console and arbitrary missing pages out of indexing and the sitemap', () => {
    for (const path of ['/app', '/404', '/unknown', '/docs?device=private', '/__proto__']) {
      expect(pageMetadata(path).robots).toBe('noindex, follow');
      expect(structuredData(path)).toBeNull();
    }
    expect(pageMetadata('/unknown').canonical).toBeNull();
    const sitemap = sitemapXml();
    expect(sitemap.match(/<loc>/g)).toHaveLength(5);
    expect(sitemap).not.toMatch(/\/app|\/404|lastmod/);
    for (const route of PUBLIC_ROUTES) expect(sitemap).toContain(`<loc>https://zavliq.com${route}</loc>`);
    expect(robotsTxt()).toContain('Sitemap: https://zavliq.com/sitemap.xml');
    expect(robotsTxt()).not.toContain('Disallow: /app'); // Crawlers must be able to see noindex.
    expect(robotsTxt()).not.toContain('Disallow: /_zavliq/stats');
  });

  it('describes the actual released beta without ratings, adoption claims, or E2EE-by-default', () => {
    const source = jsonLdText('/')!;
    const schema = JSON.parse(source);
    const software = schema['@graph'].find((item: Record<string, unknown>) => item['@type'] === 'SoftwareApplication');
    expect(software.softwareVersion).toBe('0.1.0');
    expect(software.downloadUrl).toBe('https://github.com/IsmailKharoub/zavliq/releases/tag/v0.1.0');
    expect(software.featureList).toContain('Optional end-to-end encryption for private conversations');
    expect(source).not.toMatch(/aggregateRating|ratingValue|interactionStatistic|activeUsers|reviewCount/);
    expect(pageMetadata('/privacy').description).toContain('standard messaging is the default');
  });
});
