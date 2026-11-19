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


def tlds():
    return {x.strip() for x in (HERE / 'domain-tlds.txt').read_text().splitlines() if x.strip()}


def to_ascii(name):
    name = ''.join(c for c in str(name) if not c.isspace())
    name = unicodedata.normalize('NFC', name).lower()
    if '.' not in name or name.endswith('.'):
        return None
    out = []
    for label in name.split('.'):
        if not label:
            return None
        if not label.isascii():
            try:
                label = 'xn--' + label.encode('punycode').decode('ascii')
            except UnicodeError:
                return None
        out.append(label)
    return '.'.join(out)


def refused(ascii_name, known):
    if ascii_name is None:
        return 'not a domain name'
    last = ascii_name.rsplit('.', 1)[-1]
    if last in known or last in RESERVED:
        return None
    return 'its last label is no public suffix'


def survey(names):
    known = tlds()
    groups, bad = {}, {}
    for key, name in names.items():
        a = to_ascii(name)
        why = refused(a, known)
        if why:
            bad[key] = why
        else:
            groups.setdefault(a, []).append(key)
    return {a: keys for a, keys in groups.items() if len(keys) > 1}, bad
