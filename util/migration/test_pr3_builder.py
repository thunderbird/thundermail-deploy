import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
import unittest

from test_target import HERE, Sandbox, kubeconfig, sm, stage_rules
from test_window import defs

STALWART_CLI = """#!{bash}
echo "stalwart-cli $*" >> "$FAKE_LOG"
prev=""
for a in "$@"; do
  [ "$prev" != --output ] || [ "$a" = /dev/null ] || echo '{{"kind":"Domain"}}' > "$a"
  prev=$a
done
[ "$1" != query ] || echo d1
"""
HOLDER = """
import fcntl, os, sys, time
lock, env, ready, first, rest, hold = sys.argv[1:]
fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
f = open(env, 'a')
f.write(first)
f.flush()
open(ready, 'w').close()
time.sleep(float(hold))
f.write(rest)
f.flush()
f.close()
fcntl.flock(fd, fcntl.LOCK_UN)
"""
STAGE = 'deploy-thundermail-tb-prod01'


class Base(Sandbox):
    def setUp(self):
        super().setUp()
        if shutil.which('flock'):
            (self.bin / 'flock').symlink_to(shutil.which('flock'))
        cli = self.bin / 'stalwart-cli'
        cli.write_text(STALWART_CLI.format(bash=shutil.which('bash')))
        cli.chmod(0o755)
        self.envfile = self.t / 'shm' / 'cutover.env'

    def env(self, **extra):
        e = super().env(**extra)
        for k, v in extra.items():
            if v is None:
                e.pop(k, None)
        return e

    def prod(self, **extra):
        e = {
            'TARGET_ENV': 'tb-prod',
            'KUBECONFIG_TARGET': str(self.t / 'kc-target'),
            'KUBECONFIG_HUB': str(self.t / 'kc-hub'),
            'KUBECONFIG_DEV': str(self.t / 'kc-dev'),
        }
        e.update(extra)
        return e

    def stage(self, **extra):
        e = {
            'TARGET_ENV': 'tb-stage',
            'KUBECONFIG_STAGE': str(self.t / 'kc-target'),
            'KUBECONFIG_HUB': str(self.t / 'kc-hub'),
            'KUBECONFIG_DEV': str(self.t / 'kc-dev'),
        }
        e.update(extra)
        return e

    def build(self, *args, rules=None, **env):
        self.rules(rules if rules is not None else stage_rules(ecs='accounts-prod'))
        return self.run_sh([str(HERE / 'build-cutover-env.sh'), *args], **env)

    def text(self):
        return self.envfile.read_text()

    def load(self, script, **env):
        body = '. %s/target.sh\nload_cutover_env\n%s' % (HERE, script)
        return self.run_sh(['-c', body], **env)

    def hold(self, first, rest, seconds, lock=None):
        ready = self.t / 'holder.ready'
        p = subprocess.Popen(
            [
                sys.executable,
                '-c',
                HOLDER,
                str(lock or str(self.envfile) + '.lock'),
                str(self.envfile),
                str(ready),
                first,
                rest,
                str(seconds),
            ]
        )
        self.addCleanup(p.wait)
        for _ in range(200):
            if ready.exists():
                return p
            time.sleep(0.05)
        self.fail('lock holder did not start')


class FirstPromotion(Base):
    def stage_rules(self, json_out, jsonpath_out='', jsonpath_rc=0):
        return [
            {
                'tool': 'kubectl',
                'match': r'get stage \S+ -o jsonpath=',
                'out': jsonpath_out,
                'rc': jsonpath_rc,
            },
            {'tool': 'kubectl', 'match': r'get stage \S+ -o json$', 'out': json_out},
        ]

    def snapshot(self, *args, rules=(), **env):
        self.rules(list(rules))
        e = self.prod(
            STALWART_URL='http://127.0.0.1:1',
            STALWART_USER='admin',
            STALWART_PASSWORD='pw-st',
            **env,
        )
        return self.run_sh([str(HERE / 'snapshot-settings.sh'), *args], **e)

    def test_an_empty_history_is_nothing_to_restore(self):
        cases = (
            ('array index error', '{"status": {"freightHistory": []}}', 1),
            ('empty read', '{"status": {}}', 0),
        )
        for name, json_out, rc in cases:
            for args, prefix in (((), ''), (('--dry-run',), 'dryrun-')):
                with self.subTest(case=name, args=args):
                    rc_, out = self.snapshot(*args, rules=self.stage_rules(json_out, '', rc))
                    self.assertEqual(rc_, 0, out)
                    self.assertIn(
                        'freight=none: stage %s has no Freight history, so there is no Freight'
                        ' to restore' % STAGE,
                        out,
                    )
                    last = out.strip().splitlines()[-1]
                    self.assertEqual(
                        last,
                        str(self.t / 'ev' / ('%sfreight-before-20261118T000000Z.txt' % prefix)),
                    )
                    self.assertEqual(pathlib.Path(last).read_text(), 'none\n')

    def test_a_history_without_a_freight_name_stops(self):
        rc, out = self.snapshot(
            rules=self.stage_rules('{"status": {"freightHistory": [{"items": {}}]}}')
        )
        self.assertEqual(rc, 1)
        self.assertIn('stage %s has 1 Freight history entries but no Freight name' % STAGE, out)

    def test_a_failed_stage_read_stops(self):
        rules = [{'tool': 'kubectl', 'match': 'get stage', 'rc': 1}]
        rc, out = self.snapshot(rules=rules)
        self.assertEqual(rc, 1)
        self.assertIn('kubectl get stage %s failed: Freight before W5 not read' % STAGE, out)

    def w5(self, apply, rules, target='tb-prod'):
        mig = HERE
        src = (
            defs(mig / 'lib.sh')
            + defs(mig / 'timing.sh')
            + defs(mig / 'window' / 'lib.sh')
            + defs(mig / 'window' / 'w5-promote.sh')
        )
        (self.t / 'defs.sh').write_text(src)
        suf = '' if apply else '.dry-run'
        body = (
            'set -u\nAPPLY=%d KEY=W5 SUF=%s NEEDS=0 PF="" PF_OK=1 MAIL_PF="" GO=""\n'
            'SECRET_NAMES=""\n'
            'MIG_DIR=%s ENV_FILE=%s RESTORE=%s/w5-promote-restore%s.txt\n'
            'S=%s PROMOTION_RESTORE=%s/promotion-configs-restore.yaml\n'
            'KS=(kubectl --kubeconfig %s) KH=(kubectl --kubeconfig %s)\n'
            '. %s/target.sh\n. %s/defs.sh\n'
            'snap_settings; echo "snap rc=$?"\nnot_older f9; echo "older rc=$?"\n'
        ) % (
            apply,
            repr(suf),
            mig,
            self.envfile,
            self.t / 'ev',
            suf,
            self.t / 'secret',
            self.t / 'ev',
            self.t / 'kc-target',
            self.t / 'kc-hub',
            mig,
            self.t,
        )
        self.rules(rules)
        e = dict(
            STALWART_URL='http://127.0.0.1:1',
            STALWART_USER='admin',
            STALWART_PASSWORD='pw-st',
            TIMING_TSV=str(self.t / 'ev' / 'timing.tsv'),
            KUBECONFIG_HUB=str(self.t / 'kc-hub'),
        )
        e.update(self.prod() if target == 'tb-prod' else self.stage())
        rc, out = self.run_sh(['-c', body], **e)
        restore = self.t / 'ev' / ('w5-promote-restore%s.txt' % suf)
        tsv = self.t / 'ev' / 'timing.tsv'
        return (
            out,
            restore.read_text() if restore.exists() else '',
            tsv.read_text() if tsv.exists() else '',
        )

    def test_w5_settings_snapshot_passes_and_records_no_freight(self):
        rules = self.stage_rules('{"status": {"freightHistory": []}}', '', 1)
        for apply, row in ((1, 'freight-before'), (0, 'dryrun-freight-before')):
            with self.subTest(apply=apply):
                self.setUp()
                out, restore, tsv = self.w5(apply, rules)
                self.assertIn('snap rc=0', out)
                self.assertNotIn('no freight-before file', out)
                first = restore.splitlines()[0]
                self.assertTrue(
                    first.startswith(
                        '# no Freight before W5: stage %s had no Freight history' % STAGE
                    ),
                    restore,
                )
                self.assertNotIn('create -f', restore)
                self.assertIn('stalwart-cli apply --file', restore)
                self.assertRegex(tsv, r'(?m)^%s\t\S+\t\S+\tfreight\tnone$' % row)
                self.assertIn(
                    'stage Freight before W5: none (the stage had no Freight history)', out
                )
                self.assertIn('older rc=0', out)

    def test_w5_with_a_history_still_promotes_it_back(self):
        rules = self.stage_rules('{}', 'f0')
        out, restore, tsv = self.w5(1, rules, target='tb-stage')
        self.assertIn('snap rc=0', out)
        self.assertIn('create -f %s/promotion-configs-restore.yaml' % (self.t / 'ev'), restore)
        self.assertNotIn('freight-before', tsv)
        self.assertNotIn('-o json\n', self.log())


class OperatorValues(Base):
    OPTIONAL = ('VERIFY_BODY_SAMPLE', 'TEST_MAILBOX_GMAIL', 'TEST_MAILBOX_M365', 'FLIP_PR')

    def test_an_unset_operator_value_does_not_blank_the_shell(self):
        rc, out = self.build(**self.prod())
        text = self.text()
        for name in self.OPTIONAL:
            self.assertNotRegex(text, r'(?m)^%s=' % name)
            self.assertRegex(
                out,
                r'(?m)^%s +len=0 unset \(not written, so a value exported in the shell is kept\)$'
                % name,
            )
        self.assertRegex(out, r'(?m)^TEST_USER +len=0 NEED unset \(not written')
        rc, out = self.load(
            'printf "[%s][%s][%s]" "$VERIFY_BODY_SAMPLE" "$TEST_MAILBOX_GMAIL"'
            ' "$TEST_MAILBOX_M365"',
            TARGET_ENV='tb-prod',
            VERIFY_BODY_SAMPLE='200',
            TEST_MAILBOX_GMAIL='g@example.test',
            TEST_MAILBOX_M365='m@example.test',
        )
        self.assertEqual(out, '[200][g@example.test][m@example.test]')

    def test_a_set_operator_value_is_written(self):
        op = self.t / 'home' / 'op.env'
        op.write_text("TARGET_ENV=tb-prod\nVERIFY_BODY_SAMPLE='50'\nTEST_USER='u@example.test'\n")
        op.chmod(0o600)
        rc, out = self.build(**self.prod(OPERATOR_ENV=str(op), TEST_MAILBOX_GMAIL='g@example.test'))
        text = self.text()
        self.assertIn("VERIFY_BODY_SAMPLE='50'", text)
        self.assertIn("TEST_MAILBOX_GMAIL='g@example.test'", text)
        self.assertIn("TEST_SENDER='u@example.test'", text)
        self.assertRegex(out, r'(?m)^VERIFY_BODY_SAMPLE +len=2$')

    def test_no_legacy_stalwart_password_and_no_dump_age_on_prod(self):
        rc, out = self.build(**self.prod(W4_DUMP_MAX_AGE='3600'))
        text = self.text()
        self.assertIn("STALWART_LEGACY_PG_HOST='ep-leg.example.test'", text)
        self.assertNotIn('STALWART_LEGACY_PG_PASSWORD', text)
        self.assertNotIn('p4ss', text)
        self.assertNotIn('W4_DUMP_MAX_AGE', text)
        self.assertRegex(
            out,
            r'(?m)^STALWART_LEGACY_PG_PASSWORD +not written: tb-prod holds no legacy Stalwart'
            r' database credential$',
        )
        self.envfile.unlink()
        self.build(rules=stage_rules(), **self.stage())
        self.assertIn("STALWART_LEGACY_PG_PASSWORD='p4ss'", self.text())

    def test_prod_reads_no_shared_external_dns_secret(self):
        targets = (HERE / 'targets' / 'tb-prod.sh').read_text()
        self.assertNotRegex(targets, r'(?m)^CF_EXTERNAL_DNS_SECRET=')
        self.build(**self.prod())
        self.assertNotIn('EXTERNAL_DNS', self.log())
        self.assertNotIn('external-dns', self.log())

    def check(self, **env):
        self.rules([])
        return self.run_sh(
            [str(HERE / 'build-cutover-env.sh'), '--check'],
            TIMING_TSV=str(self.t / 'ev' / 'timing.tsv'),
            **env,
        )

    def test_prod_check_has_no_row_only_a_dropped_step_needs(self):
        self.build(**self.prod(WATCH_INTERVAL=None))
        rc, out = self.check(TARGET_ENV='tb-prod')
        for name in (
            'watch-interval',
            'lane-report-max',
            'lane-wait-max',
            't7-pr-replicas',
            't8-pr-replicas',
            'rclone-lsd-legacy',
            'rclone-lsd-target',
            'cf-token-shared-scope',
            'restore-target-db-dry-run',
        ):
            self.assertNotRegex(out, r'(?m)^\S+  %s ' % re.escape(name))
        row = re.search(r'(?m)^\S+  secrets-non-empty +(.*)$', out).group(1)
        self.assertNotIn('STALWART_LEGACY_PG_PASSWORD', row)

    def test_stage_check_keeps_its_rows(self):
        self.build(rules=stage_rules(), **self.stage())
        rc, out = self.check(TARGET_ENV='tb-stage', KUBECONFIG_STAGE=str(self.t / 'kc-target'))
        for name in (
            'rclone-lsd-legacy',
            'rclone-lsd-target',
            'cf-token-shared-scope',
            'restore-target-db-dry-run',
        ):
            self.assertRegex(out, r'(?m)^(PASS|FAIL)  %s ' % re.escape(name))

    def test_prod_check_reads_the_load_balancer_and_the_kargo_configs_stage(self):
        self.build(**self.prod())
        self.rules(
            [
                {'tool': 'kubectl', 'match': 'get stage %s -o name' % STAGE, 'out': 'stage/s'},
                {'tool': 'kubectl', 'match': 'get warehouse \\S+ -o name', 'out': 'warehouse/w'},
            ]
        )
        rc, out = self.run_sh(
            [str(HERE / 'build-cutover-env.sh'), '--check'],
            TIMING_TSV=str(self.t / 'ev' / 'timing.tsv'),
            TARGET_ENV='tb-prod',
        )
        self.assertRegex(out, r'(?m)^PASS  kargo-stage-configs +stage/s$')
        self.assertRegex(out, r'(?m)^PASS  kargo-warehouse-configs +warehouse/w$')
        self.assertRegex(out, r'(?m)^FAIL  target-nlb +env=.* live=none state=none')
        self.assertNotRegex(out, r'(?m)^WAIT  (target-nlb|kargo-)')

    def test_prod_neon_branches_are_each_project_default(self):
        home = self.t / 'home'
        (home / '.neon-api.env').write_text('NEON_API_KEY=neon-key-1\n')
        (home / '.neon-api.env').chmod(0o600)
        neon = re.escape('https://console.neon.tech/api/v2/projects/')
        branches = {
            'pkc': [{'name': 'kc-main', 'default': True}, {'name': 'x', 'default': False}],
            'pacc': [{'name': 'old', 'default': False}, {'name': 'acc-prod', 'default': True}],
        }
        rules = stage_rules(ecs='accounts-prod') + [
            {'tool': 'curl', 'match': neon + '%s/branches$' % p, 'out': json.dumps({'branches': b})}
            for p, b in branches.items()
        ]
        self.build(rules=rules, **self.prod(NEON_PROJECT_KC='pkc', NEON_PROJECT_ACCOUNTS='pacc'))
        text = self.text()
        self.assertIn("NEON_BRANCH_KC='kc-main'", text)
        self.assertIn("NEON_BRANCH_ACCOUNTS='acc-prod'", text)
        self.build(
            rules=rules,
            **self.prod(
                NEON_PROJECT_KC='pkc', NEON_PROJECT_ACCOUNTS='pacc', NEON_BRANCH_KC='chosen'
            ),
        )
        self.assertIn("NEON_BRANCH_KC='chosen'", self.text())
        self.assertNotIn('neon-key-1', self.text())


class EnvLock(Base):
    def test_the_builder_waits_for_a_writer_and_keeps_its_append(self):
        self.build(rules=stage_rules(), **self.stage())
        holder = self.hold('', "FREIGHT='late-freight'\n", 5)
        start = time.monotonic()
        rc, out = self.build(rules=stage_rules(), **self.stage())
        took = time.monotonic() - start
        holder.wait()
        self.assertIn("FREIGHT='late-freight'", self.text())
        self.assertRegex(out, r'(?m)^FREIGHT +len=12 \(carried\)$')
        self.assertGreater(took, 3)
        self.assertEqual(oct(os.stat(str(self.envfile) + '.lock').st_mode & 0o777), '0o600')

    def test_a_reader_waits_for_a_writer_mid_append(self):
        self.envfile.write_text("TARGET_ENV='tb-prod'\n")
        self.envfile.chmod(0o600)
        holder = self.hold("VERIFY_X='par", "tial'\n", 2)
        rc, out = self.load('printf "[%s]" "${VERIFY_X:-}"', TARGET_ENV='tb-prod')
        holder.wait()
        self.assertEqual(out, '[partial]')

    def test_a_reader_creates_the_lock_file_mode_600(self):
        self.envfile.write_text("TARGET_ENV='tb-prod'\nVERIFY_X='v'\n")
        self.envfile.chmod(0o600)
        rc, out = self.load('printf "[%s]" "$VERIFY_X"', TARGET_ENV='tb-prod')
        self.assertEqual(out, '[v]')
        lock = str(self.envfile) + '.lock'
        self.assertEqual(oct(os.stat(lock).st_mode & 0o777), '0o600')

    def test_a_reader_without_flock_still_loads(self):
        (self.bin / 'flock').unlink(missing_ok=True)
        self.envfile.write_text("TARGET_ENV='tb-prod'\nVERIFY_X='v'\n")
        self.envfile.chmod(0o600)
        rc, out = self.load('printf "[%s]" "$VERIFY_X"', TARGET_ENV='tb-prod')
        self.assertEqual(out, '[v]')


class Attempts(Base):
    def setUp(self):
        super().setUp()
        self.state = self.t / 'home' / '.local' / 'state' / 'talos' / 'cutover-tb-prod'

    def run_ns(self, args, **env):
        unshare, mount = shutil.which('unshare'), shutil.which('mount')
        inner = '%s -t tmpfs none /dev/shm || exit 97\nexec "$@"' % mount
        p = subprocess.run(
            [unshare, '-rnm', shutil.which('bash'), '-c', inner, '_', shutil.which('bash'), *args],
            capture_output=True,
            text=True,
            env=self.env(**env),
            cwd=self.t,
            timeout=120,
        )
        return p.returncode, p.stdout + p.stderr

    def build_ns(self, *args, **env):
        self.rules(stage_rules(ecs='accounts-prod'))
        e = self.prod(EVIDENCE_DIR=None, SECRET_DIR=None, TIMING_TSV=None)
        e.update(env)
        return self.run_ns([str(HERE / 'build-cutover-env.sh'), *args], **e)

    def values(self):
        out = {}
        for line in self.text().splitlines():
            m = re.match(r"([A-Z_0-9]+)='(.*)'$", line)
            if m:
                out[m.group(1)] = m.group(2)
        return out

    def test_the_first_attempt_has_its_own_directories(self):
        rc, out = self.build_ns()
        v = self.values()
        a1 = self.state / 'attempt-1'
        self.assertEqual(v['CUTOVER_ATTEMPT'], '1')
        self.assertEqual(v['EVIDENCE_DIR'], str(a1))
        self.assertEqual(v['TIMING_TSV'], str(a1 / 'timing-tb-prod.tsv'))
        self.assertEqual(
            v['SECRET_DIR'],
            str(self.t / 'shm' / 'cutover-tb-prod' / 'attempt-1' / 'secret-artifacts'),
        )
        self.assertTrue(a1.is_dir())

    def test_a_second_attempt_starts_fresh(self):
        self.build_ns()
        with open(self.envfile, 'a') as f:
            f.write(
                "FREIGHT='f1'\nCUT_ECS_END='2026-11-20T01:00:00Z'\nSTALWART_SOURCE_PG_HOST='h1'\n"
            )
        (self.state / 'attempt-1' / 'timing-tb-prod.tsv').write_text('cut-ecs\tx\tx\t\t\n')
        rc, out = self.build_ns(CUTOVER_ATTEMPT='2')
        v = self.values()
        a2 = self.state / 'attempt-2'
        self.assertEqual(v['CUTOVER_ATTEMPT'], '2')
        self.assertEqual(v['EVIDENCE_DIR'], str(a2))
        self.assertEqual(v['TIMING_TSV'], str(a2 / 'timing-tb-prod.tsv'))
        self.assertIn('/attempt-2/secret-artifacts', v['SECRET_DIR'])
        for name in ('FREIGHT', 'CUT_ECS_END', 'STALWART_SOURCE_PG_HOST'):
            self.assertNotIn(name, v)
        self.assertIn(
            'previous env file %s is attempt 1: nothing is carried from it into attempt 2'
            % self.envfile,
            out,
        )
        rc, out = self.build_ns()
        self.assertEqual(self.values()['CUTOVER_ATTEMPT'], '2')

    def test_an_inherited_first_attempt_directory_refuses(self):
        self.build_ns()
        stale = self.values()
        for name in ('EVIDENCE_DIR', 'TIMING_TSV', 'SECRET_DIR'):
            with self.subTest(name=name):
                rc, out = self.build_ns(CUTOVER_ATTEMPT='2', **{name: stale[name]})
                self.assertEqual(rc, 1, out)
                self.assertIn(
                    '%s=%s belongs to another attempt than CUTOVER_ATTEMPT=2' % (name, stale[name]),
                    out,
                )
                self.assertEqual(self.values()['CUTOVER_ATTEMPT'], '1')

    def test_an_earlier_attempt_refuses_once_a_later_one_exists(self):
        self.build_ns()
        self.build_ns(CUTOVER_ATTEMPT='2')
        for attempt in ('1', '0', 'x'):
            with self.subTest(attempt=attempt):
                rc, out = self.build_ns(CUTOVER_ATTEMPT=attempt)
                self.assertEqual(rc, 1, out)
                self.assertEqual(self.values()['CUTOVER_ATTEMPT'], '2')

    def test_rebuild_keeps_the_source_branch_values_of_this_attempt(self):
        self.build_ns()
        with open(self.envfile, 'a') as f:
            f.write(
                "STALWART_SOURCE_PG_HOST='src.example.test'\nSTALWART_SOURCE_PG_DB='stalwart'\n"
                "STALWART_SOURCE_PG_USER='reader'\nSTALWART_SOURCE_PG_PASSWORD='pw-src1'\n"
            )
        rc, out = self.build_ns('--rebuild')
        v = self.values()
        self.assertEqual(v['STALWART_SOURCE_PG_HOST'], 'src.example.test')
        self.assertEqual(v['STALWART_SOURCE_PG_DB'], 'stalwart')
        self.assertEqual(v['STALWART_SOURCE_PG_USER'], 'reader')
        self.assertEqual(v['STALWART_SOURCE_PG_PASSWORD'], 'pw-src1')
        self.assertNotIn('pw-src1', out)
        self.envfile.unlink()
        (self.state / 'attempt-1' / 'window-values.tsv').write_text(
            'STALWART_SOURCE_PG_HOST\tsrc.example.test\nSTALWART_SOURCE_PG_PASSWORD\tleaked\n'
        )
        rc, out = self.build_ns('--rebuild')
        v = self.values()
        self.assertEqual(v['STALWART_SOURCE_PG_HOST'], 'src.example.test')
        self.assertNotIn('STALWART_SOURCE_PG_PASSWORD', v)
        self.assertNotIn('leaked', self.text())

    def test_the_timing_file_default_is_the_attempt_directory(self):
        rc, out = self.run_ns(
            ['-c', '. %s/timing.sh; printf "[%%s]" "$TIMING_TSV"' % HERE],
            TARGET_ENV='tb-prod',
            EVIDENCE_DIR=None,
            TIMING_TSV=None,
        )
        self.assertEqual(out, '[%s]' % (self.state / 'attempt-1' / 'timing-tb-prod.tsv'))

    def test_stage_keeps_its_directories(self):
        self.rules(stage_rules())
        e = self.stage(EVIDENCE_DIR=None, SECRET_DIR=None, TIMING_TSV=None)
        rc, out = self.run_ns([str(HERE / 'build-cutover-env.sh')], **e)
        v = self.values()
        base = self.t / 'home' / '.local' / 'state' / 'talos' / 'cutover-tb-stage'
        self.assertEqual(v['EVIDENCE_DIR'], str(base))
        self.assertEqual(v['TIMING_TSV'], str(base / 'timing-tb-stage.tsv'))
        self.assertEqual(v['SECRET_DIR'], '/dev/shm/talos/cutover-tb-stage/secret-artifacts')
        self.assertNotIn('CUTOVER_ATTEMPT', v)


KUSTOMIZED = """apiVersion: apps/v1
kind: StatefulSet
metadata: {name: keycloak-customer}
spec: {replicas: 2}
---
apiVersion: apps/v1
kind: Deployment
metadata: {name: accounts-web}
spec: {replicas: 3}
---
apiVersion: apps/v1
kind: Deployment
metadata: {name: accounts-celery}
spec: {replicas: 1}
---
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata: {name: stalwart}
spec: {minReplicas: 2}
---
apiVersion: vpcresources.k8s.aws/v1beta1
kind: SecurityGroupPolicy
metadata: {name: stalwart-server}
spec: {securityGroups: {groupIds: [sg-tgt]}}
"""


class ProdBuildBeforeTheWindow(Base):
    def git(self, d, *args):
        subprocess.run(
            ['git', '-c', 'user.name=t', '-c', 'user.email=t@example.test', '-C', str(d), *args],
            check=True,
            capture_output=True,
            env=self.env(),
        )

    def repo(self, d, *files):
        d.mkdir(parents=True, exist_ok=True)
        for f, text in files:
            (d / f).parent.mkdir(parents=True, exist_ok=True)
            (d / f).write_text(text)
        self.git(d, 'init', '-q')
        self.git(d, 'add', '-A')
        self.git(d, 'commit', '-qm', 'x')
        self.git(d, 'update-ref', 'refs/remotes/origin/env/tb-prod', 'HEAD')

    def rules_all(self):
        backend = json.dumps(
            {'host': 'ep-leg-pooler.example.test', 'database': 'd', 'user': 'u', 'password': 'p4ss'}
        )
        neon = 'https://console.neon.tech/api/v2/'
        endpoints = [{'id': e, 'branch_id': 'br-' + e} for e in ('ep-leg', 'ep-db', 'ep-tgt')]
        return [
            {
                'tool': 'kubectl',
                'match': 'get cm stalwart-config',
                'out': json.dumps(
                    {'host': 'ep-tgt-pooler.example.test', 'database': 'st', 'authUsername': 'own'}
                ),
            },
            {'tool': 'kubectl', 'match': 'STALWART_POSTGRESQL_ADMIN_PASSWORD', 'out': 'dGd0cHc='},
            {'tool': 'kubectl', 'match': 'STALWART_USER', 'out': 'YWRtaW4='},
            {'tool': 'kubectl', 'match': 'STALWART_PASSWORD', 'out': 'cmVjcHcx'},
            {'tool': 'kubectl', 'match': 'get svc nginx', 'out': 'nlb.example.test'},
            {'tool': 'kubectl', 'match': 'get ingress', 'out': 'alb.example.test'},
            {
                'tool': 'kubectl',
                'match': 'get application',
                'out': json.dumps({'spec': {'syncPolicy': {'automated': {}}}}),
            },
            {'tool': 'kubectl', 'match': '^kustomize ', 'out': KUSTOMIZED},
            {'tool': 'aws', 'match': 'postgresql_backend', 'out': sm(backend)},
            {'tool': 'aws', 'match': 'database-host', 'out': sm('ep-db-pooler.example.test')},
            {'tool': 'aws', 'match': 'get-secret-value', 'out': sm('value1')},
            {'tool': 'aws', 'match': 's3api list-buckets', 'out': 'legacy-mail-s3-store'},
            {
                'tool': 'aws',
                'match': r'Values=mailstrom-prod-stalwart-\*',
                'out': 'i-1\ti-2\ti-3\ti-4\ti-5\ti-6',
            },
            {'tool': 'aws', 'match': 'describe-instances', 'out': 'i-0abc'},
            {'tool': 'aws', 'match': 'describe-load-balancers', 'out': 'arn:lb/1\tsg-1'},
            {'tool': 'aws', 'match': 'describe-security-group-rules', 'out': 'sgr-1\tsgr-2'},
            {'tool': 'aws', 'match': 'ecs list-clusters', 'out': 'arn:cluster/accounts-prod-web'},
            {'tool': 'aws', 'match': 'ecs list-services', 'out': 'arn:service/accounts-web'},
            {
                'tool': 'aws',
                'match': 'describe-scalable-targets',
                'out': 'service/accounts-prod-web/accounts-web',
            },
            {'tool': 'aws', 'match': 'eks describe-cluster', 'out': 'vpc-1'},
            {'tool': 'aws', 'match': 'describe-nat-gateways', 'out': 'nat-1\tnat-2'},
            {
                'tool': 'curl',
                'match': re.escape(neon + 'projects?limit=400'),
                'out': json.dumps({'projects': [{'id': 'p1'}]}),
            },
            {
                'tool': 'curl',
                'match': re.escape(neon + 'projects/p1/endpoints'),
                'out': json.dumps({'endpoints': endpoints}),
            },
            {
                'tool': 'curl',
                'match': re.escape(neon + 'projects/p1/branches/'),
                'out': json.dumps({'branch': {'name': 'production'}}),
            },
            {
                'tool': 'curl',
                'match': re.escape(neon + 'projects/p1/branches') + '$',
                'out': json.dumps({'branches': [{'name': 'production', 'default': True}]}),
            },
            {
                'tool': 'curl',
                'match': 'zones[?]name=',
                'out': json.dumps({'result': [{'id': 'z1'}]}),
            },
            {
                'tool': 'curl',
                'match': 'dns-query',
                'out': json.dumps({'Answer': [{'type': 1, 'data': '192.0.2.1'}]}),
            },
        ]

    def test_prod_build_exits_0_with_only_the_operator_file(self):
        t = self.t
        repo = t / 'repo'
        shutil.copytree(
            HERE, repo / 'util' / 'migration', ignore=shutil.ignore_patterns('__pycache__')
        )
        tgt = repo / 'util' / 'migration' / 'targets' / 'tb-prod.sh'
        tgt.write_text(
            tgt.read_text()
            .replace('REPLACE_ME_E2E_USER_SECRET', 'mzla/tb-prod/e2e-user')
            .replace('REPLACE_ME_LEGACY_PG_SECRET_NAME', 'legacy-pg')
        )
        values = 'blobStoreBucket: tgt-blob\ninMemoryStoreURL: redis://redis.example.test:6379\n'
        self.repo(repo, ('overlays/tb-prod/stalwart-configs/config/values.yaml', values))
        dirs = {}
        for name in ('KCD_DIR', 'ACD_DIR'):
            dirs[name] = t / name.lower()
            self.repo(dirs[name], ('overlays/tb-prod/kustomization.yaml', 'resources: []\n'))
        for name in ('PI_DIR', 'APPOINTMENT_DIR', 'TMD_DIR'):
            dirs[name] = t / name.lower()
            (dirs[name] / '.git').mkdir(parents=True)
        home = t / 'home'
        for f, line in (
            ('.neon-api.env', 'NEON_API_KEY=neon-key-1\n'),
            ('.prod-cloudflare.env', 'CF_API_TOKEN_AGENT=cf-token-1\n'),
            ('.paddle.env', 'PADDLE_API_KEY=paddle-key-1\n'),
        ):
            (home / f).write_text(line)
            (home / f).chmod(0o600)
        op = home / 'operator.env'
        op.write_text(
            'TARGET_ENV=tb-prod\nTEST_USER=u@example.test\nTEST_MAILBOX=box@example.test\n'
            'TEST_MAILBOX_IMAP_HOST=imap.example.test\nTEST_MAILBOX_IMAP_USER=box@example.test\n'
            'TEST_MAILBOX_IMAP_PASSWORD=imap-pw-1\n'
        )
        op.chmod(0o600)
        (t / 'kc-target').write_text(kubeconfig('kc-prod', 'mzla-eks-tb-prod01'))
        (t / 'kc-hub').write_text(kubeconfig('hub', 'mzla-eks-shared01'))
        self.rules(self.rules_all())
        env = self.prod(OPERATOR_ENV=str(op), **{k: str(v) for k, v in dirs.items()})
        rc, out = self.run_sh([str(repo / 'util' / 'migration' / 'build-cutover-env.sh')], **env)
        self.assertNotRegex(out, r'(?m)^\S+ +len=\d+ (NEED|CONTAINS)')
        self.assertRegex(out, r'(?m)^written \S+ \(mode 600\) vars=\d+ missing=0$')
        self.assertEqual(rc, 0, out)
        for name in ('VERIFY_BODY_SAMPLE', 'WATCH_INTERVAL', 'FREEZE_PR', 'T7_PR', 'SPF_FINAL_TXT'):
            self.assertRegex(out, r'(?m)^%s +len=0 unset \(not written' % name)
        text = self.text()
        self.assertIn("TARGET_NLB_DNS='nlb.example.test'", text)
        self.assertIn("NEON_BRANCH_KC='production'", text)
        self.assertNotIn('neon-key-1', out)


if __name__ == '__main__':
    unittest.main()
