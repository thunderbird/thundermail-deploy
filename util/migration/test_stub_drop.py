import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from test_dup_check import stub_path  # noqa: E402

os.umask(0o077)

HOLDER = {
    'id': 10,
    'type': 'individual',
    'name': 'holder@example.com',
    'emails': ['holder@example.com', 'stub@example.com'],
    'description': 'Holder',
    'quota': 1073741824,
    'usedQuota': 4096,
    'roles': ['user'],
    'secrets': ['$app$x'],
}
STUB = {'id': 11, 'type': 'individual', 'name': 'stub@example.com', 'roles': ['user']}
OTHER = {
    'id': 12,
    'type': 'individual',
    'name': 'other@example.com',
    'emails': ['other@example.com'],
    'roles': ['user'],
}


class StubDrop(unittest.TestCase):
    def setUp(self):
        self.t = pathlib.Path(tempfile.mkdtemp())
        (self.t / 'settings.json').write_text('{}')
        path = stub_path(self.t / 'mods')
        self.env = dict(os.environ, PYTHONPATH=path) if path else None

    def drop(self, items, expected=1):
        (self.t / 'principals.json').write_text(json.dumps(items))
        p = subprocess.run(
            [
                sys.executable,
                str(HERE / 'stub_drop.py'),
                '--principals',
                str(self.t / 'principals.json'),
                '--output',
                str(self.t / 'principals.kept.json'),
                '--dropped-list',
                str(self.t / 'stubs-dropped.json'),
                '--expected',
                str(expected),
            ],
            capture_output=True,
            text=True,
            env=self.env,
            timeout=60,
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout, json.loads((self.t / 'stubs-dropped.json').read_text())

    def dup_check(self):
        return subprocess.run(
            [
                sys.executable,
                str(HERE / 'dup_check.py'),
                '--principals',
                str(self.t / 'principals.kept.json'),
                '--settings',
                str(self.t / 'settings.json'),
            ],
            capture_output=True,
            text=True,
            env=self.env,
            timeout=60,
        )

    def test_the_stub_is_dropped_and_the_check_and_convert_pass(self):
        out, listed = self.drop([HOLDER, STUB, OTHER])
        self.assertEqual(out, 'stubs: dropped=1 expected=1 ids 11\n')
        self.assertNotIn('example.com', out)
        self.assertEqual(listed['ids'], [11])
        self.assertEqual(listed['stubs'], [{'id': 11, 'name': 'stub@example.com', 'alias_holder': 10}])
        self.assertEqual(oct((self.t / 'stubs-dropped.json').stat().st_mode & 0o777), '0o600')
        kept = json.loads((self.t / 'principals.kept.json').read_text())
        self.assertEqual([x['id'] for x in kept], [10, 12])
        self.assertEqual(json.loads((self.t / 'principals.json').read_text())[1], STUB)
        self.assertEqual(self.dup_check().returncode, 0)
        for name, dup in (('principals.json', True), ('principals.kept.json', False)):
            with self.subTest(convert=name):
                conv = subprocess.run(
                    [
                        sys.executable,
                        str(HERE / 'migrate_v016.py'),
                        'convert',
                        '--principals',
                        str(self.t / name),
                        '--settings',
                        str(self.t / 'settings.json'),
                        '--config',
                        str(self.t / 'config.json'),
                        '--output',
                        str(self.t / 'export.json'),
                        '--unmigrated-output',
                        '',
                    ],
                    capture_output=True,
                    text=True,
                    env=self.env,
                    timeout=60,
                )
                self.assertEqual('error: duplicate email address' in conv.stderr, dup, conv.stderr)

    def test_a_count_other_than_expected_is_printed_with_check(self):
        out, _ = self.drop([HOLDER, STUB], expected=2)
        self.assertEqual(out, 'stubs: dropped=1 expected=2 CHECK ids 11\n')
        out, _ = self.drop([OTHER], expected=2)
        self.assertEqual(out, 'stubs: dropped=0 expected=2 CHECK ids -\n')

    def test_nothing_to_drop_leaves_a_byte_identical_copy(self):
        raw = json.dumps([OTHER, HOLDER], indent=1) + '\n'
        (self.t / 'principals.json').write_text(raw)
        p = subprocess.run(
            [
                sys.executable,
                str(HERE / 'stub_drop.py'),
                '--principals',
                str(self.t / 'principals.json'),
                '--output',
                str(self.t / 'principals.kept.json'),
                '--dropped-list',
                str(self.t / 'stubs-dropped.json'),
            ],
            capture_output=True,
            text=True,
            env=self.env,
            timeout=60,
        )
        self.assertEqual(p.stdout, 'stubs: dropped=0 expected=0 ids -\n')
        self.assertEqual((self.t / 'principals.kept.json').read_text(), raw)
        self.assertEqual(json.loads((self.t / 'stubs-dropped.json').read_text()), {'ids': [], 'stubs': []})

    def test_a_name_holder_that_is_not_an_empty_stub_is_kept_and_still_stops(self):
        group = {'id': 13, 'type': 'group', 'name': 'team@example.com', 'members': ['stub@example.com']}
        second = dict(OTHER, emails=['other@example.com', 'stub@example.com'])
        cases = {
            'secret': ([HOLDER, dict(STUB, secrets=['$2y$x'])], None),
            'own address': ([HOLDER, dict(STUB, emails=['stub2@example.com'])], None),
            'description': ([HOLDER, dict(STUB, description='Someone')], None),
            'quota': ([HOLDER, dict(STUB, quota=1024)], None),
            'used quota': ([HOLDER, dict(STUB, usedQuota=1)], None),
            'member of a group': ([HOLDER, dict(STUB, memberOf=['team@example.com'])], None),
            'has members': ([HOLDER, dict(STUB, members=['other@example.com'])], None),
            'listed by a group': ([HOLDER, STUB, group], None),
            'claimed by two aliases': ([HOLDER, STUB, second], None),
            'a group': ([HOLDER, dict(STUB, type='group')], None),
            'role admin': ([HOLDER, dict(STUB, roles=['admin'])], None),
            'roles user and admin': ([HOLDER, dict(STUB, roles=['user', 'admin'])], None),
            'no role': ([HOLDER, dict(STUB, roles=[])], None),
            'a permission': ([HOLDER, dict(STUB, enabledPermissions=['impersonate'])], None),
            'another field': ([HOLDER, dict(STUB, tenant='t1')], None),
            'listed by id in a list': (
                [HOLDER, STUB, {'id': 14, 'type': 'list', 'name': 'all@example.com', 'members': [{'integer': 11}]}],
                None,
            ),
            'listed by wrapped name': (
                [HOLDER, STUB, dict(group, members={'stringList': ['stub@example.com']})],
                None,
            ),
            'listed by bare id': ([HOLDER, STUB, dict(group, members=[11])], None),
            'wrapped memberOf': ([HOLDER, dict(STUB, memberOf={'stringList': ['team@example.com']})], None),
            'wrapped secret': ([HOLDER, dict(STUB, secrets={'stringList': ['$2y$x']})], None),
            'wrapped quota': ([HOLDER, dict(STUB, quota={'integer': 5})], None),
        }
        for case, (items, _) in cases.items():
            with self.subTest(case=case):
                out, listed = self.drop(items)
                self.assertEqual(listed['ids'], [], out)
                self.assertIn('dropped=0 ', out)
                p = self.dup_check()
                self.assertEqual(p.returncode, 3, p.stdout + p.stderr)

    def test_wrapped_values_are_read_as_the_converter_reads_them(self):
        holder = dict(HOLDER, emails={'stringList': ['holder@example.com', 'stub@example.com']})
        stub = {'id': {'integer': 11}, 'type': 'individual', 'name': {'string': 'stub@example.com'}, 'roles': {'stringList': ['user']}}
        out, listed = self.drop([holder, stub])
        self.assertEqual(listed['ids'], [11])
        self.assertEqual(out, 'stubs: dropped=1 expected=1 ids 11\n')
        self.assertEqual(self.dup_check().returncode, 0)

    def test_both_verify_files_read_the_dropped_list(self):
        for env in ('tb-stage', 'tb-prod'):
            with self.subTest(env=env):
                text = (HERE / 'verify' / ('%s.env' % env)).read_text()
                self.assertIn('DROPPED_STUBS="$SECRET_DIR/stubs-dropped.json"', text)

    def test_an_unclaimed_empty_account_is_not_a_stub(self):
        lone = dict(STUB, name='lone@example.com')
        out, listed = self.drop([HOLDER, STUB, lone])
        self.assertEqual(listed['ids'], [11])
        kept = json.loads((self.t / 'principals.kept.json').read_text())
        self.assertIn(lone, kept)

    def test_a_stub_is_matched_on_the_domain_in_any_case(self):
        out, listed = self.drop([HOLDER, dict(STUB, name='stub@Example.COM')])
        self.assertEqual(listed['ids'], [11])
        self.assertEqual(self.dup_check().returncode, 0)


if __name__ == '__main__':
    unittest.main()
