import pathlib
import unicodedata

HERE = pathlib.Path(__file__).resolve().parent
RESERVED = {
    'test',
    'localhost',
    'local',
    'internal',
    'lan',
    'home',
    'corp',
    'intranet',
    'private',
    'localdomain',
}
DOTS = ('。', '．', '｡')


def rules():
    return {x.strip() for x in (HERE / 'domain-suffixes.txt').read_text().splitlines() if x.strip()}


def to_ascii(name):
    name = ''.join(c for c in str(name) if not c.isspace())
    for d in DOTS:
        name = name.replace(d, '.')
    name = unicodedata.normalize('NFC', unicodedata.normalize('NFKC', name)).lower()
    if '.' not in name or name.endswith('.') or name.startswith('.'):
        return None
    labels = name.split('.')
    if not name.isascii() and any(lab.startswith('xn--') for lab in labels):
        return None
    out = []
    for label in labels:
        if not label:
            return None
        if not label.isascii():
            try:
                label = 'xn--' + label.encode('punycode').decode('ascii')
            except UnicodeError:
                return None
        out.append(label)
    return '.'.join(out)


def listed(ascii_name, known):
    labels = ascii_name.split('.')
    for k in range(1, len(labels) + 1):
        suffix = '.'.join(labels[-k:])
        if suffix in known or '!' + suffix in known:
            return True
        if k >= 2 and '*.' + '.'.join(labels[-(k - 1) :]) in known:
            return True
    return False


def refused(ascii_name, known):
    if ascii_name is None:
        return 'not a domain name'
    if ascii_name in RESERVED or ascii_name.rsplit('.', 1)[-1] in RESERVED:
        return None
    if listed(ascii_name, known):
        return None
    return 'no public suffix rule matches it'


def survey(names):
    known = rules()
    groups, bad = {}, {}
    for key, name in names.items():
        a = to_ascii(name)
        why = refused(a, known)
        if why:
            bad[key] = why
        else:
            groups.setdefault(a, []).append(key)
    return {a: keys for a, keys in groups.items() if len(keys) > 1}, bad
