#!/usr/bin/env python3
import argparse
import atexit
import base64
import concurrent.futures
import datetime
import fcntl
import hashlib
import itertools
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid

UTC = datetime.timezone.utc
REGION = 'eu-central-1'
LEGACY = 'mailstrom-prod-stalwart-s3-store'
TBPROD = 'mzla-tb-prod-thundermail-stalwart-blob-store'
LEGACY_RO = 'mzla-legacy-ro'
TBPROD_RO = 'mzla-tb-prod-ro'
TBPROD_RW = 'mzla-tb-prod'
RULE = 'tb-prod-cutover'
RULE_APPLIED = '2026-11-19T02:48:00Z'
OPERATOR_FILE = '~/.local/state/talos/cutover-operator-tb-prod.env'
NAMED = ('STALWART_WEBADMIN', 'SPAM_TRAINER_KEY', 'SPAM_CLASSIFIER_KEY')
READ_VERBS = {
    ('s3api', 'list-object-versions'),
    ('s3api', 'get-object'),
    ('s3api', 'head-object'),
    ('cloudwatch', 'get-metric-statistics'),
}
STALWART_ALPHABET = 'abcdefghijklmnopqrstuvwxyz792013'
PENDING = ('OperationsPendingReplication', 'BytesPendingReplication')
FAILED = 'OperationsFailedReplication'
CUT_ROW = 'cut-ecs'
STOP_ROW = 'stop-legacy-stalwart'
WAIT_ROW = 'blob-replication-wait'
COMPARE_ROW = 'blob-compare'
FAILED_PERIOD = 300
FAILED_RETENTION = datetime.timedelta(days=63)
PENDING_LOOKBACK = datetime.timedelta(hours=6)
WRITE_VERBS = (
    'put-object',
    'create-multipart-upload',
    'upload-part',
    'complete-multipart-upload',
    'abort-multipart-upload',
)
RETRYABLE = re.compile(
    r'SlowDown|Throttl|TooManyRequests|RequestLimitExceeded|ServiceUnavailable|InternalError|'
    r'RequestTimeout|\b50[0234]\b|Could not connect|Connection was closed|Read timeout|'
    r'EndpointConnectionError',
    re.I,
)
ZTIME = re.compile(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ')
BLOCK = 1 << 20


class Stop(Exception):
    pass


class Need(Exception):
    pass


class Usage(Exception):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Usage(message)


def z(dt):
    return dt.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')


def utcnow():
    return datetime.datetime.now(UTC).replace(microsecond=0)


def parse_z(text, what):
    if not ZTIME.fullmatch(text or ''):
        raise Usage('%s: %r is not a UTC time like 2026-11-19T05:10:00Z' % (what, text))
    return datetime.datetime.strptime(text, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=UTC)


def parse_aws_time(text):
    dt = datetime.datetime.fromisoformat(text.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def clean_etag(value):
    return (value or '').strip().strip('"')


class Aws:
    def __init__(self, retries, backoff):
        self.retries = retries
        self.backoff = backoff
        self.retried = 0
        self.lock = threading.Lock()
        self.cancel = threading.Event()
        self.env = dict(os.environ, AWS_PAGER='')

    def call(self, profile, *args):
        cmd = ['aws', '--profile', profile, '--region', REGION, '--output', 'json', *args]
        for attempt in itertools.count():
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, env=self.env)
            except FileNotFoundError:
                raise Need('aws CLI not found on PATH') from None
            if r.returncode == 0:
                return json.loads(r.stdout) if r.stdout.strip() else {}
            err = ' '.join(r.stderr.split())[-400:]
            if attempt >= self.retries or not RETRYABLE.search(err) or self.cancel.is_set():
                raise Stop('aws %s %s (profile %s) failed: %s' % (args[0], args[1], profile, err))
            with self.lock:
                self.retried += 1
            time.sleep(min(30.0, self.backoff * 2**attempt))

    def read(self, profile, *args):
        if profile not in (LEGACY_RO, TBPROD_RO):
            raise Stop('refusing a read with profile %s' % profile)
        if tuple(args[:2]) not in READ_VERBS:
            raise Stop('refusing aws %s as a read' % ' '.join(args[:2]))
        return self.call(profile, *args)

    def write(self, verb, *args):
        buckets = [a for a in args if a.startswith('--bucket')]
        if verb not in WRITE_VERBS or buckets != ['--bucket=' + TBPROD]:
            raise Stop(
                'refusing aws s3api %s %s: writes go to the tb-prod bucket only' % (verb, buckets)
            )
        return self.call(TBPROD_RW, 's3api', verb, *args)


def timing_tsv():
    path = os.environ.get('TIMING_TSV')
    if not path and os.environ.get('EVIDENCE_DIR'):
        path = os.path.join(os.environ['EVIDENCE_DIR'], 'timing-tb-prod.tsv')
    if not path:
        raise Need('TIMING_TSV (or EVIDENCE_DIR) is not set')
    return pathlib.Path(path)


def row_time(row):
    for value in (row[2] if len(row) > 2 else '', row[1] if len(row) > 1 else ''):
        if value:
            return parse_z(value, '%s row' % row[0])
    raise Usage('%s row has no time' % row[0])


def current_cut(tsv):
    try:
        rows = [line.split('\t') for line in tsv.read_text().splitlines()]
    except OSError as exc:
        return None, None, 'cannot read TIMING_TSV %s: %s' % (tsv, exc.strerror)
    cuts = [i for i, r in enumerate(rows) if r[0] == CUT_ROW]
    if not cuts:
        return None, None, 'no %s row in %s' % (CUT_ROW, tsv)
    after = rows[cuts[-1] + 1 :]
    stops = [i for i, r in enumerate(after) if r[0] == STOP_ROW]
    if not stops:
        return None, None, 'no %s row after the last %s row in %s' % (STOP_ROW, CUT_ROW, tsv)
    stop = row_time(after[stops[-1]])
    wait = None
    for r in after[stops[-1] + 1 :]:
        if r[0] == WAIT_ROW and len(r) > 4 and r[4] == 'pass' and row_time(r) >= stop:
            wait = row_time(r)
    return stop, wait, None


def append_row(tsv, name, start, end, metric, value):
    with tsv.open('a') as fh:
        fh.write('%s\t%s\t%s\t%s\t%s\n' % (name, z(start), z(end), metric, value))


def operator_value(name):
    if os.environ.get(name):
        return os.environ[name], 'the environment'
    path = pathlib.Path(os.path.expanduser(os.environ.get('OPERATOR_ENV') or OPERATOR_FILE))
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return None, None
    values = [x.split('=', 1)[1].strip().strip('\'"') for x in lines if x.startswith(name + '=')]
    return (values[-1], str(path)) if values and values[-1] else (None, None)


def failed_since():
    value, source = operator_value('REPL_FAILED_SINCE')
    if value is None:
        value, source = RULE_APPLIED, 'the default (rule applied)'
    since = parse_z(value, 'REPL_FAILED_SINCE')
    period = since.replace(minute=since.minute - since.minute % 5, second=0)
    print(
        'failed operations counted since %s, REPL_FAILED_SINCE from %s; from the 5-minute period '
        'that starts %s' % (z(since), source, z(period))
    )
    return since, period


def metric_points(aws, name, stat, start, end, period):
    points = set()
    span = datetime.timedelta(seconds=period * 1440)
    t = start
    while t < end:
        e = min(end, t + span)
        out = aws.read(
            LEGACY_RO,
            'cloudwatch',
            'get-metric-statistics',
            '--namespace=AWS/S3',
            '--metric-name=' + name,
            '--dimensions',
            'Name=SourceBucket,Value=' + LEGACY,
            'Name=DestinationBucket,Value=' + TBPROD,
            'Name=RuleId,Value=' + RULE,
            '--start-time=' + z(t),
            '--end-time=' + z(e),
            '--period=%d' % period,
            '--statistics',
            stat,
        )
        for d in out.get('Datapoints') or []:
            points.add((parse_aws_time(d['Timestamp']), float(d[stat])))
        t = e
    return sorted(points)


def read_pending(aws, name, stop, now):
    start = max(stop, now - PENDING_LOOKBACK) if stop else now - datetime.timedelta(minutes=20)
    later = [
        p for p in metric_points(aws, name, 'Maximum', start, now, 60) if not stop or p[0] > stop
    ]
    last = later[-3:]
    ok = stop is not None and len(last) == 3 and all(v == 0 for _, v in last)
    if not last:
        seen = 'no datapoint later than %s' % z(stop) if stop else 'no datapoint in 20 minutes'
    else:
        seen = ' '.join('%s=%s' % (z(t), v) for t, v in last)
    verdict = 'pass' if ok else 'not passed' + ('' if stop else ' (no stop row)')
    print('%s after %s: %s %s' % (name, z(stop) if stop else 'none', seen, verdict))
    return ok


def read_failed(aws, since, period, now):
    if since > now:
        raise Usage('REPL_FAILED_SINCE %s is in the future' % z(since))
    if now - period > FAILED_RETENTION:
        raise Stop(
            'REPL_FAILED_SINCE %s is older than 63 days: five-minute datapoints are gone' % z(since)
        )
    points = [
        p for p in metric_points(aws, FAILED, 'Sum', period, now, FAILED_PERIOD) if p[0] >= period
    ]
    total = sum(v for _, v in points)
    if not points:
        print('%s since %s: no datapoint (idle rule), sum=0.0 pass' % (FAILED, z(since)))
        return True
    ok = total == 0
    print(
        '%s since %s: datapoints=%d sum=%s %s'
        % (FAILED, z(since), len(points), total, 'pass' if ok else 'not passed')
    )
    if not ok:
        print(
            'failed operations since %s: run replication.py batch again (Go: operator), then set '
            'REPL_FAILED_SINCE in the operator file to the UTC start of that job' % z(since)
        )
    return ok


def cmd_wait(a, summary):
    tsv = timing_tsv()
    print(
        'replication rule %s: s3://%s -> s3://%s (profile %s)' % (RULE, LEGACY, TBPROD, LEGACY_RO)
    )
    since, period = failed_since()
    stop, _, why = current_cut(tsv)
    summary.update(stop=z(stop) if stop else 'none', failed_since=z(since))
    if stop is None:
        if a.apply:
            raise Stop(why)
        print('NEED  ' + why)
    started = utcnow()
    deadline = time.monotonic() + a.timeout
    while True:
        summary['polls'] += 1
        now = utcnow()
        print('read %s' % z(now))
        ok = [read_pending(a.aws, m, stop, now) for m in PENDING]
        ok.append(read_failed(a.aws, since, period, now))
        summary.update(ops=pf(ok[0]), bytes=pf(ok[1]), failed=pf(ok[2]))
        if all(ok):
            print(
                'PASS  replication wait: %s and %s read 0 three times after %s, %s sum 0 since %s'
                % (PENDING[0], PENDING[1], z(stop), FAILED, z(since))
            )
            if a.apply:
                append_row(tsv, WAIT_ROW, started, utcnow(), 'result', 'pass')
            return 0
        if stop is None:
            return 2
        if not a.apply:
            print('NEED  replication not drained yet; wait --apply polls until it is')
            return 2
        if time.monotonic() + a.interval > deadline:
            raise Stop(
                'replication wait: not passed after %d s (--timeout); last read above' % a.timeout
            )
        time.sleep(a.interval)


def pf(ok):
    return 'pass' if ok else 'not-passed'


def boundaries(alphabet, shards):
    chars = sorted(set(alphabet))
    width = 1
    while len(chars) ** width < shards:
        width += 1
    space = [''.join(p) for p in itertools.product(chars, repeat=width)]
    return [space[i * len(space) // shards] for i in range(1, shards)]


def shard_ranges(bounds):
    edges = [None, *bounds, None]
    return list(zip(edges[:-1], edges[1:]))


def in_shard(key, lower, upper):
    return (lower is None or key > lower) and (upper is None or key <= upper)


def show_range(lower, upper):
    return '(%s, %s]' % ('start' if lower is None else lower, 'end' if upper is None else upper)


class Side:
    def __init__(self, name, bucket, profile, out):
        self.name = name
        self.bucket = bucket
        self.profile = profile
        self.dir = out / 'list' / name

    def file(self, i):
        return self.dir / ('shard-%04d.jsonl' % i)

    def done(self, i):
        return self.dir / ('shard-%04d.done' % i)


def list_shard(aws, side, i, lower, upper, key, relist):
    meta = dict(key, bucket=side.bucket, lower=lower, upper=upper)
    done = side.done(i)
    if relist:
        done.unlink(missing_ok=True)
    if not relist and done.exists() and side.file(i).exists():
        try:
            old = json.loads(done.read_text())
        except ValueError:
            old = {}
        if isinstance(old, dict) and all(old.get(k) == v for k, v in meta.items()):
            return dict(old, reused=True)
    part = side.dir / ('shard-%04d.part' % i)
    objects = markers = pages = 0
    marker, version = lower, None
    with part.open('w') as fh:
        while not aws.cancel.is_set():
            args = ['--bucket=' + side.bucket, '--max-keys=1000', '--no-paginate']
            if marker is not None:
                args.append('--key-marker=' + marker)
            if version:
                args.append('--version-id-marker=' + version)
            page = aws.read(side.profile, 's3api', 'list-object-versions', *args)
            pages += 1
            latest = [
                (
                    v['Key'],
                    'obj',
                    int(v.get('Size', 0)),
                    clean_etag(v.get('ETag')),
                    v.get('VersionId'),
                )
                for v in page.get('Versions') or []
                if v.get('IsLatest')
            ]
            latest += [
                (m['Key'], 'dm', 0, '', m.get('VersionId'))
                for m in page.get('DeleteMarkers') or []
                if m.get('IsLatest')
            ]
            latest.sort(key=lambda e: e[0])
            beyond = False
            for e in latest:
                if upper is not None and e[0] > upper:
                    beyond = True
                    break
                fh.write(json.dumps(e) + '\n')
                if e[1] == 'obj':
                    objects += 1
                else:
                    markers += 1
            if beyond or not page.get('IsTruncated'):
                break
            marker, version = page.get('NextKeyMarker'), page.get('NextVersionIdMarker')
            if not marker:
                raise Stop('%s: truncated page without NextKeyMarker' % side.bucket)
            if upper is not None and marker > upper:
                break
    if aws.cancel.is_set():
        raise Stop('listing cancelled')
    os.replace(part, side.file(i))
    meta.update(objects=objects, markers=markers, pages=pages)
    done.write_text(json.dumps(meta))
    return dict(meta, reused=False)


def run_parallel(aws, workers, jobs):
    errors = []
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
    try:
        futures = {ex.submit(fn, *args): tag for tag, fn, args in jobs}
        for f in concurrent.futures.as_completed(futures):
            try:
                result = f.result()
            except Stop as exc:
                aws.cancel.set()
                errors.append((futures[f], str(exc)))
                continue
            yield futures[f], result
    except BaseException:
        aws.cancel.set()
        ex.shutdown(wait=True, cancel_futures=True)
        raise
    ex.shutdown(wait=True)
    real = [e for e in errors if e[1] != 'listing cancelled'] or errors
    if real:
        raise Stop('%d listing(s) failed, first %s: %s' % (len(real), real[0][0], real[0][1]))


def entries(path):
    with path.open() as fh:
        for line in fh:
            yield json.loads(line)


class Evidence:
    KINDS = ('missing', 'size', 'checksum', 'only-tb-prod')

    def __init__(self, run_dir):
        self.files = {k: (run_dir / ('%s.jsonl' % k)).open('w') for k in self.KINDS}
        self.paths = {k: run_dir / ('%s.jsonl' % k) for k in self.KINDS}

    def add(self, kind, record):
        self.files[kind].write(json.dumps(record) + '\n')

    def close(self):
        for fh in self.files.values():
            fh.close()


def compare_shard(i, legacy_file, tb_file, ev, counts, named):
    a, b = entries(legacy_file), entries(tb_file)
    x, y = next(a, None), next(b, None)
    while x is not None or y is not None:
        if y is None or (x is not None and x[0] < y[0]):
            if x[1] == 'obj':
                record(i, x, None, 'absent', ev, counts, named)
            x = next(a, None)
        elif x is None or y[0] < x[0]:
            if y[1] == 'obj':
                record(i, None, y, 'only-tb-prod', ev, counts, named)
            y = next(b, None)
        else:
            record(i, x, y, None, ev, counts, named)
            x, y = next(a, None), next(b, None)


def record(i, x, y, reason, ev, counts, named):
    if x is not None and x[1] == 'obj':
        counts['listed_legacy'] += 1
    if y is not None and y[1] == 'obj':
        counts['listed_tb_prod'] += 1
    if reason is None:
        if x[1] == 'dm':
            reason = 'only-tb-prod' if y[1] == 'obj' else 'both-deleted'
        elif y[1] == 'dm':
            reason = 'delete-marker'
        elif x[2] != y[2]:
            reason = 'size'
        elif x[3] != y[3]:
            reason = 'checksum'
        else:
            reason = 'equal'
    key = (x or y)[0]
    if key in NAMED:
        named[key] = (x, y, reason)
    if reason == 'both-deleted':
        return
    if reason == 'equal':
        counts['equal'] += 1
        return
    rec = {'key': key, 'reason': reason, 'shard': i}
    if x is not None:
        rec.update(version=x[4], size=x[2], etag=x[3])
    if y is not None:
        rec.update(tb_prod_version=y[4], tb_prod_size=y[2], tb_prod_etag=y[3])
    if reason in ('absent', 'delete-marker'):
        counts['missing'] += 1
        ev.add('missing', rec)
    elif reason == 'size':
        counts['size_diff'] += 1
        ev.add('size', rec)
    elif reason == 'checksum':
        counts['checksum_diff'] += 1
        ev.add('checksum', rec)
    else:
        counts['only_tb_prod'] += 1
        ev.add('only-tb-prod', rec)


def named_line(key, state, copied):
    if state is None:
        return 'named %s absent in both buckets' % key
    x, y, reason = state

    def show(e):
        if e is None:
            return 'absent'
        return 'delete-marker' if e[1] == 'dm' else '%s/%d' % (e[3], e[2])

    if x is None or x[1] == 'dm':
        verdict = 'absent in legacy'
    elif reason == 'equal':
        verdict = 'match'
    elif key in copied:
        verdict = 'copied, match'
        y = x
    else:
        verdict = 'differ' if reason in ('size', 'checksum') else 'missing in tb-prod'
    return 'named %s legacy=%s tb-prod=%s %s' % (key, show(x), show(y), verdict)


def md5_range(fh, length):
    h = hashlib.md5()
    left = length
    while left > 0:
        chunk = fh.read(min(BLOCK, left))
        if not chunk:
            break
        h.update(chunk)
        left -= len(chunk)
    return h


def local_etag(path, part):
    size = path.stat().st_size
    with path.open('rb') as fh:
        if part is None:
            return md5_range(fh, size).hexdigest()
        digests = []
        while fh.tell() < size:
            digests.append(md5_range(fh, part).digest())
        return '%s-%d' % (hashlib.md5(b''.join(digests)).hexdigest(), len(digests))


def md5_file(path):
    with path.open('rb') as fh:
        return md5_range(fh, path.stat().st_size).digest()


def b64md5(data):
    return base64.b64encode(hashlib.md5(data).digest()).decode()


def copy_one(aws, d, tmp):
    key, size, etag = d['key'], d['size'], d['etag']
    body = tmp / uuid.uuid4().hex
    source = ['--bucket=' + LEGACY, '--key=' + key]
    if d.get('version'):
        source.append('--version-id=' + d['version'])
    target = ['--bucket=' + TBPROD, '--key=' + key]
    try:
        got = aws.read(LEGACY_RO, 's3api', 'get-object', *source, str(body))
        if body.stat().st_size != size:
            raise Stop('downloaded %d bytes, legacy lists %d' % (body.stat().st_size, size))
        part = None
        if '-' in etag:
            head = aws.read(LEGACY_RO, 's3api', 'head-object', *source, '--part-number=1')
            part = int(head['ContentLength'])
            if part <= 0:
                raise Stop('legacy part 1 has size %d' % part)
        if local_etag(body, part) != etag:
            raise Stop('downloaded body does not match the legacy ETag %s' % etag)
        extra = []
        if got.get('ContentType'):
            extra.append('--content-type=' + got['ContentType'])
        if got.get('Metadata'):
            extra.append('--metadata=' + json.dumps(got['Metadata']))
        if part is None:
            aws.write(
                'put-object',
                *target,
                '--body=' + str(body),
                '--content-md5=' + base64.b64encode(md5_file(body)).decode(),
                *extra,
            )
        else:
            multipart(aws, target, body, part, extra, tmp)
        head = aws.read(TBPROD_RO, 's3api', 'head-object', *target)
        got_etag, got_size = clean_etag(head.get('ETag')), int(head.get('ContentLength', -1))
        if got_etag != etag or got_size != size:
            raise Stop('tb-prod reads ETag %s size %d after the copy' % (got_etag, got_size))
    finally:
        body.unlink(missing_ok=True)


def multipart(aws, target, body, part, extra, tmp):
    upload = aws.write('create-multipart-upload', *target, *extra)['UploadId']
    parts = []
    try:
        with body.open('rb') as fh:
            for n in itertools.count(1):
                chunk = fh.read(part)
                if not chunk:
                    break
                piece = tmp / ('%s.%d' % (body.name, n))
                piece.write_bytes(chunk)
                try:
                    r = aws.write(
                        'upload-part',
                        *target,
                        '--upload-id=' + upload,
                        '--part-number=%d' % n,
                        '--body=' + str(piece),
                        '--content-md5=' + b64md5(chunk),
                    )
                finally:
                    piece.unlink(missing_ok=True)
                parts.append({'ETag': r['ETag'], 'PartNumber': n})
        aws.write(
            'complete-multipart-upload',
            *target,
            '--upload-id=' + upload,
            '--multipart-upload=' + json.dumps({'Parts': parts}),
        )
    except BaseException:
        try:
            aws.write('abort-multipart-upload', *target, '--upload-id=' + upload)
        except Stop:
            pass
        raise


def copy_all(aws, ev, run_dir, workers, counts, tmp, target):
    lock = threading.Lock()
    touched = set()
    copied_keys = set()
    out = {k: (run_dir / ('%s.jsonl' % k)).open('w') for k in ('copied', 'failed')}

    def job(d):
        with lock:
            if d['shard'] not in touched:
                target.done(d['shard']).unlink(missing_ok=True)
                touched.add(d['shard'])
        try:
            copy_one(aws, d, tmp)
            kind, rec = 'copied', d
        except (Stop, OSError, KeyError, ValueError) as exc:
            kind, rec = 'failed', dict(d, error=str(exc))
        with lock:
            out[kind].write(json.dumps(rec) + '\n')
            counts[kind] += 1
            if kind == 'copied':
                copied_keys.add(d['key'])

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            pending = set()
            for kind in ('missing', 'size', 'checksum'):
                for d in entries(ev.paths[kind]):
                    pending.add(ex.submit(job, d))
                    if len(pending) >= workers * 4:
                        _, pending = concurrent.futures.wait(
                            pending, return_when=concurrent.futures.FIRST_COMPLETED
                        )
            for f in pending:
                f.result()
    finally:
        for fh in out.values():
            fh.close()
    return copied_keys


def lock_out(out):
    fh = (out / '.lock').open('w')
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise Stop('another blob_compare run holds %s' % (out / '.lock')) from None
    return fh


def start_condition(a, tsv):
    stop, wait, why = current_cut(tsv)
    if stop is not None and wait is None and not a.skip_wait_check:
        why = 'no passing %s row after the %s row (run wait --apply first)' % (WAIT_ROW, STOP_ROW)
    if not why:
        return stop, wait, []
    if a.apply:
        raise Stop(why)
    print('NEED  ' + why)
    return stop, wait, [why]


def cmd_compare(a, summary):
    tsv = timing_tsv()
    out = a.out or (
        os.path.join(os.environ['EVIDENCE_DIR'], 'blob-compare')
        if os.environ.get('EVIDENCE_DIR')
        else None
    )
    if not out:
        raise Need('--out or EVIDENCE_DIR is not set')
    out = pathlib.Path(out)
    stop, wait, needs = start_condition(a, tsv)
    started = utcnow()
    out.mkdir(parents=True, exist_ok=True)
    lock = lock_out(out)
    run_dir = out / ('run-%s-%s' % (started.strftime('%Y%m%dT%H%M%SZ'), summary['mode']))
    run_dir.mkdir(exist_ok=True)
    base = (
        pathlib.Path(os.environ['SECRET_DIR']) / 'blob-compare-tmp'
        if os.environ.get('SECRET_DIR')
        else out / 'tmp'
    )
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = pathlib.Path(tempfile.mkdtemp(prefix='run-', dir=base))
    atexit.register(shutil.rmtree, tmp, True)
    sides = (Side('legacy', LEGACY, LEGACY_RO, out), Side('tb-prod', TBPROD, TBPROD_RO, out))
    for s in sides:
        s.dir.mkdir(parents=True, exist_ok=True)
    bounds = boundaries(a.alphabet, a.shards)
    shards = shard_ranges(bounds)
    print('legacy  s3://%s read with %s, never written' % (LEGACY, LEGACY_RO))
    print(
        'tb-prod s3://%s read with %s, written with %s (--apply only)'
        % (TBPROD, TBPROD_RO, TBPROD_RW)
    )
    print(
        'start condition: %s %s, %s %s'
        % (STOP_ROW, z(stop) if stop else 'none', WAIT_ROW, z(wait) if wait else 'none')
    )
    print('evidence %s' % run_dir)
    print(
        'shards=%d workers=%d boundaries: %s' % (len(shards), a.workers, ' '.join(bounds) or 'none')
    )
    key = {'stop': z(stop) if stop else 'none', 'wait': z(wait) if wait else 'none'}
    jobs = [
        (
            (s.name, i),
            list_shard,
            (a.aws, s, i, lo, up, key, a.relist or (a.apply and s is sides[1])),
        )
        for i, (lo, up) in enumerate(shards)
        for s in sides
    ]
    for (name, i), m in run_parallel(a.aws, a.workers, jobs):
        print(
            'list %s shard %04d %s current=%d delete-markers=%d pages=%d%s'
            % (
                name,
                i,
                show_range(*shards[i]),
                m['objects'],
                m['markers'],
                m['pages'],
                ' reused' if m['reused'] else '',
            )
        )
    ev = Evidence(run_dir)
    named = {}
    try:
        for i in range(len(shards)):
            compare_shard(i, sides[0].file(i), sides[1].file(i), ev, summary, named)
    finally:
        ev.close()
    diffs = summary['missing'] + summary['size_diff'] + summary['checksum_diff']
    print(
        'plan: %d difference(s) to copy from legacy (missing=%d size=%d checksum=%d); '
        'only in tb-prod=%d (reported, not deleted)'
        % (
            diffs,
            summary['missing'],
            summary['size_diff'],
            summary['checksum_diff'],
            summary['only_tb_prod'],
        )
    )
    copied_keys = set()
    if a.apply and diffs:
        copied_keys = copy_all(a.aws, ev, run_dir, a.workers, summary, tmp, sides[1])
    elif not a.apply:
        print('dry run: nothing copied')
    named_bad = []
    for k in NAMED:
        text = named_line(k, named.get(k), copied_keys)
        print(text)
        if not text.endswith(' match'):
            named_bad.append(k)
    summary['left'] = diffs - summary['copied']
    if a.apply and named_bad and not summary['left'] and not summary['failed']:
        lock.close()
        raise Stop('named key(s) not matching by checksum in both buckets: ' + ' '.join(named_bad))
    if a.apply:
        append_row(tsv, COMPARE_ROW, started, utcnow(), 'left', summary['left'])
    lock.close()
    if summary['failed']:
        raise Stop('%d copy(ies) failed: %s' % (summary['failed'], run_dir / 'failed.jsonl'))
    if summary['left']:
        print('NEED  %d difference(s) left: run compare --apply' % summary['left'])
        return 2
    if named_bad:
        msg = 'named key(s) not matching by checksum in both buckets: ' + ' '.join(named_bad)
        if a.apply:
            raise Stop(msg)
        print('NEED  ' + msg)
        return 2
    if needs:
        return 2
    print('PASS  blob compare: 0 differences left')
    return 0


def count_line(prefix, summary, keys):
    return ' '.join([prefix] + ['%s=%s' % (k, summary[k]) for k in keys])


WAIT_KEYS = ('mode', 'result', 'stop', 'failed_since', 'ops', 'bytes', 'failed', 'polls')
COMPARE_KEYS = (
    'mode',
    'result',
    'listed_legacy',
    'listed_tb_prod',
    'equal',
    'missing',
    'size_diff',
    'checksum_diff',
    'only_tb_prod',
    'copied',
    'failed',
    'left',
    'retries',
)


def parser():
    ap = Parser(
        prog='blob_compare.py',
        description='Wait for the tb-prod-cutover S3 replication and compare the legacy and '
        'tb-prod blob buckets. Dry run by default; --apply writes.',
    )
    sub = ap.add_subparsers(dest='cmd', required=True, parser_class=Parser)
    w = sub.add_parser('wait', help='wait until rule tb-prod-cutover has nothing pending')
    c = sub.add_parser('compare', help='sharded compare of both buckets, copy differences')
    for p in (w, c):
        p.add_argument('--apply', action='store_true')
        p.add_argument('--dry-run', action='store_true')
        p.add_argument('--retries', type=int, default=8)
        p.add_argument('--backoff', type=float, default=1.0)
    w.add_argument('--timeout', type=int, default=1800)
    w.add_argument('--interval', type=int, default=60)
    c.add_argument('--out')
    c.add_argument('--shards', type=int, default=128)
    c.add_argument('--workers', type=int, default=16)
    c.add_argument('--alphabet', default=STALWART_ALPHABET)
    c.add_argument('--relist', action='store_true')
    c.add_argument('--skip-wait-check', action='store_true')
    return ap


def main(argv=None):
    try:
        a = parser().parse_args(argv)
        if a.apply and a.dry_run:
            raise Usage('--apply and --dry-run exclude each other')
        if os.environ.get('TARGET_ENV') != 'tb-prod':
            raise Usage('TARGET_ENV must be tb-prod')
        if a.retries < 0 or a.backoff < 0:
            raise Usage('--retries and --backoff must not be negative')
        if a.cmd == 'compare' and not (1 <= a.shards <= 4096 and 1 <= a.workers <= 256):
            raise Usage('--shards must be 1 to 4096 and --workers 1 to 256')
        if a.cmd == 'compare' and a.skip_wait_check and a.apply:
            raise Usage('--skip-wait-check is for a dry run only')
        if a.cmd == 'compare' and len(set(a.alphabet)) < 2:
            raise Usage('--alphabet needs two or more characters')
    except Usage as exc:
        print('usage: %s' % exc, file=sys.stderr)
        return 64
    a.aws = Aws(a.retries, a.backoff)
    summary = dict.fromkeys(COMPARE_KEYS if a.cmd == 'compare' else WAIT_KEYS, 0)
    summary.update(mode='apply' if a.apply else 'dry-run', result='stop')
    if a.cmd == 'wait':
        summary.update(stop='none', failed_since='none', ops='-', bytes='-', failed='-')
    try:
        rc = cmd_wait(a, summary) if a.cmd == 'wait' else cmd_compare(a, summary)
    except Usage as exc:
        print('usage: %s' % exc, file=sys.stderr)
        rc = 64
    except Need as exc:
        print('NEED  %s' % exc)
        rc = 2
    except Stop as exc:
        print('STOP  %s' % exc)
        rc = 1
    except KeyboardInterrupt:
        print('STOP  interrupted')
        rc = 1
    except Exception as exc:
        print('STOP  unexpected %s: %s' % (type(exc).__name__, exc))
        rc = 1
    summary['result'] = {0: 'pass', 2: 'need'}.get(rc, 'stop')
    if 'retries' in summary:
        summary['retries'] = a.aws.retried
    name = 'blob-compare' if a.cmd == 'compare' else 'blob-wait'
    print(count_line(name, summary, COMPARE_KEYS if a.cmd == 'compare' else WAIT_KEYS))
    return rc


if __name__ == '__main__':
    sys.exit(main())
