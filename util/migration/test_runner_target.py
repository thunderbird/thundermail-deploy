import json
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
ENV = {
    'STALWART_LEGACY_PG_HOST': 'legacy-stalwart.example.test',
    'STALWART_LEGACY_PG_PASSWORD': 'legacy-stalwart-pw',
    'KC_LEGACY_PG_HOST': 'kc.example.test',
    'KC_LEGACY_PG_PASSWORD': 'kc-pw',
    'ACCOUNTS_LEGACY_PG_HOST': 'accounts.example.test',
    'ACCOUNTS_LEGACY_PG_PASSWORD': 'accounts-pw',
}
LEGACY_STALWART = ('legacy-stalwart.example.test', 'legacy-stalwart-pw')


class RunnerTarget(unittest.TestCase):
    def setUp(self):
        self.t = pathlib.Path(tempfile.mkdtemp())
        self.t.chmod(0o700)
        self.mig = self.t / 'mig'
        shutil.copytree(HERE, self.mig, ignore=shutil.ignore_patterns('__pycache__'))
        p = self.mig / 'targets' / 'tb-prod.sh'
        p.write_text(p.read_text().replace('REPLACE_ME_RUNNER_WORK_SIZE', '20Gi'))

    def tearDown(self):
        shutil.rmtree(self.t, ignore_errors=True)

    def sh(self, env_name, *args, **extra):
        env = {
            'PATH': os.environ['PATH'],
            'HOME': str(self.t),
            'TARGET_ENV': env_name,
            'TS': '20261119T000000Z',
            'SECRET_DIR': str(self.t / 'secret'),
            'EVIDENCE_DIR': str(self.t / 'ev'),
            **extra,
        }
        return subprocess.run(
            ['bash', str(self.mig / 'runner-pod.sh'), *args],
            capture_output=True,
            text=True,
            env=env,
        )

    def secret(self, env_name, **env):
        p = self.sh(env_name, '--secret', **env)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(pathlib.Path(p.stdout.strip()).read_text())

    def pgpass(self, manifest, legacy):
        script = re.search(
            r'(?ms)^    - name: postgres\n.*?^        - \|\n(.*?)^      env:', manifest
        ).group(1)
        script = '\n'.join(line[10:] for line in script.splitlines())
        root = self.t / 'pod'
        for d in ('etc/pg/target', 'etc/pg/legacy', 'etc/stalwart-config', 'secrets'):
            (root / d).mkdir(parents=True, exist_ok=True)
        (root / 'etc/pg/target/STALWART_POSTGRESQL_ADMIN_PASSWORD').write_text('target-pw')
        (root / 'etc/stalwart-config/config.json').write_text(
            '{"host": "ep-cutover-pooler.example.test"}'
        )
        (root / 'ca.pem').write_text('ca')
        for k, v in legacy.items():
            (root / 'etc/pg/legacy' / k).write_text(v)
        for a in ('/etc/pg/', '/etc/stalwart-config/', '/secrets/'):
            script = script.replace(a, str(root) + a)
        script = script.replace('exec sleep infinity', 'exit 0')
        p = subprocess.run(
            ['sh', '-ec', script],
            capture_output=True,
            text=True,
            env={'PATH': os.environ['PATH'], 'PGSSLROOTCERT': str(root / 'ca.pem')},
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        return (root / 'secrets' / '.pgpass').read_text()

    def test_prod_runner_carries_no_legacy_stalwart_credential(self):
        p = self.sh('tb-prod')
        self.assertEqual(p.returncode, 0, p.stderr)
        manifest = p.stdout
        self.assertNotRegex(manifest, r'for k in [^;]*\bstalwart\b[^;]*; do (\[ -s|printf|cat)')
        self.assertIn('for k in stalwart stalwart-host; do [ ! -e "/etc/pg/legacy/$k" ]', manifest)
        env = {k: v for k, v in ENV.items() if not k.startswith('STALWART_LEGACY')}
        doc = self.secret('tb-prod', **env)
        self.assertEqual(
            doc['stringData'],
            {
                'keycloak': 'kc-pw',
                'keycloak-host': 'kc.example.test',
                'accounts': 'accounts-pw',
                'accounts-host': 'accounts.example.test',
            },
        )
        doc = self.secret('tb-prod', **ENV)
        for value in LEGACY_STALWART:
            self.assertNotIn(value, json.dumps(doc))
        pgpass = self.pgpass(manifest, doc['stringData'])
        self.assertEqual(
            pgpass.splitlines(),
            [
                'ep-cutover.example.test:5432:*:*:target-pw',
                'ep-cutover-pooler.example.test:5432:*:*:target-pw',
                'kc.example.test:5432:*:*:kc-pw',
                'accounts.example.test:5432:*:*:accounts-pw',
            ],
        )
        for value in LEGACY_STALWART:
            self.assertNotIn(value, pgpass)

    def test_prod_pod_refuses_a_secret_that_still_has_the_stalwart_key(self):
        p = self.sh('tb-prod')
        self.assertEqual(p.returncode, 0, p.stderr)
        legacy = {
            'keycloak': 'kc-pw',
            'keycloak-host': 'kc.example.test',
            'accounts': 'accounts-pw',
            'accounts-host': 'accounts.example.test',
            'stalwart': 'legacy-stalwart-pw',
        }
        with self.assertRaises(AssertionError) as cm:
            self.pgpass(p.stdout, legacy)
        self.assertIn('legacy-pg has a stalwart key', str(cm.exception))

    def test_prod_refuses_a_stalwart_key_list_or_a_shared_host(self):
        p = self.mig / 'targets' / 'tb-prod.sh'
        base = p.read_text()
        for keys in ('stalwart keycloak accounts', ''):
            with self.subTest(keys=keys):
                p.write_text(base + "RUNNER_LEGACY_PG_KEYS='%s'\n" % keys)
                r = self.sh('tb-prod')
                self.assertEqual(r.returncode, 1, r.stdout)
                self.assertIn('RUNNER_LEGACY_PG_KEYS in targets/tb-prod.sh', r.stderr)
        p.write_text(base)
        env = dict(ENV, KC_LEGACY_PG_HOST=ENV['STALWART_LEGACY_PG_HOST'])
        r = self.sh('tb-prod', '--secret', **env)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn('KC_LEGACY_PG_HOST equals STALWART_LEGACY_PG_HOST', r.stderr)
        self.assertFalse((self.t / 'secret').exists() and any((self.t / 'secret').iterdir()))

    def test_stage_runner_keeps_all_three_legacy_credentials(self):
        p = self.sh('tb-stage')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout, (HERE / 'runner-pod.yaml').read_text())
        doc = self.secret('tb-stage', **ENV)
        self.assertEqual(
            list(doc['stringData']),
            ['stalwart', 'stalwart-host', 'keycloak', 'keycloak-host', 'accounts', 'accounts-host'],
        )
        pgpass = self.pgpass(p.stdout, doc['stringData'])
        self.assertIn('legacy-stalwart.example.test:5432:*:*:legacy-stalwart-pw', pgpass)
        self.assertEqual(len(pgpass.splitlines()), 5)


if __name__ == '__main__':
    unittest.main()
