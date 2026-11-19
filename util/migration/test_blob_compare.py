import datetime
import hashlib
import hmac
import http.server
import importlib.util
import json
import pathlib
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
from xml.sax.saxutils import escape

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
        elif a in ('--profile', '--region', '--output', '--statistics', '--format'):
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
assert opts.get('region') == ['eu-central-1'] and opts.get('output') == ['json']


def save():
    json.dump(state, open(os.path.join(D, 'state.json'), 'w'))


if (service, op) == ('configure', 'export-credentials'):
    from datetime import datetime, timedelta, timezone
    assert opts.get('format') == ['process'] and pos[2:] == [], (opts, pos)
    n = state.setdefault('creds', {}).get(profile, 0) + 1
    state['creds'][profile] = n
    save()
    with open(os.path.join(D, 'creds.jsonl'), 'a') as log:
        log.write(json.dumps({'profile': profile, 'n': n}) + '\n')
    tag = '%s-%d' % (profile, n)
    exp = datetime.now(timezone.utc) + timedelta(seconds=state.get('expires_in', 3600))
    print(json.dumps({'Version': 1, 'AccessKeyId': 'AKID-' + tag,
                      'SecretAccessKey': 'SECRET-' + tag, 'SessionToken': 'TOKEN-' + tag,
                      'Expiration': exp.isoformat()}))
    sys.exit(0)

with open(os.path.join(D, 'calls.jsonl'), 'a') as log:
    log.write(json.dumps({'profile': profile, 'service': service, 'op': op, 'opts': opts,
                          'pos': pos[2:]}) + '\n')


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


NS = 'http://s3.amazonaws.com/doc/2006-03-01/'
REGION = 'eu-central-1'


def quote(text):
    return urllib.parse.quote(text, safe='-_.~')


def hmac256(key, text):
    return hmac.new(key, text.encode(), hashlib.sha256)


class FakeS3(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def setup(self):
        super().setup()
        with self.server.lock:
            self.server.conns += 1
            self.conn = self.server.conns

    def log_message(self, *args):
        pass

    def reply(self, status, body, headers=()):
        data = body.encode()
        self.send_response(status)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header('Content-Type', 'application/xml')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def error(self, status, code, message, headers=()):
        auth = self.headers.get('Authorization') or ''
        self.reply(
            status,
            '<?xml version="1.0" encoding="UTF-8"?>\n<Error><Code>%s</Code><Message>%s</Message>'
            '<AWSAccessKeyId>%s</AWSAccessKeyId><SignatureProvided>%s</SignatureProvided>'
            '<RequestId>FAKE</RequestId></Error>'
            % (code, escape(message), escape(auth), escape(auth.rpartition('=')[2])),
            headers,
        )

    def signer(self, path, query):
        m = re.fullmatch(
            r'AWS4-HMAC-SHA256 Credential=([^/]+)/(\d{8})/eu-central-1/s3/aws4_request, '
            r'SignedHeaders=([a-z0-9;-]+), Signature=([0-9a-f]{64})',
            self.headers.get('Authorization') or '',
        )
        if not m:
            return None
        key, day, signed, signature = m.groups()
        names = signed.split(';')
        need = {'host', 'x-amz-date', 'x-amz-content-sha256', 'x-amz-security-token'}
        if not need <= set(names) or not key.startswith('AKID-'):
            return None
        port = self.server.server_address[1]
        if self.headers['Host'] not in ('127.0.0.1:%d' % port, 'localhost:%d' % port):
            return None
        if self.headers['x-amz-security-token'] != 'TOKEN-' + key[5:]:
            return None
        pairs = urllib.parse.parse_qsl(query, keep_blank_values=True)
        cq = '&'.join('%s=%s' % kv for kv in sorted((quote(k), quote(v)) for k, v in pairs))
        canonical = '\n'.join(
            [
                'GET',
                path,
                cq,
                ''.join('%s:%s\n' % (n, self.headers[n].strip()) for n in names),
                signed,
                self.headers['x-amz-content-sha256'],
            ]
        )
        amz = self.headers['x-amz-date']
        text = '\n'.join(
            [
                'AWS4-HMAC-SHA256',
                amz,
                '%s/%s/s3/aws4_request' % (day, REGION),
                hashlib.sha256(canonical.encode()).hexdigest(),
            ]
        )
        k = ('AWS4SECRET-' + key[5:]).encode()
        for part in (day, REGION, 's3', 'aws4_request'):
            k = hmac256(k, part).digest()
        if amz[:8] != day or not hmac.compare_digest(hmac256(k, text).hexdigest(), signature):
            return None
        with self.server.lock:
            self.server.signatures.add(signature)
        return key[5:].rsplit('-', 1)

    def do_GET(self):
        srv = self.server
        path, _, query = self.path.partition('?')
        q = dict(urllib.parse.parse_qsl(query, keep_blank_values=True))
        with srv.lock:
            srv.requests.append(
                {
                    'conn': self.conn,
                    'path': path,
                    'query': q,
                    'signed': (self.headers.get('Authorization') or '').startswith(
                        'AWS4-HMAC-SHA256 Credential='
                    ),
                }
            )
            fail = srv.knobs['fail'].pop(0) if srv.knobs.get('fail') else None
        if fail == 'drop':
            self.close_connection = True
            return
        if fail:
            return self.error(*fail)
        who = self.signer(path, query)
        if who is None:
            return self.error(403, 'SignatureDoesNotMatch', 'The request signature does not match')
        profile, gen = who
        if srv.knobs.get('expire_first') and gen == '1':
            return self.error(400, 'ExpiredToken', 'The provided token has expired.')
        if srv.knobs.get('redirect'):
            return self.error(
                301,
                'PermanentRedirect',
                'The bucket you are attempting to access must be addressed using the specified '
                'endpoint.',
                [('Location', 'http://127.0.0.1:%d/elsewhere/' % srv.server_address[1])],
            )
        if 'versions' not in q or not re.fullmatch(r'/[a-z0-9-]+/', path):
            return self.error(400, 'InvalidRequest', 'not a ListObjectVersions request')
        args = ['--bucket=' + path.strip('/'), '--max-keys=' + q['max-keys']]
        if 'key-marker' in q:
            args.append('--key-marker=' + q['key-marker'])
        if 'version-id-marker' in q:
            args.append('--version-id-marker=' + q['version-id-marker'])
        r = subprocess.run(
            [
                sys.executable,
                str(srv.stub),
                's3api',
                'list-object-versions',
                *args,
                '--profile',
                profile,
                '--region',
                REGION,
                '--output',
                'json',
                '--fake-conn=%d' % self.conn,
            ],
            capture_output=True,
            text=True,
            env={'FAKE_AWS_DIR': str(srv.fake)},
        )
        if r.returncode:
            code, msg = re.search(r'\((\w+)\) when calling the operation: (.*)', r.stderr).groups()
            return self.error({'SlowDown': 503, 'AccessDenied': 403}.get(code, 400), code, msg)
        self.reply(200, xml_page(json.loads(r.stdout), q.get('encoding-type') == 'url'))


def xml_page(page, url):
    def name(key):
        return escape(urllib.parse.quote_plus(key) if url else key)

    out = ['<?xml version="1.0" encoding="UTF-8"?>\n<ListVersionsResult xmlns="%s">' % NS]
    out.append('<IsTruncated>%s</IsTruncated>' % str(page['IsTruncated']).lower())
    if url:
        out.append('<EncodingType>url</EncodingType>')
    if page.get('NextKeyMarker') is not None:
        out.append('<NextKeyMarker>%s</NextKeyMarker>' % name(page['NextKeyMarker']))
        out.append('<NextVersionIdMarker>%s</NextVersionIdMarker>' % page['NextVersionIdMarker'])
    for v in page['Versions']:
        out.append(
            '<Version><Key>%s</Key><VersionId>%s</VersionId><IsLatest>%s</IsLatest>'
            '<LastModified>2026-11-18T00:00:00.000Z</LastModified><ETag>%s</ETag>'
            '<Size>%d</Size><StorageClass>STANDARD</StorageClass></Version>'
            % (
                name(v['Key']),
                v['VersionId'],
                str(v['IsLatest']).lower(),
                escape(v['ETag'], {'"': '&quot;'}),
                v['Size'],
            )
        )
    for d in page['DeleteMarkers']:
        out.append(
            '<DeleteMarker><Key>%s</Key><VersionId>%s</VersionId><IsLatest>%s</IsLatest>'
            '<LastModified>2026-11-18T00:00:00.000Z</LastModified></DeleteMarker>'
            % (name(d['Key']), d['VersionId'], str(d['IsLatest']).lower())
        )
    out.append('</ListVersionsResult>')
    return ''.join(out)


def fake_s3(test):
    srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), FakeS3)
    srv.daemon_threads = True
    srv.lock = threading.Lock()
    srv.conns = 0
    srv.requests = []
    srv.signatures = set()
    srv.knobs = {}
    srv.stub = test.bin / 'aws'
    srv.fake = test.fake
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    test.addCleanup(srv.server_close)
    test.addCleanup(srv.shutdown)
    return srv


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
        self.env = {}

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
        env.update(self.env)
        env.update(extra_env or {})
        for name in ('calls.jsonl', 'creds.jsonl'):
            (self.fake / name).unlink(missing_ok=True)
        r = subprocess.run(
            [sys.executable, str(TOOL), *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )
        self.state = json.loads((self.fake / 'state.json').read_text())
        return r

    def calls(self, name='calls.jsonl'):
        p = self.fake / name
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def creds(self):
        return [c['profile'] for c in self.calls('creds.jsonl')]

    def last(self, r):
        return r.stdout.strip().splitlines()[-1]

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


class Wait(Harness):
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
        self.s3 = fake_s3(self)
        self.endpoint = 'http://127.0.0.1:%d' % self.s3.server_address[1]
        self.env.update(BLOB_S3_ENDPOINT=self.endpoint, BLOB_TEST='1')

    def run_tool(self, *args, extra_env=None):
        with self.s3.lock:
            self.s3.requests.clear()
        r = super().run_tool(*args, extra_env=extra_env)
        m = re.search(r' retries=(\d+)$', r.stdout.strip())
        r.retries = int(m[1]) if m else None
        return r

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
        r = self.compare(
            '--apply',
            '--timeout=0',
            '--interval=0',
            extra_env={'REPL_FAILED_SINCE': self.since()},
        )
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('STOP  replication wait: not passed after 0 s (--timeout)', r.stdout)
        self.assertEqual(self.lists(), [])
        self.assertEqual(self.s3.requests, [])
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

    def fill_more(self):
        self.fill()
        for b in (LEGACY, TBPROD):
            self.state['buckets'][b] += [
                obj('dir/a b+c', 'spaced'),
                obj('k%2Fx', 'percent'),
                obj('été', 'accent'),
            ]

    def shard_entries(self):
        return {
            '%s/%d' % (side, i): [
                json.loads(x)
                for x in (self.out / 'list' / side / ('shard-%04d.jsonl' % i))
                .read_text()
                .splitlines()
            ]
            for side in ('legacy', 'tb-prod')
            for i in range(4)
        }

    def assert_cli_plan(self, r):
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn(CLI_PLAN, r.stdout)
        self.assertEqual(self.last(r), CLI_SUMMARY + '%d' % r.retries)
        self.assertEqual(self.shard_entries(), CLI_SHARDS)

    def secrets_absent(self, r):
        text = r.stdout + r.stderr
        for word in ('SECRET-', 'TOKEN-', 'AKID-', 'Signature=', 'AWS4-HMAC-SHA256'):
            self.assertNotIn(word, text)
        for sig in self.s3.signatures:
            self.assertNotIn(sig, text)

    def test_listing_uses_one_connection_per_shard_and_matches_the_cli(self):
        self.cut()
        self.fill_more()
        r = self.compare()
        self.assert_cli_plan(r)
        self.assertEqual(r.retries, 0)
        reqs = self.s3.requests
        self.assertEqual(len(reqs), len(self.lists()))
        self.assertTrue(all(q['signed'] for q in reqs))
        by_conn = {}
        for q in reqs:
            by_conn.setdefault(q['conn'], set()).add(q['path'])
        self.assertTrue(all(len(paths) == 1 for paths in by_conn.values()), by_conn)
        self.assertEqual(len(by_conn), 8)
        self.assertGreater(len(reqs), len(by_conn))
        self.assertEqual(
            sorted(c['opts']['fake-conn'] for c in self.lists()),
            sorted(str(q['conn']) for q in reqs),
        )
        self.assertTrue(
            all(q['query']['max-keys'] == '1000' and q['query']['versions'] == '' for q in reqs)
        )
        self.assertEqual(sorted(self.creds()), ['mzla-legacy-ro', 'mzla-tb-prod-ro'])
        for side in ('legacy', 'tb-prod'):
            n = len(
                [q for q in reqs if q['path'] == '/%s/' % (LEGACY if side == 'legacy' else TBPROD)]
            )
            self.assertRegex(
                r.stdout,
                r'\nlisted %s pages=%d versions=\d+ in \d+\.\ds '
                r'\(3 workers, one connection per shard\)\n' % (side, n),
            )
        self.assertIn('\nlisted legacy pages=11 versions=21 in ', r.stdout)
        self.assertIn('\nlisted tb-prod pages=10 versions=20 in ', r.stdout)
        r = self.compare()
        self.assertEqual(self.s3.requests, [])
        self.assertIn('\nlisted legacy pages=0 versions=0 in ', r.stdout)
        self.secrets_absent(r)

    def test_expired_token_fetches_credentials_again(self):
        self.cut()
        self.fill_more()
        self.s3.knobs['expire_first'] = True
        r = self.compare('--workers=1')
        self.assert_cli_plan(r)
        self.assertEqual(r.retries, 2)
        self.assertEqual(
            self.creds(), ['mzla-legacy-ro', 'mzla-legacy-ro', 'mzla-tb-prod-ro', 'mzla-tb-prod-ro']
        )
        self.secrets_absent(r)

    def test_credentials_close_to_expiry_are_fetched_again(self):
        self.cut()
        self.fill_more()
        self.state['expires_in'] = 240
        r = self.compare('--workers=1')
        self.assert_cli_plan(r)
        legacy = len([q for q in self.s3.requests if q['path'] == '/%s/' % LEGACY])
        self.assertGreater(legacy, 1)
        self.assertEqual(self.creds().count('mzla-legacy-ro'), legacy)
        self.state['expires_in'] = 420
        r = self.compare('--workers=1', '--relist')
        self.assertEqual(sorted(self.creds()), ['mzla-legacy-ro', 'mzla-tb-prod-ro'])

    def test_access_denied_stops_with_the_code_and_no_secret(self):
        self.cut()
        self.fill_more()
        self.state['deny'] = [TBPROD]
        r = self.compare()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertRegex(
            r.stdout,
            r'STOP  \d+ listing\(s\) failed, first \(\'tb-prod\', \d\): list-object-versions '
            r's3://%s \(profile mzla-tb-prod-ro\) failed: HTTP 403 AccessDenied: Access Denied\n'
            % TBPROD,
        )
        self.assertEqual(r.retries, 0)
        self.secrets_absent(r)

    def test_a_redirect_is_not_followed(self):
        self.cut()
        self.fill_more()
        self.s3.knobs['redirect'] = True
        r = self.compare()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('answered HTTP 301 PermanentRedirect: ', r.stdout)
        self.assertIn('(redirects are not followed)', r.stdout)
        self.assertFalse([q for q in self.s3.requests if q['path'] == '/elsewhere/'])
        self.assertEqual(r.retries, 0)
        self.secrets_absent(r)

    def test_retries_open_a_new_connection(self):
        self.cut()
        self.fill_more()
        self.s3.knobs['fail'] = [
            'drop',
            (500, 'InternalError', 'We encountered an internal error.'),
            (400, 'RequestTimeout', 'Your socket connection to the server was not read.'),
            (503, 'SlowDown', 'Please reduce your request rate.'),
        ]
        r = self.compare()
        self.assert_cli_plan(r)
        self.assertEqual(r.retries, 4)
        self.assertEqual(len({q['conn'] for q in self.s3.requests}), 8 + 4)
        self.s3.knobs['fail'] = [(400, 'InvalidArgument', 'Invalid version id specified')]
        r = self.compare('--relist')
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('failed: HTTP 400 InvalidArgument: Invalid version id specified', r.stdout)
        self.assertEqual(r.retries, 0)

    def test_endpoint_override_needs_blob_test_and_loopback(self):
        self.cut()
        self.fill_more()
        port = self.s3.server_address[1]
        for env in (
            {'BLOB_TEST': ''},
            {'BLOB_TEST': '0'},
            {'BLOB_S3_ENDPOINT': 'http://10.1.2.3:%d' % port},
            {'BLOB_S3_ENDPOINT': 'https://127.0.0.1:%d' % port},
            {'BLOB_S3_ENDPOINT': 'http://127.0.0.1.example.com:%d' % port},
            {'BLOB_S3_ENDPOINT': 'http://127.0.0.1:%d/path' % port},
            {'BLOB_S3_ENDPOINT': 'http://127.0.0.1'},
        ):
            r = self.compare(extra_env=env)
            self.assertEqual(r.returncode, 64, (env, r.stdout, r.stderr))
            self.assertIn('usage: BLOB_S3_ENDPOINT is for tests only', r.stderr)
            self.assertEqual((self.s3.requests, self.creds(), self.calls()), ([], [], []))
        r = self.compare(extra_env={'BLOB_S3_ENDPOINT': 'http://localhost:%d' % port})
        self.assert_cli_plan(r)
        self.assertTrue(self.s3.requests)

    def test_reads_refuse_other_profiles_and_default_to_the_regional_endpoint(self):
        m = tool()
        aws = m.Aws(0, 0)
        for profile in (m.TBPROD_RW, 'default'):
            with self.assertRaises(m.Stop) as cm:
                aws.credentials(profile)
            self.assertEqual(str(cm.exception), 'refusing a read with profile %s' % profile)
            with self.assertRaises(m.Stop) as cm:
                m.Lister(aws, m.TBPROD, profile).page(None, None)
            self.assertEqual(str(cm.exception), 'refusing a read with profile %s' % profile)
        lister = m.Lister(aws, m.LEGACY, m.LEGACY_RO)
        self.assertEqual(
            (lister.host, lister.path), ('%s.s3.eu-central-1.amazonaws.com' % LEGACY, '/')
        )
        self.assertNotIn(('s3api', 'list-object-versions'), m.READ_VERBS)

    def test_signature_matches_the_aws_documentation_example(self):
        m = tool()
        creds = {
            'key': 'AKIAIOSFODNN7EXAMPLE',
            'secret': 'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY',
        }
        when = datetime.datetime(2013, 5, 24, tzinfo=UTC)
        target, headers = m.sign(
            creds,
            'examplebucket.s3.amazonaws.com',
            '/',
            [('prefix', 'J'), ('max-keys', '2')],
            when,
            'us-east-1',
        )
        self.assertEqual(target, '/?max-keys=2&prefix=J')
        self.assertEqual(
            headers['authorization'],
            'AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request, '
            'SignedHeaders=host;x-amz-content-sha256;x-amz-date, '
            'Signature=34b48302e7b5fa45bde8084f4b7868a86f0a534bc59db6670ed5711ef69dc6f7',
        )
        _, headers = m.sign(
            creds, 'examplebucket.s3.amazonaws.com', '/', [('lifecycle', '')], when, 'us-east-1'
        )
        self.assertTrue(
            headers['authorization'].endswith(
                'Signature=fea454ca298b7da1c68078a5d1bdbfbbe0d65c699e0f91ac7a200a0136783543'
            )
        )

    def named_equal(self, *extra):
        m = tool()
        self.state['buckets'][LEGACY] = [obj('k1', 'one'), *[obj(k, 'n' + k) for k in m.NAMED]]
        self.state['buckets'][TBPROD] = [*[obj(k, 'n' + k) for k in m.NAMED], *extra]

    def test_apply_without_a_wait_row_runs_the_wait_then_compares(self):
        self.cut(wait=False)
        self.drained()
        self.named_equal()
        env = {'REPL_FAILED_SINCE': self.since()}
        r = self.compare('--apply', extra_env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        out = r.stdout
        first = out.index(
            'no passing blob-replication-wait row after the stop-legacy-stalwart row: '
            'compare --apply runs the replication wait first\n'
        )
        passed = out.index('PASS  replication wait: OperationsPendingReplication and ')
        summary = out.index(
            'blob-wait mode=apply result=pass stop=%s failed_since=%s ops=pass bytes=pass '
            'failed=pass polls=1\n' % (ztime(self.stop), self.since())
        )
        rows = [x.split('\t') for x in self.tsv.read_text().splitlines()]
        self.assertEqual([r_[0] for r_ in rows[-2:]], ['blob-replication-wait', 'blob-compare'])
        self.assertEqual(rows[-2][3:], ['result', 'pass'])
        self.assertEqual(rows[-1][3:], ['left', '0'])
        start = out.index(
            'start condition: stop-legacy-stalwart %s, blob-replication-wait %s\n'
            % (ztime(self.stop), rows[-2][2])
        )
        listing = out.index('\nlist legacy shard ')
        self.assertLess(first, passed)
        self.assertLess(passed, summary)
        self.assertLess(summary, start)
        self.assertLess(start, listing)
        self.assertIn('PASS  blob compare: 0 differences left', out)
        self.assertIn(' copied=1 failed=0 left=0 ', self.last(r) + ' ')
        self.assertTrue([c for c in self.calls() if c['service'] == 'cloudwatch'])
        self.assertTrue([c for c in self.calls() if c['op'] == 'put-object'])
        self.assertTrue(self.s3.requests)
        r = self.compare('--apply', extra_env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn('replication wait', r.stdout)
        self.assertFalse([c for c in self.calls() if c['service'] == 'cloudwatch'])
        self.assertEqual(self.tsv.read_text().count('blob-replication-wait'), 1)

    def test_a_wait_run_by_hand_is_not_repeated(self):
        self.cut(wait=False)
        self.drained()
        self.named_equal(obj('k1', 'one'))
        env = {'REPL_FAILED_SINCE': self.since()}
        r = self.run_tool('wait', '--apply', extra_env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        r = self.compare('--apply', extra_env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn('replication wait', r.stdout)
        self.assertFalse([c for c in self.calls() if c['service'] == 'cloudwatch'])

    def test_apply_wait_timeout_writes_no_row_and_copies_nothing(self):
        self.cut(wait=False)
        self.drained()
        self.metric('BytesPendingReplication', *[(self.at(n), 9.0) for n in (2, 3, 4)])
        self.named_equal()
        before = json.dumps(self.state['buckets'], sort_keys=True)
        rows = self.tsv.read_text()
        env = {'REPL_FAILED_SINCE': self.since()}
        r = self.compare('--apply', '--timeout=0', '--interval=0', extra_env=env)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        out = r.stdout
        self.assertIn('BytesPendingReplication after %s: ' % ztime(self.stop), out)
        self.assertIn(
            'STOP  replication wait: not passed after 0 s (--timeout); last read above\n'
            'blob-wait mode=apply result=stop stop=%s failed_since=%s ops=pass bytes=not-passed '
            'failed=pass polls=1\n'
            'STOP  replication wait did not pass: no blob-replication-wait row written, '
            'nothing listed or copied\n' % (ztime(self.stop), self.since()),
            out,
        )
        self.assertTrue(self.last(r).startswith('blob-compare mode=apply result=stop '))
        self.assertEqual(self.tsv.read_text(), rows)
        self.assertEqual(self.s3.requests, [])
        self.assertEqual(self.creds(), [])
        self.assertEqual({c['service'] for c in self.calls()}, {'cloudwatch'})
        self.assertEqual(json.dumps(self.state['buckets'], sort_keys=True), before)
        self.assertFalse(self.out.exists())
        r = self.compare(extra_env=env)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn('NEED  no passing blob-replication-wait row after the', r.stdout)
        self.assertIn('plan: 1 difference(s) to copy from legacy (missing=1 ', r.stdout)
        self.assertNotIn(
            'replication wait', r.stdout.replace('no passing blob-replication-wait', '')
        )
        self.assertFalse([c for c in self.calls() if c['service'] == 'cloudwatch'])
        self.assertTrue(self.s3.requests)
        self.assertEqual(self.tsv.read_text(), rows)


CLI_PLAN = (
    'plan: 6 difference(s) to copy from legacy (missing=2 size=1 checksum=3); '
    'only in tb-prod=2 (reported, not deleted)'
)
CLI_SUMMARY = (
    'blob-compare mode=dry-run result=need listed_legacy=13 listed_tb_prod=13 equal=7 missing=2 '
    'size_diff=1 checksum_diff=3 only_tb_prod=2 copied=0 failed=0 left=6 retries='
)
CLI_SHARDS = json.loads(
    r"""
{"legacy/0": [["0abc", "obj", 4, "51037a4a37730f52c8732586d3aaa316", "v1"],
  ["SPAM_CLASSIFIER_KEY", "obj", 14, "d74762915b76ef4410f170256034212d", "v2"],
  ["SPAM_TRAINER_KEY", "obj", 7, "2c065aae9fcb37b49043a5a2012b3dbf", "v1"],
  ["STALWART_WEBADMIN", "obj", 12, "f982555217f5fc6aabcd70a5cfe8ef39", "v1"],
  ["a", "obj", 12, "be7135d551de8a6173c0726458c72444", "v1"],
  ["aaaa", "obj", 10, "688b3ec07198860b2a8b0c2dc44fe595", "v1"],
  ["bbbb", "obj", 11, "38b66e40b4faf70361c1b7f88d747e05", "v1"]],
 "legacy/1": [["cccc", "obj", 6, "6ef380fd46f6b008d566bbe3ceb3d923", "v1"],
  ["dddd", "dm", 0, "", "d2"],
  ["dir/a b+c", "obj", 6, "7daa5df1afb09f7b9b5be4afd095c626", "v1"]],
 "legacy/2": [["k%2Fx", "obj", 7, "354f047ba64552895b016bbdd60ab174", "v1"]],
 "legacy/3": [["zzzz", "obj", 4, "7aea2552dfe7eb84b9443b6fc9ba6e01", "v1"],
  ["~tail", "obj", 14, "8ced71457d957d269376c8f978236f6c", "v1"],
  ["été", "obj", 6, "727b79a9a430ccddcf9260a381d5ab10", "v1"]],
 "tb-prod/0": [["0abc", "obj", 4, "51037a4a37730f52c8732586d3aaa316", "v1"],
  ["SPAM_CLASSIFIER_KEY", "obj", 14, "872e4de98f0485c131beb771a3a294c4", "v1"],
  ["SPAM_TRAINER_KEY", "obj", 7, "2c065aae9fcb37b49043a5a2012b3dbf", "v1"],
  ["STALWART_WEBADMIN", "obj", 12, "1d6ba93aaa8575601ec8c5bd7a52824c", "v1"],
  ["a", "obj", 12, "be7135d551de8a6173c0726458c72444", "v1"],
  ["aaaa", "dm", 0, "", "d9"],
  ["bbbb", "obj", 16, "e69beccfe42f8af5c726c109d1ba577c", "v1"]],
 "tb-prod/1": [["cccc", "obj", 6, "4e5dc3764ba8e9a8952e7232d5626ce3", "v1"],
  ["dddd", "obj", 14, "b12ed5bf0e86be3e697a4ffcc9617cbd", "v1"],
  ["dir/a b+c", "obj", 6, "7daa5df1afb09f7b9b5be4afd095c626", "v1"]],
 "tb-prod/2": [["k%2Fx", "obj", 7, "354f047ba64552895b016bbdd60ab174", "v1"],
  ["only-tb", "obj", 5, "ea9f91b2cda019730f2891bd12a7a4d6", "v1"]],
 "tb-prod/3": [["zzzz", "obj", 4, "7aea2552dfe7eb84b9443b6fc9ba6e01", "v1"],
  ["été", "obj", 6, "727b79a9a430ccddcf9260a381d5ab10", "v1"]]}
"""
)


if __name__ == '__main__':
    unittest.main()
