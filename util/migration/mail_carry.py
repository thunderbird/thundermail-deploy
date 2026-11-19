#!/usr/bin/env python3
import argparse
import base64
import collections
import concurrent.futures
import datetime
import hashlib
import json
import os
import pathlib
import re
import ssl
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

UTC = datetime.timezone.utc
ALPHABET = 'abcdefghijklmnopqrstuvwxyz792013'
LOOPBACK = ('127.0.0.1', 'localhost', '::1')
CORE = ['urn:ietf:params:jmap:core', 'urn:ietf:params:jmap:mail']
GET_PAGE = 500
QUERY_PAGE = 1000
MAIL_TYPES = ('User', 'Group')
FULL = ['blobId', 'size', 'receivedAt', 'mailboxIds', 'keywords', 'messageId']
ID = re.compile(r'[A-Za-z0-9_-]{1,255}')
FILE = re.compile(r'[0-9]{1,10}/[A-Za-z0-9_-]{1,255}\.eml')


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


def parse_time(text):
    try:
        dt = datetime.datetime.fromisoformat(text.replace('Z', '+00:00'))
    except (AttributeError, ValueError):
        raise Usage('%r is not a UTC time like 2026-11-19T05:10:00Z' % (text,)) from None
    if dt.tzinfo is None:
        raise Usage('%r has no time zone' % text)
    return dt.astimezone(UTC)


def enc(n):
    if n == 0:
        return 'a'
    m = (1 << 64) - 1
    out = ''
    i = n >> 60
    n = ((n << 4) | 1) & m
    if i == 0:
        n = (n << ((64 - n.bit_length()) // 5 * 5)) & m
    else:
        out += ALPHABET[i]
    while n != 1 << 60:
        out += ALPHABET[n >> 59]
        n = (n << 5) & m
    return out


def dec(s):
    n = 0
    for ch in s:
        n = (n << 5) | ALPHABET.index(ch)
    return n


def loopback(name, url):
    u = urllib.parse.urlsplit(url or '')
    if u.scheme not in ('http', 'https') or u.hostname not in LOOPBACK or u.username is not None:
        raise Stop('%s is not a loopback URL without user info: credentials not sent' % name)
    return u


def safe_id(value, what):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise Stop('%s is not a JMAP id' % what)
    return value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Stop(
            'refusing redirect %s from %s' % (code, urllib.parse.urlsplit(req.full_url).path)
        )


class Jmap:
    def __init__(self, var, caps):
        url = os.environ.get(var + '_URL', '')
        user = os.environ.get(var + '_USER', '')
        password = os.environ.get(var + '_PASSWORD', '')
        miss = [
            var + s for s, v in (('_URL', url), ('_USER', user), ('_PASSWORD', password)) if not v
        ]
        if miss:
            raise Need('empty %s in the env file' % ' '.join(miss))
        self.split = loopback(var + '_URL', url)
        self.url = url.rstrip('/')
        self.caps = caps
        self.auth = 'Basic ' + base64.b64encode(('%s:%s' % (user, password)).encode()).decode()
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect, urllib.request.HTTPSHandler(context=ctx)
        )

    def raw(self, path, data=None, ctype='application/json', timeout=180, retry=True):
        for attempt in range(6 if retry else 1):
            req = urllib.request.Request(
                self.url + path, data=data, headers={'Authorization': self.auth}
            )
            if data is not None:
                req.add_header('Content-Type', ctype)
            try:
                with self.opener.open(req, timeout=timeout) as r:
                    return r.read()
            except urllib.error.HTTPError as e:
                body = e.read()[:2000]
                again = e.code in (429, 500, 502, 503, 504) or (
                    e.code == 400 and b'limit' in body.lower()
                )
                if retry and again and attempt < 5:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise Stop('HTTP %s from %s' % (e.code, path.split('?')[0]))
            except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
                if retry and attempt < 5:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise Stop(
                    '%s%s did not answer: %s'
                    % (self.url, path.split('?')[0], type(getattr(e, 'reason', e)).__name__)
                )
        raise Stop('%s%s did not answer and is not retried' % (self.url, path.split('?')[0]))

    def call(self, calls, retry=True):
        body = json.dumps({'using': self.caps, 'methodCalls': calls}).encode()
        return json.loads(self.raw('/jmap', body, retry=retry))['methodResponses']

    def one(self, name, args, retry=True):
        r = self.call([[name, args, '0']], retry)[0]
        if r[0] == 'error':
            raise Stop('%s: %s' % (name, r[1].get('type') if isinstance(r[1], dict) else '?'))
        return r[1]

    def query_all(self, name, args):
        ids, total = [], None
        while True:
            r = self.one(
                name, {**args, 'position': len(ids), 'limit': QUERY_PAGE, 'calculateTotal': True}
            )
            batch, total = r['ids'], r.get('total', total)
            ids += batch
            if not batch or (total is not None and len(ids) >= total):
                break
        if total is not None and len(ids) != total:
            raise Stop('%s: got %d ids, the server reports %d' % (name, len(ids), total))
        return ids

    def get(self, name, account, ids, props):
        out = []
        for i in range(0, len(ids), GET_PAGE):
            args = {'ids': ids[i : i + GET_PAGE], 'properties': props}
            if account is not None:
                args['accountId'] = account
            out += self.one(name, args)['list']
        return out


def mailbox_paths(boxes):
    by_id = {b['id']: b for b in boxes}

    def path(b):
        names, seen = [], set()
        while b and b['id'] not in seen:
            seen.add(b['id'])
            names.insert(0, b.get('name') or '')
            b = by_id.get(b.get('parentId'))
        return names

    return {b['id']: {'role': b.get('role') or None, 'path': path(b)} for b in boxes}


def private_dir(path, create):
    p = pathlib.Path(path)
    if create:
        if p.exists():
            raise Stop('%s exists: an export never writes into an earlier one' % p)
        p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        p.mkdir(mode=0o700)
    try:
        st = p.lstat()
    except OSError:
        raise Need('%s does not exist' % p) from None
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise Stop('%s is not a directory of %d with mode 700' % (p, os.getuid()))
    return p


def timing_row(name, start, metric, value):
    path = os.environ.get('TIMING_TSV')
    if not path:
        print('no TIMING_TSV: row %s not written' % name)
        return
    with open(path, 'a') as fh:
        fh.write(
            '%s\t%s\t%s\t%s\t%s\n' % (name, z(start), z(datetime.datetime.now(UTC)), metric, value)
        )
    print('row %s %s=%s written to %s' % (name, metric, value, path))


def export_account(j, account, since):
    aid = safe_id(account['id'], 'account id')
    ids = j.query_all('Email/query', {'accountId': aid})
    dates = j.get('Email/get', aid, ids, ['receivedAt'])
    picked = [e['id'] for e in dates if parse_time(e['receivedAt']) >= since]
    if not picked:
        return account, len(ids), []
    full = j.get('Email/get', aid, picked, FULL)
    boxes = mailbox_paths(
        j.one('Mailbox/get', {'accountId': aid, 'properties': ['name', 'role', 'parentId']})['list']
    )
    msgs = []
    for e in full:
        msgs.append(
            {
                'account': dec(aid),
                'address': account.get('emailAddress') or '',
                'email': safe_id(e['id'], 'email id'),
                'blob': e['blobId'],
                'size': e['size'],
                'receivedAt': e['receivedAt'],
                'keywords': sorted(k for k, v in (e.get('keywords') or {}).items() if v),
                'messageId': e.get('messageId') or [],
                'mailboxes': [
                    boxes[m] for m, v in (e.get('mailboxIds') or {}).items() if v and m in boxes
                ],
            }
        )
    return account, len(ids), msgs


def cmd_export(a):
    since = parse_time(a.since)
    start = datetime.datetime.now(UTC)
    out = pathlib.Path(a.out)
    j = Jmap('STALWART', CORE + ['urn:stalwart:jmap'])
    accounts = [
        x
        for x in j.get(
            'x:Account/get',
            None,
            j.query_all('x:Account/query', {}),
            ['name', 'emailAddress', '@type'],
        )
        if x.get('@type', 'User') in MAIL_TYPES
    ]
    print(
        'tb-prod accounts with mailboxes: %d; selecting receivedAt >= %s from the message store'
        % (len(accounts), z(since))
    )
    with concurrent.futures.ThreadPoolExecutor(a.workers) as ex:
        results = list(ex.map(lambda x: export_account(j, x, since), accounts))
    results.sort(key=lambda r: dec(r[0]['id']))
    scanned = sum(r[1] for r in results)
    msgs = [m for r in results for m in r[2]]
    if a.apply:
        out = private_dir(out, True)
    total = 0
    with open(out / 'manifest.jsonl', 'w') if a.apply else open(os.devnull, 'w') as man:
        for account, _, ms in results:
            if not ms:
                continue
            size = sum(m['size'] for m in ms)
            total += size
            print('account=%d messages=%d bytes=%d' % (dec(account['id']), len(ms), size))
            if not a.apply:
                continue
            d = out / str(ms[0]['account'])
            d.mkdir(mode=0o700, exist_ok=True)
            for m in ms:
                body = j.raw(
                    '/jmap/download/%s/%s/m.eml'
                    % (account['id'], urllib.parse.quote(m['blob'], safe='')),
                    timeout=300,
                )
                if len(body) != m['size']:
                    raise Stop(
                        'account=%d email=%s: downloaded %d bytes, the store says %d'
                        % (m['account'], m['email'], len(body), m['size'])
                    )
                f = d / ('%s.eml' % m['email'])
                fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'wb') as fh:
                    fh.write(body)
                m['file'] = '%s/%s' % (d.name, f.name)
                m['sha256'] = hashlib.sha256(body).hexdigest()
                man.write(json.dumps(m, sort_keys=True) + '\n')
    with_mail = sum(1 for r in results if r[2])
    summary = {
        'since': z(since),
        'accounts': len(accounts),
        'with_mail': with_mail,
        'messages': len(msgs),
        'bytes': total,
        'scanned': scanned,
    }
    if a.apply:
        (out / 'summary.json').write_text(json.dumps(summary, sort_keys=True) + '\n')
    else:
        print(
            'plan: write %d messages (%d bytes) of %d accounts to %s (mode 700), nothing written'
            % (len(msgs), total, with_mail, out)
        )
    print(
        'mail-export: since=%s accounts=%d with_mail=%d messages=%d bytes=%d scanned=%d '
        'mode=%s dir=%s'
        % (
            z(since),
            len(accounts),
            with_mail,
            len(msgs),
            total,
            scanned,
            'apply' if a.apply else 'dry-run',
            out,
        )
    )
    if a.apply:
        timing_row('mail-export', start, 'messages', len(msgs))
    return 0


def legacy_principals(j):
    items, page, total = [], 1, None
    while True:
        q = urllib.parse.urlencode(
            {'types': 'individual,group', 'limit': 500, 'page': page, 'fields': 'name,emails,type'}
        )
        data = json.loads(j.raw('/api/principal?' + q)).get('data') or {}
        batch, total = data.get('items') or [], data.get('total', total)
        items += batch
        if not batch or (total is not None and len(items) >= total):
            break
        page += 1
    if total is not None and len(items) != total:
        raise Stop('/api/principal: got %d principals, legacy reports %d' % (len(items), total))
    by_addr = collections.defaultdict(list)
    for p in items:
        for e in p.get('emails') or []:
            by_addr[e.strip().lower()].append(p['id'])
    return by_addr


class Legacy:
    def __init__(self, j, account, apply):
        self.j, self.acc, self.apply = j, account, apply
        boxes = mailbox_paths(
            j.one(
                'Mailbox/get', {'accountId': account, 'properties': ['name', 'role', 'parentId']}
            )['list']
        )
        self.role = {v['role']: k for k, v in boxes.items() if v['role']}
        self.path = {tuple(v['path']): k for k, v in boxes.items()}
        if 'inbox' not in self.role:
            raise Stop('legacy account %s has no Inbox' % account)
        self.keys = {}
        self.created = 0

    def mailbox(self, box):
        if box.get('role') and box['role'] in self.role:
            return self.role[box['role']]
        path = tuple(box.get('path') or ())
        if not path:
            return self.role['inbox']
        if path in self.path:
            return self.path[path]
        parent = self.mailbox({'path': path[:-1]}) if len(path) > 1 else None
        self.created += 1
        if not self.apply:
            mid = 'new-%d' % self.created
        else:
            r = self.j.one(
                'Mailbox/set',
                {'accountId': self.acc, 'create': {'m': {'name': path[-1], 'parentId': parent}}},
                retry=False,
            )
            if 'm' not in (r.get('created') or {}):
                raise Stop('Mailbox/set in legacy account %s: %s' % (self.acc, err_type(r, 'm')))
            mid = r['created']['m']['id']
        self.path[path] = mid
        return mid

    def present(self, mailbox):
        if mailbox not in self.keys:
            keys = set()
            if not mailbox.startswith('new-'):
                ids = self.j.query_all(
                    'Email/query', {'accountId': self.acc, 'filter': {'inMailbox': mailbox}}
                )
                for e in self.j.get(
                    'Email/get', self.acc, ids, ['receivedAt', 'size', 'messageId']
                ):
                    keys.add(key(e))
            self.keys[mailbox] = keys
        return self.keys[mailbox]


def err_type(r, cid):
    e = (r.get('notCreated') or {}).get(cid)
    return e.get('type', '?') if isinstance(e, dict) else 'no answer'


def read_export(src, m):
    if not FILE.fullmatch(str(m.get('file'))) or not isinstance(m.get('size'), int):
        return None
    try:
        fd = os.open(src / m['file'], os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    with os.fdopen(fd, 'rb') as fh:
        body = fh.read()
    if hashlib.sha256(body).hexdigest() != m.get('sha256') or len(body) != m['size']:
        return None
    return body


def key(m):
    return (z(parse_time(m['receivedAt'])), m['size'], tuple(m.get('messageId') or ()))


def import_one(j, leg, src, m):
    boxes = [leg.mailbox(b) for b in m['mailboxes']] or [leg.role['inbox']]
    seen = leg.present(boxes[0])
    if key(m) in seen:
        return 'present', None
    if not leg.apply:
        seen.add(key(m))
        return 'delivered', None
    body = read_export(src, m)
    if body is None:
        return 'failed', 'the file differs from the export manifest'
    blob = json.loads(j.raw('/jmap/upload/%s/' % leg.acc, body, 'message/rfc822', timeout=300))[
        'blobId'
    ]
    r = j.one(
        'Email/import',
        {
            'accountId': leg.acc,
            'emails': {
                'e': {
                    'blobId': blob,
                    'mailboxIds': {b: True for b in boxes},
                    'keywords': {k: True for k in m['keywords']},
                    'receivedAt': m['receivedAt'],
                }
            },
        },
        retry=False,
    )
    made = (r.get('created') or {}).get('e')
    if not made:
        return 'failed', 'Email/import: %s' % err_type(r, 'e')
    back = j.get('Email/get', leg.acc, [made['id']], ['size'])
    if not back or back[0].get('size') != m['size']:
        return 'failed', 'read back after the import differs'
    seen.add(key(m))
    return 'delivered', made['id']


def cmd_import(a):
    start = datetime.datetime.now(UTC)
    src = private_dir(a.source, False)
    try:
        summary = json.loads((src / 'summary.json').read_text())
        msgs = [
            json.loads(x) for x in (src / 'manifest.jsonl').read_text().splitlines() if x.strip()
        ]
    except (OSError, ValueError) as exc:
        raise Need(
            '%s holds no complete export (summary.json, manifest.jsonl): %s' % (src, exc)
        ) from None
    bad = [
        i
        for i, m in enumerate(msgs)
        if not isinstance(m.get('account'), int) or not FILE.fullmatch(str(m.get('file')))
    ]
    if bad:
        raise Stop('%s: manifest line %d is not an export line' % (src, bad[0] + 1))
    if summary.get('messages') != len(msgs):
        raise Stop(
            '%s: summary.json counts %s messages, the manifest holds %d'
            % (src, summary.get('messages'), len(msgs))
        )
    j = Jmap('OLD_STALWART', CORE)
    tgt = os.environ.get('STALWART_URL')
    if tgt:
        t = urllib.parse.urlsplit(tgt)
        if (t.port or {'http': 80, 'https': 443}.get(t.scheme)) == (
            j.split.port or {'http': 80, 'https': 443}.get(j.split.scheme)
        ):
            raise Stop(
                'OLD_STALWART_URL has the port of STALWART_URL: refusing to import into tb-prod'
            )
    by_addr = legacy_principals(j)
    groups = collections.OrderedDict()
    for m in msgs:
        groups.setdefault((m['account'], m['address'].strip().lower()), []).append(m)
    totals = collections.Counter()
    log = (
        open(src / ('import-%s.tsv' % start.strftime('%Y%m%dT%H%M%SZ')), 'w')
        if a.apply
        else open(os.devnull, 'w')
    )
    with log:
        for (account, addr), ms in groups.items():
            ids = by_addr.get(addr) or []
            if len(ids) != 1 or not isinstance(ids[0], int):
                totals['missing'] += len(ms)
                print(
                    'account=%d messages=%d MISSING: %s'
                    % (
                        account,
                        len(ms),
                        'no legacy principal holds its address'
                        if not ids
                        else '%d legacy principals hold its address' % len(ids),
                    )
                )
                continue
            leg = Legacy(j, enc(ids[0]), a.apply)
            c = collections.Counter()
            for m in ms:
                res, note = import_one(j, leg, src, m)
                c[res] += 1
                log.write('%d\t%s\t%d\t%s\t%s\n' % (account, m['email'], ids[0], res, note or ''))
                if res == 'failed':
                    print('account=%d email=%s FAILED: %s' % (account, m['email'], note))
            totals.update(c)
            print(
                'account=%d legacy=%d messages=%d %s=%d present=%d failed=%d %s=%d'
                % (
                    account,
                    ids[0],
                    len(ms),
                    'delivered' if a.apply else 'to_import',
                    c['delivered'],
                    c['present'],
                    c['failed'],
                    'mailboxes_created' if a.apply else 'mailboxes_to_create',
                    leg.created,
                )
            )
    done = totals['delivered'] + totals['present']
    print(
        'mail-import: export=%s messages=%d %s=%d present=%d failed=%d missing=%d%s mode=%s'
        % (
            src.name,
            len(msgs),
            'delivered' if a.apply else 'to_import',
            totals['delivered'],
            totals['present'],
            totals['failed'],
            totals['missing'],
            ' in_legacy=%d' % done if a.apply else '',
            'apply' if a.apply else 'dry-run',
        )
    )
    if totals['failed'] or totals['missing']:
        raise Stop(
            '%d failed, %d without a legacy account: legacy does not hold every exported message'
            % (totals['failed'], totals['missing'])
        )
    if a.apply:
        timing_row('mail-import', start, 'messages', done)
    return 0


def parser():
    ap = Parser(prog='mail_carry.py')
    sub = ap.add_subparsers(dest='cmd', required=True)
    e = sub.add_parser('export')
    e.add_argument('--since', required=True)
    e.add_argument('--out', required=True)
    e.add_argument('--workers', type=int, default=4)
    i = sub.add_parser('import')
    i.add_argument('--from', dest='source', required=True)
    for p in (e, i):
        p.add_argument('--apply', action='store_true')
        p.add_argument('--dry-run', action='store_true')
    return ap


def main(argv=None):
    os.umask(0o077)
    try:
        a = parser().parse_args(argv)
        if a.apply and a.dry_run:
            raise Usage('--apply and --dry-run exclude each other')
        if os.environ.get('TARGET_ENV') != 'tb-prod':
            raise Usage('TARGET_ENV must be tb-prod')
        if a.cmd == 'export' and not 1 <= a.workers <= 4:
            raise Usage(
                '--workers must be 1 to 4 (Stalwart allows 4 concurrent requests per login)'
            )
        return cmd_export(a) if a.cmd == 'export' else cmd_import(a)
    except Usage as exc:
        print('usage: %s' % exc, file=sys.stderr)
        return 64
    except Need as exc:
        print('NEED  %s' % exc)
        return 2
    except Stop as exc:
        print('STOP  %s' % exc)
        return 1
    except KeyboardInterrupt:
        print('STOP  interrupted')
        return 1


if __name__ == '__main__':
    sys.exit(main())
