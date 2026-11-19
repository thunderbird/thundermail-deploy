import datetime
import hashlib
import importlib.util
import json
import pathlib
import random
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
TOOL = HERE / 'blob_compare.py'
LEGACY = 'mailstrom-prod-stalwart-s3-store'
TBPROD = 'mzla-tb-prod-thundermail-stalwart-blob-store'
UTC = datetime.timezone.utc

FAKE_AWS = r"""
import base64, fcntl, hashlib, json, os, sys

D = os.environ['FAKE_AWS_DIR']
LEGACY = 'mailstrom-prod-stalwart-s3-store'
TBPROD = 'mzla-tb-prod-thundermail-stalwart-blob-store'
WRITES = ('put-object', 'create-multipart-upload', 'upload-part', 'complete-multipart-upload',
          'abort-multipart-upload')


def parse(argv):
    opts, pos, i = {}, [], 0
    while i < len(argv):
        a = argv[i]
        if a.startswith('--') and '=' in a:
            k, v = a[2:].split('=', 1)
            opts[k] = v
        elif a in ('--profile', '--region', '--output', '--statistics'):
            i += 1
            opts[a[2:]] = [argv[i]]
        elif a == '--dimensions':
            vals = []
            while i + 1 < len(argv) and not argv[i + 1].startswith('--'):
                i += 1
                vals.append(argv[i])
            opts['dimensions'] = vals
        elif a.startswith('--'):
            opts[a[2:]] = True
        else:
            pos.append(a)
        i += 1
    return opts, pos


def fail(code, msg):
    sys.stderr.write('\nAn error occurred (%s) when calling the operation: %s\n' % (code, msg))
    sys.exit(254)


def etag_of(e):
    if 'etag' in e:
        return e['etag']
    body = e['body'].encode()
    if e.get('part'):
        p = e['part']
        ds = [hashlib.md5(body[i:i + p]).digest() for i in range(0, len(body), p)]
        return '%s-%d' % (hashlib.md5(b''.join(ds)).hexdigest(), len(ds))
    return hashlib.md5(body).hexdigest()


lockf = open(os.path.join(D, 'state.lock'), 'a')
fcntl.flock(lockf, fcntl.LOCK_EX)
state = json.load(open(os.path.join(D, 'state.json')))
opts, pos = parse(sys.argv[1:])
service, op = pos[0], pos[1]
profile = opts.get('profile', [''])[0]
with open(os.path.join(D, 'calls.jsonl'), 'a') as log:
    log.write(json.dumps({'profile': profile, 'service': service, 'op': op, 'opts': opts,
                          'pos': pos[2:]}) + '\n')
assert opts.get('region') == ['eu-central-1'] and opts.get('output') == ['json']


def save():
    json.dump(state, open(os.path.join(D, 'state.json'), 'w'))


def latest(bucket, key, version=None):
    for e in state['buckets'][bucket]:
        if e['key'] == key and (e['version'] == version if version else e['latest']):
            return e
    return None


if service == 'cloudwatch':
    assert profile == 'mzla-legacy-ro', profile
    assert op == 'get-metric-statistics'
    assert opts['namespace'] == 'AWS/S3'
    assert opts['dimensions'] == ['Name=SourceBucket,Value=' + LEGACY,
                                  'Name=DestinationBucket,Value=' + TBPROD,
                                  'Name=RuleId,Value=tb-prod-cutover'], opts['dimensions']
    from datetime import datetime
    def t(x):
        return datetime.fromisoformat(x.replace('Z', '+00:00'))
    s, e = t(opts['start-time']), t(opts['end-time'])
    stat = opts['statistics'][0]
    pts = [{'Timestamp': ts, stat: v} for ts, v in state['metrics'].get(opts['metric-name'], [])
           if s <= t(ts) < e]
    print(json.dumps({'Label': opts['metric-name'], 'Datapoints': pts}))
    sys.exit(0)

assert service == 's3api'
bucket = opts.get('bucket')
if op in WRITES:
    if bucket != TBPROD or profile != 'mzla-tb-prod':
        fail('AccessDenied', 'write %s %s with %s' % (op, bucket, profile))
elif bucket == LEGACY and profile != 'mzla-legacy-ro':
    fail('AccessDenied', 'legacy read with %s' % profile)
elif bucket == TBPROD and profile not in ('mzla-tb-prod-ro', 'mzla-tb-prod'):
    fail('AccessDenied', 'tb-prod read with %s' % profile)
if bucket in state.get('deny', []):
    fail('AccessDenied', 'Access Denied')

if op == 'list-object-versions':
    if state.get('throttle', 0) > 0:
        state['throttle'] -= 1
        save()
        fail('SlowDown', 'Please reduce your request rate.')
    es = sorted(state['buckets'][bucket], key=lambda e: e['key'])
    km, vm = opts.get('key-marker'), opts.get('version-id-marker')
    i = 0
    if km is not None and vm:
        i = [n for n, e in enumerate(es) if e['key'] == km and e['version'] == vm][0] + 1
    elif km is not None:
        i = len([e for e in es if e['key'] <= km])
    size = min(int(opts['max-keys']), state.get('page', 1000))
    page = es[i:i + size]
    out = {'IsTruncated': i + size < len(es), 'Versions': [], 'DeleteMarkers': []}
    for e in page:
        if e.get('dm'):
            out['DeleteMarkers'].append({'Key': e['key'], 'VersionId': e['version'],
                                         'IsLatest': e['latest']})
        else:
            out['Versions'].append({'Key': e['key'], 'VersionId': e['version'],
                                    'IsLatest': e['latest'], 'Size': len(e['body'].encode()),
                                    'ETag': '"%s"' % etag_of(e)})
    if out['IsTruncated']:
        out['NextKeyMarker'] = page[-1]['key']
        out['NextVersionIdMarker'] = page[-1]['version']
    print(json.dumps(out))
elif op == 'get-object':
    e = latest(bucket, opts['key'], opts.get('version-id'))
    body = e['body'].encode()
    if opts['key'] in state.get('corrupt', []):
        body = body[:-1] + b'!'
    open(pos[2], 'wb').write(body)
    print(json.dumps({'ContentLength': len(body), 'ETag': '"%s"' % etag_of(e),
                      'ContentType': 'application/octet-stream', 'Metadata': {}}))
elif op == 'head-object':
    e = latest(bucket, opts['key'], opts.get('version-id'))
    if e is None or e.get('dm'):
        fail('404', 'Not Found')
    n = len(e['body'].encode())
    if opts.get('part-number'):
        n = min(e.get('part') or n, n)
    print(json.dumps({'ContentLength': n, 'ETag': '"%s"' % etag_of(e)}))
elif op == 'put-object':
    body = open(opts['body'], 'rb').read()
    assert base64.b64encode(hashlib.md5(body).digest()).decode() == opts['content-md5']
    for e in state['buckets'][bucket]:
        if e['key'] == opts['key']:
            e['latest'] = False
    state['n'] = state.get('n', 0) + 1
    state['buckets'][bucket].insert(0, {'key': opts['key'], 'version': 'w%d' % state['n'],
                                        'latest': True, 'body': body.decode()})
    save()
    print(json.dumps({'ETag': '"%s"' % hashlib.md5(body).hexdigest()}))
elif op == 'create-multipart-upload':
    state['n'] = state.get('n', 0) + 1
    up = 'u%d' % state['n']
    state.setdefault('uploads', {})[up] = {'key': opts['key'], 'parts': {}}
    save()
    print(json.dumps({'UploadId': up}))
elif op == 'upload-part':
    body = open(opts['body'], 'rb').read()
    assert base64.b64encode(hashlib.md5(body).digest()).decode() == opts['content-md5']
    state['uploads'][opts['upload-id']]['parts'][opts['part-number']] = body.decode()
    save()
    print(json.dumps({'ETag': '"%s"' % hashlib.md5(body).hexdigest()}))
elif op == 'complete-multipart-upload':
    up = state['uploads'].pop(opts['upload-id'])
    parts = [up['parts'][str(p['PartNumber'])]
             for p in json.loads(opts['multipart-upload'])['Parts']]
    for e in state['buckets'][bucket]:
        if e['key'] == up['key']:
            e['latest'] = False
    state['n'] += 1
    state['buckets'][bucket].insert(0, {'key': up['key'], 'version': 'w%d' % state['n'],
                                        'latest': True, 'body': ''.join(parts),
                                        'part': len(parts[0].encode())})
    save()
    print(json.dumps({}))
elif op == 'abort-multipart-upload':
    state['uploads'].pop(opts['upload-id'], None)
    save()
    print(json.dumps({}))
else:
    fail('Unknown', op)
"""


def ztime(dt):
    return dt.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')


def obj(key, body, version='v1', latest=True, **kw):
    return dict(key=key, body=body, version=version, latest=latest, **kw)


def dm(key, version='d1', latest=True):
    return dict(key=key, body='', version=version, latest=latest, dm=True)


class Harness(unittest.TestCase):
    def setUp(self):
        self.t = pathlib.Path(tempfile.mkdtemp(prefix='blob-compare-test-'))
        self.addCleanup(shutil.rmtree, self.t, True)
        self.bin = self.t / 'bin'
        self.bin.mkdir()
        fake = self.bin / 'aws'
        fake.write_text('#!%s\n%s' % (sys.executable, FAKE_AWS))
        fake.chmod(0o755)
        self.fake = self.t / 'fake'
        self.fake.mkdir()
        self.ev = self.t / 'evidence'
        self.ev.mkdir()
        self.tsv = self.ev / 'timing-tb-prod.tsv'
        self.now = datetime.datetime.now(UTC).replace(microsecond=0)
        self.stop = self.now - datetime.timedelta(minutes=30)
        self.state = {'metrics': {}, 'buckets': {LEGACY: [], TBPROD: []}}

    def rows(self, *rows):
        self.tsv.write_text(''.join('\t'.join(r) + '\n' for r in rows))

    def cut(self, stop=True, wait=True):
        rows = [
            ('stop-legacy-stalwart', ztime(self.now - datetime.timedelta(days=2)), ''),
            ('cut-ecs', ztime(self.stop - datetime.timedelta(hours=1)), ''),
        ]
        if stop:
            rows.append(('stop-legacy-stalwart', ztime(self.stop), ztime(self.stop), '', ''))
        if wait:
            rows.append(
                ('blob-replication-wait', ztime(self.now), ztime(self.now), 'result', 'pass')
            )
        self.rows(*rows)

    def run_tool(self, *args, extra_env=None):
        (self.fake / 'state.json').write_text(json.dumps(self.state))
        env = {
            'PATH': str(self.bin),
            'HOME': str(self.t),
            'AWS_CONFIG_FILE': str(self.t / 'no-aws-config'),
            'AWS_SHARED_CREDENTIALS_FILE': str(self.t / 'no-aws-credentials'),
            'FAKE_AWS_DIR': str(self.fake),
            'TARGET_ENV': 'tb-prod',
            'EVIDENCE_DIR': str(self.ev),
            'TIMING_TSV': str(self.tsv),
        }
        env.update(extra_env or {})
        if (self.fake / 'calls.jsonl').exists():
            (self.fake / 'calls.jsonl').unlink()
        r = subprocess.run(
            [sys.executable, str(TOOL), *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )
        self.state = json.loads((self.fake / 'state.json').read_text())
        return r

    def calls(self):
        p = self.fake / 'calls.jsonl'
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def last(self, r):
        return r.stdout.strip().splitlines()[-1]


class Wait(Harness):
    def metric(self, name, *points):
        self.state['metrics'][name] = [[ts, v] for ts, v in points]

    def at(self, minutes, offset='+00:00'):
        dt = self.stop + datetime.timedelta(minutes=minutes)
        tz = datetime.timezone(datetime.timedelta(hours=-8)) if offset == '-08:00' else UTC
        return dt.astimezone(tz).isoformat()

    def drained(self, offset='+00:00'):
        for m in ('OperationsPendingReplication', 'BytesPendingReplication'):
            self.metric(m, *[(self.at(n, offset), 0.0) for n in (-5, 2, 3, 4)])

    def since(self, minutes=-60):
        t = self.stop + datetime.timedelta(minutes=minutes)
        return ztime(t.replace(minute=t.minute - t.minute % 5, second=0))

    def test_apply_refuses_without_stop_row_after_last_cut(self):
        self.cut(stop=False, wait=False)
        self.drained()
        r = self.run_tool('wait', '--apply', extra_env={'REPL_FAILED_SINCE': self.since()})
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('STOP  no stop-legacy-stalwart row after the last cut-ecs row', r.stdout)
        self.assertEqual(self.calls(), [])
        self.assertTrue(self.last(r).startswith('blob-wait mode=apply result=stop stop=none'))

    def test_pass_prints_utc_and_writes_row_on_apply(self):
        self.cut(wait=False)
        self.drained(offset='-08:00')
        since = self.since()
        r = self.run_tool('wait', extra_env={'REPL_FAILED_SINCE': since})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        first = ztime(self.stop + datetime.timedelta(minutes=2))
        self.assertIn(
            'OperationsPendingReplication after %s: %s=0.0 ' % (ztime(self.stop), first), r.stdout
        )
        self.assertNotIn('-08:00', r.stdout)
        self.assertIn(
            'OperationsFailedReplication since %s: no datapoint (idle rule), sum=0.0 pass' % since,
            r.stdout,
        )
        self.assertIn('PASS  replication wait:', r.stdout)
        self.assertNotIn('blob-replication-wait', self.tsv.read_text())
        r = self.run_tool('wait', '--apply', extra_env={'REPL_FAILED_SINCE': since})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        row = self.tsv.read_text().splitlines()[-1].split('\t')
        self.assertEqual((row[0], row[3], row[4]), ('blob-replication-wait', 'result', 'pass'))
        self.assertEqual(
            self.last(r),
            'blob-wait mode=apply result=pass stop=%s failed_since=%s ops=pass bytes=pass '
            'failed=pass polls=1' % (ztime(self.stop), since),
        )
        starts = {c['opts']['metric-name']: c['opts']['start-time'] for c in self.calls()}
        self.assertEqual(starts['OperationsFailedReplication'], since)

    def test_pending_zero_only_before_stop_is_not_passed(self):
        self.cut(wait=False)
        self.metric('OperationsPendingReplication', *[(self.at(n), 0.0) for n in (-3, -2, -1, 0)])
        self.metric('BytesPendingReplication', *[(self.at(n), 0.0) for n in (1, 2, 3)])
        r = self.run_tool('wait', extra_env={'REPL_FAILED_SINCE': self.since()})
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn(
            'OperationsPendingReplication after %s: no datapoint later than %s not passed'
            % (ztime(self.stop), ztime(self.stop)),
            r.stdout,
        )
        self.assertIn('BytesPendingReplication after', r.stdout)
        self.assertIn(' pass\n', r.stdout)

    def test_pending_nonzero_in_last_three_is_not_passed(self):
        self.cut(wait=False)
        self.drained()
        self.metric(
            'BytesPendingReplication', *[(self.at(n), v) for n, v in ((2, 0.0), (3, 5.0), (4, 0.0))]
        )
        r = self.run_tool('wait', extra_env={'REPL_FAILED_SINCE': self.since()})
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertRegex(r.stdout, r'BytesPendingReplication after \S+: .*=5\.0 .* not passed')

    def test_failed_sum_counts_only_since_the_operator_time(self):
        self.cut(wait=False)
        self.drained()
        self.metric('OperationsFailedReplication', (self.at(-50), 2.0), (self.at(-20), 0.0))
        r = self.run_tool('wait', extra_env={'REPL_FAILED_SINCE': self.since(-60)})
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn('datapoints=2 sum=2.0 not passed', r.stdout)
        self.assertIn('run replication.py batch again', r.stdout)
        r = self.run_tool('wait', extra_env={'REPL_FAILED_SINCE': self.since(-40)})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('datapoints=1 sum=0.0 pass', r.stdout)

    def test_apply_times_out_with_a_stop(self):
        self.cut(wait=False)
        r = self.run_tool(
            'wait',
            '--apply',
            '--timeout=0',
            '--interval=0',
            extra_env={'REPL_FAILED_SINCE': self.since()},
        )
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('STOP  replication wait: not passed after 0 s (--timeout)', r.stdout)
        self.assertNotIn('blob-replication-wait', self.tsv.read_text())

    def test_since_default_operator_file_environment_and_bad_value(self):
        self.cut(wait=False)
        r = self.run_tool('wait')
        self.assertIn('failed_since=2026-11-19T02:48:00Z', self.last(r))
        self.assertIn(
            'failed operations counted since 2026-11-19T02:48:00Z, REPL_FAILED_SINCE from the '
            'default (rule applied); from the 5-minute period that starts 2026-11-19T02:45:00Z',
            r.stdout,
        )
        op = self.t / '.local' / 'state' / 'talos'
        op.mkdir(parents=True)
        fixed = self.since(-30)
        (op / 'cutover-operator-tb-prod.env').write_text(
            'TARGET_ENV=tb-prod\nREPL_FAILED_SINCE=2026-11-19T01:00:00Z\nREPL_FAILED_SINCE=%s\n'
            % fixed
        )
        self.drained()
        self.metric('OperationsFailedReplication', (self.at(-50), 3.0))
        r = self.run_tool('wait')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('failed_since=%s' % fixed, self.last(r))
        self.assertIn('REPL_FAILED_SINCE from %s' % (op / 'cutover-operator-tb-prod.env'), r.stdout)
        r = self.run_tool('wait', extra_env={'REPL_FAILED_SINCE': self.since(-60)})
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn('REPL_FAILED_SINCE from the environment', r.stdout)
        self.assertIn('datapoints=1 sum=3.0 not passed', r.stdout)
        r = self.run_tool('wait', extra_env={'REPL_FAILED_SINCE': '2026-11-19 02:45'})
        self.assertEqual(r.returncode, 64)

    def test_since_inside_a_period_counts_the_whole_period(self):
        self.cut(wait=False)
        self.drained()
        five = self.since(-60)
        mid = ztime(
            datetime.datetime.strptime(five, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=UTC)
            + datetime.timedelta(minutes=3)
        )
        self.metric('OperationsFailedReplication', (five.replace('Z', '+00:00'), 1.0))
        r = self.run_tool('wait', extra_env={'REPL_FAILED_SINCE': mid})
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn(
            'OperationsFailedReplication since %s: datapoints=1 sum=1.0 not passed' % mid, r.stdout
        )
        starts = [
            c['opts']['start-time']
            for c in self.calls()
            if c['opts']['metric-name'] == 'OperationsFailedReplication'
        ]
        self.assertEqual(starts, [five])


def tool():
    spec = importlib.util.spec_from_file_location('blob_compare', TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Shards(unittest.TestCase):
    def load(self):
        return tool()

    def test_every_key_falls_in_exactly_one_shard(self):
        m = self.load()
        rng = random.Random(7)
        chars = m.STALWART_ALPHABET + 'ABCZ_-./~é'
        keys = {''.join(rng.choice(chars) for _ in range(rng.randint(0, 6))) for _ in range(4000)}
        for shards in (1, 2, 7, 128, 1000):
            bounds = m.boundaries(m.STALWART_ALPHABET, shards)
            self.assertEqual(bounds, sorted(set(bounds)))
            ranges = m.shard_ranges(bounds)
            self.assertEqual(len(ranges), shards)
            for k in keys | set(bounds) | set(m.NAMED):
                hits = [i for i, (lo, up) in enumerate(ranges) if m.in_shard(k, lo, up)]
                self.assertEqual(len(hits), 1, (k, shards, hits))


class Compare(Harness):
    def setUp(self):
        super().setUp()
        self.state['page'] = 2
        self.out = self.t / 'out'

    def fill(self):
        L, T = self.state['buckets'][LEGACY], self.state['buckets'][TBPROD]
        L += [
            obj('STALWART_WEBADMIN', 'webadmin-015'),
            obj('SPAM_TRAINER_KEY', 'trainer'),
            obj('SPAM_CLASSIFIER_KEY', 'classifier-new', version='v2'),
            obj('SPAM_CLASSIFIER_KEY', 'classifier-old', latest=False),
            obj('0abc', 'same'),
            obj('a', 'boundary key'),
            obj('aaaa', 'gone in tb'),
            obj('bbbb', 'size legacy'),
            obj('cccc', 'etag-a'),
            dm('dddd', version='d2'),
            obj('dddd', 'legacy deleted', latest=False),
            obj('zzzz', 'tail'),
            obj('~tail', 'after alphabet'),
        ]
        T += [
            obj('STALWART_WEBADMIN', 'webadmin-016'),
            obj('SPAM_TRAINER_KEY', 'trainer'),
            obj('SPAM_CLASSIFIER_KEY', 'classifier-old'),
            obj('0abc', 'same'),
            obj('a', 'boundary key'),
            dm('aaaa', version='d9'),
            obj('aaaa', 'gone in tb', latest=False),
            obj('bbbb', 'size legacy plus'),
            obj('cccc', 'etag-b'),
            obj('dddd', 'legacy deleted'),
            obj('only-tb', 'extra'),
            obj('zzzz', 'tail'),
        ]

    def compare(self, *args, extra_env=None):
        return self.run_tool(
            'compare',
            '--out',
            str(self.out),
            '--shards=4',
            '--workers=3',
            '--backoff=0',
            *args,
            extra_env=extra_env,
        )

    def lists(self):
        return [c for c in self.calls() if c['op'] == 'list-object-versions']

    def assert_no_forbidden_writes(self):
        for c in self.calls():
            if c['op'] in (
                'put-object',
                'create-multipart-upload',
                'upload-part',
                'complete-multipart-upload',
                'abort-multipart-upload',
            ):
                self.assertEqual((c['opts']['bucket'], c['profile']), (TBPROD, 'mzla-tb-prod'))
            self.assertNotIn('delete', c['op'].replace('delete-marker', ''))
            if c['opts'].get('bucket') == LEGACY:
                self.assertEqual(c['profile'], 'mzla-legacy-ro')
                self.assertIn(c['op'], ('list-object-versions', 'get-object', 'head-object'))

    def test_dry_run_prints_plan_and_copies_nothing(self):
        self.cut()
        self.fill()
        r = self.compare()
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        out = r.stdout
        self.assertIn('legacy  s3://%s read with mzla-legacy-ro, never written' % LEGACY, out)
        self.assertIn(
            'tb-prod s3://%s read with mzla-tb-prod-ro, written with mzla-tb-prod' % TBPROD, out
        )
        self.assertIn('shards=4 workers=3 boundaries: c k s', out)
        self.assertIn('list legacy shard 0000 (start, c]', out)
        self.assertIn('list tb-prod shard 0003 (s, end]', out)
        self.assertIn(
            'plan: 6 difference(s) to copy from legacy (missing=2 size=1 checksum=3); '
            'only in tb-prod=2 (reported, not deleted)',
            out,
        )
        self.assertIn('dry run: nothing copied', out)
        self.assertIn(
            'named SPAM_TRAINER_KEY legacy=%s/7 tb-prod=%s/7 match'
            % ((hashlib.md5(b'trainer').hexdigest(),) * 2),
            out,
        )
        self.assertRegex(out, r'named STALWART_WEBADMIN legacy=\w+/12 tb-prod=\w+/12 differ')
        self.assertRegex(out, r'named SPAM_CLASSIFIER_KEY legacy=\w+/14 tb-prod=\w+/14 differ')
        self.assertIn('NEED  6 difference(s) left: run compare --apply', out)
        self.assertEqual(
            self.last(r),
            'blob-compare mode=dry-run result=need listed_legacy=10 listed_tb_prod=10 equal=4 '
            'missing=2 size_diff=1 checksum_diff=3 only_tb_prod=2 copied=0 failed=0 left=6 '
            'retries=0',
        )
        self.assertFalse([c for c in self.calls() if c['op'] not in ('list-object-versions',)])
        run = [p for p in self.out.iterdir() if p.name.startswith('run-')][0]
        missing = [json.loads(x) for x in (run / 'missing.jsonl').read_text().splitlines()]
        self.assertEqual(
            [(d['key'], d['reason']) for d in missing],
            [('aaaa', 'delete-marker'), ('~tail', 'absent')],
        )
        only = sorted(
            json.loads(x)['key'] for x in (run / 'only-tb-prod.jsonl').read_text().splitlines()
        )
        self.assertEqual(only, ['dddd', 'only-tb'])
        checks = sorted(
            json.loads(x)['key'] for x in (run / 'checksum.jsonl').read_text().splitlines()
        )
        self.assertEqual(checks, ['SPAM_CLASSIFIER_KEY', 'STALWART_WEBADMIN', 'cccc'])
        sizes = [json.loads(x) for x in (run / 'size.jsonl').read_text().splitlines()]
        self.assertEqual(
            [(d['key'], d['size'], d['tb_prod_size']) for d in sizes], [('bbbb', 11, 16)]
        )

    def test_apply_copies_differences_then_rerun_passes(self):
        self.cut()
        self.fill()
        r = self.compare('--apply')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertRegex(r.stdout, r'named STALWART_WEBADMIN legacy=(\S+) tb-prod=\1 copied, match')
        self.assertIn('PASS  blob compare: 0 differences left', r.stdout)
        self.assertTrue(self.last(r).startswith('blob-compare mode=apply result=pass '))
        self.assertIn(' copied=6 failed=0 left=0 ', self.last(r) + ' ')
        self.assert_no_forbidden_writes()
        tb = {e['key']: e for e in self.state['buckets'][TBPROD] if e['latest']}
        for e in self.state['buckets'][LEGACY]:
            if e['latest'] and not e.get('dm'):
                self.assertEqual(tb[e['key']]['body'], e['body'], e['key'])
        self.assertIn('only-tb', tb)
        self.assertEqual(tb['dddd']['body'], 'legacy deleted')
        self.assertEqual(len([e for e in self.state['buckets'][TBPROD] if e['key'] == 'cccc']), 2)
        row = self.tsv.read_text().splitlines()[-1].split('\t')
        self.assertEqual((row[0], row[3], row[4]), ('blob-compare', 'left', '0'))
        r = self.compare()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(
            ' missing=0 size_diff=0 checksum_diff=0 only_tb_prod=2 copied=0 failed=0 left=0',
            self.last(r),
        )
        self.assertFalse([c for c in self.lists() if c['opts']['bucket'] == LEGACY])
        self.assertTrue([c for c in self.lists() if c['opts']['bucket'] == TBPROD])
        self.assertIn('reused', r.stdout)

    def test_coverage_with_one_key_per_page(self):
        self.cut()
        self.state['page'] = 1
        keys = ['S', '9', 'c', 'c0', 'k', 'k0', 's', 'sz', 'zz', '~', 'été', *tool().NAMED]
        self.state['buckets'][LEGACY] = [obj(k, 'x' + k) for k in keys]
        self.state['buckets'][TBPROD] = [obj(k, 'x' + k) for k in keys]
        r = self.compare()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('listed_legacy=14 listed_tb_prod=14 equal=14 missing=0', self.last(r))
        markers = sorted(
            c['opts'].get('key-marker', '') for c in self.lists() if c['opts']['bucket'] == LEGACY
        )
        self.assertLessEqual({'', 'c', 'k', 's'}, set(markers))

    def test_the_three_named_keys_must_match(self):
        self.cut()
        self.state['buckets'][LEGACY] = [obj('k1', 'one'), obj('SPAM_TRAINER_KEY', 't')]
        self.state['buckets'][TBPROD] = [obj('k1', 'one'), obj('SPAM_TRAINER_KEY', 't')]
        r = self.compare()
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn('named STALWART_WEBADMIN absent in both buckets', r.stdout)
        self.assertIn('NEED  named key(s) not matching by checksum in both buckets: ', r.stdout)
        r = self.compare('--apply')
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('STOP  named key(s) not matching by checksum in both buckets: ', r.stdout)
        self.assertNotIn(
            'blob-compare', [x.split('\t')[0] for x in self.tsv.read_text().splitlines()]
        )

    def test_reads_are_limited_to_read_verbs(self):
        m = tool()
        aws = m.Aws(0, 0)
        with self.assertRaises(m.Stop) as cm:
            aws.read(m.LEGACY_RO, 's3api', 'put-object', '--bucket=' + LEGACY)
        self.assertIn('refusing aws s3api put-object as a read', str(cm.exception))

    def test_multipart_etag_is_reproduced(self):
        self.cut()
        body = 'abcdefghij' * 3 + 'xyz'
        m = tool()
        named = [obj(k, 'n' + k) for k in m.NAMED]
        self.state['buckets'][LEGACY] = [obj('mpart', body, part=10), *named]
        self.state['buckets'][TBPROD] = [obj(k, 'n' + k) for k in m.NAMED]
        r = self.compare('--apply')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        tb = [e for e in self.state['buckets'][TBPROD] if e['latest'] and e['key'] == 'mpart'][0]
        self.assertEqual((tb['body'], tb['part']), (body, 10))
        parts = [c for c in self.calls() if c['op'] == 'upload-part']
        self.assertEqual(len(parts), 4)
        self.assert_no_forbidden_writes()

    def test_corrupt_download_fails_and_stops(self):
        self.cut()
        self.state['buckets'][LEGACY] = [obj('k1', 'body one'), obj('k2', 'body two')]
        self.state['corrupt'] = ['k2']
        r = self.compare('--apply')
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('STOP  1 copy(ies) failed', r.stdout)
        self.assertIn(
            ' missing=2 size_diff=0 checksum_diff=0 only_tb_prod=0 copied=1 failed=1 left=1',
            self.last(r),
        )
        self.assertFalse(
            [c for c in self.calls() if c['op'] == 'put-object' and c['opts']['key'] == 'k2']
        )

    def test_throttling_is_retried_access_denied_is_not(self):
        self.cut()
        self.fill()
        self.state['throttle'] = 3
        r = self.compare()
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertTrue(self.last(r).endswith(' left=6 retries=3'))
        self.state['deny'] = [TBPROD]
        r = self.compare('--relist')
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('STOP  ', r.stdout)
        self.assertIn('AccessDenied', r.stdout)
        self.assertTrue(self.last(r).startswith('blob-compare mode=dry-run result=stop '))
        self.assertTrue(self.last(r).endswith(' retries=0'))

    def test_rerun_relists_only_incomplete_shards(self):
        self.cut()
        self.fill()
        self.compare()
        (self.out / 'list' / 'legacy' / 'shard-0002.done').unlink()
        r = self.compare()
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        listed = {(c['opts']['bucket'], c['opts'].get('key-marker')) for c in self.lists()}
        self.assertEqual({b for b, _ in listed}, {LEGACY})
        self.assertIn((LEGACY, 'k'), listed)
        self.assertIn('list legacy shard 0002 (k, s]', r.stdout)
        self.assertEqual(r.stdout.count(' reused'), 7)

    def test_apply_lists_tb_prod_again_after_a_dry_run(self):
        self.cut()
        m = tool()
        named = [obj(k, 'n' + k) for k in m.NAMED]
        self.state['buckets'][LEGACY] = [obj('k1', 'one'), *named]
        self.state['buckets'][TBPROD] = [obj('k1', 'one'), *[obj(k, 'n' + k) for k in m.NAMED]]
        r = self.compare()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.state['buckets'][TBPROD] = [
            obj('k1', 'one'),
            obj('STALWART_WEBADMIN', 'changed-after-the-dry-run'),
            *[obj(k, 'n' + k) for k in m.NAMED if k != 'STALWART_WEBADMIN'],
        ]
        r = self.compare('--apply')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(TBPROD, {c['opts']['bucket'] for c in self.lists()})
        self.assertRegex(r.stdout, r'named STALWART_WEBADMIN legacy=(\S+) tb-prod=\1 copied, match')

    def test_a_run_leaves_other_runs_temporary_files(self):
        self.cut()
        self.fill()
        base = self.t / 'secret' / 'blob-compare-tmp'
        (base / 'run-other').mkdir(parents=True)
        (base / 'run-other' / 'part').write_text('in use')
        self.compare(extra_env={'SECRET_DIR': str(self.t / 'secret')})
        self.assertEqual((base / 'run-other' / 'part').read_text(), 'in use')
        self.assertEqual(sorted(x.name for x in base.iterdir()), ['run-other'])

    def test_new_stop_row_invalidates_the_listing(self):
        self.cut()
        self.fill()
        self.compare()
        self.stop = self.stop + datetime.timedelta(minutes=5)
        self.cut()
        r = self.compare()
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertEqual(len(self.lists()), 18)
        self.assertEqual(r.stdout.count('\nlist '), 8)
        self.assertNotIn(' reused', r.stdout)

    def test_apply_refuses_without_stop_row_or_wait_row(self):
        self.fill()
        self.cut(stop=False, wait=False)
        r = self.compare('--apply')
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('STOP  no stop-legacy-stalwart row after the last cut-ecs row', r.stdout)
        self.assertEqual(self.calls(), [])
        self.assertTrue(
            self.last(r).startswith('blob-compare mode=apply result=stop listed_legacy=0')
        )
        r = self.compare()
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn('NEED  no stop-legacy-stalwart row', r.stdout)
        self.assertIn('plan: 6 difference(s)', r.stdout)
        self.cut(wait=False)
        r = self.compare('--apply')
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('STOP  no passing blob-replication-wait row', r.stdout)
        self.assertEqual(self.calls(), [])
        r = self.compare('--apply', '--skip-wait-check')
        self.assertEqual(r.returncode, 64, r.stdout + r.stderr)
        self.assertIn('--skip-wait-check is for a dry run only', r.stderr)
        r = self.compare('--skip-wait-check')
        self.assertNotIn('no passing blob-replication-wait row', r.stdout)

    def test_usage_errors_exit_64(self):
        self.cut()
        r = self.compare('--shards=0')
        self.assertEqual(r.returncode, 64)
        r = self.run_tool('compare', '--bogus')
        self.assertEqual(r.returncode, 64)
        r = self.run_tool('compare', extra_env={'TARGET_ENV': 'tb-stage'})
        self.assertEqual(r.returncode, 64)


if __name__ == '__main__':
    unittest.main()
