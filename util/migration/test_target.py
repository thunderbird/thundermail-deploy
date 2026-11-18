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
    'go-W1 go-W2 go-outbound-block go-T7 go-W4 go-T8 go-W5 go-predns go-mx-flip go-access go-open '
    'go-W8 go-P1 go-V2 go-rollback checks presync rehearsal backups verify auth-dns pre-cut'
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

    def test_prod_marks_placeholders_and_writes_the_window_names(self):
        rc, out = self.build(HERE, **self.prod_env())
        self.assertNotEqual(rc, 0)
        self.assertRegex(out, r'KARGO_STAGE_CONFIGS +len=\d+ NEED \(placeholder\)')
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
        self.assertNotRegex(out, r'(?m)^STALWART_URL ')
        text = (self.t / 'shm' / 'cutover.env').read_text()
        self.assertIn("INBOUND_TTL_WAIT='60'", text)
        self.assertIn("NEON_BRANCH_KC='main'", text)
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
        )
        rc, out = self.build(HERE, '--rebuild', **self.prod_env(FREEZE_SHA='abc'))
        text = (self.t / 'shm' / 'cutover.env').read_text()
        self.assertIn("AUTH_DNS_AT='2026-11-20T02:00:00Z'", text)
        self.assertIn("REALM_BEFORE='/x/realm.json'", text)
        self.assertNotIn('leaked', text)
        self.assertRegex(out, r'TEST_SENDER_APP_PASSWORD +not restored')
        self.assertRegex(out, r'STATE_OPEN_AT +not restored')

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

    def test_missing_file_refuses(self):
        rc, out = self.guard('kubectl --kubeconfig=%s/nope get pods' % self.t)
        self.assertEqual(rc, 64)
        self.assertIn('does not exist', out)
        self.assertEqual(self.log(), '')

    def test_existing_file_reaches_kubectl(self):
        rc, out = self.guard('kubectl --kubeconfig %s/kc-target get pods' % self.t)
        self.assertEqual((rc, out), (0, 'reached\n'))

    def test_every_library_entry_point_is_guarded(self):
        for call in ('ktarget get pods', 'khub get pods', 'pod true', '"${KS[@]}" get pods'):
            with self.subTest(call=call):
                self.setUp()
                script = (
                    'set -u\nMIG_DIR=%s\n. %s/lib.sh\n. %s/timing.sh\n'
                    'KS=(kubectl --kubeconfig "${KUBECONFIG_TARGET:-}")\n'
                    'KUBECONFIG_TARGET=%s/gone KUBECONFIG_HUB=%s/gone\n%s\n'
                ) % (HERE, HERE, HERE, self.t, self.t, call)
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
        self.assertIn('does not name mzla-eks-tb-prod01', out)
        kc.write_text('clusters:\n- name: arn:aws:eks:x:1:cluster/mzla-eks-tb-prod01\n')
        rc, out = self.run_sh(['-c', script], TARGET_ENV='tb-prod', KUBECONFIG_TARGET=str(kc))
        self.assertEqual((rc, out), (0, 'reached\n'))

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

    def test_prod_takes_stalwart_url_only_from_the_lane_port(self):
        f = self.t / 'shm' / 'cutover.env'
        f.write_text("TARGET_ENV='tb-prod'\n")
        f.chmod(0o600)
        lib = HERE / 'window' / 'lib.sh'
        script = 'set -- --dry-run\n. %s\necho "url=${STALWART_URL:-}"' % lib
        for port, want in ((None, 'url='), ('18555', 'url=http://localhost:18555')):
            with self.subTest(port=port):
                rc, out = self.run_sh(
                    ['-c', script],
                    TARGET_ENV='tb-prod',
                    STALWART_URL='http://localhost:18081',
                    LANE_PF_PORT=port,
                )
                self.assertIn(want + '\n', out)

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
        self.assertRegex(out, r'(?m)^(HAVE|NEED)  legacy-stalwart-pg-secret ')
        self.assertRegex(out, r'(?m)^(HAVE|NEED)  tool-podman ')

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
        f.write_text("TMD_CHECKOUT='%s'\nWINDOW_SHA='%s'\n" % (work, sha.strip()))
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
        'w4-migrate.sh': ['go-W4'],
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

    def test_prod_refuses_until_the_placeholders_are_filled(self):
        rc, out = self.run_sh([str(HERE / 'runner-pod.sh')], TARGET_ENV='tb-prod')
        self.assertEqual(rc, 1)
        self.assertIn('RUNNER_SGP_LABEL', out)

    def test_prod_render_with_values(self):
        defs = self.t / 'mig'
        shutil.copytree(HERE, defs, ignore=shutil.ignore_patterns('__pycache__'))
        p = defs / 'targets' / 'tb-prod.sh'
        p.write_text(
            p.read_text()
            .replace('REPLACE_ME_RUNNER_SGP_LABEL', 'runner-sgp')
            .replace('REPLACE_ME_RUNNER_WORK_SIZE', '64Gi')
            .replace('REPLACE_ME_LEGACY_PG_SECRET_NAME', 'legacy-pg')
        )
        rc, out = self.run_sh([str(defs / 'runner-pod.sh')], TARGET_ENV='tb-prod')
        self.assertEqual(rc, 0, out)
        self.assertIn('    app.kubernetes.io/name: runner-sgp\n', out)
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
            'ACCOUNTS_LEGACY_PG_PASSWORD': 'p3',
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
        self.assertNotIn('p3', out)


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


if __name__ == '__main__':
    os.environ.pop('TARGET_ENV', None)
    unittest.main()
