import { useEffect } from 'react';
import { jsonLdText, pageMetadata } from './metadata';
/** Keep the single static head in sync after ordinary client-side navigation. */
export function applyPageMetadata(path: string, document: Document) {
  const page = pageMetadata(path);
  document.title = page.title;
  const meta = (attribute: 'name' | 'property', key: string, value: string | null) => {
    const matches = [...document.head.querySelectorAll<HTMLMetaElement>(`meta[${attribute}="${key}"]`)];
    const element = matches.shift() ?? document.createElement('meta');
    for (const duplicate of matches) duplicate.remove();
    if (value === null) { element.remove(); return; }
    element.setAttribute(attribute, key); element.content = value;
    if (!element.isConnected) document.head.append(element);
  };
  meta('name', 'description', page.description); meta('name', 'robots', page.robots);
  meta('property', 'og:title', page.title); meta('property', 'og:description', page.description);
  meta('property', 'og:url', page.canonical); meta('property', 'og:image', page.image);
  meta('name', 'twitter:title', page.title); meta('name', 'twitter:description', page.description);
  meta('name', 'twitter:image', page.image);
  const canonicals = [...document.head.querySelectorAll<HTMLLinkElement>('link[rel="canonical"]')];
  const canonical = canonicals.shift() ?? document.createElement('link');
  for (const duplicate of canonicals) duplicate.remove();
  if (page.canonical) {
    canonical.rel = 'canonical'; canonical.href = page.canonical;
    if (!canonical.isConnected) document.head.append(canonical);
  } else canonical.remove();
  const previous = document.getElementById('zavliq-structured-data');
  const json = jsonLdText(path);
  if (json) {
    const script = previous ?? document.createElement('script');
    script.id = 'zavliq-structured-data'; script.setAttribute('type', 'application/ld+json');
    script.textContent = json;
    if (!script.isConnected) document.head.append(script);
  } else previous?.remove();
}
export function usePageMetadata(path: string) {
  useEffect(() => { applyPageMetadata(path, document); }, [path]);
}
