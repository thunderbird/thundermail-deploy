import http.server
import json
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

HERE = pathlib.Path(__file__).resolve().parent
BODY = b'From: a@example.test\r\nSubject: x\r\n\r\nhello\r\n'
WHEN = '2026-11-01T00:00:00Z'
RCVD = 1793491200


class Jmap(http.server.BaseHTTPRequestHandler):
    emails = {}
    gone = set()

    def log_message(self, *a):
        pass

    def send(self, code, body, ctype='application/json'):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        blob = self.path.split('/')[-2]
        if blob in self.gone:
            return self.send(404, b'{}')
        self.send(200, BODY, 'message/rfc822')

    def do_POST(self):
        calls = json.loads(self.rfile.read(int(self.headers['Content-Length'])))['methodCalls']
        out = []
        for name, args, tag in calls:
            obj, method = name.split('/')
            if obj == 'Email' and method == 'query':
                want = (args.get('filter') or {}).get('subject')
                ids = [i for i, e in self.emails.items() if want is None or want in e['subject']]
                page = ids[args.get('position', 0) :][: args.get('limit', 1000)]
                out.append([name, {'ids': page, 'total': len(ids)}, tag])
            elif obj == 'Email':
                out.append(
                    [name, {'list': [{'id': i, **self.emails[i]} for i in args['ids']]}, tag]
                )
            elif method == 'query':
                out.append([name, {'ids': [], 'total': 0}, tag])
            else:
                n = {'Mailbox': 6, 'Calendar': 1, 'AddressBook': 1}.get(obj, 0)
                out.append([name, {'list': [{'id': str(i)} for i in range(n)]}, tag])
        self.send(200, json.dumps({'methodResponses': out}).encode())


def email(subject, blob):
    return {
        'subject': subject,
        'blobId': blob,
        'size': len(BODY),
        'receivedAt': WHEN,
        'mailboxIds': {'0': True},
        'threadId': 't',
    }


class Verify(unittest.TestCase):
    def setUp(self):
        self.t = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.t, ignore_errors=True)
        (self.t / 'principals.json').write_text(
            json.dumps([{'id': 1, 'type': 'individual', 'name': 'alice'}])
        )
        source = {
            'accounts': {'1': {'emails': 2, 'mailboxes': 6}},
            'messages': {'1:0': [len(BODY), RCVD], '1:1': [len(BODY), RCVD]},
            'cal_index': {},
            'card_index': {},
        }
        (self.t / 'source.json').write_text(json.dumps(source))
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Jmap)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)

    def run_check(self, script, emails, gone=()):
        Jmap.emails, Jmap.gone = emails, set(gone)
        env = {
            'PATH': '/usr/bin:/bin',
            'VERIFY_OUT': str(self.t),
            'PRINCIPALS': str(self.t / 'principals.json'),
            'TARGET_URL': 'http://127.0.0.1:%d' % self.server.server_port,
            'TARGET_USER': 'admin',
            'TARGET_PASSWORD': 'pw',
        }
        p = subprocess.run(
            [sys.executable, str(HERE / script)],
            env=env,
            cwd=HERE,
            capture_output=True,
            text=True,
            timeout=120,
        )
        return p.returncode, p.stdout + p.stderr

    def unexpected(self, out):
        expected = [x for x in (HERE / 'expected-warn.txt').read_text().splitlines() if x.strip()]
        return [
            x
            for x in out.splitlines()
            if x.startswith('  WARN  ') and not any(e in x for e in expected)
        ]

    LIVE = {
        'a': email('one', 'b0'),
        'b': email('two', 'b1'),
        'c': email('w7-verify-20261112T000000Z', 'b2'),
        'd': email('hello', 'b3'),
    }

    def test_03_leaves_out_gate_mail_and_warns_on_inbound_growth(self):
        rc, out = self.run_check('03_messages.py', self.LIVE, gone={'b2'})
        self.assertEqual(rc, 0, out)
        self.assertIn('PASS  gate check messages on target only, left out: 1', out)
        self.assertRegex(out, r'WARN  extra on target: 1 ')
        self.assertIn('PASS  bodies downloaded: 2/2 of 2 migrated', out)
        self.assertEqual(self.unexpected(out), [])

    def test_04_counts_inbound_growth_as_a_warning(self):
        rc, out = self.run_check('04_collections.py', self.LIVE)
        self.assertEqual(rc, 0, out)
        grew = 'accounts with mail received since the snapshot 1'
        self.assertIn("WARN  emails: source 2, target 3; %s [('alice', 2, 3)]" % grew, out)
        self.assertEqual(self.unexpected(out), [])

    def test_lost_mail_still_fails(self):
        rc, out = self.run_check(
            '03_messages.py', {'a': email('one', 'b0'), 'd': email('hello', 'b3')}
        )
        self.assertEqual(rc, 1, out)
        self.assertRegex(out, r'FAIL  messages: .*missing on target 1')
        rc, out = self.run_check('04_collections.py', {'a': email('one', 'b0')})
        self.assertEqual(rc, 1, out)
        self.assertIn('FAIL  emails: source 2, target 1; differing accounts 1', out)

    def test_a_migrated_message_with_a_gate_subject_still_counts(self):
        live = {'a': email('w7-verify-old', 'b0'), 'b': email('two', 'b1')}
        rc, out = self.run_check('04_collections.py', live)
        self.assertEqual(rc, 0, out)
        self.assertIn('PASS  emails: source 2, target 2', out)
        rc, out = self.run_check('03_messages.py', live)
        self.assertIn('left out: 0', out)
        self.assertTrue(re.search(r'PASS  messages: source 2, target 2, matched 2', out), out)


if __name__ == '__main__':
    unittest.main()
