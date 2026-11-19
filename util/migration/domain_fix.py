#!/usr/bin/env python3
import argparse
import importlib.util
import json
import pathlib
import sys

_spec = importlib.util.spec_from_file_location(
    'domains', pathlib.Path(__file__).resolve().parent / 'domains.py'
)
domains = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(domains)


def refs(value, cid):
    if isinstance(value, dict):
        return sum(refs(v, cid) for v in value.values()) + sum(1 for k in value if k == '#' + cid)
    if isinstance(value, list):
        return sum(refs(v, cid) for v in value)
    return 1 if value == '#' + cid else 0


def addresses(value):
    if isinstance(value, dict):
        for v in value.values():
            yield from addresses(v)
    elif isinstance(value, list):
        for v in value:
            yield from addresses(v)
    elif isinstance(value, str) and '@' in value:
        yield value


def main(argv=None):
    ap = argparse.ArgumentParser(
        description='Drop the Domain records Stalwart 0.16 refuses (an invalid name, or one name '
        'present both in Unicode and in punycode) and their DKIM signatures from a '
        'migrate_v016.py convert export.'
    )
    ap.add_argument('--export', required=True)
    ap.add_argument('--output')
    ap.add_argument('--dropped-list')
    ap.add_argument('--principals')
    a = ap.parse_args(argv)
    path = pathlib.Path(a.export)
    ops = [json.loads(x) for x in path.read_text(encoding='utf-8').splitlines() if x.strip()]
    dom = [op for op in ops if op.get('object') == 'Domain' and op.get('@type') == 'create']
    records = {cid: rec for op in dom for cid, rec in op['value'].items()}
    names = {cid: rec.get('name', '') for cid, rec in records.items()}
    dups, bad = domains.survey(names)
    drop = dict(bad)
    for cids in dups.values():
        for cid in cids:
            drop[cid] = 'the same name as another Domain after IDNA (Unicode and punycode)'
    lines, gone, used = [], 0, 0
    for cid in sorted(drop):
        users = sorted(
            {
                op.get('object')
                for op in ops
                if op.get('object') not in ('Domain', 'DkimSignature') and refs(op['value'], cid)
            }
        )
        if users:
            used += 1
            print(
                'error: Domain %r is refused by Stalwart 0.16 (%s) and still used by %s: '
                'fix it on legacy, then dump again' % (names[cid], drop[cid], ', '.join(users)),
                file=sys.stderr,
            )
    if a.principals:
        raw = json.loads(pathlib.Path(a.principals).read_text(encoding='utf-8'))
        items = raw if isinstance(raw, list) else raw.get('items', raw)
        forms = {}
        for cid in drop:
            for form in {names[cid].strip().lower(), domains.to_ascii(names[cid])}:
                if form:
                    forms[form] = cid
        for p in items if isinstance(items, list) else []:
            hits = set()
            for addr in addresses(p):
                host = addr.rpartition('@')[2].strip().lower()
                if host in forms or domains.to_ascii(host) in forms:
                    hits.add(forms.get(host) or forms[domains.to_ascii(host)])
            for cid in sorted(hits):
                used += 1
                print(
                    'error: Domain %r is refused by Stalwart 0.16 (%s) and still used by an '
                    'address of legacy principal %s (a catch-all or an address convert leaves '
                    'out): fix it on legacy, then dump again'
                    % (names[cid], drop[cid], p.get('id')),
                    file=sys.stderr,
                )
    if used:
        return 1
    for cid in sorted(drop):
        for op in ops:
            if op.get('object') != 'DkimSignature':
                continue
            for k in sorted(k for k, r in op['value'].items() if r.get('domainId') == '#' + cid):
                lines.append(
                    'warning: dropping DkimSignature %s: its domain %r is dropped' % (k, names[cid])
                )
                del op['value'][k]
                gone += 1
        lines.append(
            'warning: dropping Domain %r: Stalwart 0.16 refuses it (%s)' % (names[cid], drop[cid])
        )
        for op in dom:
            op['value'].pop(cid, None)
    ops = [op for op in ops if op.get('@type') != 'create' or op['value']]
    out = pathlib.Path(a.output or a.export)
    tmp = out.with_name(out.name + '.tmp')
    tmp.write_text(
        ''.join(json.dumps(op, ensure_ascii=False) + '\n' for op in ops), encoding='utf-8'
    )
    tmp.replace(out)
    if a.dropped_list:
        kept = sum(
            len(op['value'])
            for op in ops
            if op.get('object') == 'DkimSignature' and op.get('@type') == 'create'
        )
        listed = pathlib.Path(a.dropped_list)
        part = listed.with_name(listed.name + '.tmp')
        part.write_text(
            json.dumps(
                {
                    'domains': sorted(names[cid] for cid in drop),
                    'dkim_dropped': gone,
                    'dkim_kept': kept,
                },
                ensure_ascii=False,
            )
            + '\n',
            encoding='utf-8',
        )
        part.replace(listed)
    for line in lines:
        print(line)
    print(
        'domains checked=%d dropped=%d invalid=%d idna_pairs=%d dkim_dropped=%d'
        % (len(records), len(drop), len(bad), len(dups), gone)
    )
    return 0


if __name__ == '__main__':
    sys.exit(main())
