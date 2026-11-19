#!/usr/bin/env python3
import argparse
import concurrent.futures
import importlib.util
import os
import pathlib
import sys
import threading
import urllib.parse

HERE = pathlib.Path(__file__).resolve().parent


def load_tool():
    spec = importlib.util.spec_from_file_location('migrate_v016', HERE / 'migrate_v016.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main(argv=None):
    ap = argparse.ArgumentParser(
        description='migrate_v016.py dump with the password from the environment and the '
        'principal reads fetched concurrently through the tool client.'
    )
    ap.add_argument('--settings', required=True)
    ap.add_argument('--principals', required=True)
    ap.add_argument('--workers', type=int, default=8)
    a = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(line_buffering=True)
    missing = [
        n
        for n in ('OLD_STALWART_URL', 'OLD_STALWART_USER', 'OLD_STALWART_PASSWORD')
        if not os.environ.get(n)
    ]
    if missing:
        print('error: missing in the environment: ' + ' '.join(missing), file=sys.stderr)
        return 2
    url = os.environ['OLD_STALWART_URL'].rstrip('/')
    if urllib.parse.urlsplit(url).hostname not in ('127.0.0.1', 'localhost', '::1'):
        print('error: OLD_STALWART_URL is not loopback: credentials not sent', file=sys.stderr)
        return 2
    if a.workers < 1 or a.workers > 32:
        print('error: --workers must be 1 to 32', file=sys.stderr)
        return 2
    user, password = os.environ['OLD_STALWART_USER'], os.environ['OLD_STALWART_PASSWORD']
    m = load_tool()
    request = m.StalwartClient._request
    get_principal = m.StalwartClient.get_principal
    list_names = m.StalwartClient.list_principal_names

    def get_only(self, method, path, **kw):
        if method != 'GET':
            raise SystemExit('REFUSED: non-GET request to legacy')
        return request(self, method, path, **kw)

    cache = {}
    local = threading.local()

    def client():
        if not hasattr(local, 'client'):
            local.client = m.StalwartClient(url, username=user, password=password, verify=False)
        return local.client

    def fetch(name):
        try:
            return name, (True, get_principal(client(), name))
        except Exception as exc:
            return name, (False, exc)

    def prefetch(self):
        names = list_names(self)
        unique = list(dict.fromkeys(name for _, name in names))
        print(f'  prefetching {len(unique)} principals with {a.workers} workers', file=sys.stderr)
        with concurrent.futures.ThreadPoolExecutor(max_workers=a.workers) as ex:
            for i, (name, result) in enumerate(ex.map(fetch, unique), 1):
                cache[name] = result
                if i % 500 == 0 or i == len(unique):
                    print(f'  prefetched {i}/{len(unique)}', file=sys.stderr)
        return names

    def cached(self, name):
        if name not in cache:
            return get_principal(self, name)
        ok, value = cache[name]
        if not ok:
            raise value
        return value

    m.StalwartClient._request = get_only
    m.StalwartClient.list_principal_names = prefetch
    m.StalwartClient.get_principal = cached
    return m.main(
        [
            'dump',
            '--url',
            url,
            '--username',
            user,
            '--password',
            password,
            '--settings',
            a.settings,
            '--principals',
            a.principals,
        ]
    )


if __name__ == '__main__':
    sys.exit(main())
