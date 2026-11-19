#!/usr/bin/env python3
"""The full compare in pieces, for `run_all.sh --piece buckets|counts|bodies [--resume]`.

buckets: 06 alone, before the others. counts: 01, 04 and 05. bodies: 01 and 03 with every body.
counts and bodies run per batch of VERIFY_BATCH accounts (default 100, ids in order): 01 then
reads only the key range of that batch. Each finished batch is a row of
$VERIFY_PIECES_DIR/<piece>/progress.tsv (default $VERIFY_OUT/pieces), the resume key: --resume
skips the batches whose last row passed and reruns the rest. No new batch starts once the time
used plus the longest batch so far would pass VERIFY_PIECE_BUDGET seconds (default 12600), so a
run ends inside one 4-hour AWS session. Without --resume an earlier piece directory is moved
aside. Exit code: batches with a FAIL in this run.
"""

import os
import pathlib
import shutil
import subprocess
import sys
import time

from common import OUT, principals

HERE = pathlib.Path(__file__).resolve().parent
PIECES = {
    'buckets': ['06_buckets.py'],
    'counts': ['01_source_snapshot.py', '04_collections.py', '05_folders.py'],
    'bodies': ['01_source_snapshot.py', '03_messages.py'],
}
LAST = 0xFFFFFFFF


def number(var, default):
    v = os.environ.get(var, '').strip()
    if not v:
        return default
    if not v.isdigit() or int(v) == 0:
        sys.exit(f'{var} must be a whole number above 0, got {v!r}')
    return int(v)


def make_batches(piece):
    if piece == 'buckets':
        return ['all']
    os.environ.pop('VERIFY_ACCOUNT_RANGE', None)
    ids = sorted(principals()[0])
    size = number('VERIFY_BATCH', 100)
    out, lo = [], 0
    for i in range(0, len(ids), size):
        hi = ids[i + size - 1] if i + size < len(ids) else LAST
        out.append(f'{lo}-{hi}')
        lo = hi + 1
    return out or [f'0-{LAST}']


def last_rows(progress):
    rows = {}
    if progress.exists():
        for line in progress.read_text().splitlines():
            f = line.split('\t')
            if len(f) >= 2:
                rows[f[0]] = f[1]
    return rows


def run_batch(piece, batch, d, resume):
    bd = d if piece == 'buckets' else d / batch
    bd.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, VERIFY_OUT=str(bd), VERIFY_PIECE=piece)
    env.pop('VERIFY_ACCOUNT_RANGE', None)
    env.pop('VERIFY_REUSE_LISTINGS', None)
    if piece == 'buckets':
        if resume:
            env['VERIFY_REUSE_LISTINGS'] = '1'
    else:
        env['VERIFY_ACCOUNT_RANGE'] = batch
    if piece == 'bodies':
        env.pop('VERIFY_SKIP_BODIES', None)
    fails = 0
    for check in PIECES[piece]:
        name = check[:-3]
        out = bd / f'check-{name}.txt'
        with open(out, 'w') as f:
            rc = subprocess.run(
                [sys.executable, check], stdout=f, stderr=subprocess.STDOUT, env=env, cwd=HERE
            ).returncode
        sys.stdout.write(out.read_text())
        sys.stdout.flush()
        shutil.copy(out, f'{OUT}/check-{piece}-{batch}-{name}.txt')
        fails += rc != 0
        if rc and name == '01_source_snapshot':
            print(f'== piece {piece} batch {batch}: 01 failed, the rest of this batch is skipped')
            break
    return fails


def main(argv):
    if not argv or argv[0] not in PIECES or argv[1:] not in ([], ['--resume']):
        print('usage: pieces.py buckets|counts|bodies [--resume]')
        return 99
    piece, resume = argv[0], argv[1:] == ['--resume']
    budget = number('VERIFY_PIECE_BUDGET', 12600)
    d = pathlib.Path(os.environ.get('VERIFY_PIECES_DIR') or f'{OUT}/pieces') / piece
    progress = d / 'progress.tsv'
    if d.exists() and not resume:
        old = d.with_name(f'{piece}.{time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())}')
        d.rename(old)
        print(f'== piece {piece}: earlier run moved to {old}')
    d.mkdir(parents=True, exist_ok=True)
    listed = d / 'batches.txt'
    if not listed.exists():
        listed.write_text(''.join(b + '\n' for b in make_batches(piece)))
    batches = listed.read_text().split()
    rows = last_rows(progress)
    start, longest, failed_now = time.time(), 0.0, 0
    for i, b in enumerate(batches, 1):
        if rows.get(b) == 'pass':
            continue
        used = time.time() - start
        if longest and used + longest > budget:
            print(
                f'== piece {piece}: stopping before batch {b}: {used:.0f} s used, '
                f'the longest batch took {longest:.0f} s, budget {budget} s (VERIFY_PIECE_BUDGET)'
            )
            break
        print(f'== piece {piece} batch {b} ({i} of {len(batches)})', flush=True)
        t0 = time.time()
        fails = run_batch(piece, b, d, resume)
        took = time.time() - t0
        longest = max(longest, took)
        rows[b] = 'fail' if fails else 'pass'
        failed_now += fails != 0
        stamp = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(t0))
        with open(progress, 'a') as f:
            f.write(f'{b}\t{rows[b]}\t{fails}\t{stamp}\t{took:.0f}\n')
    passed = [b for b in batches if rows.get(b) == 'pass']
    failed = [b for b in batches if rows.get(b) == 'fail']
    left = len(batches) - len(passed)
    print(
        f'== piece {piece}: {len(batches)} batches; passed {len(passed)}, failed {len(failed)} '
        f'{failed[:5]}, left {left} (resume key {progress})'
    )
    if left:
        print(f'== piece {piece}: not finished: rerun run_all.sh --piece {piece} --resume')
    return min(failed_now, 98)


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
