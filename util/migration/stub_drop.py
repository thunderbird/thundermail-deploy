#!/usr/bin/env python3
import argparse
import importlib.util
import json
import os
import pathlib
import shutil
import sys

HERE = pathlib.Path(__file__).resolve().parent
KEPT_KEYS = {'id', 'type', 'name', 'roles'}


def load_converter():
    spec = importlib.util.spec_from_file_location('migrate_v016', HERE / 'migrate_v016.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M = load_converter()


def key(addr):
    if not isinstance(addr, str) or '@' not in addr:
        return None
    local, _, domain = addr.rpartition('@')
    return local, domain.strip().lower()


def empty(v):
    if v is None or v == '' or v == 0 or v == [] or v == {}:
        return True
    return isinstance(v, dict) and not M.pv_list(v) and M.pv_string(v) == '' and not M.pv_int(v)


def member_refs(p):
    names, ids = set(), set()
    for f in ('members', 'memberOf'):
        for ref in M.pv_list(p.get(f)):
            if isinstance(ref, dict) and 'string' in ref:
                names.add(str(ref['string']))
            elif isinstance(ref, dict) and 'integer' in ref:
                ids.add(M.pv_int(ref['integer']))
            elif isinstance(ref, str):
                names.add(ref)
            elif isinstance(ref, int) and not isinstance(ref, bool):
                ids.add(ref)
    return names, ids


def stubs(items):
    holders = {}
    for p in items:
        for e in M.pv_list(p.get('emails')):
            k = key(M.pv_string(e))
            if k is not None:
                holders.setdefault(k, set()).add(id(p))
    listed_names, listed_ids = set(), set()
    for p in items:
        names, ids = member_refs(p)
        listed_names |= names
        listed_ids |= ids
    found = []
    for p in items:
        pid = M.pv_int(p.get('id'))
        if M.pv_string(p.get('type')) != 'individual' or pid is None:
            continue
        name = M.pv_string(p.get('name'))
        k = key(name)
        if k is None:
            continue
        others = holders.get(k, set()) - {id(p)}
        if len(others) != 1:
            continue
        if [M.pv_string(r) for r in M.pv_list(p.get('roles'))] != ['user']:
            continue
        if any(not empty(v) for f, v in p.items() if f not in KEPT_KEYS):
            continue
        if name in listed_names or pid in listed_ids:
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
                'ids': sorted(M.pv_int(p['id']) for p, _ in found),
                'stubs': [
                    {'id': M.pv_int(p['id']), 'name': M.pv_string(p['name']), 'alias_holder': M.pv_int(h.get('id'))}
                    for p, h in sorted(found, key=lambda x: M.pv_int(x[0]['id']))
                ],
            },
            fh,
            ensure_ascii=False,
        )
        fh.write('\n')
    lpart.replace(listed)
    ids = ' '.join(str(i) for i in sorted(M.pv_int(p['id']) for p, _ in found)) or '-'
    check = '' if len(found) == a.expected else ' CHECK'
    print('stubs: dropped=%d expected=%d%s ids %s' % (len(found), a.expected, check, ids))
    return 0


if __name__ == '__main__':
    sys.exit(main())
