import http.server
import json
import pathlib
import subprocess
import sys
import tempfile
import threading
import unittest

HERE = pathlib.Path(__file__).resolve().parent
UNI, PUNY, BAD = 'bücher.example.com', 'xn--bcher-kva.example.com', 'mail.example.notatld'


class Fake(http.server.BaseHTTPRequestHandler):
    objects = {}

    def log_message(self, *a):
        pass

    def send(self, body):
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self.send({'primaryAccounts': {'urn:stalwart:jmap': 'admin'}, 'accounts': {'admin': {}}})

    def do_POST(self):
        calls = json.loads(self.rfile.read(int(self.headers['Content-Length'])))['methodCalls']
        out = []
        for name, args, tag in calls:
            obj, method = name[2:].split('/')
            items = self.objects.get(obj, [])
            if method == 'query':
                ids = [x['id'] for x in items][args.get('position', 0) :][: args.get('limit', 1000)]
                out.append([name, {'ids': ids, 'total': len(items)}, tag])
            else:
                out.append([name, {'list': [x for x in items if x['id'] in args['ids']]}, tag])
        self.send({'methodResponses': out})


class Directory(unittest.TestCase):
    def setUp(self):
        self.t = pathlib.Path(tempfile.mkdtemp())
        (self.t / 'principals.json').write_text(
            json.dumps(
                [
                    {
                        'id': 1,
                        'type': 'individual',
                        'name': 'u@a.example.com',
                        'emails': ['u@a.example.com', '@' + BAD],
                    },
                    {'id': 2, 'type': 'domain', 'name': 'a.example.com'},
                    {'id': 3, 'type': 'domain', 'name': UNI},
                    {'id': 4, 'type': 'domain', 'name': PUNY},
                    {'id': 5, 'type': 'domain', 'name': BAD},
                ],
                ensure_ascii=False,
            )
        )
        Fake.objects = {
            'Domain': [{'id': 'd1', 'name': 'a.example.com'}],
            'Account': [{'id': 'b', '@type': 'User', 'name': 'u', 'domainId': 'd1', 'emailAddress': 'u@a.example.com'}],
            'DkimSignature': [{'id': 'k1', 'domainId': 'd1'}],
        }
        self.srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Fake)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)

    def run02(self, dropped=None, stubs=None):
        env = {
            'PATH': '/usr/bin:/bin',
            'VERIFY_OUT': str(self.t / 'out'),
            'PRINCIPALS': str(self.t / 'principals.json'),
            'TARGET_URL': 'http://127.0.0.1:%d' % self.srv.server_port,
            'TARGET_USER': 'admin',
            'TARGET_PASSWORD': 'pw',
        }
        if dropped is not None:
            (self.t / 'dropped.json').write_text(json.dumps(dropped, ensure_ascii=False))
            env['DROPPED_DOMAINS'] = str(self.t / 'dropped.json')
        if stubs is not None:
            (self.t / 'stubs.json').write_text(json.dumps(stubs))
            env['DROPPED_STUBS'] = str(self.t / 'stubs.json')
        p = subprocess.run(
            [sys.executable, str(HERE / '02_directory.py')],
            env=env,
            cwd=HERE,
            capture_output=True,
            text=True,
            timeout=60,
        )
        return p.returncode, p.stdout + p.stderr

    def test_the_dropped_domains_are_left_out_and_counted(self):
        listed = {'domains': [BAD, UNI, PUNY], 'dkim_dropped': 6, 'dkim_kept': 1}
        rc, out = self.run02(listed)
        self.assertEqual(rc, 0, out)
        self.assertIn('PASS  domains dropped before the apply (domain_fix.py), left out of the source set: 3', out)
        self.assertIn('PASS  domains used on source present on target: missing []', out)
        self.assertIn('PASS  DkimSignature count: target 1, export after the drop 1', out)

    def test_without_the_list_the_dropped_domains_fail_the_compare(self):
        rc, out = self.run02()
        self.assertEqual(rc, 1, out)
        self.assertIn('FAIL  domains used on source present on target: missing', out)

    def test_a_missing_key_fails_and_an_extra_key_warns(self):
        rc, out = self.run02({'domains': [BAD, UNI, PUNY], 'dkim_dropped': 6, 'dkim_kept': 2})
        self.assertEqual(rc, 1, out)
        self.assertIn('FAIL  DkimSignature count: target 1, export after the drop 2', out)
        rc, out = self.run02({'domains': [BAD, UNI, PUNY], 'dkim_dropped': 6, 'dkim_kept': 0})
        self.assertEqual(rc, 0, out)
        self.assertIn('WARN  DkimSignature count: target 1 is above the export after the drop (0)', out)
        warn = [x for x in (HERE / 'expected-warn.txt').read_text().splitlines() if x.strip()]
        self.assertTrue(any(w in out.split('WARN  DkimSignature')[1] for w in warn))

    def test_dropped_stub_accounts_are_left_out_of_the_account_compare(self):
        items = json.loads((self.t / 'principals.json').read_text())
        items.append({'id': 6, 'type': 'individual', 'name': 'stub@a.example.com', 'roles': ['user']})
        (self.t / 'principals.json').write_text(json.dumps(items, ensure_ascii=False))
        listed = {'domains': [BAD, UNI, PUNY], 'dkim_dropped': 6, 'dkim_kept': 1}
        rc, out = self.run02(listed)
        self.assertEqual(rc, 1, out)
        self.assertIn('FAIL  account ids: source 2, target 1, missing [6]', out)
        rc, out = self.run02(listed, {'ids': [6], 'stubs': [{'id': 6, 'name': 'x', 'alias_holder': 1}]})
        self.assertEqual(rc, 0, out)
        self.assertIn('PASS  stub accounts dropped before the convert (stub_drop.py), left out of the source set: 1', out)
        self.assertIn('PASS  account ids: source 1, target 1', out)
        rc, out = self.run02(listed, {'nope': 1})
        self.assertEqual(rc, 1, out)
        self.assertIn('dropped-stub list', out)

    def test_an_unreadable_list_fails(self):
        (self.t / 'dropped.json').write_text('{')
        rc, out = self.run02({'nope': 1})
        self.assertEqual(rc, 1, out)
        self.assertIn('dropped-domain list', out)
        self.assertIn('unreadable (KeyError): run the domains step of w4-migrate.sh', out)


if __name__ == '__main__':
    unittest.main()
