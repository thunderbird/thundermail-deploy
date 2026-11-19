import datetime
import http.server
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import threading
import unittest

HERE = pathlib.Path(__file__).resolve().parent
P, PARENT, CHILD, SRC = 'proj-1', 'br-parent', 'br-cutover', 'br-source-old'


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


class FakeNeon:
    def __init__(self):
        self.log, self.lock_left, self.reset_moves = [], 0, True
        self.redirect, self.followed = False, 0
        self.branches = {
            PARENT: {'id': PARENT, 'name': 'production', 'default': True, 'parent_id': None},
            CHILD: {
                'id': CHILD,
                'name': 'tb-prod-cutover',
                'parent_id': PARENT,
                'last_reset_at': '2026-11-01T00:00:00Z',
                'parent_lsn': '0/100',
                'created_at': '2026-11-01T00:00:00Z',
            },
            SRC: {
                'id': SRC,
                'name': 'tb-prod-cutover-source',
                'parent_id': PARENT,
                'created_at': '2026-11-01T00:00:00Z',
            },
        }
        for b in self.branches.values():
            b.setdefault('current_state', 'ready')
        self.endpoints = [
            {'id': 'ep-p', 'branch_id': PARENT, 'type': 'read_write', 'host': 'ep-p.example.test'},
            {'id': 'ep-c', 'branch_id': CHILD, 'type': 'read_write', 'host': 'ep-c.example.test'},
            {'id': 'ep-s', 'branch_id': SRC, 'type': 'read_only', 'host': 'ep-s.example.test'},
        ]
        fake = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def reply(self, code, body):
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def handle_any(self, method):
                if self.path.startswith('/elsewhere'):
                    fake.followed += 1
                    return self.reply(200, {})
                if fake.redirect and method == 'POST':
                    self.send_response(302)
                    self.send_header('Location', '/elsewhere')
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                n = int(self.headers.get('Content-Length') or 0)
                body = json.loads(self.rfile.read(n) or b'null')
                fake.log.append((method, self.path, body, self.headers.get('Authorization')))
                code, out = fake.route(method, self.path.split('?')[0], body)
                self.reply(code, out)

            def do_GET(self):
                self.handle_any('GET')

            def do_POST(self):
                self.handle_any('POST')

            def do_DELETE(self):
                self.handle_any('DELETE')

        self.srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = 'http://127.0.0.1:%d' % self.srv.server_address[1]

    def route(self, method, path, body):
        pre = '/projects/%s' % P
        if method != 'GET' and self.lock_left:
            self.lock_left -= 1
            return 423, {'message': 'project is locked'}
        if method == 'GET' and path == pre + '/branches':
            return 200, {'branches': list(self.branches.values())}
        if method == 'GET' and path == pre + '/operations':
            return 200, {'operations': []}
        if method == 'GET' and path == pre + '/endpoints':
            return 200, {'endpoints': self.endpoints}
        m = re.fullmatch(pre + r'/branches/([^/]+)(/.*)?', path)
        if m:
            bid, rest = m.group(1), m.group(2) or ''
            if bid not in self.branches:
                return 404, {'message': 'no branch'}
            b = self.branches[bid]
            if method == 'GET' and not rest:
                return 200, {'branch': b}
            if method == 'POST' and rest == '/restore':
                if self.reset_moves:
                    b['last_reset_at'] = now()
                    b['parent_lsn'] = '0/999'
                return 200, {'branch': b}
            if method == 'DELETE' and not rest:
                del self.branches[bid]
                self.endpoints = [e for e in self.endpoints if e['branch_id'] != bid]
                return 200, {'branch': b}
            if method == 'POST' and rest.endswith('/reset_password'):
                return 200, {'role': {'name': rest.split('/')[2], 'password': 'S3cret-only-here'}}
        if method == 'POST' and path == pre + '/branches':
            bid = 'br-source-new'
            self.branches[bid] = {
                'id': bid,
                'name': body['branch']['name'],
                'parent_id': body['branch']['parent_id'],
                'created_at': now(),
                'current_state': 'ready',
            }
            self.endpoints.append(
                {
                    'id': 'ep-n',
                    'branch_id': bid,
                    'type': body['endpoints'][0]['type'],
                    'host': 'ep-n.example.test',
                }
            )
            return 201, {'branch': self.branches[bid], 'endpoints': [self.endpoints[-1]]}
        return 404, {'message': 'unknown ' + method + ' ' + path}


class NeonBranch(unittest.TestCase):
    def setUp(self):
        self.fake = FakeNeon()
        self.addCleanup(self.fake.srv.server_close)
        self.addCleanup(self.fake.srv.shutdown)
        self.t = pathlib.Path(tempfile.mkdtemp())

    def run_tool(self, *args, base=None, key='k-123', parent=PARENT):
        env = {
            'PATH': os.environ['PATH'],
            'HOME': str(self.t),
            'NEON_API_BASE': base or self.fake.base,
            'NEON_POLL_SECONDS': '0.01',
            'NEON_TEST': '1',
        }
        if key:
            env['NEON_API_KEY'] = key
        return subprocess.run(
            [
                sys.executable,
                str(HERE / 'neon_branch.py'),
                '--project',
                P,
                '--parent',
                parent,
                *args,
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )

    def writes(self):
        return [(m, p) for m, p, _, _ in self.fake.log if m != 'GET']

    def test_reset_proves_a_reset_later_than_the_legacy_stop(self):
        p = self.run_tool(
            'reset', '--branch', 'tb-prod-cutover', '--after', '2026-11-19T00:00:00Z', '--apply'
        )
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn('reset proof: last_reset_at ', p.stdout)
        self.assertRegex(
            p.stdout,
            r'(?m)^after: branch tb-prod-cutover state=ready last_reset_at=\S+ parent_lsn=0/999 ',
        )
        self.assertEqual(self.writes(), [('POST', '/projects/%s/branches/%s/restore' % (P, CHILD))])
        body = [b for m, p, b, _ in self.fake.log if m == 'POST'][0]
        self.assertEqual(body, {'source_branch_id': PARENT})

    def test_a_reset_that_did_not_move_the_branch_fails(self):
        self.fake.reset_moves = False
        p = self.run_tool(
            'reset', '--branch', 'tb-prod-cutover', '--after', '2026-11-19T00:00:00Z', '--apply'
        )
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn('reset proof FAILED: last_reset_at 2026-11-01T00:00:00Z', p.stdout)

    def test_dry_run_reads_only(self):
        p = self.run_tool('reset', '--branch', 'tb-prod-cutover', '--after', '2026-11-19T00:00:00Z')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn(
            'plan: reset tb-prod-cutover from its parent; nothing written (dry run)', p.stdout
        )
        p = self.run_tool(
            'source',
            '--name',
            'tb-prod-cutover-source',
            '--role',
            'stalwart_owner',
            '--db',
            'stalwart',
            '--out',
            str(self.t / 'src.env'),
            '--after',
            '2026-11-19T00:00:00Z',
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.writes(), [])
        self.assertFalse((self.t / 'src.env').exists())

    def test_the_parent_and_the_default_branch_are_never_written(self):
        for name in ('production',):
            p = self.run_tool(
                'reset', '--branch', name, '--after', '2026-11-19T00:00:00Z', '--apply'
            )
            self.assertEqual(p.returncode, 1)
            self.assertIn('REFUSED', p.stdout + p.stderr)
        self.fake.branches[CHILD]['parent_id'] = 'br-other'
        p = self.run_tool(
            'reset', '--branch', 'tb-prod-cutover', '--after', '2026-11-19T00:00:00Z', '--apply'
        )
        self.assertIn('is not a child of the parent branch', p.stderr)
        self.assertEqual(self.writes(), [])
        sys.path.insert(0, str(HERE))
        import neon_branch

        n = neon_branch.Neon(P, PARENT, 'k')
        n.child = PARENT
        with self.assertRaises(SystemExit) as cm:
            n.call('POST', '/projects/%s/branches/%s/restore' % (P, PARENT), {})
        self.assertIn('REFUSED write', str(cm.exception))

    def test_a_locked_project_is_retried(self):
        self.fake.lock_left = 2
        p = self.run_tool(
            'reset', '--branch', 'tb-prod-cutover', '--after', '2026-11-19T00:00:00Z', '--apply'
        )
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(p.stdout.count('project locked (HTTP 423)'), 2)

    def test_source_replaces_the_branch_read_only_with_its_own_password(self):
        out = self.t / 'src.env'
        p = self.run_tool(
            'source',
            '--name',
            'tb-prod-cutover-source',
            '--role',
            'stalwart_owner',
            '--db',
            'stalwart',
            '--out',
            str(out),
            '--after',
            '2026-11-19T00:00:00Z',
            '--apply',
        )
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertNotIn('S3cret-only-here', p.stdout + p.stderr)
        self.assertEqual(oct(out.stat().st_mode & 0o777), '0o600')
        self.assertEqual(
            out.read_text().splitlines(),
            [
                'STALWART_SOURCE_PG_HOST=ep-n.example.test',
                'STALWART_SOURCE_PG_DB=stalwart',
                'STALWART_SOURCE_PG_USER=stalwart_owner',
                'STALWART_SOURCE_PG_PASSWORD=S3cret-only-here',
            ],
        )
        self.assertEqual(
            [w[0] for w in self.writes()],
            ['DELETE', 'POST', 'POST'],
        )
        create = [b for m, p, b, _ in self.fake.log if m == 'POST' and p.endswith('/branches')][0]
        self.assertEqual(create['endpoints'], [{'type': 'read_only'}])
        self.assertIn('source proof: created_at ', p.stdout)
        self.assertIn('credential: role stalwart_owner password length 16 written to', p.stdout)

    def test_the_parent_is_resolved_from_its_name(self):
        p = self.run_tool(
            'reset',
            '--branch',
            'tb-prod-cutover',
            '--after',
            '2026-11-19T00:00:00Z',
            '--apply',
            parent='production',
        )
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        body = [b for m, p, b, _ in self.fake.log if m == 'POST'][0]
        self.assertEqual(body, {'source_branch_id': PARENT})
        p = self.run_tool('hosts', '--branch', 'tb-prod-cutover', parent='nope')
        self.assertIn('parent branch nope matches 0 branches', p.stderr)

    def test_a_role_cannot_walk_to_the_parent(self):
        p = self.run_tool(
            'source',
            '--name',
            'tb-prod-cutover-source',
            '--role',
            '../../%s/roles/x' % PARENT,
            '--db',
            'stalwart',
            '--out',
            str(self.t / 'o'),
            '--after',
            '2026-11-19T00:00:00Z',
            '--apply',
        )
        self.assertIn('is not a plain name', p.stderr)
        self.assertEqual(self.writes(), [])
        sys.path.insert(0, str(HERE))
        import neon_branch

        n = neon_branch.Neon(P, PARENT, 'k')
        n.child = CHILD
        with self.assertRaises(SystemExit) as cm:
            n.call(
                'POST',
                '/projects/%s/branches/%s/roles/../../%s/roles/x/reset_password'
                % (P, CHILD, PARENT),
            )
        self.assertIn('REFUSED write', str(cm.exception))

    def test_check_wants_both_ready_later_and_the_same_lsn(self):
        self.fake.branches[CHILD].update(last_reset_at='2026-11-19T05:00:00Z', parent_lsn='0/9')
        self.fake.branches[SRC].update(created_at='2026-11-19T05:00:05Z', parent_lsn='0/9')
        args = (
            'check',
            '--cutover',
            'tb-prod-cutover',
            '--source',
            'tb-prod-cutover-source',
            '--after',
            '2026-11-19T04:30:00Z',
        )
        p = self.run_tool(*args)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn(
            'branch check: both ready, later than the legacy stop '
            '2026-11-19T04:30:00Z, parent_lsn 0/9',
            p.stdout,
        )
        self.fake.branches[SRC]['parent_lsn'] = '0/a'
        p = self.run_tool(*args)
        self.assertEqual(p.returncode, 1)
        self.assertIn(
            'parent_lsn differs: tb-prod-cutover=0/9 tb-prod-cutover-source=0/a', p.stdout
        )
        self.fake.branches[SRC]['parent_lsn'] = '0/9'
        self.fake.branches[CHILD]['last_reset_at'] = '2026-11-19T04:00:00Z'
        p = self.run_tool(*args)
        self.assertIn(
            'last_reset_at 2026-11-19T04:00:00Z is not later than 2026-11-19T04:30:00Z', p.stdout
        )

    def test_a_redirect_is_not_followed(self):
        self.fake.redirect = True
        p = self.run_tool(
            'reset', '--branch', 'tb-prod-cutover', '--after', '2026-11-19T00:00:00Z', '--apply'
        )
        self.assertEqual(p.returncode, 1)
        self.assertIn('HTTP 302', p.stderr)
        self.assertEqual(self.fake.followed, 0)

    def test_hosts_and_a_non_loopback_api_base(self):
        p = self.run_tool('hosts', '--branch', 'tb-prod-cutover')
        self.assertEqual(
            p.stdout.splitlines(),
            ['parent_host=ep-p.example.test', 'branch_host=ep-c.example.test'],
        )
        p = self.run_tool('hosts', '--branch', 'tb-prod-cutover', base='http://192.0.2.1:9')
        self.assertIn('NEON_API_BASE is not the Neon API', p.stderr)
        p = self.run_tool('hosts', '--branch', 'tb-prod-cutover', key=None)
        self.assertIn('no Neon API key', p.stderr)


if __name__ == '__main__':
    unittest.main()
