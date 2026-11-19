#!/usr/bin/env python3
import argparse
import json
import os
import pathlib
import shutil
import sys

KEPT_KEYS = {'id', 'type', 'name', 'roles'}


def key(addr):
    if not isinstance(addr, str) or '@' not in addr:
        return None
    local, _, domain = addr.rpartition('@')
    return local, domain.strip().lower()


def empty(v):
    return v is None or v == '' or v == 0 or v == [] or v == {}


def member_names(p):
    out = set()
    for f in ('members', 'memberOf'):
        v = p.get(f)
        if isinstance(v, list):
            out.update(x for x in v if isinstance(x, (str, int)))
    return out


def stubs(items):
    holders = {}
    for p in items:
        for e in p.get('emails') or []:
            k = key(e)
            if k is not None:
                holders.setdefault(k, set()).add(id(p))
    listed = set()
    for p in items:
        listed |= member_names(p)
    found = []
    for p in items:
        if p.get('type') != 'individual' or not isinstance(p.get('id'), int):
            continue
        k = key(p.get('name'))
        if k is None:
            continue
        others = holders.get(k, set()) - {id(p)}
        if len(others) != 1:
            continue
        if p.get('roles') != ['user']:
            continue
        if any(not empty(v) for f, v in p.items() if f not in KEPT_KEYS):
            continue
        if p['name'] in listed or p['id'] in listed:
            continue
        holder = next(q for q in items if id(q) in others)
        found.append((p, holder))
    return found


def main(argv=None):
    ap = argparse.ArgumentParser(description='Drop empty OIDC stub accounts from a principals dump.')
    ap.add_argument('--principals', required=True)
    ap.add_argument('--output', required=True)
    ap.add_argument('--dropped-list', required=True)
    ap.add_argument('--expected', type=int, default=0)
    a = ap.parse_args(argv)
    try:
        items = json.load(open(a.principals, encoding='utf-8'))
    except (OSError, ValueError) as exc:
        print('error: %s: %s' % (a.principals, type(exc).__name__), file=sys.stderr)
        return 2
    if not isinstance(items, list):
        print('error: %s is not a JSON array' % a.principals, file=sys.stderr)
        return 2
    found = stubs(items)
    drop = {id(p) for p, _ in found}
    out = pathlib.Path(a.output)
    part = out.with_name(out.name + '.part')
    if drop:
        part.write_text(
            json.dumps([p for p in items if id(p) not in drop], ensure_ascii=False), encoding='utf-8'
        )
    else:
        shutil.copyfile(a.principals, part)
    os.chmod(part, 0o600)
    part.replace(out)
    listed = pathlib.Path(a.dropped_list)
    lpart = listed.with_name(listed.name + '.part')
    fd = os.open(lpart, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as fh:
        json.dump(
            {
                'ids': sorted(p['id'] for p, _ in found),
                'stubs': [
                    {'id': p['id'], 'name': p['name'], 'alias_holder': h.get('id')}
                    for p, h in sorted(found, key=lambda x: x[0]['id'])
                ],
            },
            fh,
            ensure_ascii=False,
        )
        fh.write('\n')
    lpart.replace(listed)
    ids = ' '.join(str(p['id']) for p, _ in sorted(found, key=lambda x: x[0]['id'])) or '-'
    check = '' if len(found) == a.expected else ' CHECK'
    print('stubs: dropped=%d expected=%d%s ids %s' % (len(found), a.expected, check, ids))
    return 0


if __name__ == '__main__':
    sys.exit(main())
