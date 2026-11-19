#!/usr/bin/env python3
"""06: blob buckets. Lists both buckets (read-only) and compares keys and sizes. Every blob the
target references (message bodies from 03) must exist in the target bucket. Objects only on the
source are expected when 0.16 has purged unreferenced blobs (they show up as delete markers on a
versioned bucket).

The listings go to files under $VERIFY_OUT (bucket-source.tsv, bucket-target.tsv,
bucket-target-delete-markers.txt), sorted bytewise, and are compared line by line, so no listing
is held in memory. VERIFY_PIECE=buckets (piece buckets of the full compare) runs before 03: the
blob references are proven by piece bodies, which downloads every message body through Stalwart.
VERIFY_REUSE_LISTINGS=1 keeps a complete listing file from an earlier run."""

import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor

from common import OUT, Report, need

r = Report('06 blob buckets')
PIECE = os.environ.get('VERIFY_PIECE') == 'buckets'
OBJECTS = ['list-objects-v2', '--query', 'Contents[].[Key,Size]']
MARKERS = ['list-object-versions', '--query', 'DeleteMarkers[?IsLatest].[Key]']


def to_file(args, bucket, profile, name):
    path = f'{OUT}/{name}'
    if os.environ.get('VERIFY_REUSE_LISTINGS') == '1' and os.path.exists(path):
        return path
    cmd = ['aws', 's3api', *args, '--bucket', bucket, '--profile', profile]
    cmd += ['--region', need('AWS_REGION'), '--output', 'text']
    with open(f'{path}.part', 'w') as f:
        subprocess.run(cmd, stdout=f, check=True)
    sort = ['sort', '-t', '\t', '-k1,1', '-T', OUT, '-o', f'{path}.sorted', f'{path}.part']
    subprocess.run(sort, env=dict(os.environ, LC_ALL='C'), check=True)
    os.replace(f'{path}.sorted', path)
    os.remove(f'{path}.part')
    return path


def lines(path):
    with open(path, encoding='utf-8', errors='surrogateescape') as f:
        for line in f:
            line = line.rstrip('\n')
            if line and line != 'None':
                yield line


def objects(path):
    for line in lines(path):
        k, _, size = line.rpartition('\t')
        yield k, int(size)


def merged(a, b):
    x, y = next(a, None), next(b, None)
    while x is not None or y is not None:
        if y is None or (x is not None and x[0] < y[0]):
            yield x[0], x[1], None
            x = next(a, None)
        elif x is None or y[0] < x[0]:
            yield y[0], None, y[1]
            y = next(b, None)
        else:
            yield x[0], x[1], y[1]
            x, y = next(a, None), next(b, None)


src_b, src_p = need('SOURCE_BUCKET'), need('SOURCE_AWS_PROFILE')
tgt_b, tgt_p = need('TARGET_BUCKET'), need('TARGET_AWS_PROFILE')
with ThreadPoolExecutor(3) as ex:
    jobs = [
        ex.submit(to_file, OBJECTS, src_b, src_p, 'bucket-source.tsv'),
        ex.submit(to_file, OBJECTS, tgt_b, tgt_p, 'bucket-target.tsv'),
        ex.submit(to_file, MARKERS, tgt_b, tgt_p, 'bucket-target-delete-markers.txt'),
    ]
    src_f, tgt_f, dm_f = [j.result() for j in jobs]

n_src = n_tgt = n_only_tgt = 0
diff = []
only_src = f'{OUT}/bucket-only-source.txt'
with open(only_src, 'w', encoding='utf-8', errors='surrogateescape') as f:
    for k, s, t in merged(objects(src_f), objects(tgt_f)):
        n_src += s is not None
        n_tgt += t is not None
        if s is None:
            n_only_tgt += 1
        elif t is None:
            f.write(k + '\n')
        elif s != t:
            diff.append(k)
r.ok(f'objects: source {n_src}, target {n_tgt}')
r.check(not diff, f'same key, different size: {len(diff)} {diff[:3]}')

path = f'{OUT}/target-message-blobs.json'
sampled = True
if PIECE:
    sampled = False
    r.ok(
        'message blobs referenced by the target: not read by piece buckets; '
        'piece bodies downloads every one through Stalwart'
    )
elif not os.path.exists(path):
    r.fail(
        'no target-message-blobs.json: run 03 first, '
        'otherwise nothing proves the referenced blobs exist'
    )
else:
    refs = json.load(open(path))
    sampled = refs.get('bodies_checked') != 'all'
    if not sampled:
        r.ok(
            f'message blobs referenced by the target: {len(refs["blobs"])}; '
            '03 downloaded every one through Stalwart'
        )
    else:
        r.warn(
            f'message blobs referenced by the target: {len(refs["blobs"])}; '
            '03 downloaded only a sample, '
            'so blob presence rests on the key and size comparison below'
        )

n_only = purged = 0
new = []
dm = lines(dm_f)
m = next(dm, None)
for k in lines(only_src):
    n_only += 1
    while m is not None and m < k:
        m = next(dm, None)
    if m == k:
        purged += 1
    elif len(new) < 3:
        new.append(k)
n_new = n_only - purged
hint = (
    '; 03 checked only a sample, so rerun it without VERIFY_SKIP_BODIES '
    'to prove the purged blobs are unreferenced'
)
r.check(
    not n_new and not (sampled and purged),
    f'only on source: {n_only} ({purged} purged by the target, recoverable from versions; '
    f'{n_new} missing from the target with no delete marker) {new}'
    + (hint if sampled and purged else ''),
)
r.ok(f'only on target: {n_only_tgt} (written by the target since the copy)')
r.done()
