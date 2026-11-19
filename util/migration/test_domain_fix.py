import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
stub_path = importlib.import_module('test_dup_check').stub_path
Prod = importlib.import_module('test_pr3_w4').Prod

UNI, PUNY, BAD = 'bücher.example.com', 'xn--bcher-kva.example.com', 'mail.example.notatld'


def load(name):
    spec = importlib.util.spec_from_file_location(name, HERE / (name + '.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def export(account_domain='#create-1', alias_domain='#create-2'):
    return [
        {
            '@type': 'create',
            'object': 'Domain',
            'value': {
                'create-1': {'name': 'a.example.com'},
                'create-2': {'name': UNI},
                'create-3': {'name': PUNY},
                'create-4': {'name': BAD},
            },
        },
        {
            '@type': 'create',
            'object': 'Account',
            'value': {
                'restore-1': {
                    '@type': 'User',
                    'name': 'u',
                    'domainId': account_domain,
                    'aliases': {'0': {'name': 'x', 'domainId': alias_domain}},
                }
            },
        },
        {
            '@type': 'create',
            'object': 'DkimSignature',
            'value': {
                'create-10': {'domainId': '#create-2', 'selector': 's1'},
                'create-11': {'domainId': '#create-2', 'selector': 's2'},
                'create-12': {'domainId': '#create-4', 'selector': 's3'},
                'create-13': {'domainId': '#create-4', 'selector': 's4'},
                'create-14': {'domainId': '#create-1', 'selector': 's5'},
                'create-15': {'domainId': '#create-3', 'selector': 's6'},
            },
        },
        {'@type': 'update', 'object': 'SystemSettings', 'value': {'defaultDomainId': '#create-1'}},
    ]


class DomainFix(unittest.TestCase):
    def setUp(self):
        self.t = pathlib.Path(tempfile.mkdtemp())
        self.f = self.t / 'export.json'

    def run_fix(self, ops, principals=None):
        self.f.write_text(''.join(json.dumps(op, ensure_ascii=False) + '\n' for op in ops))
        extra = []
        if principals is not None:
            (self.t / 'principals.json').write_text(json.dumps(principals, ensure_ascii=False))
            extra = ['--principals', str(self.t / 'principals.json')]
        return subprocess.run(
            [sys.executable, str(HERE / 'domain_fix.py'), '--export', str(self.f), *extra],
            capture_output=True,
            text=True,
            timeout=60,
        )

    def ops(self):
        return {
            op['object']: op['value'] for op in map(json.loads, self.f.read_text().splitlines())
        }

    def test_both_idna_entries_and_a_refused_name_are_dropped_with_their_dkim(self):
        p = self.run_fix(export(alias_domain='#create-1'))
        self.assertEqual(p.returncode, 0, p.stderr)
        o = self.ops()
        self.assertEqual(sorted(o['Domain']), ['create-1'])
        self.assertEqual(sorted(o['DkimSignature']), ['create-14'])
        self.assertEqual(o['SystemSettings']['defaultDomainId'], '#create-1')
        why = 'the same name as another Domain after IDNA (Unicode and punycode)'
        self.assertEqual(
            p.stdout.splitlines(),
            [
                "warning: dropping DkimSignature create-10: its domain '%s' is dropped" % UNI,
                "warning: dropping DkimSignature create-11: its domain '%s' is dropped" % UNI,
                "warning: dropping Domain '%s': Stalwart 0.16 refuses it (%s)" % (UNI, why),
                "warning: dropping DkimSignature create-15: its domain '%s' is dropped" % PUNY,
                "warning: dropping Domain '%s': Stalwart 0.16 refuses it (%s)" % (PUNY, why),
                "warning: dropping DkimSignature create-12: its domain '%s' is dropped" % BAD,
                "warning: dropping DkimSignature create-13: its domain '%s' is dropped" % BAD,
                "warning: dropping Domain '%s': Stalwart 0.16 refuses it "
                '(no public suffix rule matches it)' % BAD,
                'domains checked=4 dropped=3 invalid=1 idna_pairs=1 dkim_dropped=5',
            ],
        )

    def test_a_dropped_domain_still_used_by_an_alias_stops(self):
        p = self.run_fix(export())
        self.assertEqual(p.returncode, 1)
        self.assertIn("error: Domain '%s' is refused by Stalwart 0.16" % UNI, p.stderr)
        self.assertIn('still used by Account', p.stderr)

    def test_a_refused_name_still_in_use_stops_and_writes_nothing(self):
        before = export(account_domain='#create-4', alias_domain='#create-1')
        p = self.run_fix(before)
        self.assertEqual(p.returncode, 1)
        self.assertIn("error: Domain '%s' is refused by Stalwart 0.16" % BAD, p.stderr)
        self.assertIn('still used by Account', p.stderr)
        self.assertEqual(
            self.f.read_text().splitlines()[0], json.dumps(before[0], ensure_ascii=False)
        )

    def test_a_catch_all_on_a_dropped_domain_is_a_warning_and_an_address_stops(self):
        ok = [{'id': 1, 'type': 'individual', 'name': 'u', 'emails': ['u@a.example.com']}]
        catch = ok + [
            {'id': 7, 'type': 'individual', 'name': 'v', 'emails': ['v@a.example.com', '@' + BAD]}
        ]
        p = self.run_fix(export(alias_domain='#create-1'), catch)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn(
            "warning: not restoring the catch-all of legacy principal 7: its domain '%s' is dropped"
            % BAD,
            p.stdout,
        )
        before = export(alias_domain='#create-1')
        p = self.run_fix(before, ok + [{'id': 8, 'emails': ['w@' + PUNY.upper()]}])
        self.assertEqual(p.returncode, 1)
        self.assertIn(
            "error: Domain '%s' is refused by Stalwart 0.16 (the same name as another Domain after "
            'IDNA (Unicode and punycode)) and still used by an address of legacy principal 8' % PUNY,
            p.stderr,
        )
        self.assertEqual(
            self.f.read_text().splitlines()[0], json.dumps(before[0], ensure_ascii=False)
        )

    def test_shapes_compares_a_unicode_name_as_0_16_stores_it(self):
        sh = load('shapes')
        self.assertEqual(sh.norm_domain(' B\u00fccher.Example.com '), PUNY)
        self.assertEqual(sh.norm_domain('A.Example.COM'), 'a.example.com')
        self.assertEqual(sh.norm_addr('x@B\u00fccher.example.com'), 'x@' + PUNY)

    def test_names_as_stalwart_0_16_reads_them(self):
        d = load('domains')
        self.assertEqual(d.to_ascii('Straße.Example.COM'), 'xn--strae-oqa.example.com')
        self.assertEqual(d.to_ascii(' a .example.com'), 'a.example.com')
        self.assertIsNone(d.to_ascii('nodot'))
        self.assertIsNone(d.to_ascii('a..example.com'))
        known = d.rules()
        self.assertGreater(len(known), 10000)
        self.assertIsNone(d.refused('a.example.com', known))
        self.assertIsNone(d.refused('host.test', known))
        self.assertEqual(d.refused(BAD, known), 'no public suffix rule matches it')
        self.assertEqual(d.refused('mail.x.za', known), 'no public suffix rule matches it')
        self.assertIsNone(d.refused('mail.x.co.za', known))
        self.assertIsNone(d.refused('x.ck', known))
        self.assertEqual(
            d.to_ascii('\uff45\uff58\uff41\uff4d\uff50\uff4c\uff45.com'), 'example.com'
        )
        self.assertEqual(d.to_ascii('a\u3002example.com'), 'a.example.com')
        self.assertIsNone(d.to_ascii('b\u00fccher.xn--zz.com'))


class DupCheckDomains(unittest.TestCase):
    def test_dup_check_reports_both_kinds_and_keeps_its_exit_code(self):
        t = pathlib.Path(tempfile.mkdtemp())
        (t / 'settings.json').write_text('{}')
        (t / 'principals.json').write_text(
            json.dumps(
                [
                    {'id': 1, 'type': 'individual', 'name': 'u1@' + UNI, 'emails': ['u1@' + UNI]},
                    {'id': 2, 'type': 'individual', 'name': 'u2@' + PUNY, 'emails': ['u2@' + PUNY]},
                    {'id': 3, 'type': 'individual', 'name': 'u3@' + BAD, 'emails': ['u3@' + BAD]},
                ]
            )
        )
        path = stub_path(t / 'stubs')
        env = dict(os.environ, PYTHONPATH=path) if path else None
        p = subprocess.run(
            [
                sys.executable,
                str(HERE / 'dup_check.py'),
                '--principals',
                str(t / 'principals.json'),
                '--settings',
                str(t / 'settings.json'),
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('class\tDomain:idna-duplicate\t1', p.stdout)
        self.assertIn('class\tDomain:refused-by-0.16\t1', p.stdout)
        self.assertIn('domain\tDomain:idna-duplicate\t%s\t' % PUNY, p.stdout)
        self.assertTrue(p.stdout.endswith('total\t0\n'), p.stdout)


class W4DomainsGate(Prod):
    def gate(self, baseline):
        (self.t / 'secret').mkdir(exist_ok=True, mode=0o700)
        (self.t / 'secret' / 'export.json').write_text(
            ''.join(
                json.dumps(op, ensure_ascii=False) + '\n' for op in export(alias_domain='#create-1')
            )
        )
        (self.t / 'secret' / 'principals.json').write_text('[]')
        if baseline is not None:
            (self.t / 'secret' / 'convert-dropped-baseline.txt').write_text(baseline)
        return self.w4(
            'domain_fix_step; echo "rc=$? VALUE=$VALUE"',
            setup='DROP_BASELINE=$S/convert-dropped-baseline.txt',
        )

    def test_the_gate_counts_the_drops_and_goes_on(self):
        out = self.gate(None)
        self.assertIn('domains checked=4 dropped=3 invalid=1 idna_pairs=1 dkim_dropped=5', out)
        self.assertIn('domains: 8 warning line(s), one per dropped Domain and DkimSignature', out)
        self.assertIn('rc=0 VALUE=8', out)
        self.assertNotIn(UNI, out)
        self.assertNotIn(BAD, out)
        listed = json.loads((self.t / 'secret' / 'domains-dropped.json').read_text())
        self.assertEqual(listed, {'domains': sorted([BAD, PUNY, UNI]), 'dkim_dropped': 5, 'dkim_kept': 1})


if __name__ == '__main__':
    unittest.main()
