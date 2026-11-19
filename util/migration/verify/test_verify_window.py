import datetime
import http.server
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

HERE = pathlib.Path(__file__).resolve().parent
MIG = HERE.parent
BODY = b'From: a@example.test\r\nSubject: x\r\n\r\nhello\r\n'
RCVD = 1793491200
WHEN = '2026-11-01T00:00:00Z'
PROD = {
    'STALWART_SOURCE_PG_HOST': 'ep-branch-1.example.test',
    'STALWART_SOURCE_PG_DB': 'stalwart',
    'STALWART_SOURCE_PG_USER': 'reader',
    'STALWART_SOURCE_PG_PASSWORD': 'branch-pw',
    'STALWART_LEGACY_PG_HOST': 'ep-production-1.example.test',
    'STALWART_LEGACY_PG_PASSWORD': 'production-pw',
}
FAKE_PSQL = """#!{py}
import json, os, re, sys
args = sys.argv[1:]
query = [args[i + 1] for i, a in enumerate(args) if a == '-c'][1]
with open(os.environ['FAKE_PSQL_LOG'], 'a') as f:
    row = {{'dsn': args[0], 'query': query, 'pw': os.environ.get('PGPASSWORD')}}
    f.write(json.dumps(row) + '\\n')
for pattern, out in json.load(open(os.environ['FAKE_PSQL_RULES'])):
    if re.search(pattern, query, re.S):
        sys.stdout.write('BEGIN\\n' + out + 'COMMIT\\n')
        sys.exit(0)
sys.stderr.write('no rule for ' + query)
sys.exit(1)
"""
FAKE_AWS = """#!{py}
import json, os, sys
args = sys.argv[1:]
with open(os.environ['FAKE_AWS_LOG'], 'a') as f:
    f.write(' '.join(args) + '\\n')
opt = {{args[i]: args[i + 1] for i in range(len(args) - 1) if args[i].startswith('--')}}
b = json.load(open(os.environ['FAKE_AWS_FIXTURE']))[opt['--bucket']]
text = opt['--output'] == 'text'
if args[1] == 'list-objects-v2':
    rows = list(b['objects'].items())
    if not text:
        print(json.dumps([[k, s] for k, s in rows] or None))
    elif not rows:
        print('None')
    for i in range(0, len(rows) if text else 0, 2):
        print(''.join('%s\\t%s\\n' % kv for kv in rows[i : i + 2]), end='')
else:
    if not text:
        print(json.dumps(b['markers']))
    else:
        print(''.join(k + '\\n' for k in b['markers']) or 'None\\n', end='')
"""


def enc_id(thread, doc):
    os.environ.setdefault('VERIFY_OUT', tempfile.mkdtemp())
    from common import enc

    return enc((thread << 32) | doc)


def utc(t):
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


class Target(http.server.BaseHTTPRequestHandler):
    emails = {}
    log = []
    filter_fails = False

    def log_message(self, *a):
        pass

    def send(self, code, body, ctype='application/json'):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.endswith('/jmap/session'):
            session = {'primaryAccounts': {'urn:stalwart:jmap': 'admin'}, 'accounts': {'admin': {}}}
            return self.send(200, json.dumps(session).encode())
        self.send(200, BODY, 'message/rfc822')

    def x(self, obj):
        return {
            'Account': [
                {
                    'id': 'b',
                    '@type': 'User',
                    'name': 'alice',
                    'domainId': 'd1',
                    'emailAddress': 'alice@example.test',
                }
            ],
            'Domain': [{'id': 'd1', 'name': 'example.test'}],
            'DkimSignature': [{'id': 'k1', 'domainId': 'd1'}],
            'WebHook': [{'id': 'w1', 'url': 'https://hook.example.test/x'}],
            'SystemSettings': [{'id': 'singleton', 'defaultHostname': 'mail.thundermail.com'}],
            'BlobStore': [{'id': 'singleton', '@type': 'S3', 'bucket': 'tgt-bucket'}],
            'MtaSts': [{'id': 'singleton', 'mxHosts': {'mail.thundermail.com': True}}],
        }.get(obj, [{'id': 'singleton', '@type': 'Default'}] if obj[0].isupper() else [])

    def do_POST(self):
        calls = json.loads(self.rfile.read(int(self.headers['Content-Length'])))['methodCalls']
        out = []
        for name, args, tag in calls:
            obj, method = name.split('/')
            self.log.append([name, args])
            if obj.startswith('x:'):
                items = self.x(obj[2:])
                if obj[2:] in ('BlockedIp', 'AllowedIp', 'MtaQueueQuota'):
                    items = []
                if method == 'query':
                    ids = [i['id'] for i in items]
                    out.append([name, {'ids': ids, 'total': len(ids)}, tag])
                else:
                    out.append([name, {'list': [i for i in items if i['id'] in args['ids']]}, tag])
            elif obj == 'Email' and method == 'query':
                f = args.get('filter') or {}
                if f and self.filter_fails:
                    out.append(['error', {'type': 'unsupportedFilter'}, tag])
                    continue
                ids = [
                    i
                    for i, e in self.emails.items()
                    if e['account'] == args['accountId']
                    and f.get('after', '') <= e['receivedAt'] < f.get('before', '9')
                ]
                page = ids[args.get('position', 0) :][: args.get('limit', 1000)]
                out.append([name, {'ids': page, 'total': len(ids)}, tag])
            elif obj == 'Email':
                items = [{'id': i, **self.emails[i]} for i in args['ids'] if i in self.emails]
                out.append([name, {'list': items}, tag])
            else:
                out.append([name, {'ids': [], 'total': 0, 'list': []}, tag])
        self.send(200, json.dumps({'methodResponses': out}).encode())


def email(account, blob, when=WHEN, size=len(BODY)):
    return {
        'account': account,
        'blobId': blob,
        'size': size,
        'receivedAt': when,
        'mailboxIds': {'0': True},
        'threadId': 't',
        'subject': 's',
    }


class Base(unittest.TestCase):
    def setUp(self):
        self.t = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.t, ignore_errors=True)
        self.bin = self.t / 'bin'
        self.bin.mkdir()
        for name, text in (('psql', FAKE_PSQL), ('aws', FAKE_AWS)):
            (self.bin / name).write_text(text.format(py=sys.executable))
            (self.bin / name).chmod(0o755)
        (self.bin / 'python3').symlink_to(sys.executable)
        self.out = self.t / 'out'
        (self.t / 'principals.json').write_text(
            json.dumps(
                [
                    {
                        'id': 1,
                        'type': 'individual',
                        'name': 'alice',
                        'emails': ['alice@example.test'],
                    },
                    {'id': 3, 'type': 'domain', 'name': 'example.test'},
                ]
            )
        )
        self.rules = []
        Target.emails, Target.log, Target.filter_fails = {}, [], False
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Target)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def env(self, **extra):
        e = {
            'PATH': '%s:/usr/bin:/bin' % self.bin,
            'HOME': str(self.t),
            'LANG': 'C.UTF-8',
            'PSQL_MODE': 'local',
            'VERIFY_OUT': str(self.out),
            'PRINCIPALS': str(self.t / 'principals.json'),
            'TARGET_URL': 'http://127.0.0.1:%d' % self.server.server_port,
            'TARGET_USER': 'admin',
            'TARGET_PASSWORD': 'pw',
            'FAKE_PSQL_LOG': str(self.t / 'psql.log'),
            'FAKE_PSQL_RULES': str(self.t / 'psql.json'),
            'FAKE_AWS_LOG': str(self.t / 'aws.log'),
            'FAKE_AWS_FIXTURE': str(self.t / 'aws.json'),
        }
        e.update(extra)
        return e

    def run_py(self, args, **env):
        (self.t / 'psql.json').write_text(json.dumps(self.rules))
        p = subprocess.run(
            [sys.executable, *args],
            env=self.env(**env),
            cwd=HERE,
            capture_output=True,
            text=True,
            timeout=120,
        )
        return p.returncode, p.stdout + p.stderr

    def run_all(self, *args, **env):
        (self.t / 'psql.json').write_text(json.dumps(self.rules))
        p = subprocess.run(
            ['bash', str(HERE / 'run_all.sh'), *args],
            env=self.env(**env),
            capture_output=True,
            text=True,
            timeout=120,
        )
        return p.returncode, p.stdout + p.stderr

    def queries(self):
        p = self.t / 'psql.log'
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


SNAPSHOT_RULES = [
    [r'count\(\*\) from f', '2432\n'],
    [r'group by 1,2', '1\t0\t2\n1\t1\t6\n'],
    [r'get_byte\(k,4\)=0\)', '1\t0\t%d\t%d\n1\t1\t%d\t%d\n' % (len(BODY), RCVD, len(BODY), RCVD)],
    [r'from s_cal', ''],
    [r'from s_card', ''],
    [r'to_char', '2026-11-01 00:00:00\n'],
]


class ProdSource(Base):
    def test_tb_prod_reads_only_the_branch_credential(self):
        self.rules = SNAPSHOT_RULES
        rc, out = self.run_py(['01_source_snapshot.py'], TARGET_ENV='tb-prod', **PROD)
        self.assertEqual(rc, 0, out)
        q = self.queries()
        self.assertTrue(q)
        for x in q:
            self.assertIn('host=ep-branch-1.example.test dbname=stalwart user=reader ', x['dsn'])
            self.assertEqual(x['pw'], 'branch-pw')

    def test_tb_prod_refuses_a_secret_id_or_other_source_names(self):
        for name, value in (
            ('SOURCE_PG_SECRET_ID', 'mailstrom/prod/stalwart.postboot.postgresql_backend'),
            ('SOURCE_PG_HOST', 'ep-production-1.example.test'),
            ('SOURCE_PG_PASSWORD', 'production-pw'),
        ):
            with self.subTest(name=name):
                rc, out = self.run_py(
                    ['01_source_snapshot.py'], TARGET_ENV='tb-prod', **PROD, **{name: value}
                )
                self.assertEqual(rc, 1, out)
                self.assertIn(
                    'refusing: on tb-prod the source is branch tb-prod-cutover-source', out
                )
                self.assertIn('unset %s' % name, out)
                self.assertEqual(self.queries(), [])

    def test_tb_prod_refuses_missing_names_and_the_production_endpoint_or_password(self):
        cases = (
            (
                {'STALWART_SOURCE_PG_HOST': ''},
                'refusing: empty STALWART_SOURCE_PG_HOST: on tb-prod',
            ),
            (
                {'STALWART_SOURCE_PG_HOST': 'ep-production-1-pooler.example.test'},
                'STALWART_SOURCE_PG_HOST is the production endpoint',
            ),
            (
                {'STALWART_SOURCE_PG_PASSWORD': 'production-pw'},
                'STALWART_SOURCE_PG_PASSWORD is the production credential',
            ),
        )
        for change, msg in cases:
            with self.subTest(change=change):
                rc, out = self.run_py(
                    ['01_source_snapshot.py'], TARGET_ENV='tb-prod', **{**PROD, **change}
                )
                self.assertEqual(rc, 1, out)
                self.assertIn(msg, out)
                self.assertEqual(self.queries(), [])

    def test_the_tb_prod_env_file_carries_no_production_secret_id(self):
        text = (HERE / 'tb-prod.env').read_text()
        self.assertNotIn('mailstrom/prod', text)
        p = subprocess.run(
            [
                'bash',
                '-c',
                'SOURCE_PG_SECRET_ID=x; set -a; . "$0"; printf %s "$SOURCE_PG_SECRET_ID"',
                str(HERE / 'tb-prod.env'),
            ],
            capture_output=True,
            text=True,
            env={'PATH': '/usr/bin:/bin'},
        )
        self.assertEqual(p.stdout, '')

    def test_run_all_on_tb_prod_refuses_before_any_check(self):
        rc, out = self.run_all(
            '--group', 'compare', TARGET_ENV='tb-prod', **{**PROD, 'STALWART_SOURCE_PG_DB': ''}
        )
        self.assertEqual(rc, 99, out)
        self.assertIn('refusing: empty STALWART_SOURCE_PG_DB', out)
        self.assertIn('== refusing: no tb-prod source, nothing ran', out)
        self.assertNotIn('== 02 directory', out)


class Snapshot(Base):
    def test_a_full_task_queue_is_printed_and_does_not_fail(self):
        self.rules = SNAPSHOT_RULES
        rc, out = self.run_py(['01_source_snapshot.py'], TARGET_ENV='tb-prod', **PROD)
        self.assertEqual(rc, 0, out)
        self.assertIn('PASS  v0.15 task queue (table f): 2432 pending tasks, not a failure', out)
        self.assertNotIn('FAIL', out)

    def test_an_account_range_reads_only_that_key_range(self):
        self.rules = SNAPSHOT_RULES
        rc, out = self.run_py(
            ['01_source_snapshot.py'],
            TARGET_ENV='tb-stage',
            VERIFY_ACCOUNT_RANGE='3-9',
            SOURCE_PG_HOST='h',
            SOURCE_PG_DB='d',
            SOURCE_PG_USER='u',
            SOURCE_PG_PASSWORD='p',
        )
        self.assertEqual(rc, 0, out)
        p_queries = [x['query'] for x in self.queries() if ' from p ' in x['query']]
        self.assertTrue(p_queries)
        for q in p_queries:
            self.assertIn("k >= '\\x00000003'::bytea and k < '\\x0000000a'::bytea", q)
        s_queries = [x['query'] for x in self.queries() if 's_email' in x['query']]
        for q in s_queries:
            self.assertIn('accid between 3 and 9', q)


class Sample(Base):
    def rules_for(self, rows):
        return [
            [r'pg_class', '1000\n'],
            [r'tablesample system', ''.join('%d\t%d\t%d\t%d\n' % r for r in rows)],
        ]

    def emails(self, n):
        return {enc_id(7, d): email('b', 'b%d' % d, when=utc(RCVD + d)) for d in range(n)}

    def test_compare_samples_on_the_source_and_fetches_only_those(self):
        Target.emails = self.emails(30)
        self.rules = self.rules_for([(1, d, len(BODY), RCVD + d) for d in range(0, 30, 3)])
        rc, out = self.run_py(
            ['03_messages.py', '--sample'], TARGET_ENV='tb-prod', VERIFY_BODY_SAMPLE='4', **PROD
        )
        self.assertEqual(rc, 0, out)
        self.assertIn('PASS  sample: 4 live messages picked on the source in 1 accounts', out)
        self.assertIn('PASS  messages: sampled 4, found on target 4 (by receivedAt 4', out)
        self.assertIn(
            'PASS  bodies downloaded (random sample): 4/4 of 4 sampled on the source', out
        )
        self.assertFalse((self.out / 'source.json').exists())
        listings = [a for n, a in Target.log if n == 'Email/query' and not a.get('filter')]
        self.assertEqual(listings, [])
        gets = [i for n, a in Target.log if n == 'Email/get' for i in a['ids']]
        self.assertEqual(len(gets), 4)
        for x in self.queries():
            self.assertTrue('pg_class' in x['query'] or 'tablesample' in x['query'], x)
            self.assertNotIn('group by', x['query'])

    def test_an_empty_sample_size_uses_the_default(self):
        Target.emails = self.emails(3)
        self.rules = self.rules_for([(1, d, len(BODY), RCVD + d) for d in range(3)])
        rc, out = self.run_py(
            ['03_messages.py', '--sample'], TARGET_ENV='tb-prod', VERIFY_BODY_SAMPLE='', **PROD
        )
        self.assertEqual(rc, 0, out)
        self.assertIn('wanted 200)', out)
        self.assertIn('bodies downloaded (random sample): 3/3 of 3 sampled', out)

    def test_a_missing_or_changed_message_fails(self):
        Target.emails = self.emails(2)
        Target.emails[enc_id(7, 1)]['size'] = 5
        self.rules = self.rules_for([(1, d, len(BODY), RCVD + d) for d in range(3)])
        rc, out = self.run_py(
            ['03_messages.py', '--sample'], TARGET_ENV='tb-prod', VERIFY_BODY_SAMPLE='3', **PROD
        )
        self.assertEqual(rc, 1, out)
        self.assertIn("missing on target 1 ['1:2']", out)
        self.assertIn("FAIL  size mismatches: 1 of 2 sampled ['1:1']", out)

    def test_a_window_miss_falls_back_to_that_account_only(self):
        Target.emails = self.emails(2)
        Target.filter_fails = True
        self.rules = self.rules_for([(1, d, len(BODY), RCVD + d) for d in range(2)])
        rc, out = self.run_py(
            ['03_messages.py', '--sample'], TARGET_ENV='tb-prod', VERIFY_BODY_SAMPLE='2', **PROD
        )
        self.assertEqual(rc, 0, out)
        self.assertIn('found on target 2 (by receivedAt 0, by account listing 2)', out)

    def test_full_mode_with_an_empty_sample_size_does_not_crash(self):
        Target.emails = {enc_id(7, 0): email('b', 'b0'), enc_id(7, 1): email('b', 'b1')}
        self.out.mkdir()
        (self.out / 'source.json').write_text(
            json.dumps({'messages': {'1:0': [len(BODY), RCVD], '1:1': [len(BODY), RCVD]}})
        )
        rc, out = self.run_py(['03_messages.py'], VERIFY_SKIP_BODIES='1', VERIFY_BODY_SAMPLE='')
        self.assertEqual(rc, 0, out)
        self.assertIn('PASS  bodies downloaded (random sample): 2/2 of 2 migrated', out)


class Compare(Base):
    def test_compare_group_runs_02_03_sample_and_07_only(self):
        Target.emails = {enc_id(7, d): email('b', 'b%d' % d, when=utc(RCVD + d)) for d in range(3)}
        self.rules = [
            [r'pg_class', '1000\n'],
            [
                r'tablesample system',
                ''.join('1\t%d\t%d\t%d\n' % (d, len(BODY), RCVD + d) for d in range(3)),
            ],
        ]
        rc, out = self.run_all(
            '--group',
            'compare',
            TARGET_ENV='tb-prod',
            VERIFY_BODY_SAMPLE='',
            EXPECT_HOSTNAME='mail.thundermail.com',
            EXPECT_BUCKET='tgt-bucket',
            INTERNAL_CIDRS='10.130.0.0/16',
            **PROD,
        )
        self.assertEqual(rc, 0, out)
        for head in ('== 02 directory', '== 03 messages', '== 07 target configuration'):
            self.assertIn(head, out)
        for head in ('== 01', '== 04', '== 05', '== 06'):
            self.assertNotIn(head, out)
        self.assertIn('PASS  account ids: source 1, target 1', out)
        self.assertIn('PASS  bodies downloaded (random sample): 3/3 of 3 sampled', out)
        self.assertIn('== done (compare): 0 check(s) with FAIL', out)
        self.assertNotIn('  FAIL  ', out)


class RealPiece(Base):
    def test_piece_bodies_runs_the_real_01_and_03_per_batch(self):
        Target.emails = {enc_id(7, 0): email('b', 'b0'), enc_id(7, 1): email('b', 'b1')}
        self.rules = SNAPSHOT_RULES
        rc, out = self.run_all(
            '--piece',
            'bodies',
            TARGET_ENV='tb-prod',
            VERIFY_PIECES_DIR=str(self.t / 'pieces'),
            VERIFY_SKIP_BODIES='1',
            **PROD,
        )
        self.assertEqual(rc, 0, out)
        self.assertIn('== piece bodies batch 0-4294967295 (1 of 1)', out)
        self.assertIn('PASS  messages: source 2, target 2, matched 2', out)
        self.assertIn('PASS  bodies downloaded: 2/2 of 2 migrated', out)
        self.assertIn('passed 1, failed 0 [], left 0', out)
        self.assertTrue((self.t / 'pieces' / 'bodies' / '0-4294967295' / 'source.json').exists())
        for x in self.queries():
            if ' from p ' in x['query']:
                self.assertIn("k >= '\\x00000000'::bytea", x['query'])


class Buckets(Base):
    FIXTURE = {
        'src-bucket': {
            'objects': {'k3': 30, 'k1': 10, 'k2': 20, 'k5': 50, 'k6': 60},
            'markers': [],
        },
        'tgt-bucket': {
            'objects': {'k2': 21, 'k1': 10, 'k7': 70, 'k3': 30},
            'markers': ['k6', 'k9'],
        },
    }
    ENV = {
        'TARGET_ENV': 'tb-stage',
        'AWS_REGION': 'eu-central-1',
        'SOURCE_BUCKET': 'src-bucket',
        'TARGET_BUCKET': 'tgt-bucket',
        'SOURCE_AWS_PROFILE': 'src',
        'TARGET_AWS_PROFILE': 'tgt',
    }
    OLD_LINES = (
        '== 06 blob buckets\n'
        '  PASS  objects: source 5, target 4\n'
        "  FAIL  same key, different size: 1 ['k2']\n"
        '  PASS  message blobs referenced by the target: 2; '
        '03 downloaded every one through Stalwart\n'
        '  FAIL  only on source: 2 (1 purged by the target, recoverable from versions; '
        "1 missing from the target with no delete marker) ['k5']\n"
        '  PASS  only on target: 1 (written by the target since the copy)\n'
        '  -> 2 fail, 0 warn\n'
    )

    def setUp(self):
        super().setUp()
        (self.t / 'aws.json').write_text(json.dumps(self.FIXTURE))
        self.out.mkdir()

    def test_listings_go_to_sorted_files_and_the_lines_are_unchanged(self):
        (self.out / 'target-message-blobs.json').write_text(
            json.dumps({'bodies_checked': 'all', 'blobs': {'1:0': 'x', '1:1': 'y'}})
        )
        rc, out = self.run_py(['06_buckets.py'], **self.ENV)
        self.assertEqual(rc, 1, out)
        self.assertEqual(out, self.OLD_LINES)
        self.assertEqual(
            (self.out / 'bucket-source.tsv').read_text(), 'k1\t10\nk2\t20\nk3\t30\nk5\t50\nk6\t60\n'
        )
        self.assertEqual((self.out / 'bucket-target-delete-markers.txt').read_text(), 'k6\nk9\n')
        calls = (self.t / 'aws.log').read_text()
        self.assertEqual(calls.count('--output text'), 3)
        self.assertNotIn('--output json', calls)

    def test_piece_buckets_runs_before_03_and_a_resume_reuses_the_listings(self):
        rc, out = self.run_py(['06_buckets.py'], VERIFY_PIECE='buckets', **self.ENV)
        self.assertIn(
            'PASS  message blobs referenced by the target: not read by piece buckets', out
        )
        self.assertNotIn('run 03 first', out)
        self.assertIn('1 purged by the target', out)
        self.assertNotIn('rerun it without VERIFY_SKIP_BODIES', out)
        n = len((self.t / 'aws.log').read_text().splitlines())
        rc2, out2 = self.run_py(
            ['06_buckets.py'], VERIFY_PIECE='buckets', VERIFY_REUSE_LISTINGS='1', **self.ENV
        )
        self.assertEqual(out2, out)
        self.assertEqual(len((self.t / 'aws.log').read_text().splitlines()), n)


FAKE_CHECK = """import os, sys, time
name = os.path.basename(sys.argv[0])[:-3]
rng = os.environ.get('VERIFY_ACCOUNT_RANGE', 'all')
time.sleep(float(os.environ.get('FAKE_SLEEP', '0')))
print('== ' + name)
print('  PASS  %s range %s out %s' % (name, rng, os.path.basename(os.environ['VERIFY_OUT'])))
with open(os.environ['FAKE_RUNS'], 'a') as f:
    f.write('%s %s\\n' % (name, rng))
if os.environ.get('FAKE_FAIL') == rng + ':' + name:
    print('  FAIL  planted')
    sys.exit(1)
"""


class Pieces(Base):
    def setUp(self):
        super().setUp()
        self.mig = self.t / 'mig'
        shutil.copytree(MIG, self.mig, ignore=shutil.ignore_patterns('__pycache__', 'test_*.py'))
        for name in (
            '01_source_snapshot',
            '03_messages',
            '04_collections',
            '05_folders',
            '06_buckets',
        ):
            (self.mig / 'verify' / (name + '.py')).write_text(FAKE_CHECK)
        (self.t / 'principals.json').write_text(
            json.dumps([{'id': i, 'type': 'individual', 'name': 'u%d' % i} for i in (1, 2, 5)])
        )

    def piece(self, *args, **env):
        p = subprocess.run(
            ['bash', str(self.mig / 'verify' / 'run_all.sh'), '--piece', *args],
            env=self.env(
                TARGET_ENV='tb-stage',
                VERIFY_PIECES_DIR=str(self.t / 'pieces'),
                FAKE_RUNS=str(self.t / 'runs'),
                VERIFY_BATCH='2',
                **env,
            ),
            capture_output=True,
            text=True,
            timeout=120,
        )
        return p.returncode, p.stdout + p.stderr

    def runs(self):
        p = self.t / 'runs'
        return p.read_text().splitlines() if p.exists() else []

    def test_batches_stop_at_the_budget_and_resume_by_progress(self):
        rc, out = self.piece('counts', VERIFY_PIECE_BUDGET='1', FAKE_SLEEP='0.4')
        self.assertEqual(rc, 0, out)
        self.assertEqual(
            self.runs(), ['01_source_snapshot 0-2', '04_collections 0-2', '05_folders 0-2']
        )
        self.assertIn('== piece counts: stopping before batch 3-4294967295', out)
        self.assertIn('== piece counts: 2 batches; passed 1, failed 0 [], left 1 (resume key ', out)
        self.assertIn('not finished: rerun run_all.sh --piece counts --resume', out)
        progress = self.t / 'pieces' / 'counts' / 'progress.tsv'
        self.assertEqual(progress.read_text().split('\t')[:3], ['0-2', 'pass', '0'])
        rc, out = self.piece('counts', '--resume')
        self.assertEqual(rc, 0, out)
        self.assertEqual(
            self.runs()[3:],
            [x + ' 3-4294967295' for x in ('01_source_snapshot', '04_collections', '05_folders')],
        )
        self.assertIn('passed 2, failed 0 [], left 0', out)
        self.assertNotIn('not finished', out)
        self.assertIn('PASS  04_collections range 3-4294967295 out 3-4294967295', out)
        self.assertIn('== done (piece counts): 0 check(s) with FAIL', out)

    def test_a_failed_batch_is_rerun_on_resume_and_a_fresh_run_moves_the_old_one(self):
        rc, out = self.piece('bodies', FAKE_FAIL='0-2:03_messages')
        self.assertEqual(rc, 1, out)
        self.assertIn("passed 1, failed 1 ['0-2'], left 1", out)
        self.assertIn('== done (piece bodies): 1 check(s) with FAIL', out)
        rc, out = self.piece('bodies', '--resume')
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.runs()[4:], ['01_source_snapshot 0-2', '03_messages 0-2'])
        self.assertIn('left 0', out)
        rc, out = self.piece('bodies')
        self.assertIn('== piece bodies: earlier run moved to ', out)
        self.assertEqual(len(self.runs()), 10)

    def test_a_failed_01_skips_the_rest_of_its_batch(self):
        rc, out = self.piece('bodies', FAKE_FAIL='0-2:01_source_snapshot')
        self.assertEqual(rc, 1, out)
        self.assertIn(
            '== piece bodies batch 0-2: 01 failed, the rest of this batch is skipped', out
        )
        self.assertNotIn('03_messages 0-2', self.runs())

    def test_piece_buckets_runs_06_once_with_the_piece_flag(self):
        rc, out = self.piece('buckets', '--resume')
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.runs(), ['06_buckets all'])
        self.assertIn('PASS  06_buckets range all out buckets', out)

    def test_bad_piece_arguments_are_refused(self):
        for args in (('nope',), ('counts', '--again')):
            rc, out = self.piece(*args)
            self.assertEqual(rc, 99, out)
            self.assertIn('usage: run_all.sh', out)
        self.assertEqual(self.runs(), [])


if __name__ == '__main__':
    unittest.main()
