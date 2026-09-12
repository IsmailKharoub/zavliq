"""Opt-in real Caddy checks, with synthetic files and container-local HTTP only.

ZAVLIQ_CADDY_TEST_IMAGE=caddy@sha256:... python -I -B -m unittest discover \
  -s infra/tests -p test_caddy_routing.py
The image must already be cached. No ports, network, application volumes, or pulls.
"""
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[2]
IMAGE = os.environ.get('ZAVLIQ_CADDY_TEST_IMAGE', '')
PAGES = ['docs', 'privacy', 'status', 'stats', 'app']


def command(args, **kwargs):
    return subprocess.run(args, capture_output=True, timeout=20, check=True, **kwargs)


class Socket:
    def __init__(self, value):
        self.value = value

    def makefile(self, *_):
        return io.BytesIO(self.value)


@unittest.skipUnless(IMAGE, 'requires an explicit cached pinned Caddy image')
class CaddyRouting(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not re.fullmatch(r'caddy@sha256:[0-9a-f]{64}', IMAGE):
            raise ValueError('PINNED_CADDY_IMAGE_REQUIRED')
        cls.temporary = tempfile.TemporaryDirectory(prefix='zavliq-caddy-routing-')
        cls.addClassCleanup(cls.temporary.cleanup)
        folder = Path(cls.temporary.name)
        folder.chmod(0o755)
        public, status = folder / 'public', folder / 'status'
        public.mkdir(); status.mkdir()
        cls.files = {'index.html': b'<h1>Home fixture</h1>', '404.html': b'<h1>Missing fixture</h1>'}
        for page in PAGES:
            meta = '<meta name="robots" content="noindex, follow">' if page == 'app' else ''
            cls.files[page + '.html'] = (meta + '<h1>' + page + ' fixture</h1>').encode()
        cls.files.update({name: ('fixture ' + name).encode() for name in [
            'install.md', 'skill.md', 'protocol.md', 'llms.txt', 'sitemap.xml', 'robots.txt',
            'favicon.svg', 'social-card.svg', 'social-card.png', 'assets/old-client-fixed.js',
            'assets/new-client-fixed.js', 'assets/crypto-fixed.wasm']})
        for name, body in cls.files.items():
            target = public / name
            target.parent.mkdir(exist_ok=True)
            target.write_bytes(body)
        (status / 'stats.json').write_text('{"fixture":"stats"}')
        (status / 'health.json').write_text('{"fixture":"health"}')
        cls.container = 'zavliq-caddy-routing-' + uuid.uuid4().hex[:12]
        cls.addClassCleanup(lambda: command(['docker', 'rm', '-f', cls.container]))
        command(['docker', 'run', '-d', '--name', cls.container, '--pull', 'never',
                 '--platform', 'linux/amd64', '--network', 'none', '--read-only',
                 # The official Caddy executable carries this file capability; dropping
                 # it from the bounding set makes exec fail before port 8080 is opened.
                 '--user', '65534:65534', '--cap-drop', 'ALL', '--cap-add', 'NET_BIND_SERVICE',
                 '--security-opt', 'no-new-privileges',
                 '--cpus', '0.5', '--memory', '128m', '--pids-limit', '64',
                 '--tmpfs', '/config:mode=1777', '--tmpfs', '/data:mode=1777',
                 '--env', 'ZAVLIQ_SITE_ADDRESS=:8080', '--env', 'ZAVLIQ_PUBLIC_URL=https://zavliq.com',
                 '--mount', 'type=bind,src=' + str(ROOT / 'infra/Caddyfile') + ',dst=/etc/caddy/Caddyfile,readonly',
                 '--mount', 'type=bind,src=' + str(public) + ',dst=/srv,readonly',
                 '--mount', 'type=bind,src=' + str(status) + ',dst=/status,readonly',
                 IMAGE, 'caddy', 'run', '--config', '/etc/caddy/Caddyfile', '--adapter', 'caddyfile'])
        for attempt in range(10):
            try:
                if cls.request('/')[0] == 200:
                    break
            except (subprocess.CalledProcessError, http.client.HTTPException):
                pass
            time.sleep(0.2)
        else:
            logs = command(['docker', 'logs', cls.container])
            raise RuntimeError('ISOLATED_CADDY_DID_NOT_START: ' + logs.stderr.decode()[-4000:])
        output = command(['docker', 'exec', cls.container, 'caddy', 'adapt', '--config', '/etc/caddy/Caddyfile', '--adapter', 'caddyfile'])
        cls.adapted = json.loads(output.stdout)
        command(['docker', 'exec', cls.container, 'caddy', 'validate', '--config', '/etc/caddy/Caddyfile', '--adapter', 'caddyfile'])

    @classmethod
    def request(cls, path, method='GET'):
        data = f'{method} {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n'.encode()
        result = command(['docker', 'exec', '-i', cls.container, '/bin/busybox', 'nc', '-w', '3', '127.0.0.1', '8080'], input=data)
        response = http.client.HTTPResponse(Socket(result.stdout), method=method)
        response.begin()
        return response.status, dict((k.lower(), v) for k, v in response.getheaders()), response.read()

    def test_distinct_public_pages_and_console_noindex(self):
        for path in ['/', *['/' + page for page in PAGES]]:
            with self.subTest(path=path):
                status, headers, body = self.request(path)
                self.assertEqual(status, 200)
                expected = 'index.html' if path == '/' else path[1:] + '.html'
                self.assertEqual(body, self.files[expected])
                self.assertEqual(headers['cache-control'], 'no-cache')
                self.assertEqual(headers['x-content-type-options'], 'nosniff')
                self.assertIn("script-src 'self' 'wasm-unsafe-eval'", headers['content-security-policy'])
                if path == '/app':
                    self.assertEqual(headers['x-robots-tag'], 'noindex, follow')

    def test_aliases_redirect_once_and_keep_queries(self):
        for page in PAGES:
            for suffix in ['/', '.html', '.html/']:
                for query in ['', '?from=guide&next=%2Fdocs']:
                    with self.subTest(page=page, suffix=suffix, query=query):
                        status, headers, _ = self.request('/' + page + suffix + query)
                        self.assertEqual(status, 308)
                        self.assertEqual(headers['location'], '/' + page + query)
                        self.assertEqual(self.request(headers['location'])[0], 200)
        for alias in ['/index', '/index/', '/index.html', '/index.html/']:
            self.assertEqual(self.request(alias)[1]['location'], '/')

    def test_unknown_and_missing_assets_have_real_uncached_404(self):
        for path in ['/unknown', '/unknown.html', '/docs/unknown', '/404', '/404.html', '/assets/missing.js', '/assets/']:
            with self.subTest(path=path):
                status, headers, body = self.request(path)
                self.assertEqual(status, 404)
                self.assertEqual(body, self.files['404.html'])
                self.assertEqual(headers['cache-control'], 'no-store')
                self.assertEqual(headers['x-robots-tag'], 'noindex, follow')

    def test_public_files_and_old_assets_keep_exact_bytes(self):
        for name, expected in self.files.items():
            if name.endswith('.html'):
                continue
            with self.subTest(name=name):
                status, headers, body = self.request('/' + name)
                self.assertEqual(status, 200)
                self.assertEqual(hashlib.sha256(body).digest(), hashlib.sha256(expected).digest())
                if name.startswith('assets/'):
                    self.assertEqual(headers['cache-control'], 'public, max-age=31536000, immutable')

    def test_stats_health_discovery_and_original_private_rejections(self):
        for name in ['stats', 'health']:
            status, headers, body = self.request('/_zavliq/' + name)
            self.assertEqual(status, 200)
            self.assertEqual(headers['cache-control'], 'no-store')
            self.assertEqual(json.loads(body), {'fixture': name})
        status, headers, body = self.request('/.well-known/matrix/client')
        self.assertEqual(status, 200)
        self.assertEqual(headers['access-control-allow-origin'], '*')
        self.assertEqual(json.loads(body)['m.homeserver']['base_url'], 'https://zavliq.com')
        for path in ['/_synapse/admin/v1/server_version', '/_matrix/federation/v1/version', '/_matrix/key/v2/server', '/metrics']:
            with self.subTest(path=path):
                status, _, body = self.request(path)
                self.assertEqual(status, 404)
                self.assertNotIn(b'Missing fixture', body)

    def test_adapted_proxy_targets_and_request_bounds_remain_exact(self):
        def walk(value):
            if isinstance(value, dict):
                yield value
                for child in value.values():
                    yield from walk(child)
            elif isinstance(value, list):
                for child in value:
                    yield from walk(child)
        nodes = list(walk(self.adapted))
        proxies = [n for n in nodes if n.get('handler') == 'reverse_proxy']
        self.assertEqual(sorted(p['upstreams'][0]['dial'] for p in proxies), ['control:3000', 'control:3000', 'synapse:8008'])
        self.assertEqual(sorted(n['max_size'] for n in nodes if n.get('handler') == 'request_body'), [64000, 11000000])
        paths = [path for node in nodes for path in node.get('path', []) if isinstance(node.get('path'), list)]
        for path in ['/v1/*', '/health', '/ready', '/.well-known/zavliq', '/_matrix/*', '/_synapse/client/zavliq/*']:
            self.assertIn(path, paths)


if __name__ == '__main__':
    unittest.main()
