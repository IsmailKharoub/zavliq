import { readFile, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { createElement } from 'react';
import { renderToString } from 'react-dom/server';
import { createServer } from 'vite';

/** The same public React content is served to humans and crawlers; no user-agent variants. */
export function renderDocument(template, head, body) {
  if ((template.match(/<!-- seo:start -->/g) ?? []).length !== 1 ||
      (template.match(/<!-- seo:end -->/g) ?? []).length !== 1 ||
      (template.match(/<div id="root"><\/div>/g) ?? []).length !== 1) {
    throw Error('PRERENDER_TEMPLATE_MARKERS_REQUIRED');
  }
  return template.replace(/<!-- seo:start -->[\s\S]*?<!-- seo:end -->/, () => `<!-- seo:start -->\n${head}\n<!-- seo:end -->`)
    .replace('<div id="root"></div>', () => `<div id="root">${body}</div>`);
}

export async function prerender() {
  const root = fileURLToPath(new URL('../', import.meta.url));
  const dist = new URL('../dist/', import.meta.url);
  const template = await readFile(new URL('index.html', dist), 'utf8');
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => { throw Error('PRERENDER_NETWORK_FORBIDDEN'); };
  const server = await createServer({ root, server: { middlewareMode: true }, appType: 'custom' });
  try {
    const [{ default: App }, seo] = await Promise.all([
      server.ssrLoadModule('/src/App.tsx'), server.ssrLoadModule('/src/seo/metadata.ts'),
    ]);
    for (const path of seo.PRERENDER_ROUTES) {
      const body = renderToString(createElement(App, { initialPath: path }));
      if (!body.includes('<h1')) throw Error('PRERENDER_PAGE_HEADING_REQUIRED');
      const filename = path === '/' ? 'index.html' : path.slice(1) + '.html';
      await writeFile(new URL(filename, dist), renderDocument(template, seo.headMarkup(path), body));
    }
    await writeFile(new URL('sitemap.xml', dist), seo.sitemapXml());
    await writeFile(new URL('robots.txt', dist), seo.robotsTxt());
    console.log(`Prerendered ${seo.PRERENDER_ROUTES.length} public/utility pages; generated sitemap and robots.txt.`);
  } finally {
    await server.close();
    globalThis.fetch = originalFetch;
  }
}
if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) await prerender();
