import { describe, expect, it } from 'vitest';
import { renderDocument } from './prerender-seo.mjs';

describe('static HTML build boundary', () => {
  const template = '<!doctype html><html><head><!-- seo:start --><title>Old</title><!-- seo:end --><script type="module" src="/assets/entry-hash.js"></script><link rel="stylesheet" href="/assets/site-hash.css"></head><body><div id="root"></div></body></html>';
  it('keeps the built asset references and replaces only the expected metadata and root', () => {
    const output = renderDocument(template, '<title>Docs</title>', '<main><h1>Install $& safely</h1></main>');
    expect(output).toContain('<title>Docs</title>');
    expect(output).not.toContain('<title>Old</title>');
    expect(output).toContain('<div id="root"><main><h1>Install $& safely</h1></main></div>');
    expect(output).toContain('/assets/entry-hash.js');
    expect(output).toContain('/assets/site-hash.css');
  });
  it('fails instead of silently publishing an empty shell after a template drift or second render', () => {
    expect(() => renderDocument(template.replace('<!-- seo:start -->', ''), '', '')).toThrow('PRERENDER_TEMPLATE_MARKERS_REQUIRED');
    expect(() => renderDocument(template.replace('<div id="root"></div>', '<div id="root"><h1>Rendered</h1></div>'), '', '')).toThrow('PRERENDER_TEMPLATE_MARKERS_REQUIRED');
    expect(() => renderDocument(template + '<div id="root"></div>', '', '')).toThrow('PRERENDER_TEMPLATE_MARKERS_REQUIRED');
  });
});
