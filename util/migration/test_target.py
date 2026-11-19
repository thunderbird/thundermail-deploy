import base64
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
os.umask(0o077)
REPO = HERE.parent.parent
REAL = (
    'awk base64 basename bash cat chmod cp cut date dirname env find grep head id jq ls mkdir '
    'mktemp mv printf realpath rm sed sort stat tail tar tee timeout tr true false uniq wc xargs '
    'yq git setsid'
).split()
FAKES = ('kubectl', 'gh', 'aws', 'curl', 'podman', 'neonctl', 'rclone', 'ss', 'openssl')
FAKE = """#!{py}
import json, os, re, sys
name, args = os.path.basename(sys.argv[0]), ' '.join(sys.argv[1:])
open(os.environ['FAKE_LOG'], 'a').write(name + ' ' + args + '\\n')
for rule in json.load(open(os.environ['FAKE_RULES'])):
    if rule['tool'] == name and re.search(rule['match'], args):
        sys.stdout.write(rule.get('out', ''))
        sys.exit(rule.get('rc', 0))
sys.exit(1)
"""
GATE_IDS = (
    'go-W1 go-W2 go-outbound-block go-T7 go-W4 go-W4-read go-W4-reset go-T8 go-W5 go-predns go-mx-flip go-access go-open '
    'go-W8 go-P1 go-V2 go-rollback start checks presync backups verify auth-dns pre-cut'
).split()
MAIN_STAGE_BUILD = (
    'STALWART_TARGET_PG_HOST 19 STALWART_TARGET_PG_DB 2 STALWART_TARGET_PG_USER 3 '
    'STALWART_TARGET_PG_PASSWORD 5 STALWART_USER 5 STALWART_PASSWORD 6 STALWART_URL 22 '
    'OLD_STALWART_URL 23 OLD_STALWART_USER 5 OLD_STALWART_PASSWORD 6 '
    'STALWART_LEGACY_PG_HOST 19 STALWART_LEGACY_PG_DB 1 STALWART_LEGACY_PG_USER 1 '
    'STALWART_LEGACY_PG_PASSWORD 4 ACCOUNTS_LEGACY_PG_HOST 18 ACCOUNTS_LEGACY_PG_NAME 6 '
    'ACCOUNTS_LEGACY_PG_USER 6 ACCOUNTS_LEGACY_PG_PASSWORD 6 KC_LEGACY_PG_HOST 18 '
    'KC_LEGACY_PG_DB 6 KC_LEGACY_PG_USER 6 KC_LEGACY_PG_PASSWORD 6 '
    'KC_LEGACY_ADMIN_CLIENT_ID 6 KC_LEGACY_ADMIN_CLIENT_SECRET 6 KC_LEGACY_URL 25 '
    'NEON_PROJECT_STALWART_LEGACY 0 NEON_LEGACY_STALWART_BRANCH 0 NEON_PROJECT_KC 0 '
    'NEON_PROJECT_ACCOUNTS 0 LEGACY_BLOB_BUCKET 24 TARGET_BLOB_BUCKET * REDIS_URL * '
    'LEGACY_MGMT_INSTANCE 6 LEGACY_PUB_LB_ARN 8 LEGACY_PUB_SG 4 LEGACY_PUB_SG_RULE_IDS 11 '
    'LEGACY_ECS_TARGETS 31 LEGACY_ASG_RESOURCE_IDS 39 ARGO_APPS 86 '
    'KARGO_PROJECT_ACCOUNTS 11 KARGO_STAGE_ACCOUNTS 8 KARGO_PROJECT_CONFIGS 19 '
    'KARGO_STAGE_CONFIGS 29 KARGO_WAREHOUSE_CONFIGS 33 FREEZE_PR 2 T6_PR 0 T7_PR 2 '
    'T7_REVERT_PR 2 T8_PR 2 T8_REVERT_PR 2 FLIP_PR 0 APPOINTMENT_PR 0 ADMIN_MERGE 0 '
    'PI_DIR 0 KCD_DIR 0 ACD_DIR 0 APPOINTMENT_DIR 0 TMD_DIR 0 AWS_REGION 12 '
    'CF_API_TOKEN_AGENT 0 CF_ZONE_ID 0 CF_ZONE_ID_TBPRO 0 PADDLE_API_KEY 0 '
    'ACCOUNTS_WEBHOOK_URL 73 TARGET_NLB_DNS 16 TEST_MAILBOX 0 TEST_MAILBOX_IMAP_HOST 0 '
    'TEST_MAILBOX_IMAP_USER 0 TEST_MAILBOX_IMAP_PASSWORD 0 TEST_USER 0 TEST_SENDER 0 '
    'INBOUND_FROM 0 SPF_FINAL_TXT 0 SUPPORT_CHANNEL 0 KUBECONFIG_TARGET * '
    'KUBECONFIG_HUB * KUBECONFIG_DEV * EVIDENCE_DIR * TIMING_TSV * SECRET_DIR * '
    'RCLONE_CONFIG_LEG_TYPE 2 RCLONE_CONFIG_LEG_PROVIDER 3 RCLONE_CONFIG_LEG_ENV_AUTH 4 '
    'RCLONE_CONFIG_LEG_REGION 12 RCLONE_CONFIG_LEG_PROFILE 11 RCLONE_CONFIG_TGT_TYPE 2 '
    'RCLONE_CONFIG_TGT_PROVIDER 3 RCLONE_CONFIG_TGT_ENV_AUTH 4 '
    'RCLONE_CONFIG_TGT_REGION 12 RCLONE_CONFIG_TGT_PROFILE 13 '
).split()
LIBS = {'lib.sh', 'timing.sh', 'target.sh', 'window/lib.sh'}


def b64(s):
    return base64.b64encode(s.encode()).decode()


def kubeconfig(current, *clusters):
    arn = 'arn:aws:eks:eu-central-1:1:cluster/%s'
    return json.dumps(
        {
            'apiVersion': 'v1',
            'clusters': [
                {'name': arn % c, 'cluster': {'server': 'https://%s.example.test' % c}}
                for c in clusters
            ],
            'contexts': [
                {
                    'name': 'kc-prod' if c.endswith('prod01') else 'hub',
                    'context': {'cluster': arn % c, 'user': c},
                }
                for c in clusters
            ],
            'current-context': current,
        }
    )


def sm(value):
    return json.dumps({'SecretString': value})


def stage_rules(prefix='stage', ecs='accounts-stage'):
    backend = json.dumps(
        {'host': 'ep-leg-pooler.example.test', 'database': 'd', 'user': 'u', 'password': 'p4ss'}
    )
    return [
        {
            'tool': 'kubectl',
            'match': 'get cm stalwart-config',
            'out': json.dumps(
                {'host': 'ep-tgt-pooler.example.test', 'database': 'st', 'authUsername': 'own'}
            ),
        },
        {'tool': 'kubectl', 'match': 'STALWART_POSTGRESQL_ADMIN_PASSWORD', 'out': b64('tgtpw')},
        {'tool': 'kubectl', 'match': 'STALWART_USER', 'out': b64('admin')},
        {'tool': 'kubectl', 'match': 'STALWART_PASSWORD', 'out': b64('recpw1')},
        {'tool': 'kubectl', 'match': 'get svc nginx', 'out': 'nlb.example.test'},
        {'tool': 'aws', 'match': 'postgresql_backend', 'out': sm(backend)},
        {'tool': 'aws', 'match': 'database-host', 'out': sm('ep-db-pooler.example.test')},
        {'tool': 'aws', 'match': 'get-secret-value', 'out': sm('value1')},
        {'tool': 'aws', 'match': 's3api list-buckets', 'out': 'legacy-stalwart-s3-store'},
        {'tool': 'aws', 'match': 'describe-instances', 'out': 'i-0abc'},
        {'tool': 'aws', 'match': 'describe-load-balancers', 'out': 'arn:lb/1\tsg-1'},
        {'tool': 'aws', 'match': 'describe-security-group-rules', 'out': 'sgr-1\tsgr-2'},
        {'tool': 'aws', 'match': 'ecs list-clusters', 'out': 'arn:cluster/%s-web' % ecs},
        {'tool': 'aws', 'match': 'ecs list-services', 'out': 'arn:service/accounts-web'},
        {
            'tool': 'aws',
            'match': 'describe-scalable-targets',
            'out': 'service/%s-web/accounts-web' % ecs,
        },
        {'tool': 'gh', 'match': 'pr list', 'out': '42'},
    ]


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.t = pathlib.Path(tempfile.mkdtemp(prefix='target-test-'))
        self.addCleanup(shutil.rmtree, self.t, True)
        self.t.chmod(0o700)
        self.bin = self.t / 'bin'
        self.bin.mkdir()
        for name in REAL:
            if shutil.which(name):
                (self.bin / name).symlink_to(shutil.which(name))
        for name in FAKES:
            (self.bin / name).write_text(FAKE.format(py=sys.executable))
            (self.bin / name).chmod(0o755)
        for d in ('ev', 'secret', 'shm', 'home'):
            (self.t / d).mkdir(mode=0o700)
        for k in ('kc-target', 'kc-hub', 'kc-dev'):
            (self.t / k).write_text('')
        self.rules([])

    def rules(self, rules):
        (self.t / 'rules.json').write_text(json.dumps(rules))

    def real_kubectl(self):
        (self.bin / 'kubectl').unlink()
        (self.bin / 'kubectl').symlink_to(shutil.which('kubectl'))

    def log(self):
        p = self.t / 'fake.log'
        return p.read_text() if p.exists() else ''

    def env(self, **extra):
        e = {
            'PATH': str(self.bin),
            'HOME': str(self.t / 'home'),
            'LANG': 'C.UTF-8',
            'FAKE_LOG': str(self.t / 'fake.log'),
            'FAKE_RULES': str(self.t / 'rules.json'),
            'EVIDENCE_DIR': str(self.t / 'ev'),
            'SECRET_DIR': str(self.t / 'secret'),
            'CUTOVER_ENV': str(self.t / 'shm' / 'cutover.env'),
            'OPERATOR_ENV': str(self.t / 'home' / 'none.env'),
            'TS': '20261118T000000Z',
        }
        e.update({k: v for k, v in extra.items() if v is not None})
        return e

    def run_sh(self, args, cwd=None, stdin='', **env):
        p = subprocess.run(
            [shutil.which('bash'), *args],
            input=stdin,
            capture_output=True,
            text=True,
            env=self.env(**env),
            cwd=cwd or self.t,
            timeout=120,
        )
        return p.returncode, p.stdout + p.stderr


def names_and_lengths(out):
    return [ln for ln in out.splitlines() if re.match(r'[A-Z][A-Z0-9_]+ +len=', ln)]


class Builder(Sandbox):
    def build(self, mig, *args, **env):
        self.rules(env.pop('rules', stage_rules()))
        return self.run_sh([str(mig / 'build-cutover-env.sh'), *args], **env)

    def test_stage_names_and_lengths_match_main(self):
        kc = {
            'KUBECONFIG_STAGE': str(self.t / 'kc-target'),
            'KUBECONFIG_HUB': str(self.t / 'kc-hub'),
            'KUBECONFIG_DEV': str(self.t / 'kc-dev'),
        }
        _, out = self.build(HERE, TARGET_ENV='tb-stage', **kc)
        got = [ln.split()[:2] for ln in names_and_lengths(out)]
        want = list(zip(MAIN_STAGE_BUILD[::2], MAIN_STAGE_BUILD[1::2]))
        self.assertEqual([n for n, _ in got], [n for n, _ in want], out)
        for (name, length), (_, expect) in zip(got, want):
            if expect != '*':
                self.assertEqual(length, 'len=' + expect, name)
        self.assertIn("TARGET_ENV='tb-stage'", (self.t / 'shm' / 'cutover.env').read_text())

    def prod_env(self, **extra):
        e = {
            'TARGET_ENV': 'tb-prod',
            'KUBECONFIG_TARGET': str(self.t / 'kc-target'),
            'KUBECONFIG_HUB': str(self.t / 'kc-hub'),
            'KUBECONFIG_DEV': str(self.t / 'kc-dev'),
            'rules': stage_rules(ecs='accounts-prod'),
        }
        e.update(extra)
        return e

    def test_prod_fails_on_a_value_that_contains_stage(self):
        rc, out = self.build(HERE, **self.prod_env(TEST_USER='someone@stage-thundermail.com'))
        self.assertNotEqual(rc, 0)
        self.assertRegex(out, r'TEST_USER +len=\d+ CONTAINS stage')

    def test_prod_takes_a_direct_database_host_as_it_is(self):
        rules = [
            dict(r, out=r['out'].replace('ep-tgt-pooler.example.test', 'ep-tgt.example.test'))
            if r['match'] == 'get cm stalwart-config'
            else r
            for r in stage_rules(ecs='accounts-prod')
        ]
        (self.t / 'kc-target').write_text(kubeconfig('kc-prod', 'mzla-eks-tb-prod01'))
        self.build(HERE, **self.prod_env(rules=rules))
        text = (self.t / 'shm' / 'cutover.env').read_text()
        self.assertIn("STALWART_TARGET_PG_HOST='ep-tgt.example.test'", text)
        self.assertNotIn('pooler', text.replace('-pooler stripped', ''))

    def test_prod_marks_placeholders_and_writes_the_window_names(self):
        rc, out = self.build(HERE, **self.prod_env())
        self.assertNotEqual(rc, 0)
        self.assertRegex(out, r'(?m)^LEGACY_PG_SECRET_NAME +len=9$')
        for name in (
            'TARGET_NLB_IPS',
            'TBPROD_AUTH_ALB',
            'TBPROD_ACCOUNTS_ALB_IP',
            'KEYCLOAK_REPLICAS',
            'STALWART_REPLICAS',
            'ARGO_SYNC_MODE_TB_PROD_ACCOUNTS',
            'NEON_PROJECT_STALWART_TARGET',
            'LEGACY_STALWART_NODE_IDS',
            'TARGET_STALWART_NAT_IDS',
            'WATCH_INTERVAL',
            'LEGACY_PG_SECRET_NAME',
            'CUTOVER_ENV',
        ):
            self.assertRegex(out, r'(?m)^%s +len=' % name)
        self.assertRegex(out, r'(?m)^STALWART_URL +len=22$')
        text = (self.t / 'shm' / 'cutover.env').read_text()
        head = subprocess.run(
            ['git', '-C', str(REPO), 'rev-parse', 'HEAD'], capture_output=True, text=True
        ).stdout.strip()
        self.assertIn("TMD_CHECKOUT='%s'" % REPO, text)
        self.assertIn("WINDOW_SHA='%s'" % head, text)
        self.assertNotIn('REHEARSAL', text)
        self.assertIn("INBOUND_TTL_WAIT='60'", text)
        self.assertIn("NEON_BRANCH_KC=''", text)
        self.assertEqual(oct((self.t / 'shm' / 'cutover.env').stat().st_mode & 0o777), '0o600')

    def test_prod_refuses_an_open_window_without_rebuild(self):
        rc, out = self.build(HERE, **self.prod_env(FREEZE_SHA='abc'))
        self.assertEqual(rc, 1)
        self.assertIn('FREEZE_SHA is set', out)
        self.assertFalse((self.t / 'shm' / 'cutover.env').exists())

    def test_rebuild_restores_appended_values_and_names_the_rest(self):
        (self.t / 'ev' / 'window-values.tsv').write_text(
            'AUTH_DNS_AT\t2026-11-20T01:00:00Z\nREALM_BEFORE\t/x/realm.json\n'
            'TEST_SENDER_APP_PASSWORD\tleaked\nAUTH_DNS_AT\t2026-11-20T02:00:00Z\n'
            'WINDOW_SHA\tfix2\nINBOUND_TTL_WAIT\t300\n'
        )
        rc, out = self.build(HERE, '--rebuild', **self.prod_env(FREEZE_SHA='abc'))
        text = (self.t / 'shm' / 'cutover.env').read_text()
        self.assertIn("AUTH_DNS_AT='2026-11-20T02:00:00Z'", text)
        self.assertIn("REALM_BEFORE='/x/realm.json'", text)
        self.assertNotIn('leaked', text)
        self.assertIn("WINDOW_SHA='fix2'", text)
        self.assertIn("INBOUND_TTL_WAIT='300'", text)
        self.assertRegex(out, r'TEST_SENDER_APP_PASSWORD +not restored')
        self.assertRegex(out, r'STATE_OPEN_AT +not restored')

    def test_prod_check_waits_for_what_only_the_window_creates(self):
        self.build(HERE, **self.prod_env())
        self.rules([])
        rc, out = self.run_sh(
            [str(HERE / 'build-cutover-env.sh'), '--check'],
            TARGET_ENV='tb-prod',
            TIMING_TSV=str(self.t / 'ev' / 'timing.tsv'),
        )
        self.assertNotEqual(rc, 0)
        for name in (
            'runner-pod',
            'pg_isready-STALWART_TARGET',
            'psql-select1-ACCOUNTS_LEGACY',
            'legacy-url-loopback',
            'stalwart-legacy-healthz',
            'stalwart-target-healthz',
        ):
            self.assertRegex(out, r'(?m)^WAIT  %s +\S' % re.escape(name))
            self.assertNotRegex(out, r'(?m)^(PASS|FAIL)  %s ' % re.escape(name))
        for name in ('kargo-stage-configs', 'kargo-warehouse-configs'):
            self.assertRegex(out, r'(?m)^FAIL  %s ' % name)
        for name in (
            'spf-final-txt',
            't7-prs',
            't8-prs',
            'restore-replicas-dry-run',
            'pg_isready-STALWART_LEGACY',
            'psql-select1-STALWART_LEGACY',
            'restore-target-db-dry-run',
        ):
            self.assertNotRegex(out, r'(?m)^\S+  %s ' % re.escape(name))
        self.assertRegex(out, r'(?m)^FAIL  operator-values +empty: TEST_MAILBOX ')
        self.assertNotIn('FREEZE_PR', out)
        self.assertIn('env-check\t', (self.t / 'ev' / 'timing-tb-prod.tsv').read_text())
        self.assertNotIn('pod/runner', self.log())

    def test_rebuild_keeps_the_window_sha_and_a_build_takes_head(self):
        self.build(HERE, **self.prod_env())
        f = self.t / 'shm' / 'cutover.env'
        f.write_text(f.read_text() + "WINDOW_SHA='fix3'\n")
        self.build(HERE, '--rebuild', **self.prod_env())
        self.assertIn("WINDOW_SHA='fix3'", f.read_text())
        self.build(HERE, **self.prod_env(WINDOW_SHA='stale'))
        self.assertNotIn('fix3', f.read_text())
        self.assertNotIn('stale', f.read_text())

    def test_prod_build_writes_the_file_when_the_node_lookup_is_empty(self):
        rules = [r for r in stage_rules(ecs='accounts-prod') if r['match'] != 'describe-instances']
        rc, out = self.build(HERE, **self.prod_env(rules=rules))
        self.assertRegex(out, r'(?m)^LEGACY_STALWART_NODE_IDS +len=0 NEED$')
        self.assertRegex(out, r'(?m)^written ')
        self.assertTrue((self.t / 'shm' / 'cutover.env').exists())

    def test_prod_ingress_load_balancers_are_picked_by_host(self):
        def ing(name, host, lb):
            rules = [{'host': host}] if host else []
            return {
                'metadata': {'name': name},
                'spec': {'rules': rules},
                'status': {'loadBalancer': {'ingress': [{'hostname': lb}]}},
            }

        items = {
            'keycloak-customer': [
                ing('keycloak-customer-admin', None, 'admin.ts.example.test'),
                ing('keycloak-customer-public', 'auth.tb.pro', 'k8s-public-auth.elb.example.test'),
            ],
            'accounts': [ing('accounts-tb-prod', None, 'accounts.ts.example.test')],
        }
        rules = [
            {
                'tool': 'kubectl',
                'match': '-n %s get ingress -o json' % ns,
                'out': json.dumps({'items': v}),
            }
            for ns, v in items.items()
        ] + stage_rules(ecs='accounts-prod')
        (self.t / 'kc-target').write_text(kubeconfig('kc-prod', 'mzla-eks-tb-prod01'))
        self.build(HERE, **self.prod_env(rules=rules))
        text = (self.t / 'shm' / 'cutover.env').read_text()
        self.assertIn("TBPROD_AUTH_ALB='k8s-public-auth.elb.example.test'", text)
        self.assertIn("TBPROD_ACCOUNTS_ALB=''", text)

    def test_rebuild_moves_the_window_sha_to_head_on_request(self):
        self.build(HERE, **self.prod_env())
        f = self.t / 'shm' / 'cutover.env'
        f.write_text(f.read_text() + "WINDOW_SHA='fix3'\n")
        head = subprocess.run(
            ['git', '-C', str(REPO), 'rev-parse', 'HEAD'], capture_output=True, text=True
        ).stdout.strip()
        self.build(HERE, '--rebuild', '--move-window-sha', **self.prod_env())
        self.assertIn("WINDOW_SHA='%s'" % head, f.read_text())
        self.assertNotIn('fix3', f.read_text())

    def test_prod_cloudflare_token_comes_from_the_prod_file(self):
        home = self.t / 'home'
        for name, token in (('.cloudflare.env', 'stage-tok'), ('.prod-cloudflare.env', 'prod-token-1')):
            (home / name).write_text("CF_API_TOKEN_AGENT='%s'\n" % token)
            (home / name).chmod(0o600)
        rc, out = self.build(HERE, **self.prod_env())
        text = (self.t / 'shm' / 'cutover.env').read_text()
        self.assertIn("CF_API_TOKEN_AGENT='prod-token-1'", text)
        self.assertIn('# source: %s/.prod-cloudflare.env CF_API_TOKEN_AGENT' % home, text)
        self.assertRegex(out, r'(?m)^CF_API_TOKEN_AGENT +len=12$')
        self.assertNotIn('prod-token-1', out)
        (home / '.prod-cloudflare.env').chmod(0o644)
        rc, out = self.run_sh(
            [str(HERE / 'check-prereqs.sh'), '--for', 'start'], TARGET_ENV='tb-prod'
        )
        self.assertRegex(
            out, r'(?m)^NEED  cf-token-agent +%s/.prod-cloudflare.env missing or not mode 600$' % home
        )
        self.assertNotIn('token-1', out)

    def test_build_refuses_a_file_of_the_other_environment(self):
        f = self.t / 'shm' / 'cutover.env'
        for content, needle in (
            ("TARGET_ENV='tb-stage'\n", 'built for TARGET_ENV=tb-stage'),
            ("STALWART_URL='http://localhost:18081'\n", 'built for TARGET_ENV=<none>'),
        ):
            with self.subTest(content=content):
                f.write_text(content)
                f.chmod(0o600)
                rc, out = self.build(HERE, **self.prod_env())
                self.assertEqual(rc, 64)
                self.assertIn(needle, out)
                rc, out = self.build(HERE, '--check', **self.prod_env())
                self.assertEqual(rc, 64)
                self.assertIn(needle, out)


class CheckSnapshots(Sandbox):
    def test_check_snapshots_leave_the_files_rollback_reads(self):
        text = (HERE / 'build-cutover-env.sh').read_text()
        start = text.index('  pair() {')
        fn = text[start : text.index('\n  }\n', start) + 4]
        mig = self.t / 'mig'
        mig.mkdir()
        (mig / 'snapshot-zone.sh').write_text(
            'f=$EVIDENCE_DIR/zone-before-$1-1.json; echo "[]" > "$f"; echo "$f"\n'
        )
        (mig / 'snapshot-realm.sh').write_text(
            'f=$SECRET_DIR/realm-before-1.json; echo "{}" > "$f"; echo "$f"\n'
        )
        for name in ('restore-zone.sh', 'restore-realm.sh'):
            (mig / name).write_text('[ -f "$1" ] && echo "create=0 update=0 delete=0"\n')
        for f in mig.iterdir():
            f.chmod(0o755)
        body = 'MIG_DIR=%s\n%s\npair zone zone z1\npair realm realm https://kc https://kc\n' % (mig, fn)
        rc, out = self.run_sh(['-c', body])
        self.assertEqual(out, 'create=0 update=0 delete=0\n' * 2, out)
        self.assertEqual(list((self.t / 'ev').glob('zone-before-*')), [])
        self.assertEqual(list((self.t / 'secret').glob('realm-before-*')), [])
        self.assertTrue((self.t / 'ev' / 'env-check' / 'zone-before-z1-1.json').is_file())
        self.assertTrue((self.t / 'secret' / 'env-check' / 'realm-before-1.json').is_file())
        (mig / 'restore-zone.sh').write_text('echo "create=1 failed=1"; exit 1\n')
        rc, out = self.run_sh(['-c', 'set -o pipefail\n%s\npair zone zone z1; echo "rc=$?"' % body.split('\npair')[0]])
        self.assertIn('create=1 failed=1\nrc=1\n', out)
        self.assertEqual(list((self.t / 'ev').glob('zone-before-*')), [])

class HeldReplicas(Sandbox):
    def repo(self, replicas):
        origin = self.t / 'origin.git'
        work = self.t / 'kcd'
        subprocess.run(['git', 'init', '-q', '--bare', str(origin)], check=True)
        subprocess.run(
            ['git', 'clone', '-q', str(origin), str(work)], check=True, capture_output=True
        )
        ov = work / 'overlays' / 'tb-prod'
        ov.mkdir(parents=True)
        (ov / 'kustomization.yaml').write_text('resources:\n  - sts.yaml\n')
        (ov / 'sts.yaml').write_text(
            'apiVersion: apps/v1\nkind: StatefulSet\nmetadata:\n  name: keycloak-customer\n'
            'spec:\n  replicas: %d\n' % replicas
        )
        g = ['git', '-C', str(work), '-c', 'user.name=t', '-c', 'user.email=t@example.test']
        subprocess.run([*g, 'add', '-A'], check=True)
        subprocess.run([*g, 'commit', '-qm', 'held'], check=True)
        subprocess.run([*g, 'push', '-q', 'origin', 'HEAD:refs/pull/7/head'], check=True)
        return work

    def held(self, replicas, want):
        self.real_kubectl()
        work = self.repo(replicas)
        defs = HERE / 'build-cutover-env.sh'
        text = defs.read_text()
        fns = ''.join(
            text[text.index(n) : text.index('\n}\n', text.index(n)) + 3]
            for n in ('render_ref() {', 'held_replicas() {')
        )
        (self.t / 'fns.sh').write_text(fns)
        script = '. %s/target.sh\n. %s\nheld_replicas %s 7 StatefulSet keycloak-customer %s\n' % (
            HERE,
            self.t / 'fns.sh',
            work,
            want,
        )
        return self.run_sh(['-c', script], TARGET_ENV='tb-prod')

    def test_same_count_passes(self):
        rc, out = self.held(3, 3)
        self.assertEqual(rc, 0, out)
        self.assertIn('#7 head renders keycloak-customer replicas=3', out)

    def test_a_held_pr_that_changes_the_count_fails(self):
        rc, out = self.held(1, 3)
        self.assertEqual(rc, 1, out)
        self.assertIn('replicas=1, env file has 3', out)


class KubeconfigGuard(Sandbox):
    def guard(self, script, **env):
        self.rules([{'tool': 'kubectl', 'match': '.', 'out': 'reached\n'}])
        return self.run_sh(
            ['-c', '. %s/target.sh\n%s' % (HERE, script)], TARGET_ENV='tb-stage', **env
        )

    def test_empty_value_refuses_before_kubectl_runs(self):
        rc, out = self.guard('kubectl --kubeconfig "$KUBECONFIG_TARGET" get pods')
        self.assertEqual(rc, 64)
        self.assertIn('--kubeconfig is empty', out)
        self.assertEqual(self.log(), '')

    def test_a_trailing_kubeconfig_flag_refuses(self):
        rc, out = self.guard('kubectl get pods --kubeconfig')
        self.assertEqual(rc, 64)
        self.assertIn('--kubeconfig has no value', out)
        self.assertEqual(self.log(), '')

    def test_a_directory_or_unreadable_kubeconfig_refuses(self):
        (self.t / 'kc-dir').mkdir()
        kc = self.t / 'kc-locked'
        kc.write_text('clusters:\n- name: arn:aws:eks:x:1:cluster/mzla-eks-tb-prod01\n')
        kc.chmod(0)
        for path in ('kc-dir', 'kc-locked'):
            with self.subTest(path=path):
                rc, out = self.guard('kubectl --kubeconfig %s/%s get pods' % (self.t, path))
                self.assertEqual(rc, 64, out)
                self.assertIn('cannot be checked', out)
                self.assertEqual(self.log(), '')

    def test_missing_file_refuses(self):
        rc, out = self.guard('kubectl --kubeconfig=%s/nope get pods' % self.t)
        self.assertEqual(rc, 64)
        self.assertIn('does not exist', out)
        self.assertEqual(self.log(), '')

    def test_a_call_without_a_kubeconfig_refuses_except_kustomize(self):
        rc, out = self.guard('kubectl get pods -n thundermail')
        self.assertEqual(rc, 64, out)
        self.assertIn('no --kubeconfig, kubectl would use the current context', out)
        self.assertEqual(self.log(), '')
        rc, out = self.guard('kubectl kustomize %s' % self.t)
        self.assertEqual((rc, out), (0, 'reached\n'))

    def test_existing_file_reaches_kubectl(self):
        rc, out = self.guard('kubectl --kubeconfig %s/kc-target get pods' % self.t)
        self.assertEqual((rc, out), (0, 'reached\n'))

    def test_every_library_entry_point_is_guarded(self):
        calls = ('ktarget get pods', 'khub get pods', 'pod true', '"${KS[@]}" get pods')
        for call in (*calls, '"${KH[@]}" get pods'):
            with self.subTest(call=call):
                self.setUp()
                script = (
                    'set -u\nMIG_DIR=%s\n. %s/lib.sh\n. %s/timing.sh\n'
                    'KUBECONFIG_TARGET=%s/gone KUBECONFIG_HUB=%s/gone\n'
                    'eval "$(grep -E \'^K[SH]=\' %s/window/lib.sh)"\n%s\n'
                ) % (HERE, HERE, HERE, self.t, self.t, HERE, call)
                rc, out = self.guard(script)
                self.assertEqual(rc, 64, out)
                self.assertEqual(self.log(), '')

    def test_a_kubeconfig_of_the_other_environment_refuses(self):
        (self.t / 'kc-target').write_text(
            'clusters:\n- name: arn:aws:eks:x:1:cluster/mzla-eks-tb-prod01\n'
        )
        rc, out = self.guard('kubectl --kubeconfig %s/kc-target get pods' % self.t)
        self.assertEqual(rc, 64)
        self.assertIn('names mzla-eks-tb-prod01, not a tb-stage cluster', out)
        self.assertEqual(self.log(), '')

    def test_prod_target_kubeconfig_must_name_the_prod_cluster(self):
        self.rules([{'tool': 'kubectl', 'match': '.', 'out': 'reached\n'}])
        kc = self.t / 'kc-target'
        script = '. %s/target.sh\nkubectl --kubeconfig "$KUBECONFIG_TARGET" get pods' % HERE
        rc, out = self.run_sh(['-c', script], TARGET_ENV='tb-prod', KUBECONFIG_TARGET=str(kc))
        self.assertEqual(rc, 64)
        self.assertIn('is not cluster mzla-eks-tb-prod01', out)
        kc.write_text(kubeconfig('kc-prod', 'mzla-eks-tb-prod01'))
        rc, out = self.run_sh(['-c', script], TARGET_ENV='tb-prod', KUBECONFIG_TARGET=str(kc))
        self.assertEqual((rc, out), (0, 'reached\n'))

    def test_prod_target_current_context_must_be_the_prod_cluster(self):
        self.rules([{'tool': 'kubectl', 'match': '.', 'out': 'reached\n'}])
        kc = self.t / 'kc-target'
        kc.write_text(kubeconfig('hub', 'mzla-eks-tb-prod01', 'mzla-eks-hub01'))
        script = '. %s/target.sh\nkubectl --kubeconfig "$KUBECONFIG_TARGET" get pods' % HERE
        rc, out = self.run_sh(['-c', script], TARGET_ENV='tb-prod', KUBECONFIG_TARGET=str(kc))
        self.assertEqual(rc, 64, out)
        self.assertIn('is not cluster mzla-eks-tb-prod01', out)
        self.assertEqual(self.log(), '')

    def test_flags_that_pick_another_cluster_refuse(self):
        for flag in (
            '--context hub',
            '--context=hub',
            '--server https://x',
            '--server=https://x',
            '-s https://x',
            '-shttps://x',
            '--cluster c',
            '--token t',
            '--user u',
            '--as admin',
            '--client-key=k',
            '--insecure-skip-tls-verify',
            '--certificate-authority ca',
            '--username u',
            '--proxy-url http://p',
        ):
            with self.subTest(flag=flag):
                rc, out = self.guard(
                    'kubectl --kubeconfig %s/kc-target %s get pods' % (self.t, flag)
                )
                self.assertEqual(rc, 64, out)
                self.assertEqual(self.log(), '')

    def test_arguments_after_a_double_dash_belong_to_the_remote_command(self):
        rc, out = self.guard(
            'kubectl --kubeconfig %s/kc-target exec runner -- psql -s --user u' % self.t
        )
        self.assertEqual((rc, out), (0, 'reached\n'))

    def test_prereqs_default_target_kubeconfig_is_checked(self):
        self.rules([{'tool': 'kubectl', 'match': '.', 'out': 'yes\n'}])
        work = self.t / 'work'
        work.mkdir()
        (work / 'kc-prod').write_text(kubeconfig('hub', 'mzla-eks-hub01'))
        rc, out = self.run_sh(
            [str(HERE / 'check-prereqs.sh'), '--for', 'go-W1'],
            TARGET_ENV='tb-prod',
            WORK_DIR=str(work),
        )
        self.assertRegex(out, r'(?m)^NEED  kube-tb-prod ')
        self.assertNotIn('auth can-i', self.log())

    def test_prod_target_rbac_covers_the_apply_pod_attach_and_log(self):
        self.rules(
            [
                {'tool': 'kubectl', 'match': '--subresource=attach', 'out': 'no\n'},
                {'tool': 'kubectl', 'match': 'auth can-i', 'out': 'yes\n'},
            ]
        )
        work = self.t / 'work'
        work.mkdir()
        (work / 'kc-prod').write_text(kubeconfig('kc-prod', 'mzla-eks-tb-prod01'))
        (work / 'kc-stage').write_text(kubeconfig('kc-stage', 'mzla-eks-tb-stage01'))
        rc, out = self.run_sh(
            [str(HERE / 'check-prereqs.sh'), '--for', 'go-W4'], TARGET_ENV='tb-prod', WORK_DIR=str(work)
        )
        self.assertRegex(out, r'(?m)^NEED  kube-tb-prod +can-i no: create:pods/attach@thundermail$')
        self.assertIn('auth can-i get pods -n thundermail --subresource=log', self.log())
        (self.t / 'fake.log').unlink()
        rc, out = self.run_sh(
            [str(HERE / 'check-prereqs.sh'), '--for', 'go-W4'], TARGET_ENV='tb-stage', WORK_DIR=str(work)
        )
        self.assertIn('auth can-i', self.log())
        self.assertNotIn('--subresource=attach', self.log())
        self.assertNotIn('--subresource=log', self.log().replace('get pods -n accounts --subresource=log', ''))

    def test_the_stage_kubeconfig_name_still_works(self):
        rc, out = self.guard(
            'kubectl --kubeconfig "$KUBECONFIG_TARGET" get pods',
            KUBECONFIG_STAGE=str(self.t / 'kc-target'),
        )
        self.assertEqual((rc, out), (0, 'reached\n'))


class TargetEnv(Sandbox):
    def scripts(self):
        return sorted(
            p.relative_to(HERE).as_posix()
            for p in HERE.rglob('*.sh')
            if p.relative_to(HERE).as_posix() not in LIBS and 'targets' not in p.parts
        )

    def test_every_script_exits_64_without_target_env(self):
        for rel in self.scripts():
            with self.subTest(script=rel):
                rc, out = self.run_sh([str(HERE / rel)])
                self.assertEqual(rc, 64, out)
                self.assertIn('set TARGET_ENV', out)
        self.assertEqual(self.log(), '')

    def test_stage_only_scripts_refuse_tb_prod(self):
        for rel in ('window/w7.sh', 'post/p2-redirects.sh', 'post/p2-verify.sh'):
            with self.subTest(script=rel):
                rc, out = self.run_sh([str(HERE / rel), '--dry-run'], TARGET_ENV='tb-prod')
                self.assertEqual(rc, 64, out)
                self.assertIn('is a tb-stage step', out)

    def test_window_lib_refuses_an_env_file_of_the_other_environment(self):
        f = self.t / 'shm' / 'cutover.env'
        f.write_text("TARGET_ENV='tb-stage'\n")
        f.chmod(0o600)
        rc, out = self.run_sh([str(HERE / 'window' / 'w7-since-flip.sh')], TARGET_ENV='tb-prod')
        self.assertEqual(rc, 64, out)
        self.assertIn('built for TARGET_ENV=tb-stage, not tb-prod', out)

    def test_prod_takes_stalwart_url_from_the_env_file_and_the_lane_port(self):
        f = self.t / 'shm' / 'cutover.env'
        f.write_text("TARGET_ENV='tb-prod'\nSTALWART_URL='http://localhost:18081'\n")
        f.chmod(0o600)
        for lib in (HERE / 'window' / 'lib.sh', HERE / 'lib.sh'):
            script = 'set -- --dry-run\n. %s\necho "url=${STALWART_URL:-}"' % lib
            for port, want in ((None, 'url=http://localhost:18081'), ('18555', 'url=http://localhost:18555')):
                with self.subTest(lib=lib.name, port=port):
                    rc, out = self.run_sh(
                        ['-c', script],
                        TARGET_ENV='tb-prod',
                        STALWART_URL='http://localhost:19999',
                        LANE_PF_PORT=port,
                    )
                    self.assertIn(want + '\n', out)

    def test_a_stage_env_file_with_the_old_kubeconfig_name_still_works(self):
        f = self.t / 'shm' / 'cutover.env'
        f.write_text("KUBECONFIG_STAGE='%s'\n" % (self.t / 'kc-target'))
        f.chmod(0o600)
        rc, out = self.run_sh(['-c', '. %s/lib.sh\necho "kc=$KUBECONFIG_TARGET"' % HERE], TARGET_ENV='tb-stage')
        self.assertEqual(out, 'kc=%s\n' % (self.t / 'kc-target'))

    def test_an_empty_stalwart_url_names_its_source(self):
        f = self.t / 'shm' / 'cutover.env'
        f.write_text("TARGET_ENV='tb-prod'\nSTALWART_URL=''\n")
        f.chmod(0o600)
        rc, out = self.run_sh([str(HERE / 'quota-reset.sh')], TARGET_ENV='tb-prod')
        self.assertEqual(rc, 1, out)
        self.assertIn('missing STALWART_URL: not in %s or the environment; build-cutover-env.sh writes http://localhost:18081' % f, out)
        self.assertEqual(self.log(), '')

    def test_scripts_read_values_appended_after_the_shell_loaded_the_file(self):
        f = self.t / 'shm' / 'cutover.env'
        f.write_text(
            "TARGET_ENV='tb-prod'\nTARGET_NLB_DNS=''\nFREIGHT='f1'\n"
            "CUT_ECS_END='2026-11-20T01:00:00Z'\nTARGET_NLB_DNS='nlb.example.test'\nFREIGHT='f2'\n"
        )
        f.chmod(0o600)
        script = '. %s/lib.sh\necho "$CUT_ECS_END $TARGET_NLB_DNS $FREIGHT"' % HERE
        rc, out = self.run_sh(
            ['-c', script], TARGET_ENV='tb-prod', TARGET_NLB_DNS='', FREIGHT='f1'
        )
        self.assertEqual(out, '2026-11-20T01:00:00Z nlb.example.test f2\n', out)

    def test_the_builder_does_not_load_the_previous_env_file_over_the_environment(self):
        f = self.t / 'shm' / 'cutover.env'
        f.write_text("TARGET_ENV='tb-prod'\nT6_PR='5'\n")
        f.chmod(0o600)
        self.rules(stage_rules(ecs='accounts-prod'))
        self.run_sh(
            [str(HERE / 'build-cutover-env.sh')],
            TARGET_ENV='tb-prod',
            T6_PR='6',
            KUBECONFIG_TARGET=str(self.t / 'kc-target'),
            KUBECONFIG_HUB=str(self.t / 'kc-hub'),
            KUBECONFIG_DEV=str(self.t / 'kc-dev'),
        )
        self.assertIn("T6_PR='6'", f.read_text())

    def test_no_stage_literal_outside_the_stage_definitions(self):
        pattern = re.compile(
            r'tb-stage|tbstage|mzla-tb-stage|stage-thundermail|mailstrom/stage/|accounts/stage/'
            r'|mailstrom-stage|accounts-stage|-stage\.tb\.pro|KUBECONFIG_STAGE|STAGE_AWS_PROFILE'
            r'|\bkstage\b|aws_stage|stage\.env'
        )
        allowed = {'targets/tb-stage.sh', 'verify/tb-stage.env', 'target.sh'}
        hits = []
        for p in sorted(HERE.rglob('*')):
            rel = p.relative_to(HERE).as_posix()
            if not p.is_file() or '__pycache__' in p.parts or rel in allowed:
                continue
            if p.suffix == '.md' or p.name.startswith('test_'):
                continue
            for n, line in enumerate(p.read_text(errors='replace').splitlines(), 1):
                if pattern.search(line):
                    hits.append('%s:%d: %s' % (rel, n, line.strip()[:120]))
        self.assertEqual(hits, [])


class Prereqs(Sandbox):
    def prereqs(self, *args, env='tb-stage'):
        self.rules([{'tool': 'curl', 'match': '.', 'out': '000'}])
        return self.run_sh([str(HERE / 'check-prereqs.sh'), *args], TARGET_ENV=env)

    def test_every_gate_id_is_accepted(self):
        for gid in GATE_IDS:
            with self.subTest(gid=gid):
                rc, out = self.prereqs('--for', gid)
                self.assertNotEqual(rc, 64, out)
                self.assertTrue(out.rstrip().endswith('(--for %s)' % gid), out[-300:])

    def test_a_manual_mailbox_check_skips_the_imap_login(self):
        shm = self.t / 'shm'
        shm.mkdir(exist_ok=True, mode=0o700)
        base = (
            "TARGET_ENV='tb-stage'\nTEST_MAILBOX='box@example.test'\nTEST_MAILBOX_IMAP_HOST='imap.example.test'\n"
            "TEST_MAILBOX_IMAP_USER='box@example.test'\nTEST_MAILBOX_IMAP_PASSWORD='imap-pw-1'\n"
        )
        f = shm / 'cutover.env'
        for extra, row in (('', r'(?m)^(HAVE|NEED)  test-mailbox-external '), ("TEST_MAILBOX_CHECK='manual'\n", None)):
            with self.subTest(manual=bool(extra)):
                f.write_text(base + extra)
                f.chmod(0o600)
                (self.t / 'fake.log').write_text('')
                rc, out = self.prereqs('--for', 'go-open')
                if row:
                    self.assertRegex(out, row)
                    self.assertIn('openssl', self.log())
                else:
                    self.assertRegex(
                        out,
                        r'(?m)^SKIP  test-mailbox-external +TEST_MAILBOX_CHECK=manual: the operator reads '
                        r'TEST_MAILBOX by hand; no IMAP login$',
                    )
                    self.assertNotIn('openssl', self.log())
        f.write_text(base + "TEST_MAILBOX_CHECK='web'\n")
        rc, out = self.prereqs('--for', 'go-open')
        self.assertEqual(rc, 64, out)
        self.assertIn("TEST_MAILBOX_CHECK=web in %s: the only value is manual" % f, out)

    def test_p3_verify_stops_at_once_in_manual_mode(self):
        shm = self.t / 'shm'
        shm.mkdir(exist_ok=True, mode=0o700)
        f = shm / 'cutover.env'
        base = "TARGET_ENV='tb-stage'\nTEST_USER='u@example.test'\nTEST_MAILBOX='box@example.test'\n"
        for extra, want in (
            ("TEST_MAILBOX_CHECK='manual'\n", 'TEST_MAILBOX_CHECK=manual: p3-verify.sh reads TEST_MAILBOX over IMAP and needs '
             'TEST_MAILBOX_IMAP_HOST, TEST_MAILBOX_IMAP_USER and TEST_MAILBOX_IMAP_PASSWORD; check the reset mail in '
             'TEST_MAILBOX by hand instead'),
            ("TEST_MAILBOX_CHECK='web'\n", 'TEST_MAILBOX_CHECK=web: the only value is manual'),
            ('', 'missing TEST_MAILBOX_IMAP_HOST'),
        ):
            with self.subTest(extra=extra):
                f.write_text(base + extra)
                f.chmod(0o600)
                (self.t / 'fake.log').write_text('')
                rc, out = self.run_sh([str(HERE / 'post' / 'p3-verify.sh'), '--apply'], TARGET_ENV='tb-stage')
                self.assertEqual(rc, 1, out)
                self.assertIn(want, out)
                self.assertEqual(self.log(), '')

    def test_old_keys_are_a_usage_error(self):
        for key in ('T0', 'W1', 'T7', 'P1', 'V1'):
            with self.subTest(key=key):
                rc, out = self.prereqs('--for', key)
                self.assertEqual(rc, 64, out)

    def test_a_list_prints_one_block_per_id(self):
        rc, out = self.prereqs('--for', 'go-W1,pre-cut', '--for', 'go-W2')
        self.assertNotEqual(rc, 64)
        self.assertEqual(re.findall(r'(?m)^== --for (\S+)$', out), ['go-W1', 'pre-cut', 'go-W2'])
        self.assertRegex(out, r'NEED \d+ across go-W1 pre-cut go-W2')

    def test_without_for_every_row_prints(self):
        rc, out = self.prereqs()
        rows = re.findall(r'(?m)^(?:HAVE|NEED|SKIP)  (\S+)', out)
        self.assertGreater(len(rows), 45)
        self.assertEqual(len(rows), len(set(rows)))
        self.assertNotIn('checkout', rows)

    def test_backups_rows(self):
        _, out = self.prereqs('--for', 'backups', env='tb-prod')
        for item in (
            'legacy-aws-reads',
            'tb-prod-aws-reads',
            'kube-tb-prod',
            'ghcr-login',
            'paddle',
        ):
            self.assertRegex(out, r'(?m)^(HAVE|NEED)  %s ' % re.escape(item))
        self.assertRegex(out, r'(?m)^SKIP  log-export-store ')
        self.assertIn('cloudwatch describe-alarms --profile mzla-legacy', self.log())

    def test_go_w4_has_the_verify_source_side(self):
        _, out = self.prereqs('--for', 'go-W4', env='tb-prod')
        self.assertNotRegex(out, r'(?m)^(HAVE|NEED)  legacy-stalwart-pg-secret ')
        self.assertRegex(out, r'(?m)^(HAVE|NEED)  tool-podman ')
        _, out = self.prereqs('--for', 'go-W4', env='tb-stage')
        self.assertRegex(out, r'(?m)^(HAVE|NEED)  legacy-stalwart-pg-secret ')

    def rows(self, out):
        return re.findall(r'(?m)^(?:HAVE|NEED|SKIP)  (\S+)', out)

    def test_start_holds_only_what_exists_before_the_first_step(self):
        _, out = self.prereqs('--for', 'start', env='tb-prod')
        rows = self.rows(out)
        for item in (
            'checkout',
            'placeholders',
            'sso-mzla-tb-prod',
            'sso-mzla-legacy',
            'kube-tb-prod',
            'kube-hub',
            'cf-token-agent',
            'neon-api-key+neonctl',
            'keycloak-admin-legacy',
            'git-transport',
            'tool-session-manager-plugin',
            'python-venv',
        ):
            self.assertIn(item, rows)
        for item in (
            'stalwart-admin-legacy',
            'test-user-tb-prod',
            'accounts-secret-bundle',
            'keycloak-admin-tb-prod',
            'rollback-restore-files',
            't7-pr-and-revert',
            't8-pr-and-revert',
            'freeze-pr',
            'flip-pr',
            'spf-final-txt',
            'paddle',
        ):
            self.assertNotIn(item, rows)
        self.assertNotIn('127.0.0.1:18080', self.log())

    def test_steps_after_the_legacy_stop_need_no_legacy_admin_api(self):
        for gid in ('go-W5', 'go-predns'):
            with self.subTest(gid=gid):
                _, out = self.prereqs('--for', gid, env='tb-prod')
                rows = self.rows(out)
                self.assertNotIn('stalwart-admin-legacy', rows)
                self.assertNotIn('test-user-tb-prod', rows)
                self.assertIn('stalwart-recovery-admin', rows)

    def test_rehearsal_is_not_an_id(self):
        rc, out = self.prereqs('--for', 'rehearsal')
        self.assertEqual(rc, 64, out)

    def test_prod_rollback_files_are_the_ones_the_window_writes(self):
        f = self.t / 'shm' / 'cutover.env'
        f.write_text("TARGET_ENV='tb-prod'\nEVIDENCE_DIR='%s'\n" % (self.t / 'ev'))
        f.chmod(0o600)
        _, out = self.prereqs('--for', 'go-rollback', env='tb-prod')
        self.assertRegex(out, r'(?m)^NEED  rollback-restore-files +missing: cut-legacy-restore.txt$')
        (self.t / 'ev' / 'cut-legacy-restore.txt').write_text('x\n')
        _, out = self.prereqs('--for', 'go-rollback', env='tb-prod')
        self.assertRegex(out, r'(?m)^HAVE  rollback-restore-files +restore files present$')

    def test_prod_checkout_row_and_paddle_probe(self):
        (self.t / 'home' / '.paddle.env').write_text("PADDLE_API_KEY='k'\n")
        (self.t / 'home' / '.paddle.env').chmod(0o600)
        _, out = self.prereqs('--for', 'go-P1', env='tb-prod')
        self.assertRegex(out, r'(?m)^NEED  checkout +TMD_CHECKOUT or WINDOW_SHA not in')
        self.assertIn('https://api.paddle.com/notification-settings', self.log())
        self.assertNotIn('sandbox-api', self.log())

    def test_checkout_row_passes_on_a_clean_clone_at_window_sha(self):
        work = self.t / 'tmd'
        g = ['git', '-C', str(work), '-c', 'user.name=t', '-c', 'user.email=t@example.test']
        subprocess.run(['git', 'init', '-q', str(work)], check=True)
        subprocess.run([*g, 'commit', '-q', '--allow-empty', '-m', 'w'], check=True)
        sha = subprocess.run([*g, 'rev-parse', 'HEAD'], capture_output=True, text=True).stdout
        f = self.t / 'shm' / 'cutover.env'
        f.write_text(
            "TARGET_ENV='tb-prod'\nTMD_CHECKOUT='%s'\nWINDOW_SHA='%s'\n" % (work, sha.strip())
        )
        self.rules([])
        rc, out = self.run_sh(
            [str(HERE / 'check-prereqs.sh'), '--for', 'auth-dns'], cwd=work, TARGET_ENV='tb-prod'
        )
        self.assertRegex(out, r'(?m)^HAVE  checkout +cwd is TMD_CHECKOUT at WINDOW_SHA, clean')

    def test_merged_repoint_with_revert_on_main_is_have(self):
        f = self.t / 'shm' / 'cutover.env'
        f.write_text("T7_PR='5'\nT7_REVERT_PR='6'\n")
        self.rules(
            [
                {'tool': 'gh', 'match': 'pr view 5 .*headRefName', 'out': ''},
                {'tool': 'gh', 'match': 'pr view 6 .*baseRefName', 'out': 'main\n'},
                {'tool': 'gh', 'match': 'pr view 5 .*--json state -q .state', 'out': 'MERGED\n'},
            ]
        )
        rc, out = self.run_sh(
            [str(HERE / 'check-prereqs.sh'), '--for', 'go-T7'], TARGET_ENV='tb-stage'
        )
        self.assertRegex(
            out, r'(?m)^HAVE  t7-pr-and-revert +T7_PR=#5 merged, T7_REVERT_PR=#6 open on main'
        )


class GateIds(Sandbox):
    CALLED = {
        'w1.sh': ['go-W1'],
        'w2-cut-legacy.sh': ['go-W2'],
        't7.sh': ['go-T7'],
        't8.sh': ['go-T8'],
        'w3-blob-copy.sh': ['go-W4', 'presync', 'backups', 'pre-cut'],
        'w4-migrate.sh': ['go-W4', 'go-W4-read', 'go-W4-reset'],
        'w5-promote.sh': ['go-W5'],
        'w6-verify.sh': ['go-predns', 'go-P1'],
        'w7.sh': ['go-open'],
        'w7-verify.sh': ['go-open'],
        'w8.sh': ['go-W8'],
    }

    def test_every_window_script_passes_an_accepted_gate_id(self):
        text = (HERE / 'window' / 'lib.sh').read_text()
        fn = text[text.index('gate_id() {') : text.index('\n}\n', text.index('gate_id() {')) + 3]
        for script, ids in self.CALLED.items():
            self.assertRegex((HERE / 'window' / script).read_text(), r'\n\s*prereqs\n')
            key = re.sub(r'-.*', '', script[:-3]).upper()
            for i, gid in enumerate(ids):
                with self.subTest(script=script, gid=gid):
                    gate = None if i == 0 else gid
                    rc, got = self.run_sh(['-c', 'KEY=%s\n%sgate_id' % (key, fn)], GATE_ID=gate)
                    self.assertEqual(got.strip(), gid)
                    rc, out = self.run_sh(
                        [str(HERE / 'check-prereqs.sh'), '--for', got.strip()], TARGET_ENV='tb-prod'
                    )
                    self.assertNotEqual(rc, 64, out)


class Runner(Sandbox):
    def test_stage_render_is_the_committed_file(self):
        rc, out = self.run_sh([str(HERE / 'runner-pod.sh')], TARGET_ENV='tb-stage')
        self.assertEqual(rc, 0, out)
        self.assertEqual(out, (HERE / 'runner-pod.yaml').read_text())

    def test_prod_refuses_a_placeholder(self):
        mig = with_placeholder(HERE, self.t / 'mig-ph', 'LEGACY_PG_SECRET_NAME')
        rc, out = self.run_sh([str(mig / 'runner-pod.sh')], TARGET_ENV='tb-prod')
        self.assertEqual(rc, 1)
        self.assertIn('fill it before rendering the runner', out)
        self.assertEqual(out.count('\n'), 1, out)

    def test_prod_renders_with_the_target_values(self):
        rc, out = self.run_sh([str(HERE / 'runner-pod.sh')], TARGET_ENV='tb-prod')
        self.assertEqual(rc, 0, out)
        self.assertIn('secretName: legacy-pg\n', out)
        self.assertNotIn('REPLACE_ME', out)

    def test_prod_render_with_values(self):
        defs = self.t / 'mig'
        shutil.copytree(HERE, defs, ignore=shutil.ignore_patterns('__pycache__'))
        p = defs / 'targets' / 'tb-prod.sh'
        p.write_text(p.read_text() + 'RUNNER_WORK_SIZE=64Gi\n')
        rc, out = self.run_sh([str(defs / 'runner-pod.sh')], TARGET_ENV='tb-prod')
        self.assertEqual(rc, 0, out)
        self.assertIn('    app.kubernetes.io/name: stalwart-config-applier\n', out)
        self.assertIn('secretName: legacy-pg\n', out)
        self.assertIn('legacy-pg has no $k key', out)
        self.assertIn('      emptyDir:\n        sizeLimit: 64Gi\n', out)
        self.assertIn('        sizeLimit: 1Mi\n', out)

    def test_no_stalwart_service_selects_the_runner(self):
        self.real_kubectl()
        labels = {'app.kubernetes.io/name': 'stalwart-config-applier'}
        for overlay in ('overlays/tb-stage-thundermail', 'overlays/tb-prod'):
            with self.subTest(overlay=overlay):
                out = subprocess.run(
                    ['kubectl', 'kustomize', str(REPO / overlay)],
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout
                sel = subprocess.run(
                    ['yq', '-o=json', '-I=0', 'select(.kind == "Service") | .spec.selector'],
                    input=out,
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout.split()
                self.assertTrue(sel)
                for s in map(json.loads, sel):
                    self.assertFalse(s and all(labels.get(k) == v for k, v in s.items()), s)

    def test_secret_render_needs_three_distinct_hosts(self):
        env = {
            'TARGET_ENV': 'tb-stage',
            'STALWART_LEGACY_PG_HOST': 'a',
            'STALWART_LEGACY_PG_PASSWORD': 'p1',
            'KC_LEGACY_PG_HOST': 'b',
            'KC_LEGACY_PG_PASSWORD': 'p2',
            'ACCOUNTS_LEGACY_PG_HOST': 'b',
            'ACCOUNTS_LEGACY_PG_PASSWORD': 'Pw-3!',
        }
        rc, out = self.run_sh([str(HERE / 'runner-pod.sh'), '--secret'], **env)
        self.assertEqual(rc, 1)
        self.assertIn('not distinct', out)
        env['ACCOUNTS_LEGACY_PG_HOST'] = 'c'
        rc, out = self.run_sh([str(HERE / 'runner-pod.sh'), '--secret'], **env)
        self.assertEqual(rc, 0, out)
        path = pathlib.Path(out.strip())
        self.assertEqual(oct(path.stat().st_mode & 0o777), '0o600')
        doc = json.loads(path.read_text())
        self.assertEqual(doc['metadata']['name'], 'rehearsal-legacy-pg')
        self.assertEqual(doc['stringData']['accounts-host'], 'c')
        self.assertNotIn('Pw-3!', out)



def jmap_reply(method, body):
    return json.dumps({'methodResponses': [[method, body, '0']]})


class QuotaReset(Sandbox):
    def setUp(self):
        super().setUp()
        f = self.t / 'shm' / 'cutover.env'
        f.write_text(
            "TARGET_ENV='tb-prod'\nSTALWART_URL='http://127.0.0.1:9'\nSTALWART_USER='admin'\n"
            "STALWART_PASSWORD='recpw1'\nSTALWART_ADMIN_ACCOUNT_ID='a'\n"
        )
        f.chmod(0o600)

    def queue(self, store, account):
        return [
            {'tool': 'curl', 'match': 'x:Task/set', 'out': jmap_reply('x:Task/set', {'created': {'quota-reset': {'id': 't9'}}})},
            {'tool': 'curl', 'match': 'Task/query.*StoreMaintenance', 'out': jmap_reply('x:Task/query', {'ids': store})},
            {'tool': 'curl', 'match': 'Task/query.*AccountMaintenance', 'out': jmap_reply('x:Task/query', {'ids': account})},
            {'tool': 'curl', 'match': 'x:Account/query', 'out': jmap_reply('x:Account/query', {'ids': []})},
        ]

    def tool(self, *args):
        return self.run_sh([str(HERE / 'quota-reset.sh'), *args], TARGET_ENV='tb-prod')

    def test_no_wait_creates_the_task_writes_a_row_and_does_not_poll(self):
        self.rules(self.queue(['s1'], ['a1']))
        rc, out = self.tool('--apply', '--no-wait')
        self.assertEqual(rc, 0, out)
        self.assertRegex(out, r'(?m)^created task t9 due \S+$')
        self.assertIn('not waiting: the reset runs on the server; check it with bash %s/quota-reset.sh status' % HERE, out)
        self.assertNotIn('Task/query', self.log())
        rows = [r.split('\t') for r in (self.t / 'ev' / 'timing-tb-prod.tsv').read_text().splitlines()]
        self.assertEqual([(r[0], r[3], r[4]) for r in rows], [('quota-reset-started', 'task', 't9')])
        rc, out = self.tool('--no-wait')
        self.assertEqual(rc, 0, out)
        self.assertIn('dry-run: then print the task id, write a quota-reset-started row and return without waiting', out)

    def test_status_does_not_report_done_when_the_usage_read_fails(self):
        first = {'tool': 'curl', 'match': 'x:Account/query.*"position":0,', 'out': jmap_reply('x:Account/query', {'ids': ['a1']})}
        self.rules([first] + self.queue([], []))
        rc, out = self.tool('status', 't9')
        self.assertEqual(rc, 1, out)
        self.assertIn('quota reset status: the queues are empty but the account usage read failed: usage not read', out)
        self.assertNotIn('DONE', out)

    def test_status_never_says_done_after_a_failed_read(self):
        cases = {
            'account query fails': [r for r in self.queue([], []) if r['match'] != 'x:Account/query'],
            'task reply without ids': [
                {'tool': 'curl', 'match': 'Task/query', 'out': jmap_reply('x:Task/query', {'notIds': []})},
            ],
        }
        for case, rules in cases.items():
            with self.subTest(case=case):
                self.rules(rules)
                rc, out = self.tool('status', 't9')
                self.assertEqual(rc, 1, out)
                self.assertNotIn('DONE', out)
        self.rules(self.queue([], []))
        rc, out = self.tool('status')
        self.assertEqual(rc, 1, out)
        self.assertIn('no quota-reset-started row after the last cut-ecs row and no task id given', out)

    def test_status_reads_done_running_and_error(self):
        (self.t / 'ev' / 'timing-tb-prod.tsv').write_text('quota-reset-started\tx\tx\ttask\tt9\n')
        self.rules(self.queue([], ['a1', 'a2']))
        rc, out = self.tool('status')
        self.assertEqual(rc, 2, out)
        self.assertIn('quota reset task t9: queued StoreMaintenance=0 AccountMaintenance=2', out)
        self.assertIn('WAIT  quota reset task t9 not finished: 2 account task(s) and 0 store task(s) still queued', out)
        self.rules(self.queue([], []))
        rc, out = self.tool('status')
        self.assertEqual(rc, 0, out)
        self.assertIn('DONE  quota reset finished: accounts=0 with_usage=0 used_bytes=0', out)
        rc, out = self.tool('status', 't7')
        self.assertIn('quota reset task t7: ', out)
        self.rules([])
        rc, out = self.tool('status')
        self.assertEqual(rc, 1, out)
        self.assertIn('quota reset status: x:Task/query failed', out)
        rc, out = self.tool('--wait-forever')
        self.assertEqual(rc, 1, out)
        self.assertIn('usage: quota-reset.sh', out)


class ZoneCapture(Sandbox):
    def test_a_failed_lookup_reports_a_partial_capture(self):
        recs = [{'id': str(i), 'name': 'h%d.example.test' % i, 'type': 'A'} for i in range(450)]
        page = json.dumps({'success': True, 'result': recs, 'result_info': {'total_pages': 1}})
        self.rules(
            [
                {'tool': 'curl', 'match': 'zones/z1/dns_records', 'out': page},
                {
                    'tool': 'curl',
                    'match': 'zones/z1$',
                    'out': '{"result": {"name": "example.test"}}',
                },
                {'tool': 'curl', 'match': 'name=h7\\.example', 'rc': 28},
                {'tool': 'curl', 'match': 'dns-query', 'out': '{"Answer": []}'},
            ]
        )
        rc, out = self.run_sh(
            [str(HERE / 'snapshot-zone.sh'), 'z1'], TARGET_ENV='tb-prod', CF_API_TOKEN_AGENT='t'
        )
        self.assertEqual(rc, 0, out)
        self.assertIn('doh capture partial: 1 of 450 names unread', out)
        self.assertIn('doh_lines=450', out)


def filled(src, dst, keep=None, value=None):
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns('__pycache__'))
    for rel in ('targets/tb-prod.sh', 'verify/tb-prod.env'):
        p = dst / rel
        lines = []
        for line in p.read_text().splitlines():
            name, sep, rest = line.partition('=')
            if sep and name == keep:
                rest = value
            elif sep and 'REPLACE_ME' in rest:
                if name == 'INTERNAL_CIDRS':
                    rest = '10.0.0.0/16'
                elif name == 'RUNNER_WORK_SIZE':
                    rest = '64Gi'
                else:
                    rest = 'filled-' + name.lower().replace('_', '-')
            lines.append(name + sep + rest)
        p.write_text('\n'.join(lines) + '\n')
    return dst


def with_placeholder(src, dst, name):
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns('__pycache__'))
    p = dst / 'targets' / 'tb-prod.sh'
    text = p.read_text()
    assert '\n%s=' % name in text, name
    p.write_text(re.sub(r'(?m)^%s=.*$' % re.escape(name), '%s=REPLACE_ME_%s' % (name, name), text))
    return dst


class Placeholders(Sandbox):
    def test_thaw_refuses_tb_prod_before_any_call(self):
        mig = filled(HERE, self.t / 'mig')
        for root in (HERE, mig):
            for mode in ('--dry-run', '--apply'):
                with self.subTest(root=root.name, mode=mode):
                    rc, out = self.run_sh(
                        [str(root / 'thaw.sh'), mode],
                        TARGET_ENV='tb-prod',
                        FREEZE_PR='7',
                        PI_DIR=str(self.t),
                        THAW_BRANCH='thaw-tb-stage',
                    )
                    self.assertEqual(rc, 64, out)
                    if root == mig or mode == '--dry-run':
                        self.assertEqual(
                            out,
                            'thaw.sh: THAW_BRANCH is not defined in targets/tb-prod.sh, so this'
                            ' script does not run for TARGET_ENV=tb-prod\n',
                        )
        self.assertEqual(self.log(), '')

    def test_thaw_still_runs_on_tb_stage(self):
        self.rules([{'tool': 'gh', 'match': 'pr view 7', 'out': 'OPEN\t\n'}])
        rc, out = self.run_sh(
            [str(HERE / 'thaw.sh'), '--dry-run'], TARGET_ENV='tb-stage', FREEZE_PR='7', PI_DIR=str(self.t)
        )
        self.assertNotIn('is not defined', out)
        self.assertIn('gh pr view 7', self.log())

    def test_prod_build_looks_up_no_freeze_or_repoint_pr(self):
        self.rules(stage_rules(ecs='accounts-prod'))
        self.run_sh(
            [str(HERE / 'build-cutover-env.sh')],
            TARGET_ENV='tb-prod',
            KUBECONFIG_TARGET=str(self.t / 'kc-target'),
            KUBECONFIG_HUB=str(self.t / 'kc-hub'),
            KUBECONFIG_DEV=str(self.t / 'kc-dev'),
            FREEZE_BRANCH='freeze-tb-stage',
            PR_HEAD_PREFIX='tbstage',
        )
        self.assertNotIn('pr list', self.log())
        text = (self.t / 'shm' / 'cutover.env').read_text()
        for name in ('FREEZE_PR', 'T7_PR', 'T7_REVERT_PR', 'T8_PR', 'T8_REVERT_PR'):
            self.assertNotRegex(text, r'(?m)^%s=' % name)

    def test_no_placeholder_is_left_on_tb_prod(self):
        for rel in ('targets/tb-prod.sh', 'verify/tb-prod.env'):
            self.assertNotIn('replace_me', (HERE / rel).read_text().lower(), rel)
        rc, out = self.run_sh(
            ['-c', 'set -- --apply\n. %s/lib.sh\necho reached\n' % HERE], TARGET_ENV='tb-prod'
        )
        self.assertEqual(rc, 0, out)
        self.assertNotIn('refusing', out)
        self.assertIn('reached', out)
        rc, out = self.run_sh(
            [str(HERE / 'check-prereqs.sh'), '--for', 'go-W1'], TARGET_ENV='tb-prod'
        )
        self.assertRegex(
            out,
            r'(?m)^HAVE  placeholders +no REPLACE_ME value in targets/tb-prod.sh or verify/tb-prod.env$',
        )

    def test_every_apply_refuses_while_one_remains(self):
        script = 'set -- --apply\n. %s/lib.sh\necho reached\n'
        mig = with_placeholder(HERE, self.t / 'mig-ph', 'LEGACY_PG_SECRET_NAME')
        rc, out = self.run_sh(['-c', script % mig], TARGET_ENV='tb-prod')
        self.assertEqual(rc, 64, out)
        self.assertNotIn('reached', out)
        for value in ('REPLACE_ME_X', 'replace_me_x', 'REPLACE_ME', 'pre-Replace_Me-post'):
            with self.subTest(value=value):
                mig = filled(HERE, self.t / ('mig-' + str(abs(hash(value)))), 'LEGACY_PG_SECRET_NAME', value)
                rc, out = self.run_sh(['-c', script % mig], TARGET_ENV='tb-prod')
                self.assertEqual(rc, 64, out)
                self.assertIn('LEGACY_PG_SECRET_NAME', out)
        mig = filled(HERE, self.t / 'mig-full')
        rc, out = self.run_sh(['-c', script % mig], TARGET_ENV='tb-prod')
        self.assertEqual(rc, 0, out)
        self.assertIn('reached', out)

    def test_tb_prod_names_the_legacy_pg_secret_and_has_no_e2e_user(self):
        text = (HERE / 'targets' / 'tb-prod.sh').read_text()
        self.assertRegex(text, r'(?m)^LEGACY_PG_SECRET_NAME=legacy-pg$')
        self.assertNotRegex(text, r'(?m)^E2E_USER_SECRET=')
        rc, out = self.run_sh(
            ['-c', 'set -- --apply\n. %s/lib.sh\necho reached\n' % HERE], TARGET_ENV='tb-prod'
        )
        self.assertNotIn('LEGACY_PG_SECRET_NAME', out)
        self.assertNotIn('E2E_USER_SECRET', out)
        rc, out = self.run_sh(
            [str(HERE / 'check-prereqs.sh'), '--for', 'go-W8'], TARGET_ENV='tb-prod'
        )
        self.assertRegex(
            out,
            r'(?m)^SKIP  accounts-e2e-user +tb-prod has no Accounts e2e user \(operator decision\): '
            r'no E2E_USER_SECRET to check$',
        )
        self.assertNotIn('unbound variable', out)
        rc, out = self.run_sh(
            [str(HERE / 'check-prereqs.sh'), '--for', 'go-W8'], TARGET_ENV='tb-stage'
        )
        self.assertRegex(out, r'(?m)^(HAVE|NEED)  accounts-e2e-user ')

    def test_prereqs_and_builder_name_each_one(self):
        mig = filled(HERE, self.t / 'mig', 'INTERNAL_CIDRS', 'replace_me')
        rc, out = self.run_sh(
            [str(mig / 'check-prereqs.sh'), '--for', 'go-W1'], TARGET_ENV='tb-prod'
        )
        self.assertRegex(out, r'(?m)^NEED  placeholders +fill: verify/tb-prod.env:INTERNAL_CIDRS$')
        ph = with_placeholder(HERE, self.t / 'mig-ph', 'LEGACY_PG_SECRET_NAME')
        rc, out = self.run_sh(
            [str(ph / 'check-prereqs.sh'), '--for', 'go-W1'], TARGET_ENV='tb-prod'
        )
        self.assertRegex(out, r'(?m)^NEED  placeholders +fill: .*\bLEGACY_PG_SECRET_NAME\b')
        rc, out = self.run_sh(
            [str(HERE / 'check-prereqs.sh'), '--for', 'go-W1'], TARGET_ENV='tb-stage'
        )
        self.assertNotIn('placeholders', out)
        self.rules(stage_rules(ecs='accounts-prod'))
        rc, out = self.run_sh(
            [str(ph / 'build-cutover-env.sh')],
            TARGET_ENV='tb-prod',
            KUBECONFIG_TARGET=str(self.t / 'kc-target'),
            KUBECONFIG_HUB=str(self.t / 'kc-hub'),
            KUBECONFIG_DEV=str(self.t / 'kc-dev'),
        )
        self.assertNotEqual(rc, 0)
        self.assertRegex(out, r'(?m)^LEGACY_PG_SECRET_NAME +NEED \(placeholder')
        mig = filled(HERE, self.t / 'mig-verify', 'SOURCE_PG_SECRET_ID', 'REPLACE_ME_X')
        rc, out = self.run_sh(
            [str(mig / 'build-cutover-env.sh')],
            TARGET_ENV='tb-prod',
            KUBECONFIG_TARGET=str(self.t / 'kc-target'),
            KUBECONFIG_HUB=str(self.t / 'kc-hub'),
            KUBECONFIG_DEV=str(self.t / 'kc-dev'),
        )
        self.assertRegex(out, r'(?m)^verify/tb-prod.env:SOURCE_PG_SECRET_ID +NEED \(placeholder')

    def test_runner_refuses_each_of_its_names_in_any_case(self):
        for name in ('RUNNER_SGP_LABEL', 'RUNNER_WORK_SIZE', 'LEGACY_PG_SECRET_NAME'):
            with self.subTest(name=name):
                mig = filled(HERE, self.t / ('mig-' + name), name, 'replace_me_x')
                p = mig / 'targets' / 'tb-prod.sh'
                if '\n%s=' % name not in p.read_text():
                    p.write_text(p.read_text() + '%s=replace_me_x\n' % name)
                rc, out = self.run_sh([str(mig / 'runner-pod.sh')], TARGET_ENV='tb-prod')
                self.assertEqual(rc, 1, out)
                self.assertIn(name, out)

    def test_run_all_refuses_a_verify_placeholder(self):
        mig = filled(HERE, self.t / 'mig', 'INTERNAL_CIDRS', 'REPLACE_ME_X')
        rc, out = self.run_sh([str(mig / 'verify' / 'run_all.sh')], TARGET_ENV='tb-prod')
        self.assertEqual(rc, 99, out)
        self.assertIn('placeholder in verify/tb-prod.env:INTERNAL_CIDRS', out)
        self.assertEqual(self.log(), '')



class CrossEnvironment(Sandbox):
    def operator_file(self, name, text):
        d = self.t / 'home' / '.local' / 'state' / 'talos'
        d.mkdir(parents=True, exist_ok=True)
        f = d / name
        f.write_text(text)
        f.chmod(0o600)
        return f

    def prod_build(self, *args):
        self.rules(stage_rules(ecs='accounts-prod'))
        return self.run_sh(
            [str(HERE / 'build-cutover-env.sh'), *args],
            TARGET_ENV='tb-prod',
            OPERATOR_ENV='',
            KUBECONFIG_TARGET=str(self.t / 'kc-target'),
            KUBECONFIG_HUB=str(self.t / 'kc-hub'),
            KUBECONFIG_DEV=str(self.t / 'kc-dev'),
        )

    def test_prod_never_reads_the_stage_operator_file(self):
        self.operator_file('cutover-operator.env', "T7_PR='11'\nADMIN_MERGE='1'\n")
        self.prod_build()
        text = (self.t / 'shm' / 'cutover.env').read_text()
        self.assertNotIn("'11'", text)
        self.assertNotRegex(text, r'(?m)^ADMIN_MERGE=')

    def test_a_prod_operator_file_needs_its_target_line(self):
        f = self.operator_file('cutover-operator-tb-prod.env', "ADMIN_MERGE='1'\n")
        rc, out = self.prod_build()
        self.assertEqual(rc, 64, out)
        self.assertIn('built for TARGET_ENV=<none>, not tb-prod', out)
        f.write_text("TARGET_ENV='tb-prod'\nADMIN_MERGE='1'\n")
        self.prod_build()
        self.assertIn("ADMIN_MERGE='1'", (self.t / 'shm' / 'cutover.env').read_text())

    def test_tb_prod_reads_its_env_file_from_disk_and_refuses_one_others_can_read(self):
        state = self.t / 'home' / '.local' / 'state' / 'talos'
        state.mkdir(parents=True, exist_ok=True)
        state.chmod(0o700)
        f = state / 'cutover-tb-prod.env'
        f.write_text("TARGET_ENV='tb-prod'\nWINDOW_SHA='from-disk'\n")
        f.chmod(0o600)
        show = '. %s/lib.sh; printf "file=%%s sha=%%s\\n" "$ENV_FILE" "${WINDOW_SHA:-}"' % HERE
        rc, out = self.run_sh(['-c', show], TARGET_ENV='tb-prod', CUTOVER_ENV='')
        self.assertEqual(rc, 0, out)
        self.assertEqual(out, 'file=%s sha=from-disk\n' % f)
        rc, out = self.run_sh(
            [str(HERE / 'check-prereqs.sh'), '--for', 'go-W1'], TARGET_ENV='tb-prod', CUTOVER_ENV=''
        )
        self.assertNotIn('refusing', out)
        for path, mode in ((f, 0o640), (state, 0o750)):
            with self.subTest(path=path.name, mode=oct(mode)):
                path.chmod(mode)
                rc, out = self.run_sh(['-c', show], TARGET_ENV='tb-prod', CUTOVER_ENV='')
                self.assertNotEqual(rc, 0, out)
                self.assertRegex(
                    out, r'not owned by \S+, or open to group or others \(want the directory 700 and the file 600\)'
                )
                self.assertNotIn('sha=from-disk', out)
                path.chmod(0o600 if path.is_file() else 0o700)
        stage = state / 'cutover-tb-stage.env'
        stage.write_text("TARGET_ENV='tb-stage'\nWINDOW_SHA='stage-disk'\n")
        stage.chmod(0o640)
        state.chmod(0o750)
        rc, out = self.run_sh(['-c', show], TARGET_ENV='tb-stage', CUTOVER_ENV='')
        self.assertEqual(rc, 0, out)
        self.assertEqual(out, 'file=%s sha=stage-disk\n' % stage)

    def test_prereqs_never_source_an_env_file_others_can_write(self):
        plant = self.t / 'plant'
        plant.mkdir()
        plant.chmod(0o777)
        f = plant / 'cutover.env'
        f.write_text(": > %s/PWNED; TARGET_ENV='tb-prod'\n" % self.t)
        f.chmod(0o666)
        for args in (
            [str(HERE / 'check-prereqs.sh'), '--for', 'start'],
            [str(HERE / 'thaw.sh'), '--dry-run'],
            [str(HERE / 'build-cutover-env.sh')],
            [str(HERE / 'build-cutover-env.sh'), '--check'],
        ):
            with self.subTest(script=args[0]):
                rc, out = self.run_sh(args, TARGET_ENV='tb-prod', CUTOVER_ENV=str(f), FREEZE_PR='7', PI_DIR=str(self.t))
                self.assertIn(rc, (1, 64), out)
                self.assertIn('open to group or others (want the directory 700 and the file 600)', out)
                self.assertFalse((self.t / 'PWNED').exists())

    def test_thaw_and_prereqs_refuse_an_env_file_of_the_other_environment(self):
        f = self.t / 'shm' / 'cutover.env'
        f.write_text("TARGET_ENV='tb-prod'\nTHAW_PR='9'\n")
        f.chmod(0o600)
        for args in (
            [str(HERE / 'thaw.sh'), '--apply'],
            [str(HERE / 'check-prereqs.sh'), '--for', 'go-W1'],
        ):
            with self.subTest(script=args[0]):
                rc, out = self.run_sh(args, TARGET_ENV='tb-stage', FREEZE_PR='7', PI_DIR=str(self.t))
                self.assertEqual(rc, 64, out)
                self.assertIn('built for TARGET_ENV=tb-prod, not tb-stage', out)
        self.assertEqual(self.log(), '')
        self.assertEqual(f.read_text(), "TARGET_ENV='tb-prod'\nTHAW_PR='9'\n")

    def test_stage_ignores_inherited_target_values(self):
        names = 'TARGET_AWS_PROFILE STALWART_REPLICAS LEGACY_PG_SECRET_NAME KARGO_STAGE_ACCOUNTS MAIL_ZONE'
        script = '. %s/lib.sh\nfor n in %s; do printf "%%s " "${!n}"; done' % (HERE, names)
        rc, out = self.run_sh(
            ['-c', script],
            TARGET_ENV='tb-stage',
            TARGET_AWS_PROFILE='mzla-tb-prod',
            STALWART_REPLICAS='4',
            LEGACY_PG_SECRET_NAME='prodlegacy-pg',
            KARGO_STAGE_ACCOUNTS='tb-prod',
            MAIL_ZONE='thundermail.com',
        )
        self.assertEqual(
            out, 'mzla-tb-stage 1 rehearsal-legacy-pg tb-stage stage-thundermail.com ', out
        )
        rc, out = self.run_sh(
            ['-c', script], TARGET_ENV='tb-stage', STAGE_AWS_PROFILE='mzla-tb-stage-ro'
        )
        self.assertTrue(out.startswith('mzla-tb-stage-ro '), out)

    def test_a_failing_git_status_is_not_clean(self):
        work = self.t / 'tmd'
        g = ['git', '-C', str(work), '-c', 'user.name=t', '-c', 'user.email=t@example.test']
        subprocess.run(['git', 'init', '-q', str(work)], check=True)
        subprocess.run([*g, 'commit', '-q', '--allow-empty', '-m', 'w'], check=True)
        sha = subprocess.run([*g, 'rev-parse', 'HEAD'], capture_output=True, text=True).stdout
        f = self.t / 'shm' / 'cutover.env'
        f.write_text(
            "TARGET_ENV='tb-prod'\nTMD_CHECKOUT='%s'\nWINDOW_SHA='%s'\n" % (work, sha.strip())
        )
        (self.bin / 'git').unlink()
        (self.bin / 'git').write_text(
            '#!%s\n[ "$1" != status ] || exit 128\nexec %s "$@"\n'
            % (shutil.which('bash'), shutil.which('git'))
        )
        (self.bin / 'git').chmod(0o755)
        rc, out = self.run_sh(
            [str(HERE / 'check-prereqs.sh'), '--for', 'auth-dns'], cwd=work, TARGET_ENV='tb-prod'
        )
        self.assertRegex(out, r'(?m)^NEED  checkout +git status failed in TMD_CHECKOUT')

if __name__ == '__main__':
    os.environ.pop('TARGET_ENV', None)
    unittest.main()
