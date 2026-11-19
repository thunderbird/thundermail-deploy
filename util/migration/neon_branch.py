#!/usr/bin/env python3
import argparse
import datetime
import json
import os
import pathlib
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = 'https://console.neon.tech/api/v2'
SLEEP = float(os.environ.get('NEON_POLL_SECONDS', '2'))
ID = re.compile(r'[a-z0-9][a-z0-9-]{0,62}')
ROLE = re.compile(r'[A-Za-z_][A-Za-z0-9_]{0,62}')
LSN = re.compile(r'[0-9A-Fa-f]{1,8}/[0-9A-Fa-f]{1,8}')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None


OPENER = urllib.request.build_opener(NoRedirect)


def api_base():
    base = os.environ.get('NEON_API_BASE', API).rstrip('/')
    if base == API:
        return base
    if os.environ.get('NEON_TEST') != '1' or urllib.parse.urlsplit(base).hostname not in (
        '127.0.0.1',
        'localhost',
    ):
        sys.exit(
            'NEON_API_BASE is not the Neon API (a loopback base needs NEON_TEST=1): key not sent'
        )
    return base


def plain_id(v, what):
    if not ID.fullmatch(str(v or '')):
        sys.exit(f'REFUSED: {what} {v!r} is not a plain Neon id or name')
    return v


def api_key(project_var):
    for name in ('NEON_API_KEY_' + project_var, 'NEON_API_KEY'):
        if os.environ.get(name):
            return os.environ[name]
    p = pathlib.Path(os.environ.get('NEON_API_ENV', '~/.neon-api.env')).expanduser()
    if p.exists():
        found = {}
        for line in p.read_text().splitlines():
            m = re.match(r'\s*(?:export\s+)?(NEON_API_KEY(?:_[A-Z_]+)?)=(.*)$', line)
            if m:
                found[m.group(1)] = m.group(2).strip('\'" ')
        for name in ('NEON_API_KEY_' + project_var, 'NEON_API_KEY'):
            if found.get(name):
                return found[name]
    sys.exit('no Neon API key: NEON_API_KEY or ~/.neon-api.env')


class Neon:
    def __init__(self, project, parent, key):
        self.project, self.parent, self.key, self.base = (
            plain_id(project, 'project'),
            parent,
            key,
            api_base(),
        )
        self.child = None

    def resolve_parent(self):
        name = plain_id(self.parent, 'parent')
        hits = [b for b in self.branches() if name in (b.get('id'), b.get('name'))]
        if len(hits) != 1:
            sys.exit(f'parent branch {name} matches {len(hits)} branches')
        self.parent = plain_id(hits[0]['id'], 'parent id')

    def call(self, method, path, body=None):
        if method != 'GET':
            seg = path.split('/')
            create = method == 'POST' and seg == ['', 'projects', self.project, 'branches']
            child = (
                self.child is not None
                and self.child != self.parent
                and len(seg) >= 5
                and seg[:4] == ['', 'projects', self.project, 'branches']
                and seg[4] == self.child
                and '..' not in seg
                and '.' not in seg
            )
            if not (create or child) or self.parent in seg:
                sys.exit(f'REFUSED write: {method} {path}')
        data = json.dumps(body).encode() if body is not None else None
        for attempt in range(30):
            req = urllib.request.Request(
                self.base + path,
                method=method,
                data=data,
                headers={
                    'Authorization': 'Bearer ' + self.key,
                    'Accept': 'application/json',
                    'Content-Type': 'application/json',
                },
            )
            try:
                with OPENER.open(req, timeout=120) as r:
                    raw = r.read()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as e:
                text = e.read().decode(errors='replace')[:300]
                if e.code == 423 and attempt < 29:
                    print(f'project locked (HTTP 423) on {method} {path}: retry {attempt + 1}')
                    time.sleep(SLEEP)
                    continue
                sys.exit(f'HTTP {e.code} on {method} {path}: {text}')
            except urllib.error.URLError as e:
                sys.exit(f'Neon API unreachable on {method} {path}: {e.reason}')
        sys.exit(f'project still locked after 30 tries on {method} {path}')

    def branches(self):
        return self.call('GET', f'/projects/{self.project}/branches').get('branches', [])

    def branch(self, bid):
        return self.call('GET', f'/projects/{self.project}/branches/{bid}')['branch']

    def by_name(self, name):
        hits = [b for b in self.branches() if b.get('name') == name]
        if len(hits) > 1:
            sys.exit(f'{len(hits)} branches are named {name}')
        return hits[0] if hits else None

    def endpoints(self, bid):
        eps = self.call('GET', f'/projects/{self.project}/endpoints').get('endpoints', [])
        return [e for e in eps if e.get('branch_id') == bid]

    def quiet(self, what):
        for _ in range(int(600 / max(SLEEP, 0.01))):
            ops = self.call('GET', f'/projects/{self.project}/operations?limit=50').get(
                'operations', []
            )
            if not [o for o in ops if o.get('status') in ('running', 'scheduling', 'cancelling')]:
                return
            time.sleep(SLEEP)
        sys.exit(f'{what}: project operations still running after 600 s')

    def settle(self, bid, what):
        for _ in range(int(600 / max(SLEEP, 0.01))):
            b = self.branch(bid)
            ops = self.call('GET', f'/projects/{self.project}/operations?limit=50').get(
                'operations', []
            )
            mine = [o for o in ops if o.get('branch_id') == bid]
            failed = [o for o in mine if o.get('status') in ('failed', 'error')]
            if failed:
                sys.exit(
                    f'{what}: operation {failed[0].get("action")} on {bid} is {failed[0]["status"]}'
                )
            busy = [o for o in mine if o.get('status') not in ('finished', 'skipped', 'cancelled')]
            if b.get('current_state') == 'ready' and not busy:
                return b
            time.sleep(SLEEP)
        sys.exit(f'{what}: branch {bid} not ready after 600 s')

    def check_child(self, b, name):
        if b['id'] == self.parent or b.get('default') or b.get('protected'):
            sys.exit(f'REFUSED: branch {name} is the parent, the default or a protected branch')
        if b.get('parent_id') != self.parent:
            sys.exit(f'REFUSED: branch {name} is not a child of the parent branch')


def line(b):
    return (
        f'branch {b.get("name")} state={b.get("current_state")} '
        f'last_reset_at={b.get("last_reset_at") or "-"} parent_lsn={b.get("parent_lsn") or "-"} '
        f'parent_timestamp={b.get("parent_timestamp") or "-"} '
        f'created_at={b.get("created_at") or "-"}'
    )


def stamp(t):
    try:
        d = datetime.datetime.fromisoformat(str(t).replace('Z', '+00:00'))
    except ValueError:
        return None
    return d if d.tzinfo else None


def later(t, after):
    a, b = stamp(t), stamp(after)
    return a is not None and b is not None and a > b


def cmd_check(n, a):
    c, src = n.by_name(a.cutover), n.by_name(a.source)
    bad = []
    for name, b in ((a.cutover, c), (a.source, src)):
        if not b:
            bad.append(f'{name} not found')
            continue
        n.check_child(b, name)
        print(line(b))
        if b.get('current_state') != 'ready':
            bad.append(f'{name} is {b.get("current_state")}, not ready')
    if c and not later(c.get('last_reset_at'), a.after):
        bad.append(
            f'{a.cutover} last_reset_at {c.get("last_reset_at")} is not later than {a.after}'
        )
    if src and not later(src.get('created_at'), a.after):
        bad.append(f'{a.source} created_at {src.get("created_at")} is not later than {a.after}')
    lsn = [b.get('parent_lsn') for b in (c, src) if b]
    if len(lsn) == 2 and (not lsn[0] or lsn[0] != lsn[1]):
        bad.append(f'parent_lsn differs: {a.cutover}={lsn[0] or "-"} {a.source}={lsn[1] or "-"}')
    if bad:
        print('branch check FAILED: ' + '; '.join(bad))
        return 1
    print(f'branch check: both ready, later than the legacy stop {a.after}, parent_lsn {lsn[0]}')
    return 0


def cmd_show(n, a):
    for name in a.branch:
        b = n.by_name(name)
        print(line(b) if b else f'branch {name} not found')


def cmd_hosts(n, a):
    b = n.by_name(a.branch)
    if not b:
        sys.exit(f'branch {a.branch} not found')
    n.check_child(b, a.branch)
    rw = [e for e in n.endpoints(n.parent) if e.get('type') == 'read_write']
    mine = [e for e in n.endpoints(b['id']) if e.get('type') == 'read_write']
    if len(rw) != 1 or len(mine) != 1:
        sys.exit('want one read_write endpoint on the parent and one on the branch')
    if rw[0]['host'] == mine[0]['host']:
        sys.exit('REFUSED: the branch endpoint host equals the parent endpoint host')
    print(f'parent_host={rw[0]["host"]}')
    print(f'branch_host={mine[0]["host"]}')


def cmd_reset(n, a):
    b = n.by_name(a.branch)
    if not b:
        sys.exit(f'branch {a.branch} not found: it is created before the window')
    n.check_child(b, a.branch)
    n.child = plain_id(b['id'], 'branch id')
    print('before: ' + line(b))
    if not a.apply:
        print(f'plan: reset {a.branch} from its parent; nothing written (dry run)')
        return 0
    n.quiet('reset')
    n.call(
        'POST',
        f'/projects/{n.project}/branches/{b["id"]}/restore',
        {'source_branch_id': n.parent},
    )
    before = b.get('parent_lsn')
    b = n.settle(b['id'], 'reset')
    print('after: ' + line(b))
    if not later(b.get('last_reset_at'), a.after):
        print(
            f'reset proof FAILED: last_reset_at {b.get("last_reset_at")} '
            f'is not later than {a.after}'
        )
        return 1
    if not LSN.fullmatch(b.get('parent_lsn') or '') or b.get('parent_lsn') == before:
        print(
            f'reset proof FAILED: parent_lsn {b.get("parent_lsn") or "-"} after the reset '
            f'(before {before or "-"}): no new LSN to create {a.branch}-source at'
        )
        return 1
    print(
        f'reset proof: last_reset_at {b["last_reset_at"]} is later than the legacy stop {a.after}'
    )
    return 0


def cmd_source(n, a):
    old = n.by_name(a.name)
    if not ROLE.fullmatch(a.role) or not ROLE.fullmatch(a.db):
        sys.exit(f'REFUSED: role {a.role!r} or database {a.db!r} is not a plain name')
    if old:
        n.check_child(old, a.name)
        print('existing: ' + line(old))
    if a.lsn is not None and not LSN.fullmatch(a.lsn):
        sys.exit(f'REFUSED: --lsn {a.lsn!r} is not an LSN')
    at = f' at parent_lsn {a.lsn}' if a.lsn else ''
    if not a.apply:
        print(
            f'plan: {"delete and " if old else ""}create {a.name} from the parent{at} with a '
            f'read_only endpoint, reset the password of {a.role} on it; nothing written (dry run)'
        )
        return 0
    if old:
        n.child = plain_id(old['id'], 'branch id')
        n.quiet('delete')
        n.call('DELETE', f'/projects/{n.project}/branches/{old["id"]}')
        for _ in range(int(300 / max(SLEEP, 0.01))):
            if not n.by_name(a.name):
                break
            time.sleep(SLEEP)
        else:
            sys.exit(f'{a.name} still listed 300 s after the delete')
        n.child = None
    r = n.call(
        'POST',
        f'/projects/{n.project}/branches',
        {
            'branch': dict(
                {'parent_id': n.parent, 'name': a.name},
                **({'parent_lsn': a.lsn} if a.lsn else {}),
            ),
            'endpoints': [{'type': 'read_only'}],
        },
    )
    b = r['branch']
    if b['id'] == n.parent or b.get('parent_id') != n.parent:
        sys.exit('REFUSED: the created branch is not a new child of the parent')
    n.child = plain_id(b['id'], 'branch id')
    b = n.settle(b['id'], 'create')
    if a.lsn and b.get('parent_lsn') != a.lsn:
        sys.exit(f'{a.name} has parent_lsn {b.get("parent_lsn") or "-"}, asked for {a.lsn}')
    eps = n.endpoints(b['id'])
    if len(eps) != 1 or eps[0].get('type') != 'read_only':
        sys.exit(f'{a.name} has {len(eps)} endpoint(s), want one read_only endpoint')
    pw = n.call(
        'POST',
        f'/projects/{n.project}/branches/{b["id"]}/roles/'
        f'{urllib.parse.quote(a.role, safe="")}/reset_password',
    )['role']['password']
    n.settle(b['id'], 'password reset')
    if any('\n' in v or '\r' in v for v in (eps[0]['host'], a.db, a.role, pw)):
        sys.exit('REFUSED: a credential value holds a line break')
    out = pathlib.Path(a.out)
    out.touch(mode=0o600)
    os.chmod(out, 0o600)
    out.write_text(
        ''.join(
            f'{k}={v}\n'
            for k, v in (
                ('STALWART_SOURCE_PG_HOST', eps[0]['host']),
                ('STALWART_SOURCE_PG_DB', a.db),
                ('STALWART_SOURCE_PG_USER', a.role),
                ('STALWART_SOURCE_PG_PASSWORD', pw),
            )
        )
    )
    print('created: ' + line(b) + ' endpoint=read_only')
    print(f'credential: role {a.role} password length {len(pw)} written to {out} (mode 600)')
    if not later(b.get('created_at'), a.after):
        print(f'source proof FAILED: created_at {b.get("created_at")} is not later than {a.after}')
        return 1
    print(f'source proof: created_at {b["created_at"]} is later than the legacy stop {a.after}')
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description='Neon branch steps of the tb-prod reset phase.')
    ap.add_argument('--project', required=True)
    ap.add_argument('--parent', required=True)
    ap.add_argument('--key-name', default='STALWART_LEGACY')
    sub = ap.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('show')
    s.add_argument('branch', nargs='+')
    s = sub.add_parser('check')
    s.add_argument('--cutover', required=True)
    s.add_argument('--source', required=True)
    s.add_argument('--after', required=True)
    s = sub.add_parser('hosts')
    s.add_argument('--branch', required=True)
    s = sub.add_parser('reset')
    s.add_argument('--branch', required=True)
    s.add_argument('--after', required=True)
    s.add_argument('--apply', action='store_true')
    s = sub.add_parser('source')
    s.add_argument('--name', required=True)
    s.add_argument('--role', required=True)
    s.add_argument('--db', required=True)
    s.add_argument('--out', required=True)
    s.add_argument('--lsn')
    s.add_argument('--after', required=True)
    s.add_argument('--apply', action='store_true')
    a = ap.parse_args(argv)
    n = Neon(a.project, a.parent, api_key(a.key_name))
    n.resolve_parent()
    rc = {
        'check': cmd_check,
        'show': cmd_show,
        'hosts': cmd_hosts,
        'reset': cmd_reset,
        'source': cmd_source,
    }[a.cmd](n, a)
    return rc or 0


if __name__ == '__main__':
    sys.exit(main())
