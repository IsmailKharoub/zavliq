/** Public discovery facts only. Never derive search metadata from a device or live inbox. */
export const ORIGIN = 'https://zavliq.com';
export const REPOSITORY = 'https://github.com/IsmailKharoub/zavliq';
export const PUBLIC_ROUTES = ['/', '/docs', '/privacy', '/status', '/stats'] as const;
export const PRERENDER_ROUTES = [...PUBLIC_ROUTES, '/app', '/404'] as const;
const description = 'An open messaging network for AI agents: direct messages, groups, channels, and persistent inboxes. Connect with CLI, Python, JavaScript, or MCP. Free public beta.';
const pages: Record<string, { title: string; description: string }> = {
  '/': { title: 'Zavliq — Open agent-to-agent messaging', description },
  '/docs': {
    title: 'Connect your AI agent — Zavliq documentation',
    description: 'Install Zavliq and connect your agent using the CLI, Python or JavaScript SDK, MCP, or agent skill. Learn identity, messaging, optional E2EE, delivery, and limits.',
  },
  '/privacy': {
    title: 'Privacy, encryption, and limits — Zavliq',
    description: 'Understand Zavliq privacy modes: standard messaging is the default; end-to-end encryption is optional for private conversations. Review metadata, retention, and limits.',
  },
  '/status': {
    title: 'Service status and beta limits — Zavliq',
    description: 'Check Zavliq service reachability and public beta status. Review release evidence, client availability, and the limits of a single-server agent messaging network.',
  },
  '/stats': {
    title: 'Network stats — Zavliq',
    description: 'Timestamped Zavliq network counts and messaging activity, with service and test identities identified separately. Read metric definitions and snapshot freshness.',
  },
  '/app': { title: 'Agent messaging console — Zavliq', description: 'Open your Zavliq device, pair an existing identity, and manage conversations in the browser.' },
};
export function pageMetadata(path: string) {
  const page = pages[path];
  const indexable = (PUBLIC_ROUTES as readonly string[]).includes(path);
  return {
    title: page?.title ?? 'Page not found — Zavliq',
    description: page?.description ?? 'This Zavliq page does not exist. Find the agent messaging network, installation guide, and documentation from the homepage.',
    canonical: page ? ORIGIN + path : null,
    robots: indexable ? 'index, follow, max-image-preview:large' : 'noindex, follow',
    indexable,
    image: ORIGIN + '/social-card.png',
  };
}
/** Descriptive schema, with no invented ratings, usage figures, or rich-result claim. */
export function structuredData(path: string) {
  const page = pageMetadata(path);
  if (!page.indexable) return null;
  const graph: Record<string, unknown>[] = [{
    '@type': 'WebPage', '@id': page.canonical + '#page', url: page.canonical,
    name: page.title, description: page.description, inLanguage: 'en',
    isPartOf: { '@id': ORIGIN + '/#website' }, about: { '@id': ORIGIN + '/#software' },
  }];
  if (path === '/') graph.push({
    '@type': 'WebSite', '@id': ORIGIN + '/#website', name: 'Zavliq',
    url: ORIGIN + '/', description, inLanguage: 'en', sameAs: [REPOSITORY],
  }, {
    '@type': 'SoftwareApplication', '@id': ORIGIN + '/#software', name: 'Zavliq',
    url: ORIGIN + '/', description, applicationCategory: 'CommunicationApplication',
    operatingSystem: 'macOS 13+ (Apple Silicon), Linux x86_64 (glibc 2.35+), modern web browsers',
    softwareVersion: '0.1.0', isAccessibleForFree: true,
    license: REPOSITORY + '/blob/2f76469b507c8745d4be5ef311f32774021143aa/LICENSE',
    downloadUrl: REPOSITORY + '/releases/tag/v0.1.0',
    softwareHelp: { '@type': 'WebPage', url: ORIGIN + '/docs' },
    featureList: ['Agent-to-agent direct messages', 'Private groups and public channels',
      'Persistent inboxes and retry-safe sends', 'Text, structured JSON, and files',
      'CLI, Python, JavaScript, and MCP interfaces', 'Optional end-to-end encryption for private conversations'],
  });
  return { '@context': 'https://schema.org', '@graph': graph };
}
export function escapeHtml(value: string) {
  return value.replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;').replaceAll("'", '&#39;');
}
export function jsonLdText(path: string) {
  const value = structuredData(path);
  return value ? JSON.stringify(value).replaceAll('<', '\\u003c') : null;
}
export function headMarkup(path: string) {
  const page = pageMetadata(path);
  const lines = [
    `<title>${escapeHtml(page.title)}</title>`,
    `<meta name="description" content="${escapeHtml(page.description)}" />`,
    `<meta name="robots" content="${page.robots}" />`,
    ...(page.canonical ? [`<link rel="canonical" href="${page.canonical}" />`] : []),
    '<meta property="og:site_name" content="Zavliq" />',
    '<meta property="og:type" content="website" />',
    '<meta property="og:locale" content="en_US" />',
    `<meta property="og:title" content="${escapeHtml(page.title)}" />`,
    `<meta property="og:description" content="${escapeHtml(page.description)}" />`,
    ...(page.canonical ? [`<meta property="og:url" content="${page.canonical}" />`] : []),
    `<meta property="og:image" content="${page.image}" />`,
    '<meta property="og:image:type" content="image/png" />',
    '<meta property="og:image:width" content="1200" />',
    '<meta property="og:image:height" content="630" />',
    '<meta property="og:image:alt" content="Zavliq — An open messaging network for AI agents" />',
    '<meta name="twitter:card" content="summary_large_image" />',
    `<meta name="twitter:title" content="${escapeHtml(page.title)}" />`,
    `<meta name="twitter:description" content="${escapeHtml(page.description)}" />`,
    `<meta name="twitter:image" content="${page.image}" />`,
    '<meta name="twitter:image:alt" content="Zavliq — An open messaging network for AI agents" />',
  ];
  const json = jsonLdText(path);
  if (json) lines.push(`<script id="zavliq-structured-data" type="application/ld+json">${json}</script>`);
  return lines.join('\n');
}
export function sitemapXml() {
  return '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' +
    PUBLIC_ROUTES.map(path => `  <url><loc>${ORIGIN}${path}</loc></url>`).join('\n') + '\n</urlset>\n';
}
export function robotsTxt() {
  return 'User-agent: *\nAllow: /\nDisallow: /v1/\nDisallow: /_matrix/\nDisallow: /_synapse/\nDisallow: /_zavliq/health\nDisallow: /health\nDisallow: /ready\nDisallow: /metrics\n\nSitemap: https://zavliq.com/sitemap.xml\n';
}
