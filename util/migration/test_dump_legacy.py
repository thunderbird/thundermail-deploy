import base64
import http.server
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse

HERE = pathlib.Path(__file__).resolve().parent
PASSWORD = 'legacy-admin-pw-77'
REQUESTS_STUB = """
import json as _json, urllib.error, urllib.parse, urllib.request


class _Resp:
    def __init__(self, code, reason, raw):
        self.status_code, self.reason, self.text = code, reason, raw.decode()
        self.ok = 200 <= code < 400

    def json(self):
        return _json.loads(self.text)


class Session:
    def __init__(self):
        self.verify, self.headers, self.auth = True, {}, None

    def request(self, method, url, params=None, json=None, timeout=None):
        if params:
            url += "?" + urllib.parse.urlencode(params)
        h = dict(self.headers)
        if self.auth:
            import base64
            h["Authorization"] = "Basic " + base64.b64encode(":".join(self.auth).encode()).decode()
        req = urllib.request.Request(url, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return _Resp(r.status, r.reason, r.read())
        except urllib.error.HTTPError as e:
            return _Resp(e.code, e.reason, e.read())
"""
URLLIB3_STUB = (
    'class exceptions:\n    InsecureRequestWarning = Warning\n\n\n'
    'def disable_warnings(*a):\n    pass\n'
)


class FakeAdmin:
    def __init__(self, n=40, delay=0.03, missing=('user7@example.test',)):
        self.names = ['user%d@example.test' % i for i in range(n)]
        self.missing, self.delay = set(missing), delay
        self.paths, self.active, self.peak, self.cmdlines = [], 0, 0, []
        self.watch_pid, self.gate = None, None
        self.lock = threading.Lock()
        fake = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                with fake.lock:
                    fake.active += 1
                    fake.peak = max(fake.peak, fake.active)
                    fake.paths.append(self.path)
                    if fake.watch_pid:
                        try:
                            fake.cmdlines.append(
                                pathlib.Path('/proc/%d/cmdline' % fake.watch_pid).read_bytes()
                            )
                        except OSError:
                            pass
                try:
                    want = 'Basic ' + base64.b64encode(('admin:' + PASSWORD).encode()).decode()
                    if self.headers.get('Authorization') != want:
                        return self.reply(401, {'error': 'unauthorized'})
                    u = urllib.parse.urlsplit(self.path)
                    q = dict(urllib.parse.parse_qsl(u.query))
                    if u.path == '/api/settings/list':
                        return self.reply(
                            200, {'data': {'items': {'a.b': '1', 'c.d': '2'}, 'total': 2}}
                        )
                    if u.path == '/api/principal':
                        items = []
                        if q.get('types') == 'individual':
                            page, limit = int(q['page']), int(q['limit'])
                            items = [
                                {'type': 'individual', 'name': x}
                                for x in fake.names[(page - 1) * limit : page * limit]
                            ]
                            return self.reply(
                                200, {'data': {'items': items, 'total': len(fake.names)}}
                            )
                        return self.reply(200, {'data': {'items': [], 'total': 0}})
                    if u.path.startswith('/api/principal/'):
                        if fake.gate is not None:
                            fake.gate.wait(20)
                        time.sleep(fake.delay)
                        name = urllib.parse.unquote(u.path.split('/', 3)[3])
                        if name in fake.missing:
                            return self.reply(404, {'error': 'notFound'})
                        i = fake.names.index(name)
                        return self.reply(
                            200,
                            {
                                'data': {
                                    'id': i,
                                    'type': 'individual',
                                    'name': name,
                                    'emails': [name],
                                }
                            },
                        )
                    self.reply(404, {'error': 'no'})
                finally:
                    with fake.lock:
                        fake.active -= 1

            def reply(self, code, body):
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = 'http://127.0.0.1:%d' % self.srv.server_address[1]


class DumpLegacy(unittest.TestCase):
    def setUp(self):
        self.t = pathlib.Path(tempfile.mkdtemp())
        stubs = self.t / 'stubs'
        for name, src in (('requests', REQUESTS_STUB), ('urllib3', URLLIB3_STUB)):
            if importlib.util.find_spec(name) is None:
                (stubs / name).mkdir(parents=True)
                (stubs / name / '__init__.py').write_text(src)
        self.env = {
            'PATH': os.environ['PATH'],
            'HOME': str(self.t),
            'OLD_STALWART_USER': 'admin',
            'OLD_STALWART_PASSWORD': PASSWORD,
        }
        if stubs.exists():
            self.env['PYTHONPATH'] = str(stubs)

    def serve(self, **kw):
        f = FakeAdmin(**kw)
        self.addCleanup(f.srv.server_close)
        self.addCleanup(f.srv.shutdown)
        self.env['OLD_STALWART_URL'] = f.url
        return f

    def files(self, tag):
        return str(self.t / ('settings-%s.json' % tag)), str(self.t / ('principals-%s.json' % tag))

    def plain(self, fake):
        s, p = self.files('plain')
        r = subprocess.run(
            [
                sys.executable,
                str(HERE / 'migrate_v016.py'),
                'dump',
                '--url',
                fake.url,
                '--username',
                'admin',
                '--password',
                PASSWORD,
                '--settings',
                s,
                '--principals',
                p,
            ],
            capture_output=True,
            text=True,
            env=self.env,
            timeout=120,
        )
        return r, s, p

    def wrapped(self, fake, workers=8, log=None):
        s, p = self.files('wrapped')
        args = [
            sys.executable,
            str(HERE / 'dump_legacy.py'),
            '--settings',
            s,
            '--principals',
            p,
            '--workers',
            str(workers),
        ]
        if log:
            with open(log, 'w') as f:
                proc = subprocess.Popen(args, stdout=f, stderr=subprocess.STDOUT, env=self.env)
                return proc, s, p
        r = subprocess.run(args, capture_output=True, text=True, env=self.env, timeout=120)
        return r, s, p

    def test_same_files_and_warnings_as_the_tool_with_parallel_reads(self):
        fake = self.serve()
        r1, s1, p1 = self.plain(fake)
        self.assertEqual(r1.returncode, 0, r1.stderr)
        plain_paths, fake.paths, fake.peak = list(fake.paths), [], 0
        r2, s2, p2 = self.wrapped(fake)
        self.assertEqual(r2.returncode, 0, r2.stderr)
        self.assertEqual(pathlib.Path(s1).read_text(), pathlib.Path(s2).read_text())
        self.assertEqual(pathlib.Path(p1).read_text(), pathlib.Path(p2).read_text())
        self.assertEqual(sorted(plain_paths), sorted(fake.paths))
        self.assertGreater(fake.peak, 1)
        warn = [x for x in r1.stderr.splitlines() if 'WARN failed to fetch' in x]
        self.assertEqual(len(warn), 1)
        self.assertIn(warn[0], r2.stderr.splitlines())
        self.assertIn('prefetching 40 principals with 8 workers', r2.stderr)

    def test_the_password_is_never_on_the_command_line(self):
        fake = self.serve(n=5)
        log = self.t / 'dump.log'
        proc, _, _ = self.wrapped(fake, log=log)
        fake.watch_pid = proc.pid
        self.assertEqual(proc.wait(60), 0, log.read_text())
        self.assertTrue(fake.cmdlines)
        for c in fake.cmdlines:
            self.assertNotIn(PASSWORD.encode(), c)
        self.assertNotIn(PASSWORD, log.read_text())

    def test_the_log_is_flushed_per_line_while_the_reads_wait(self):
        fake = self.serve(n=5)
        fake.gate = threading.Event()
        log = self.t / 'dump.log'
        proc, _, _ = self.wrapped(fake, log=log)
        deadline = time.time() + 20
        while 'prefetching 5 principals' not in log.read_text() and time.time() < deadline:
            time.sleep(0.05)
        seen = log.read_text()
        fake.gate.set()
        self.assertEqual(proc.wait(60), 0)
        self.assertIn('Listing principals...\n  prefetching 5 principals with 8 workers\n', seen)
        self.assertNotIn('wrote 4 principals', seen)

    def test_refusals(self):
        self.serve(n=2)
        sys.path.insert(0, str(HERE))
        env = dict(self.env, OLD_STALWART_URL='http://192.0.2.1:8080')
        r = subprocess.run(
            [sys.executable, str(HERE / 'dump_legacy.py'), '--settings', 'x', '--principals', 'y'],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        self.assertEqual(r.returncode, 2)
        self.assertIn('not loopback', r.stderr)
        env = {k: v for k, v in self.env.items() if k != 'OLD_STALWART_PASSWORD'}
        r = subprocess.run(
            [sys.executable, str(HERE / 'dump_legacy.py'), '--settings', 'x', '--principals', 'y'],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        self.assertIn('missing in the environment: OLD_STALWART_PASSWORD', r.stderr)
        probe = (
            'import sys\n'
            'sys.argv = ["dump_legacy.py", "--settings", "s", "--principals", "p"]\n'
            'import dump_legacy\n'
            'm = dump_legacy.load_tool()\n'
            'dump_legacy.load_tool = lambda: m\n'
            'c = m.StalwartClient(%r, username="a", password="b")\n'
            'm.main = lambda argv: c._request("POST", "/api/principal")\n'
            'print(dump_legacy.main())\n'
        ) % self.env['OLD_STALWART_URL']
        r = subprocess.run(
            [sys.executable, '-c', probe],
            capture_output=True,
            text=True,
            env=dict(
                self.env,
                PYTHONPATH=os.pathsep.join(x for x in (str(HERE), self.env.get('PYTHONPATH')) if x),
            ),
            timeout=60,
        )
        self.assertIn('REFUSED: non-GET request to legacy', r.stdout + r.stderr)


if __name__ == '__main__':
    unittest.main()
