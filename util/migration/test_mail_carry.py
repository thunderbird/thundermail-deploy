import base64
import email
import http.server
import importlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
mc = importlib.import_module('mail_carry')
tt = importlib.import_module('test_target')

PW16 = 'pw16-secret-x'
PW15 = 'pw15-secret-y'
SINCE = '2026-11-19T18:00:00Z'


def raw(n, to, subject, mid=True):
    head = 'From: x@ext.test\r\nTo: %s\r\nSubject: %s\r\n' % (to, subject)
    if mid:
        head += 'Message-ID: <%s@fixture.test>\r\n' % n
    return (head + '\r\nbody %s\r\n' % n).encode()


class Server:
    def __init__(self, kind, password):
        self.kind, self.auth = (
            kind,
            'Basic ' + base64.b64encode(b'admin:' + password.encode()).decode(),
        )
        self.accounts, self.principals, self.blobs, self.calls = {}, [], {}, []
        self.indexed = set()
        self.fail_download = self.truncate = self.short_total = self.mailbox_fail = False
        self.readback_delta = self.commit_then_500 = 0
        srv = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def send(self, code, body, ctype='application/json'):
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(code)
                self.send_header('Content-Type', ctype)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                srv.calls.append(('GET', self.path))
                if self.headers.get('Authorization') != srv.auth:
                    return self.send(401, {'detail': 'auth'})
                u = urllib.parse.urlsplit(self.path)
                if u.path.startswith('/jmap/download/'):
                    _, _, _, acc, blob, _ = u.path.split('/', 5)
                    if srv.fail_download or blob not in srv.blobs:
                        return self.send(404, {'detail': 'blob'})
                    data = srv.blobs[blob][:-1] if srv.truncate else srv.blobs[blob]
                    return self.send(200, data, 'message/rfc822')
                if u.path == '/api/principal' and srv.kind == 15:
                    q = urllib.parse.parse_qs(u.query)
                    page, limit = int(q['page'][0]), int(q['limit'][0])
                    items = srv.principals[(page - 1) * limit : page * limit]
                    return self.send(200, {'data': {'items': items, 'total': len(srv.principals)}})
                return self.send(404, {'detail': 'path'})

            def do_POST(self):
                n = int(self.headers.get('Content-Length') or 0)
                body = self.rfile.read(n)
                srv.calls.append(('POST', self.path))
                if self.headers.get('Authorization') != srv.auth:
                    return self.send(401, {'detail': 'auth'})
                if self.path.startswith('/jmap/upload/'):
                    bid = 'up%d' % len(srv.blobs)
                    srv.blobs[bid] = body
                    return self.send(200, {'blobId': bid, 'size': len(body)})
                req = json.loads(body)
                out = [srv.method(name, args, cid) for name, args, cid in req['methodCalls']]
                if srv.commit_then_500 and any(c[0] == 'Email/import' for c in req['methodCalls']):
                    srv.commit_then_500 -= 1
                    return self.send(500, {'detail': 'lost answer'})
                return self.send(200, {'methodResponses': out})

        self.httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), H)
        self.url = 'http://127.0.0.1:%d' % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def account(self, num, address, typ='User'):
        aid = mc.enc(num)
        self.accounts[aid] = {
            'name': address.split('@')[0],
            'emailAddress': address,
            '@type': typ,
            'boxes': {
                'a': {'name': 'Inbox', 'role': 'inbox', 'parentId': None},
                'b': {'name': 'Junk Mail', 'role': 'junk', 'parentId': None},
            },
            'emails': {},
        }
        if self.kind == 15:
            self.principals.append(
                {'id': num, 'type': 'individual', 'name': address, 'emails': [address]}
            )
        return aid

    def put(self, aid, body, when, boxes=('a',), keywords=(), indexed=True, eid=None):
        acc = self.accounts[aid]
        eid = eid or mc.enc(len(acc['emails']) + 100)
        bid = 'b-%s-%s' % (aid, eid)
        self.blobs[bid] = body
        mid = email.message_from_bytes(body).get('Message-ID')
        acc['emails'][eid] = {
            'blobId': bid,
            'size': len(body),
            'receivedAt': when,
            'mailboxIds': {b: True for b in boxes},
            'keywords': {k: True for k in keywords},
            'messageId': [mid.strip('<>')] if mid else None,
        }
        if indexed:
            self.indexed.add(eid)
        return eid

    def method(self, name, args, cid):
        acc = self.accounts.get(args.get('accountId'))
        if name == 'x:Account/query' and self.kind == 16:
            return [name, {'ids': sorted(self.accounts), 'total': len(self.accounts)}, cid]
        if name == 'x:Account/get' and self.kind == 16:
            lst = [
                {'id': i, **{k: self.accounts[i][k] for k in ('name', 'emailAddress', '@type')}}
                for i in args['ids']
            ]
            return [name, {'list': lst, 'notFound': []}, cid]
        if acc is None:
            return ['error', {'type': 'accountNotFound'}, cid]
        if name == 'Mailbox/get':
            return [name, {'list': [{'id': k, **v} for k, v in acc['boxes'].items()]}, cid]
        if name == 'Mailbox/set':
            if self.mailbox_fail:
                return [
                    name,
                    {'notCreated': {'m': {'type': 'forbidden', 'description': 'Projects'}}},
                    cid,
                ]
            made = {}
            for k, v in args['create'].items():
                mid = 'm%d' % len(acc['boxes'])
                acc['boxes'][mid] = {'name': v['name'], 'role': None, 'parentId': v['parentId']}
                made[k] = {'id': mid}
            return [name, {'created': made}, cid]
        if name == 'Email/query':
            f = args.get('filter') or {}
            ids = sorted(acc['emails'], key=lambda i: (len(i), i))
            if 'after' in f:
                ids = [
                    i
                    for i in ids
                    if i in self.indexed and acc['emails'][i]['receivedAt'] > f['after']
                ]
            if 'inMailbox' in f:
                ids = [i for i in ids if f['inMailbox'] in acc['emails'][i]['mailboxIds']]
            pos, lim = args.get('position', 0), args.get('limit', 5000)
            total = len(ids) + (1 if self.short_total else 0)
            return [name, {'ids': ids[pos : pos + lim], 'total': total}, cid]
        if name == 'Email/get':
            lst = []
            for i in args['ids']:
                if i in acc['emails']:
                    e = acc['emails'][i]
                    got = {p: e[p] for p in args['properties']}
                    if args['properties'] == ['size']:
                        got['size'] += self.readback_delta
                    lst.append({'id': i, **got})
            return [name, {'list': lst, 'notFound': []}, cid]
        if name == 'Email/set' and self.kind == 15:
            done = {}
            for eid, patch in args['update'].items():
                for k, v in patch.items():
                    acc['emails'][eid]['mailboxIds'][k.split('/', 1)[1]] = v
                done[eid] = None
            return [name, {'updated': done}, cid]
        if name == 'Email/import' and self.kind == 15:
            spec = args['emails']['e']
            body = self.blobs[spec['blobId']]
            mid = email.message_from_bytes(body).get('Message-ID')
            eid = mc.enc(len(acc['emails']) + 500)
            acc['emails'][eid] = {
                'blobId': spec['blobId'],
                'size': len(body),
                'receivedAt': spec['receivedAt'],
                'mailboxIds': spec['mailboxIds'],
                'keywords': spec['keywords'],
                'messageId': [mid.strip('<>')] if mid else None,
            }
            return [name, {'created': {'e': {'id': eid}}}, cid]
        return ['error', {'type': 'unknownMethod'}, cid]


class Tool(unittest.TestCase):
    def setUp(self):
        self.t = pathlib.Path(tempfile.mkdtemp(prefix='mail-carry-test-'))
        self.addCleanup(shutil.rmtree, self.t, True)
        self.t.chmod(0o700)
        self.v16, self.v15 = Server(16, PW16), Server(15, PW15)
        self.addCleanup(self.v16.close)
        self.addCleanup(self.v15.close)
        self.alice16 = self.v16.account(1, 'alice@example.test')
        self.bob16 = self.v16.account(2, 'bob@example.test')
        self.alice15 = self.v15.account(1, 'alice@example.test')
        self.bob15 = self.v15.account(2, 'bob@example.test')
        self.v16.accounts[self.alice16]['boxes'].update(
            {
                'p': {'name': 'Projects', 'role': None, 'parentId': None},
                'q': {'name': '2026', 'role': None, 'parentId': 'p'},
            }
        )
        self.v16.put(self.alice16, raw('old', 'alice', 'migrated'), '2026-11-01T10:00:00Z')
        self.v16.put(
            self.alice16, raw('new1', 'alice', 'n1'), '2026-11-19T18:05:00Z', keywords=['$seen']
        )
        self.v16.put(
            self.alice16,
            raw('new2', 'alice', 'n2'),
            '2026-11-19T18:06:00Z',
            boxes=['q'],
            keywords=['$flagged'],
            indexed=False,
        )
        self.v16.put(
            self.bob16, raw('new3', 'bob', 'n3', mid=False), '2026-11-19T18:07:00Z', ('b',)
        )
        self.out = self.t / 'secret' / 'mail-export-1'
        self.tsv = self.t / 'timing.tsv'

    def run_tool(self, *args, **env):
        e = {
            'PATH': os.environ.get('PATH', '/usr/bin:/bin'),
            'TARGET_ENV': 'tb-prod',
            'TIMING_TSV': str(self.tsv),
            'STALWART_URL': self.v16.url,
            'STALWART_USER': 'admin',
            'STALWART_PASSWORD': PW16,
            'OLD_STALWART_URL': self.v15.url,
            'OLD_STALWART_USER': 'admin',
            'OLD_STALWART_PASSWORD': PW15,
        }
        e.update(env)
        e = {k: v for k, v in e.items() if v is not None}
        p = subprocess.run(
            [sys.executable, '-I', str(HERE / 'mail_carry.py'), *args],
            capture_output=True,
            text=True,
            env=e,
            timeout=120,
        )
        return p.returncode, p.stdout + p.stderr

    def export(self, *flags, **env):
        return self.run_tool('export', '--since', SINCE, '--out', str(self.out), *flags, **env)

    def rows(self):
        return (
            [r.split('\t') for r in self.tsv.read_text().splitlines()] if self.tsv.exists() else []
        )

    def legacy(self, aid):
        return self.v15.accounts[aid]

    def test_export_counts_by_store_time_and_dry_run_writes_nothing(self):
        rc, out = self.export()
        self.assertEqual(rc, 0, out)
        self.assertIn('account=1 messages=2 bytes=', out)
        self.assertIn('account=2 messages=1 bytes=', out)
        self.assertIn('messages=3', out.splitlines()[-1])
        self.assertIn('scanned=4', out)
        self.assertIn('nothing written', out)
        self.assertFalse(self.out.exists())
        self.assertFalse(self.out.parent.exists())
        self.assertEqual(self.rows(), [])
        self.assertNotIn('after', json.dumps(self.v16.calls))

    def test_export_apply_writes_a_mode_700_directory_and_its_row(self):
        rc, out = self.export('--apply')
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.out.stat().st_mode & 0o777, 0o700)
        for f in self.out.rglob('*'):
            self.assertEqual(f.stat().st_mode & 0o777, 0o700 if f.is_dir() else 0o600, f)
        man = [json.loads(x) for x in (self.out / 'manifest.jsonl').read_text().splitlines()]
        self.assertEqual(len(man), 3)
        by = {m['messageId'][0] if m['messageId'] else None: m for m in man}
        self.assertEqual(
            by['new2@fixture.test']['mailboxes'], [{'role': None, 'path': ['Projects', '2026']}]
        )
        self.assertEqual(by[None]['mailboxes'], [{'role': 'junk', 'path': ['Junk Mail']}])
        for m in man:
            self.assertEqual((self.out / m['file']).read_bytes(), self.v16.blobs[m['blob']])
        self.assertEqual(json.loads((self.out / 'summary.json').read_text())['messages'], 3)
        self.assertEqual([r[0] for r in self.rows()], ['mail-export'])
        self.assertEqual(self.rows()[0][3:], ['messages', '3'])
        rc, out = self.export('--apply')
        self.assertEqual(rc, 1, out)
        self.assertIn('exists', out)
        self.assertEqual(len(self.rows()), 1)

    def test_a_failed_export_writes_no_row_and_no_summary(self):
        self.v16.fail_download = True
        rc, out = self.export('--apply')
        self.assertEqual(rc, 1, out)
        self.assertIn('STOP  HTTP 404', out)
        self.assertEqual(self.rows(), [])
        self.assertFalse((self.out / 'summary.json').exists())
        rc, out = self.run_tool('import', '--from', str(self.out), '--apply')
        self.assertEqual(rc, 2, out)
        self.assertIn('no complete export', out)

    def test_urls_must_be_loopback_and_the_import_never_targets_tb_prod(self):
        rc, out = self.export(STALWART_URL='http://192.0.2.1:18081')
        self.assertEqual(rc, 1, out)
        self.assertIn('not a loopback URL', out)
        self.assertEqual(self.export('--apply')[0], 0)
        n = len(self.v16.calls)
        rc, out = self.run_tool(
            'import', '--from', str(self.out), '--apply', OLD_STALWART_URL=self.v16.url
        )
        self.assertEqual(rc, 1, out)
        self.assertIn('refusing to import into tb-prod', out)
        self.assertEqual(len(self.v16.calls), n)
        rc, out = self.export(STALWART_PASSWORD=None)
        self.assertEqual(rc, 2, out)
        self.assertIn('NEED  empty STALWART_PASSWORD', out)
        rc, out = self.export(TARGET_ENV='tb-stage')
        self.assertEqual(rc, 64, out)

    def test_import_keeps_bytes_mailbox_and_flags_and_a_rerun_adds_nothing(self):
        self.assertEqual(self.export('--apply')[0], 0)
        rc, out = self.run_tool('import', '--from', str(self.out))
        self.assertEqual(rc, 0, out)
        self.assertIn('to_import=3 present=0', out)
        self.assertEqual(sum(len(a['emails']) for a in self.v15.accounts.values()), 0)
        self.assertEqual([r[0] for r in self.rows()], ['mail-export'])
        rc, out = self.run_tool('import', '--from', str(self.out), '--apply')
        self.assertEqual(rc, 0, out)
        self.assertIn(
            'account=1 legacy=1 messages=2 delivered=2 present=0 linked=0 failed=0 '
            'mailboxes_created=2',
            out,
        )
        self.assertIn('delivered=3 present=0 linked=0 failed=0 missing=0 in_legacy=3', out)
        al = self.legacy(self.alice15)
        names = {k: v['name'] for k, v in al['boxes'].items()}
        got = {}
        for e in al['emails'].values():
            got[e['messageId'][0]] = (
                [names[b] for b in e['mailboxIds']],
                sorted(e['keywords']),
                e['receivedAt'],
                self.v15.blobs[e['blobId']],
            )
        self.assertEqual(
            got['new1@fixture.test'][:3], (['Inbox'], ['$seen'], '2026-11-19T18:05:00Z')
        )
        self.assertEqual(
            got['new2@fixture.test'][:3], (['2026'], ['$flagged'], '2026-11-19T18:06:00Z')
        )
        self.assertEqual(got['new2@fixture.test'][3], raw('new2', 'alice', 'n2'))
        parent = [v for v in al['boxes'].values() if v['name'] == '2026'][0]['parentId']
        self.assertEqual(al['boxes'][parent]['name'], 'Projects')
        bob = list(self.legacy(self.bob15)['emails'].values())
        self.assertEqual([list(e['mailboxIds']) for e in bob], [['b']])
        self.assertEqual(self.rows()[-1][0::3], ['mail-import', 'messages'])
        self.assertEqual(self.rows()[-1][4], '3')
        rc, out = self.run_tool('import', '--from', str(self.out), '--apply')
        self.assertEqual(rc, 0, out)
        self.assertIn('delivered=0 present=3 linked=0 failed=0 missing=0 in_legacy=3', out)
        self.assertEqual(sum(len(a['emails']) for a in self.v15.accounts.values()), 3)
        tsv = sorted(self.out.glob('import-*.tsv'))
        self.assertTrue(tsv)

    def test_an_account_missing_on_legacy_stops_without_a_row(self):
        carol = self.v16.account(3, 'carol@example.test')
        self.v16.put(carol, raw('new4', 'carol', 'n4'), '2026-11-19T18:08:00Z')
        self.assertEqual(self.export('--apply')[0], 0)
        rc, out = self.run_tool('import', '--from', str(self.out), '--apply')
        self.assertEqual(rc, 1, out)
        self.assertIn('account=3 messages=1 MISSING: no legacy principal holds its address', out)
        self.assertIn('delivered=3 present=0 linked=0 failed=0 missing=1', out)
        self.assertEqual([r[0] for r in self.rows()], ['mail-export'])

    def test_a_changed_file_is_not_imported(self):
        self.assertEqual(self.export('--apply')[0], 0)
        f = next(self.out.glob('1/*.eml'))
        f.write_bytes(f.read_bytes().replace(b'body', b'BODY'))
        rc, out = self.run_tool('import', '--from', str(self.out), '--apply')
        self.assertEqual(rc, 1, out)
        self.assertIn('FAILED: the file differs from the export manifest', out)
        self.assertEqual(sum(len(a['emails']) for a in self.v15.accounts.values()), 2)
        self.assertEqual([r[0] for r in self.rows()], ['mail-export'])

    def test_the_export_directory_must_be_private(self):
        self.assertEqual(self.export('--apply')[0], 0)
        self.out.chmod(0o755)
        rc, out = self.run_tool('import', '--from', str(self.out), '--apply')
        self.assertEqual(rc, 1, out)
        self.assertIn('mode 700', out)
        self.assertEqual(self.v15.calls, [])

    def test_no_password_in_output_or_files(self):
        self.assertEqual(self.export('--apply')[0], 0)
        rc, out = self.run_tool('import', '--from', str(self.out), '--apply')
        self.assertEqual(rc, 0, out)
        texts = [out] + [p.read_bytes().decode('latin-1') for p in self.t.rglob('*') if p.is_file()]
        for text in texts:
            self.assertNotIn(PW16, text)
            self.assertNotIn(PW15, text)

    def test_a_message_received_at_the_merge_second_is_exported(self):
        self.v16.put(self.bob16, raw('edge', 'bob', 'edge'), SINCE)
        rc, out = self.export()
        self.assertEqual(rc, 0, out)
        self.assertIn('account=2 messages=2 bytes=', out)

    def test_a_short_download_or_a_short_listing_stops_the_export(self):
        self.v16.truncate = True
        rc, out = self.export('--apply')
        self.assertEqual(rc, 1, out)
        self.assertIn('bytes, the store says', out)
        self.assertFalse((self.out / 'summary.json').exists())
        self.assertEqual(self.rows(), [])
        self.v16.truncate, self.v16.short_total = False, True
        rc, out = self.export()
        self.assertEqual(rc, 1, out)
        self.assertIn('the server reports', out)

    def test_server_ids_never_become_paths(self):
        self.v16.put(self.bob16, raw('evil', 'bob', 'evil'), '2026-11-19T18:09:00Z', eid='../x')
        rc, out = self.export('--apply')
        self.assertEqual(rc, 1, out)
        self.assertIn('email id is not a JMAP id', out)
        self.assertFalse(self.out.exists())

    def test_the_manifest_must_match_the_summary_and_name_files_inside_the_export(self):
        self.assertEqual(self.export('--apply')[0], 0)
        summary = self.out / 'summary.json'
        good = summary.read_text()
        summary.write_text(good.replace('"messages": 3', '"messages": 4'))
        rc, out = self.run_tool('import', '--from', str(self.out), '--apply')
        self.assertEqual(rc, 1, out)
        self.assertIn('summary.json counts 4 messages, the manifest holds 3', out)
        summary.write_text(good)
        man = self.out / 'manifest.jsonl'
        lines = man.read_text().splitlines()
        m = json.loads(lines[0])
        m['file'] = '../../outside.eml'
        man.write_text('\n'.join([json.dumps(m)] + lines[1:]) + '\n')
        rc, out = self.run_tool('import', '--from', str(self.out), '--apply')
        self.assertEqual(rc, 1, out)
        self.assertIn('manifest line 1 is not an export line', out)
        self.assertEqual(self.v15.calls, [])

    def test_a_read_back_that_differs_fails_the_message(self):
        self.assertEqual(self.export('--apply')[0], 0)
        self.v15.readback_delta = 1
        rc, out = self.run_tool('import', '--from', str(self.out), '--apply')
        self.assertEqual(rc, 1, out)
        self.assertIn('FAILED: read back after the import differs', out)
        self.assertEqual([r[0] for r in self.rows()], ['mail-export'])

    def test_a_refused_mailbox_stops_and_prints_only_the_error_type(self):
        self.assertEqual(self.export('--apply')[0], 0)
        self.v15.mailbox_fail = True
        rc, out = self.run_tool('import', '--from', str(self.out), '--apply')
        self.assertEqual(rc, 1, out)
        self.assertIn('Mailbox/set in legacy account b: forbidden', out)
        self.assertNotIn('Projects', out)
        self.assertEqual([r[0] for r in self.rows()], ['mail-export'])

    def test_a_lost_import_answer_is_not_retried_and_a_rerun_finds_the_message(self):
        self.assertEqual(self.export('--apply')[0], 0)
        self.v15.commit_then_500 = 1
        rc, out = self.run_tool('import', '--from', str(self.out), '--apply')
        self.assertEqual(rc, 1, out)
        self.assertIn('STOP  HTTP 500 from /jmap', out)
        self.assertEqual(sum(len(a['emails']) for a in self.v15.accounts.values()), 1)
        rc, out = self.run_tool('import', '--from', str(self.out), '--apply')
        self.assertEqual(rc, 0, out)
        self.assertIn('delivered=2 present=1 linked=0 failed=0 missing=0 in_legacy=3', out)
        self.assertEqual(sum(len(a['emails']) for a in self.v15.accounts.values()), 3)

    def test_a_legacy_account_without_an_inbox_stops(self):
        self.assertEqual(self.export('--apply')[0], 0)
        del self.legacy(self.alice15)['boxes']['a']
        rc, out = self.run_tool('import', '--from', str(self.out), '--apply')
        self.assertEqual(rc, 1, out)
        self.assertIn('legacy account b has no Inbox', out)

    def test_no_proxy_no_user_info_and_the_tb_prod_port_is_refused(self):
        rc, out = self.export(http_proxy='http://127.0.0.1:9', HTTP_PROXY='http://127.0.0.1:9')
        self.assertEqual(rc, 0, out)
        rc, out = self.export(STALWART_URL=self.v16.url.replace('//', '//admin:x@'))
        self.assertEqual(rc, 1, out)
        self.assertIn('without user info', out)
        self.assertEqual(self.export('--apply')[0], 0)
        same = self.v16.url.replace('127.0.0.1', 'localhost')
        rc, out = self.run_tool('import', '--from', str(self.out), '--apply', OLD_STALWART_URL=same)
        self.assertEqual(rc, 1, out)
        self.assertIn('refusing to import into tb-prod', out)

    def test_two_messages_with_the_same_key_and_other_bytes_are_both_imported(self):
        when = '2026-11-19T18:10:00Z'
        self.v16.put(self.bob16, raw('aaa', 'bob', 't', mid=False), when)
        self.v16.put(self.bob16, raw('bbb', 'bob', 't', mid=False), when)
        self.assertEqual(self.export('--apply')[0], 0)
        rc, out = self.run_tool('import', '--from', str(self.out), '--apply')
        self.assertEqual(rc, 0, out)
        self.assertIn('delivered=5 present=0 linked=0 failed=0 missing=0 in_legacy=5', out)
        self.assertEqual(len(self.legacy(self.bob15)['emails']), 3)
        rc, out = self.run_tool('import', '--from', str(self.out), '--apply')
        self.assertEqual(rc, 0, out)
        self.assertIn('delivered=0 present=5 linked=0 failed=0 missing=0 in_legacy=5', out)

    def test_a_message_legacy_holds_in_one_of_its_folders_gets_the_other(self):
        body = raw('both', 'alice', 'both')
        when = '2026-11-19T18:11:00Z'
        self.v16.put(self.alice16, body, when, boxes=('a', 'q'))
        self.legacy(self.alice15)['boxes'].update(
            {
                'p': {'name': 'Projects', 'role': None, 'parentId': None},
                'q': {'name': '2026', 'role': None, 'parentId': 'p'},
            }
        )
        held = self.v15.put(self.alice15, body, when)
        self.assertEqual(self.export('--apply')[0], 0)
        rc, out = self.run_tool('import', '--from', str(self.out), '--apply')
        self.assertEqual(rc, 0, out)
        self.assertIn('account=1 legacy=1 messages=3 delivered=2 present=0 linked=1 failed=0', out)
        al = self.legacy(self.alice15)['emails']
        self.assertEqual(sorted(al[held]['mailboxIds']), ['a', 'q'])
        self.assertEqual(len(al), 3)
        rc, out = self.run_tool('import', '--from', str(self.out), '--apply')
        self.assertEqual(rc, 0, out)
        self.assertIn('delivered=0 present=4 linked=0 failed=0', out)


FAKE_PY = """#!{py}
import json, os, sys
e = os.environ
line = {{'argv': sys.argv[1:], 'pw': 'STALWART_PASSWORD' in e,
        'old_pw': 'OLD_STALWART_PASSWORD' in e, 'tsv': e.get('TIMING_TSV')}}
open(e['PY_LOG'], 'a').write(json.dumps(line) + '\\n')
sys.exit(int(os.environ.get('PY_RC', '0')))
"""


class Wrapper(tt.Sandbox):
    def setUp(self):
        super().setUp()
        self.mig = self.t / 'mig'
        shutil.copytree(HERE, self.mig, ignore=shutil.ignore_patterns('__pycache__', 'test_*.py'))
        tp = self.mig / 'targets' / 'tb-prod.sh'
        tp.write_text(re.sub(r'REPLACE_ME_[A-Z_]+', 'filled-for-test', tp.read_text()))
        (self.bin / 'fakepy').write_text(FAKE_PY.format(py=sys.executable))
        (self.bin / 'fakepy').chmod(0o755)
        self.kc = self.t / 'kc-prod'
        self.kc.write_text(tt.kubeconfig('kc-prod', 'mzla-eks-tb-prod01'))
        self.pylog = self.t / 'py.log'

    def forward(self):
        p = subprocess.Popen(
            [sys.executable, '-c', 'import time; time.sleep(60)', '--kubeconfig', str(self.kc)]
        )
        self.addCleanup(p.wait)
        self.addCleanup(p.kill)
        return {
            'tool': 'ss',
            'match': 'sport = :18081',
            'out': 'LISTEN 0 4096 127.0.0.1:18081 0.0.0.0:* users:(("kubectl",pid=%d,fd=8))\n'
            % p.pid,
        }

    def tunnel(self, owner='session-manager'):
        return {
            'tool': 'ss',
            'match': 'sport = :18080',
            'out': 'LISTEN 0 4096 127.0.0.1:18080 0.0.0.0:* users:(("%s",pid=1,fd=8))\n' % owner,
        }

    def carry(self, *args, rules=(), **env):
        self.rules(list(rules))
        e = {
            'TARGET_ENV': 'tb-prod',
            'KUBECONFIG_TARGET': str(self.kc),
            'STALWART_URL': 'http://localhost:18081',
            'STALWART_USER': 'admin',
            'STALWART_PASSWORD': PW16,
            'OLD_STALWART_URL': 'https://127.0.0.1:18080',
            'OLD_STALWART_USER': 'admin',
            'OLD_STALWART_PASSWORD': PW15,
            'PY': str(self.bin / 'fakepy'),
            'PY_LOG': str(self.pylog),
            'TIMING_TSV': str(self.t / 'ev' / 'timing.tsv'),
        }
        e.update(env)
        return self.run_sh([str(self.mig / 'window' / 'mail-carry.sh'), *args], **e)

    def py(self):
        return (
            [json.loads(x) for x in self.pylog.read_text().splitlines()]
            if self.pylog.exists()
            else []
        )

    def timing(self):
        p = self.t / 'ev' / 'timing.tsv'
        return [x.split('\t')[0] for x in p.read_text().splitlines()] if p.exists() else []

    def merged(self, state='MERGED'):
        return {
            'tool': 'gh',
            'match': r'pr view 198 -R thunderbird/thundermail-deploy --json state,mergedAt',
            'out': json.dumps(
                {'state': state, 'mergedAt': '2026-11-19T18:00:00Z' if state == 'MERGED' else None}
            ),
        }

    def test_usage_and_target(self):
        for args in ([], ['copy'], ['export', 'x'], ['import', 'a', 'b']):
            rc, out = self.carry(*args)
            self.assertEqual(rc, 64, (args, out))
        rc, out = self.carry('export', TARGET_ENV='tb-stage')
        self.assertEqual(rc, 64, out)
        self.assertIn('tb-prod only', out)
        rc, out = self.carry('export', '--apply', '--go', 'yes')
        self.assertEqual(rc, 64, out)
        self.assertEqual(self.py(), [])

    def test_export_needs_the_merged_scale_up_pr(self):
        rc, out = self.carry('export')
        self.assertEqual(rc, 2, out)
        self.assertIn('NEED  TM_SCALE_PR is empty', out)
        rc, out = self.carry(
            'export', TM_SCALE_PR='198', rules=[self.merged('OPEN'), self.forward()]
        )
        self.assertEqual(rc, 2, out)
        self.assertIn('NEED  thundermail-deploy PR 198 (the scale-up of 5.1) is not merged', out)
        self.assertEqual(self.py(), [])
        rc, out = self.carry(
            'export', '--apply', TM_SCALE_PR='198', rules=[self.merged('OPEN'), self.forward()]
        )
        self.assertEqual(rc, 1, out)
        self.assertIn('STOP MAIL (apply)', out)
        self.assertEqual(self.py(), [])
        self.assertEqual(self.timing(), ['stop-MAIL'])

    def test_export_needs_an_owned_port_forward(self):
        rules = [self.merged(), {'tool': 'kubectl', 'match': 'get pod', 'out': '{"items": []}'}]
        rc, out = self.carry('export', TM_SCALE_PR='198', rules=rules)
        self.assertEqual(rc, 2, out)
        self.assertIn('no Running Stalwart pod', out)
        self.assertEqual(self.py(), [])
        other = [
            self.merged(),
            dict(self.forward(), out=self.forward()['out'].replace('kubectl', 'nc')),
        ]
        rc, out = self.carry('export', '--apply', TM_SCALE_PR='198', rules=other)
        self.assertEqual(rc, 1, out)
        self.assertIn('credentials not sent', out)
        self.assertEqual(self.py(), [])

    def test_export_runs_the_tool_with_the_merge_time_and_no_password_argument(self):
        rules = [self.merged(), self.forward(), {'tool': 'curl', 'match': '18081/healthz/live'}]
        rc, out = self.carry('export', TM_SCALE_PR='198', rules=rules)
        self.assertEqual(rc, 0, out)
        self.assertIn('since 2026-11-19T18:00:00Z: the merge of thundermail-deploy PR 198', out)
        self.assertIn('MAIL dry-run: done', out)
        (call,) = self.py()
        self.assertEqual(
            call['argv'],
            [
                '-I',
                str(self.mig / 'mail_carry.py'),
                'export',
                '--since',
                '2026-11-19T18:00:00Z',
                '--out',
                str(self.t / 'secret' / 'mail-export-20261118T000000Z'),
                '--dry-run',
            ],
        )
        self.assertTrue(call['pw'])
        self.assertEqual(call['tsv'], str(self.t / 'ev' / 'timing.tsv'))
        self.assertNotIn(PW16, out)
        rc, out = self.carry('export', '--apply', TM_SCALE_PR='198', rules=rules, PY_RC='1')
        self.assertEqual(rc, 1, out)
        self.assertIn('mail_carry.py export exited 1', out)
        self.assertEqual(self.py()[-1]['argv'][-1], '--apply')

    def export_dir(self, name, complete=True):
        d = self.t / 'secret' / name
        d.mkdir(mode=0o700)
        if complete:
            (d / 'summary.json').write_text('{"messages": 0}')
        return d

    def test_import_needs_an_export_the_tunnel_and_a_running_legacy(self):
        ok = [self.tunnel(), {'tool': 'curl', 'match': '18080/healthz/live', 'out': '200'}]
        rc, out = self.carry('import', rules=ok)
        self.assertEqual(rc, 2, out)
        self.assertIn('NEED  no complete export', out)
        self.export_dir('mail-export-1')
        rc, out = self.carry('import', rules=[self.tunnel('nc'), ok[1]])
        self.assertEqual(rc, 2, out)
        self.assertIn('credentials not sent', out)
        rc, out = self.carry('import', rules=[self.tunnel(), dict(ok[1], out='503')])
        self.assertEqual(rc, 2, out)
        self.assertIn('NEED  legacy Stalwart answers 503 on healthz/live', out)
        self.assertEqual(self.py(), [])
        rc, out = self.carry('import', '--apply', rules=[self.tunnel(), dict(ok[1], out='000')])
        self.assertEqual(rc, 1, out)
        self.assertEqual(self.py(), [])

    def test_import_takes_the_newest_complete_export_or_the_named_one(self):
        ok = [self.tunnel(), {'tool': 'curl', 'match': '18080/healthz/live', 'out': '200'}]
        new = self.export_dir('mail-export-20261119T020000Z')
        old = self.export_dir('mail-export-20261119T010000Z')
        os.utime(new, (1, 1))
        self.export_dir('mail-export-20261119T030000Z', complete=False)
        rc, out = self.carry('import', '--apply', rules=ok)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.py()[-1]['argv'][2:], ['import', '--from', str(new), '--apply'])
        self.assertTrue(self.py()[-1]['old_pw'])
        rc, out = self.carry('import', str(old), rules=ok)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.py()[-1]['argv'][2:], ['import', '--from', str(old), '--dry-run'])
        rc, out = self.carry('import', rules=ok, PY_RC='2')
        self.assertEqual(rc, 2, out)
        self.assertNotIn(PW15, out)
        rc, out = self.carry('import', '--apply', rules=ok, PY_RC='2')
        self.assertEqual(rc, 1, out)
        self.assertIn('mail_carry.py import reports NEED', out)


if __name__ == '__main__':
    unittest.main()
