#!/usr/bin/env python3
import argparse
import collections
import importlib.util
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent


def load_converter():
    spec = importlib.util.spec_from_file_location('migrate_v016', HERE / 'migrate_v016.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def claims(conv, accounts, mailing_lists):
    owners = collections.defaultdict(list)

    def claim(local, domain_ref, kind, role, cid, name):
        d_cid = domain_ref[1:] if domain_ref.startswith('#') else domain_ref
        owners[(local, d_cid)].append((kind, role, cid, name))

    for cid, obj in accounts.items():
        kind = obj.get('@type', 'Account')
        claim(obj['name'], obj['domainId'], kind, 'primary', cid, obj['name'])
        for alias in obj.get('aliases', {}).values():
            claim(alias['name'], alias['domainId'], kind, 'alias', cid, obj['name'])
    for cid, obj in mailing_lists.items():
        claim(obj['name'], obj['domainId'], 'MailingList', 'primary', cid, obj['name'])
        for alias in obj.get('aliases', {}).values():
            claim(alias['name'], alias['domainId'], 'MailingList', 'alias', cid, obj['name'])
    return {
        '%s@%s' % (k[0], conv.domain_cid_to_name.get(k[1], k[1])): v
        for k, v in owners.items()
        if len(v) > 1
    }


def load_domains():
    spec = importlib.util.spec_from_file_location('domains', HERE / 'domains.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def domain_report(m, conv, by_class):
    names = dict(conv.domain_cid_to_name)
    kept = {c: n for c, n in names.items() if m.is_valid_domain_name(n)}
    for c in sorted(set(names) - set(kept)):
        by_class['Domain:dropped-by-convert'] += 1
        print('domain\tDomain:dropped-by-convert\t%s' % names[c])
    dups, bad = load_domains().survey(kept)
    for a, cids in sorted(dups.items()):
        by_class['Domain:idna-duplicate'] += 1
        print(
            'domain\tDomain:idna-duplicate\t%s\t%s' % (a, '; '.join(kept[c] for c in sorted(cids)))
        )
    for c, why in sorted(bad.items()):
        by_class['Domain:refused-by-0.16'] += 1
        print('domain\tDomain:refused-by-0.16\t%s\t%s' % (kept[c], why))


def main(argv=None):
    ap = argparse.ArgumentParser(
        description='List every address that migrate_v016.py convert would reject as a duplicate.'
    )
    ap.add_argument('--principals', required=True)
    ap.add_argument('--settings', required=True)
    a = ap.parse_args(argv)
    m = load_converter()
    try:
        principals = json.load(open(a.principals, encoding='utf-8'))
        settings = json.load(open(a.settings, encoding='utf-8'))
    except (OSError, ValueError) as exc:
        print('error: %s' % exc, file=sys.stderr)
        return 2
    if not isinstance(principals, list) or not isinstance(settings, dict):
        print('error: principals must be a JSON array and settings a JSON object', file=sys.stderr)
        return 2
    settings = {str(k): ('' if v is None else str(v)) for k, v in settings.items()}
    conv = m.Converter(principals, settings)
    try:
        conv._build_tenants()
        conv._build_domains()
        conv._pick_default_domain()
        accounts = conv._build_accounts()
        mailing_lists = conv._build_mailing_lists()
    except m.ConvertError as exc:
        print('error: %s' % exc, file=sys.stderr)
        return 1
    dups = claims(conv, accounts, mailing_lists)
    by_class = collections.Counter()
    for addr in sorted(dups):
        who = dups[addr]
        cls = ' + '.join(sorted('%s:%s' % (k, r) for k, r, _, _ in who))
        by_class[cls] += 1
        print(
            'duplicate\t%s\t%s\t%s'
            % (cls, addr, '; '.join('%s %s %s (%s)' % (k, r, c, n) for k, r, c, n in who))
        )
    domain_report(m, conv, by_class)
    for cls, n in sorted(by_class.items()):
        print('class\t%s\t%d' % (cls, n))
    print('total\t%d' % len(dups))
    return 3 if dups else 0


if __name__ == '__main__':
    sys.exit(main())
