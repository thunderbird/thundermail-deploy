import hashlib
import importlib.util
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
STUBS = {
    'requests': 'class Session:\n    pass\n',
    'urllib3': (
        'class exceptions:\n    InsecureRequestWarning = Warning\n\n\n'
        'def disable_warnings(*a):\n    pass\n'
    ),
}


def stub_path(root):
    missing = [n for n in STUBS if importlib.util.find_spec(n) is None]
    for n in missing:
        (root / n).mkdir(parents=True, exist_ok=True)
        (root / n / '__init__.py').write_text(STUBS[n])
    return str(root) if missing else None


def user(i, name, *emails, kind='individual'):
    return {
        'id': i,
        'type': kind,
        'name': name,
        'emails': [name, *emails] if '@' in name else list(emails),
    }


class DupCheck(unittest.TestCase):
    def setUp(self):
        self.t = pathlib.Path(tempfile.mkdtemp())
        (self.t / 'settings.json').write_text('{}')
        path = stub_path(self.t / 'stubs')
        self.env = dict(os.environ, PYTHONPATH=path) if path else None

    def run_tool(self, principals, tool='dup_check.py', *extra):
        (self.t / 'principals.json').write_text(json.dumps(principals))
        args = [
            '--principals',
            str(self.t / 'principals.json'),
            '--settings',
            str(self.t / 'settings.json'),
        ]
        if tool == 'migrate_v016.py':
            args = [
                'convert',
                *args,
                '--config',
                str(self.t / 'config.json'),
                '--output',
                str(self.t / 'export.json'),
                '--unmigrated-output',
                '',
            ]
        return subprocess.run(
            [sys.executable, str(HERE / tool), *args, *extra],
            capture_output=True,
            text=True,
            env=self.env,
        )

    def rows(self, out, kind):
        return [line.split('\t')[1:] for line in out.splitlines() if line.startswith(kind + '\t')]

    def test_clean_input_exits_0(self):
        p = self.run_tool(
            [user(1, 'alice@example.com', 'a2@example.com'), user(2, 'bob@example.com')]
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout, 'total\t0\n')

    def test_every_duplicate_is_listed_while_convert_stops_on_the_first(self):
        data = [
            user(1, 'alice@example.com', 'bob@example.com'),
            user(2, 'bob@example.com'),
            user(3, 'carol@example.org', 'dave@example.org'),
            user(4, 'dave@example.org'),
        ]
        conv = self.run_tool(data, 'migrate_v016.py')
        self.assertEqual(conv.returncode, 1)
        self.assertEqual(len(re.findall(r'(?m)^error: duplicate email address', conv.stderr)), 1)
        p = self.run_tool(data)
        self.assertEqual(p.returncode, 3, p.stderr)
        self.assertEqual(
            sorted(r[1] for r in self.rows(p.stdout, 'duplicate')),
            ['bob@example.com', 'dave@example.org'],
        )
        self.assertEqual(self.rows(p.stdout, 'class'), [['User:alias + User:primary', '2']])
        self.assertEqual(self.rows(p.stdout, 'total'), [['2']])

    def test_primary_and_alias_found_in_both_dump_orders(self):
        a, b = user(1, 'alice@example.com', 'bob@example.com'), user(2, 'bob@example.com')
        for data in ([a, b], [b, a]):
            with self.subTest(first=data[0]['name']):
                p = self.run_tool(data)
                self.assertEqual(p.returncode, 3)
                (row,) = self.rows(p.stdout, 'duplicate')
                self.assertEqual(row[:2], ['User:alias + User:primary', 'bob@example.com'])
                self.assertIn('restore-1', row[2])
                self.assertIn('restore-2', row[2])

    def test_group_and_list_claims_are_classed(self):
        p = self.run_tool(
            [
                user(1, 'team@example.com', kind='group'),
                user(2, 'eve@example.com', 'team@example.com'),
                user(3, 'news@example.com', kind='list'),
                user(4, 'frank@example.com', 'news@example.com'),
                user(5, 'ops@example.com', 'help@example.com', kind='group'),
                user(6, 'desk@example.com', 'help@example.com', kind='list'),
            ]
        )
        self.assertEqual(p.returncode, 3, p.stderr)
        self.assertEqual(
            self.rows(p.stdout, 'class'),
            [
                ['Group:alias + MailingList:alias', '1'],
                ['Group:primary + User:alias', '1'],
                ['MailingList:primary + User:alias', '1'],
            ],
        )

    def test_domain_case_matches_convert_and_catch_all_is_not_counted(self):
        p = self.run_tool(
            [
                user(1, 'alice@Example.COM', '@example.com'),
                user(2, 'bob@example.com', 'alice@example.com', '@example.com'),
            ]
        )
        self.assertEqual(p.returncode, 3)
        self.assertEqual([r[1] for r in self.rows(p.stdout, 'duplicate')], ['alice@example.com'])
        self.assertEqual(self.rows(p.stdout, 'total'), [['1']])

    def test_another_convert_error_exits_1_with_its_text(self):
        p = self.run_tool([{'type': 'individual', 'name': 'noid@example.com', 'emails': []}])
        self.assertEqual(p.returncode, 1)
        self.assertEqual(p.stderr.strip(), "error: user 'noid@example.com' has no id")
        self.assertEqual(p.stdout, '')

    def test_bad_input_exits_2(self):
        (self.t / 'principals.json').write_text('{}')
        p = subprocess.run(
            [
                sys.executable,
                str(HERE / 'dup_check.py'),
                '--principals',
                str(self.t / 'principals.json'),
                '--settings',
                str(self.t / 'settings.json'),
            ],
            capture_output=True,
            text=True,
            env=self.env,
        )
        self.assertEqual(p.returncode, 2)

    def test_migrate_v016_is_the_pinned_upstream_copy(self):
        want = re.search(r'sha256 `([0-9a-f]{64})`', (HERE / 'README.md').read_text()).group(1)
        self.assertEqual(hashlib.sha256((HERE / 'migrate_v016.py').read_bytes()).hexdigest(), want)


if __name__ == '__main__':
    unittest.main()
