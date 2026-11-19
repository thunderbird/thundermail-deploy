"""Body sample for `03_messages.py --sample` (the compare-only mode of window/w6-verify.sh).

The sample is picked on the source first, with bounded queries: block samples of s_email
(TABLESAMPLE SYSTEM, grown until enough rows), each row kept only when its email document is
live in p (a primary key lookup). The target then fetches only those messages: one Email/query
per message by a receivedAt window, and the account's id list only when the window misses.
No source.json, no scan of every source row, no listing of every message of every account.
"""

import datetime
import random
import re
from concurrent.futures import ThreadPoolExecutor

from common import body_sample, dec, download, enc, get_paged, ids_of, query_all, short, sql

HEADER = re.compile(rb'^[!-9;-~]+:', re.M)
PROPS = ['id', 'blobId', 'size', 'receivedAt', 'mailboxIds', 'threadId', 'subject']
MASK = 0xFFFFFFFF
LIVE_KEY = (
    "substring(int8send(s.accid::bigint) from 5) || '\\x0032'::bytea "
    '|| substring(int8send(s.docid::bigint) from 5)'
)


def ts(s):
    return int(datetime.datetime.fromisoformat(s.replace('Z', '+00:00')).timestamp())


def utc(t):
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


SMALL = 200000


def source_sample(n, accounts):
    est = sql("select coalesce(max(reltuples), 0) from pg_class where relname = 's_email'")
    est = float(est[0][0])
    cap = 100.0 if est <= SMALL else 10.0
    pct = min(cap, 100.0 * 20 * n / est) if est > 0 else 0.01
    while True:
        pct = max(pct, 0.0001)
        rows = sql(
            'select s.accid, s.docid, coalesce(s.size, -1), coalesce(s.rcvd, -1) '
            f'from s_email s tablesample system ({pct:.4f}) '
            f'where exists (select 1 from p where p.k = {LIVE_KEY}) '
            f'order by random() limit {20 * n}'
        )
        got = {(int(a), int(d)): (int(z), int(t)) for a, d, z, t in rows if int(a) in accounts}
        if len(got) >= n or pct >= cap:
            keys = random.sample(sorted(got), min(n, len(got)))
            return [(a, d, *got[(a, d)]) for a, d in keys], pct
        pct = min(cap, pct * 4)


def find(src_acc, item):
    lid, doc, _, rcvd = item
    who, aid = src_acc[lid]['name'], enc(lid)
    try:
        if rcvd >= 0:
            window = {'after': utc(rcvd - 2), 'before': utc(rcvd + 3)}
            try:
                ids = query_all('Email/query', {'accountId': aid, 'filter': window}, who)
            except RuntimeError:
                ids = []
            ids = [i for i in ids if dec(i) & MASK == doc]
            if ids:
                return item, get_paged(who, aid, 'Email', ids, PROPS)[0], 'window', None
        ids = [i for i in ids_of(who, aid, 'Email') if dec(i) & MASK == doc]
        return item, (get_paged(who, aid, 'Email', ids, PROPS) or [None])[0], 'listing', None
    except Exception as ex:
        return item, None, 'error', str(ex)[:120]


def check_body(who, aid, e):
    try:
        b = download(who, aid, e['blobId'])
    except Exception as ex:
        return ('error', str(ex)[:60])
    head = b[:16384].split(b'\r\n\r\n', 1)[0].split(b'\n\n', 1)[0]
    shaped = len(HEADER.findall(head)) >= 2
    return ('ok' if len(b) == e['size'] and shaped else 'bad', len(b))


def key(item):
    return f'{item[0]}:{item[1]}'


def compare(r, src_acc):
    n = body_sample(200)
    picked, pct = source_sample(n, set(src_acc))
    r.check(
        len(picked) == n or pct >= 100,
        f'sample: {len(picked)} live messages picked on the source in '
        f'{len({p[0] for p in picked})} accounts (s_email table sample {pct:.4f}%, wanted {n})',
    )
    with ThreadPoolExecutor(4) as ex:
        found = list(ex.map(lambda item: find(src_acc, item), picked))
    failed = [(short(src_acc[i[0]]['name']), err) for i, _, how, err in found if how == 'error']
    r.check(not failed, f'accounts that could not be read: {len(failed)} {failed[:3]}')
    hit = [(i, e) for i, e, _, _ in found if e]
    missing = [key(i) for i, e, how, _ in found if not e and how != 'error']
    by = {h: sum(1 for _, e, how, _ in found if e and how == h) for h in ('window', 'listing')}
    r.check(
        not missing,
        f'messages: sampled {len(picked)}, found on target {len(hit)} '
        f'(by receivedAt {by["window"]}, by account listing {by["listing"]}); '
        f'missing on target {len(missing)} {missing[:5]}',
    )
    indexed = [(i, e) for i, e in hit if i[2] >= 0]
    sz = [key(i) for i, e in indexed if i[2] != e['size']]
    r.check(not sz, f'size mismatches: {len(sz)} of {len(indexed)} sampled {sz[:3]}')
    tm = [key(i) for i, e in indexed if i[3] >= 0 and abs(i[3] - ts(e['receivedAt'])) > 1]
    r.check(not tm, f'receivedAt mismatches: {len(tm)} of {len(indexed)} sampled {tm[:3]}')
    nf = [key(i) for i, e in hit if not e.get('mailboxIds')]
    r.check(not nf, f'messages in no folder: {len(nf)}')
    nt = [key(i) for i, e in hit if not e.get('threadId')]
    r.check(not nt, f'messages without a thread: {len(nt)}')
    with ThreadPoolExecutor(4) as ex:
        res = list(ex.map(lambda x: check_body(src_acc[x[0][0]]['name'], enc(x[0][0]), x[1]), hit))
    errs = sum(1 for s, _ in res if s == 'error')
    bad = sum(1 for s, _ in res if s == 'bad')
    ok = sum(1 for s, _ in res if s == 'ok')
    r.check(
        bool(res) and errs == 0 and bad == 0,
        f'bodies downloaded (random sample): {ok}/{len(res)} of {len(picked)} sampled on the '
        f'source (errors {errs}, wrong size or not an email {bad}), '
        f'{sum(int(n) for s, n in res if s == "ok")} bytes',
    )
    r.done()
