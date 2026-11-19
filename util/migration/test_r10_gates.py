import importlib
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
Prod = importlib.import_module('test_pr3_w4').Prod

CUT = ('cut-ecs', '2026-11-19T04:00:00Z', 'tasks', '0')


def tsv(rows):
    return ''.join('%s\t%s\t%s\t%s\t%s\n' % (n, t, t, m, v) for n, t, m, v in rows)


class Gate(unittest.TestCase):
    def gate(self, name, rows):
        t = pathlib.Path(tempfile.mkdtemp())
        (t / 'timing.tsv').write_text(tsv(rows))
        p = subprocess.run(
            ['bash', str(HERE / 'window' / 'gate.sh'), name],
            capture_output=True,
            text=True,
            env={
                'PATH': '/usr/bin:/bin',
                'HOME': str(t),
                'TARGET_ENV': 'tb-stage',
                'CUTOVER_ENV': str(t / 'none.env'),
                'TIMING_TSV': str(t / 'timing.tsv'),
            },
            timeout=60,
        )
        return p.returncode, p.stdout + p.stderr

    def test_stop_legacy_needs_a_drained_queue_then_the_read_done_row(self):
        q0 = ('queue-drain', '2026-11-19T04:40:00Z', 'queued', '0')
        done = ('w4-read-done', '2026-11-19T04:41:00Z', 'export', 'x')
        rc, out = self.gate('stop-legacy', [CUT, q0, done])
        self.assertEqual(rc, 0, out)
        self.assertEqual(
            out,
            'GO    stop-legacy: queue-drain 0 at 2026-11-19T04:40:00Z, w4-read-done at '
            '2026-11-19T04:41:00Z; legacy Stalwart may be stopped\n',
        )
        rc, out = self.gate('stop-legacy', [CUT, ('queue-drain', 'x', 'queued', '3'), done])
        self.assertEqual((rc, out), (2, 'WAIT  stop-legacy: the last queue-drain row has value 3, not 0\n'))
        rc, out = self.gate('stop-legacy', [CUT, q0])
        self.assertEqual(rc, 2)
        self.assertIn('no w4-read-done row after the last cut-ecs row', out)
        rc, out = self.gate('stop-legacy', [q0, done, CUT])
        self.assertEqual(rc, 2)
        rc, out = self.gate('stop-legacy', [CUT, q0, done, ('stop-W4', 'x', 'reason', 'y')])
        self.assertEqual(rc, 0, out)
        rerun = ('w4-read-start', 'z', 'export', 'x')
        rc, out = self.gate('stop-legacy', [CUT, q0, done, rerun, ('stop-W4', 'y', 'reason', 'y')])
        self.assertEqual(rc, 2)
        self.assertIn('a read phase started at z after the last w4-read-done row', out)

    def test_merge_recovery_passes_after_an_apply_that_sent_nothing(self):
        base = [
            CUT,
            ('reset', '2026-11-19T05:00:00Z', 'last_reset_at', 'x'),
            ('pre-delete', '2026-11-19T05:02:00Z', 'deleted', '9'),
            ('recovery-wait', '2026-11-19T05:02:10Z', 'seconds', '3600'),
            ('apply-start', '2026-11-19T05:10:00Z', 'pod', 'stalwart-apply-a'),
        ]
        rc, out = self.gate('merge-recovery', base)
        self.assertEqual(rc, 2, out)
        unsent = base + [('apply-unsent', '2026-11-19T05:10:05Z', 'pod', 'stalwart-apply-a')]
        rc, out = self.gate('merge-recovery', unsent + [('recovery-wait', 'y', 'seconds', '3600')])
        self.assertEqual(rc, 0, out)

    def test_no_gate_passes_without_a_cut_ecs_row(self):
        q0 = ('queue-drain', '2026-11-19T04:40:00Z', 'queued', '0')
        done = ('w4-read-done', '2026-11-19T04:41:00Z', 'export', 'x')
        rc, out = self.gate('stop-legacy', [q0, done])
        self.assertEqual(rc, 2, out)
        self.assertRegex(
            out,
            r'^WAIT  stop-legacy: no cut-ecs row in \S+timing.tsv: W2 has not cut legacy in this attempt\n$',
        )

    def test_merge_recovery_passes_again_after_a_timeout_and_a_resume(self):
        base = [
            CUT,
            ('reset', '2026-11-19T05:00:00Z', 'last_reset_at', 'x'),
            ('pre-delete', '2026-11-19T05:02:00Z', 'deleted', '9'),
            ('recovery-wait', '2026-11-19T05:02:10Z', 'seconds', '3600'),
        ]
        rc, out = self.gate('merge-recovery', base)
        self.assertEqual(rc, 0, out)
        self.assertIn('GO    merge-recovery: recovery-wait at 2026-11-19T05:02:10Z after the pre-delete', out)
        timed_out = base + [('stop-W4', '2026-11-19T06:02:10Z', 'reason', 'after 3600s')]
        rc, out = self.gate('merge-recovery', timed_out)
        self.assertEqual(rc, 2)
        self.assertIn('resume with --phase boot --apply', out)
        resumed = timed_out + [('recovery-wait', '2026-11-19T06:05:00Z', 'seconds', '3600')]
        rc, out = self.gate('merge-recovery', resumed)
        self.assertEqual(rc, 0, out)
        self.assertIn('recovery-wait at 2026-11-19T06:05:00Z', out)
        started = base + [('apply-start', '2026-11-19T05:20:00Z', 'pod', 'p')]
        rc, out = self.gate('merge-recovery', started + [('stop-W4', 'x', 'reason', 'y')])
        self.assertEqual(rc, 2)
        self.assertIn('an apply started at 2026-11-19T05:20:00Z after this pre-delete', out)
        rc, out = self.gate('merge-recovery', started + [('export-applied', 'z', 'errors', '0')])
        self.assertEqual(rc, 0, out)
        rc, out = self.gate('merge-recovery', base[:3])
        self.assertEqual(rc, 2)
        self.assertIn('W4 is not waiting for the pod yet', out)

    def test_verify_compare_needs_both_rows_at_zero_and_no_later_stop(self):
        ok = [
            CUT,
            ('verify-compare', '2026-11-19T07:00:00Z', 'fails', '0'),
            ('verify-compare', '2026-11-19T07:00:00Z', 'unexpected_warns', '0'),
        ]
        rc, out = self.gate('verify-compare', ok)
        self.assertEqual(rc, 0, out)
        self.assertIn('GO    verify-compare: verify-compare fails 0, unexpected_warns 0', out)
        rc, out = self.gate('verify-compare', ok + [('stop-W6', 'y', 'reason', 'z')])
        self.assertEqual(rc, 2)
        rc, out = self.gate('verify-compare', [CUT, ('verify', 'x', 'fails', '0')])
        self.assertEqual(rc, 2)
        self.assertIn('run w6-verify.sh --apply --compare-only', out)
        rc, out = self.gate('nope', ok)
        self.assertEqual(rc, 64)


class ResumeWritesTheRowTheGateReads(Prod):
    def test_a_boot_resume_writes_recovery_wait_after_the_stop(self):
        rows = [
            CUT,
            ('reset', '2026-11-19T05:00:00Z', 'last_reset_at', 'x'),
            ('guard', '2026-11-19T05:01:00Z', 'result', 'pass'),
            ('pre-delete', '2026-11-19T05:02:00Z', 'deleted', '9'),
            ('recovery-wait', '2026-11-19T05:02:10Z', 'seconds', '3600'),
            ('stop-W4', '2026-11-19T06:02:10Z', 'reason', 'after 3600s'),
        ]
        self.rows([[n, t, t, m, v] for n, t, m, v in rows])
        self.w4('RECOVERY_WAIT=0 RECOVERY_PR=197; boot_resume; prod_recovery_boot; echo "rc=$?"')
        names = [r[0] for r in self.timing()]
        self.assertEqual(names[-2:], ['stop-W4', 'recovery-wait'])
        p = subprocess.run(
            ['bash', str(HERE / 'window' / 'gate.sh'), 'merge-recovery'],
            capture_output=True,
            text=True,
            env={
                'PATH': '/usr/bin:/bin',
                'HOME': str(self.t),
                'TARGET_ENV': 'tb-stage',
                'CUTOVER_ENV': str(self.t / 'none.env'),
                'TIMING_TSV': str(self.t / 'ev' / 'timing.tsv'),
            },
            timeout=60,
        )
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)


Harness = importlib.import_module('test_window').Harness
NODES = ['i-0000000000000000%d' % i for i in range(6)]


def invocations(states):
    return json.dumps(
        {
            'CommandInvocations': [
                {'InstanceId': i, 'Status': 'Success', 'CommandPlugins': [{'Output': s}]}
                for i, s in zip(NODES, states)
            ]
        }
    )


class LegacyRead(Harness):
    def read(self, states, rows=None):
        (self.t / 'ev').mkdir(exist_ok=True, mode=0o700)
        rows = rows if rows is not None else [
            CUT,
            ('queue-drain', '2026-11-19T04:40:00Z', 'queued', '0'),
            ('w4-read-done', '2026-11-19T04:41:00Z', 'export', 'x'),
        ]
        (self.t / 'ev' / 'timing.tsv').write_text(tsv(rows))
        rules = [
            {'tool': 'aws', 'match': 'ssm send-command', 'out': 'cmd-1\n'},
            {'tool': 'aws', 'match': 'ssm list-command-invocations', 'out': invocations(states)},
        ]
        setup = (
            'LEGACY_NODE_COUNT=6 UNIT=thundermail SSM_WAIT=30 '
            'SSM_READ="systemctl is-active thundermail; systemctl is-enabled thundermail; true"\n'
            'NODES=(%s) AWSL=(aws --profile mzla-legacy --region eu-central-1)\n'
            'CUTOVER_ENV=%s/none.env\n' % (' '.join(NODES), self.t)
        )
        return self.fn(
            'legacy-stalwart.sh',
            'read_gate; echo "gate=$?"; read_nodes; echo "rc=$?"',
            setup,
            rules=rules,
        )

    def rows_named(self, name):
        p = self.t / 'ev' / 'timing.tsv'
        return [x.split('\t') for x in p.read_text().splitlines() if x.split('\t')[0] == name]

    def test_the_row_is_written_only_when_all_six_are_inactive_and_disabled(self):
        out = self.read(['inactive\ndisabled\n'] * 6)
        self.assertIn('gate=0', out)
        self.assertIn('node %s thundermail=inactive enabled=disabled' % NODES[0], out)
        self.assertIn('legacy Stalwart stopped on all 6 nodes: stop-legacy-stalwart row written', out)
        self.assertIn('rc=0', out)
        self.assertEqual(len(self.rows_named('stop-legacy-stalwart')), 1)
        self.assertEqual(self.rows_named('stop-legacy-stalwart')[0][3:], ['nodes', '6'])
        send = [x for x in (self.t / 'fake.log').read_text().splitlines() if 'send-command' in x]
        self.assertIn('--instance-ids %s' % ' '.join(NODES), send[0])
        self.assertIn('systemctl is-active thundermail; systemctl is-enabled thundermail; true', send[0])

    def test_one_enabled_node_writes_no_row(self):
        out = self.read(['inactive\ndisabled\n'] * 5 + ['inactive\nenabled\n'])
        self.assertIn('node %s thundermail=inactive enabled=enabled' % NODES[5], out)
        self.assertIn('5 of 6 nodes report thundermail inactive and disabled: no stop-legacy-stalwart row', out)
        self.assertIn('rc=1', out)
        self.assertEqual(self.rows_named('stop-legacy-stalwart'), [])

    def test_the_read_refuses_before_the_read_phase_ends(self):
        out = self.read(['inactive\ndisabled\n'] * 6, rows=[CUT])
        self.assertIn('WAIT  stop-legacy: no w4-read-done row', out)
        self.assertIn('gate=1', out)


class T6OnTbProd(Prod):
    def resolve(self, rules, env=''):
        (self.t / 'cutover.env').write_text('')
        return self.fn(
            'w5-promote.sh',
            '%s\nresolve_t6; echo "rc=$? T6_SHA=${T6_SHA:-}"' % env,
            'CUTOVER_ENV=%s/cutover.env' % self.t,
            rules=rules,
            env=self.env(TARGET_ENV='tb-prod'),
        )

    def test_t6_is_the_merge_commit_of_pr_195_with_no_operator_value(self):
        rules = [{'tool': 'gh', 'match': r'^pr view 195 .*mergeCommit', 'out': 'abc123\n'}]
        out = self.resolve(rules, 'unset T6_PR T6_SHA')
        self.assertIn('T6_SHA from PR 195 (T6_PR_DEFAULT of targets/tb-prod.sh): abc123', out)
        self.assertIn('rc=0 T6_SHA=abc123', out)
        self.assertIn("T6_SHA='abc123'", (self.t / 'cutover.env').read_text())
        out = self.resolve([{'tool': 'gh', 'match': 'pr view 195', 'out': '\n'}], 'unset T6_PR T6_SHA')
        self.assertIn('PR 195 has no merge commit', out)
        rules = [{'tool': 'gh', 'match': r'^pr view 7 ', 'out': 'def456\n'}]
        out = self.resolve(rules, 'unset T6_SHA; T6_PR=7')
        self.assertIn('T6_SHA from PR 7: def456', out)


PODMAN = """#!/bin/sh
case "$1" in
  image) exit "${PODMAN_CACHED:-1}" ;;
  run) [ -n "${PODMAN_FAIL:-}" ] && { echo "Error: initializing source docker://x: $PODMAN_FAIL" >&2; exit 125; }; echo "Keycloak 26.7.5"; exit 0 ;;
esac
exit 1
"""
KUBECTL = """#!/bin/sh
case "$*" in
  *"get sts keycloak-customer"*) printf 'quay.io/keycloak/keycloak@sha256:abc' ;;
  *psql*) echo 26.7.5 ;;
  *) exit 1 ;;
esac
"""


class KeycloakGate(unittest.TestCase):
    def run_gate(self, *args, **env):
        t = pathlib.Path(tempfile.mkdtemp())
        b = t / 'bin'
        b.mkdir()
        for name, src in (('podman', PODMAN), ('kubectl', KUBECTL)):
            (b / name).write_text(src)
            (b / name).chmod(0o755)
        e = {
            'PATH': '%s:/usr/bin:/bin' % b,
            'HOME': str(t),
            'TARGET_ENV': 'tb-stage',
            'CUTOVER_ENV': str(t / 'none.env'),
            'KUBECONFIG_TARGET': '/dev/null',
            'KC_LEGACY_PG_HOST': 'kc.example.test',
            'KC_LEGACY_PG_USER': 'kc',
            'EVIDENCE_DIR': str(t / 'ev'),
            'SECRET_DIR': str(t / 'secret'),
        }
        e.update(env)
        p = subprocess.run(
            ['bash', str(HERE / 'gate-keycloak-version.sh'), *args],
            capture_output=True,
            text=True,
            env=e,
            timeout=60,
        )
        return p.returncode, p.stdout + p.stderr

    def test_a_failed_pull_says_why(self):
        rc, out = self.run_gate(PODMAN_FAIL='connection refused')
        self.assertEqual(rc, 1, out)
        self.assertNotIn('pulling', out)
        self.assertIn(
            'GATE FAIL: podman run of quay.io/keycloak/keycloak@sha256:abc gave no Keycloak '
            'version (rc=125): Error: initializing source docker://x: connection refused',
            out,
        )

    def test_pull_caches_the_image_before_the_window(self):
        rc, out = self.run_gate('--pull=quay.io/keycloak/keycloak@sha256:def')
        self.assertEqual(rc, 0, out)
        self.assertIn('image quay.io/keycloak/keycloak@sha256:def cached: Keycloak 26.7.5', out)
        rc, out = self.run_gate(PODMAN_CACHED='0')
        self.assertEqual(rc, 0, out)
        self.assertNotIn('pulling', out)
        self.assertIn('image=26.7.5 migration_model=26.7.5\nGATE PASS', out)


class StaleRuleIds(Prod):
    def test_a_rule_id_from_before_a_rollback_says_to_rebuild_the_env_file(self):
        fix = self.t / 'fix'
        fix.mkdir()
        snap = self.t / 'sg.json'
        snap.write_text(
            json.dumps({'rules': [{'SecurityGroupRuleId': 'sgr-new', 'IsEgress': False}]})
        )
        (fix / 'snapshot-legacy-sg.sh').write_text('echo %s\n' % snap)
        out = self.fn(
            'w2-cut-legacy.sh',
            'snap_sg; echo "rc=$?"',
            'MIG_DIR=%s LEGACY_PUB_SG=sg-1 RULES=(sgr-old)' % fix,
            env=self.env(TARGET_ENV='tb-prod'),
        )
        self.assertIn('rule sgr-old is not an ingress rule of sg-1\na rollback puts the rules back', out)
        self.assertIn('build-cutover-env.sh reads LEGACY_PUB_SG_RULE_IDS live', out)
        self.assertIn('rc=1', out)


GPG = r"""#!{py}
import base64, os, sys
a = sys.argv[1:]
open(os.environ['FAKE_LOG'], 'a').write('gpg ' + ' '.join(a) + '\n')
out, src = a[a.index('--output') + 1], a[-1]
data = open(src, 'rb').read()
if '--encrypt' in a:
    rcpt = a[a.index('--recipient') + 1]
    open(out, 'wb').write(b'ENC ' + rcpt.encode() + b' ' + base64.b64encode(data))
elif '--decrypt' in a:
    open(out, 'wb').write(base64.b64decode(data.split(b' ', 2)[2]))
"""


class Keep(Prod):
    def setUp(self):
        super().setUp()
        (self.t / 'bin' / 'gpg').write_text(GPG.format(py=sys.executable))
        (self.t / 'bin' / 'gpg').chmod(0o755)
        self.s = self.t / 'secret'
        self.s.mkdir(mode=0o700)
        (self.s / 'export.json').write_text('{"secret": "dkim-private-key"}\n')
        (self.s / 'source-branch.env').write_text('STALWART_SOURCE_PG_PASSWORD=pw-9\n')

    def keep(self, mode, recipient='ops@example.test', apply=1):
        setup = (
            'KEEP_DIR=%s/ev/keep KEEP_GPG_RECIPIENT=%s APPLY=%d\n'
            'KEEP_FILES="export.json principals.json source-branch.env"\n'
            'GPG=(gpg --batch --yes --quiet)\n' % (self.t, recipient, apply)
        )
        return self.fn(
            'keep.sh', '%s; echo "rc=$? NEEDS=$NEEDS"' % mode, setup, env=self.env(TARGET_ENV='tb-prod')
        )

    def test_save_encrypts_and_restore_puts_back_after_a_reboot(self):
        out = self.keep('save')
        self.assertIn('keep: 2 file(s) of', out)
        self.assertIn('rc=0', out)
        k = self.t / 'ev' / 'keep'
        self.assertEqual(sorted(x.name for x in k.iterdir()), ['export.json.gpg', 'manifest.tsv', 'source-branch.env.gpg'])
        for f in k.iterdir():
            self.assertNotIn(b'dkim-private-key', f.read_bytes())
            self.assertNotIn(b'pw-9', f.read_bytes())
            self.assertEqual(oct(f.stat().st_mode & 0o777), '0o600')
        for f in list(self.s.iterdir()):
            f.unlink()
        out = self.keep('restore')
        self.assertIn('restored export.json', out)
        self.assertIn('restored source-branch.env', out)
        self.assertEqual((self.s / 'export.json').read_text(), '{"secret": "dkim-private-key"}\n')
        self.assertEqual(oct((self.s / 'export.json').stat().st_mode & 0o777), '0o600')
        out = self.keep('restore')
        self.assertIn('present export.json', out)
        (self.s / 'source-branch.env').write_text('STALWART_SOURCE_PG_PASSWORD=newer\n')
        out = self.keep('restore')
        self.assertIn('kept source-branch.env differs from the file in', out)
        self.assertEqual((self.s / 'source-branch.env').read_text(), 'STALWART_SOURCE_PG_PASSWORD=newer\n')

    def test_a_tampered_copy_is_not_restored(self):
        self.keep('save')
        (self.s / 'export.json').unlink()
        (self.t / 'ev' / 'keep' / 'export.json.gpg').write_bytes(b'ENC x ' + __import__('base64').b64encode(b'other'))
        out = self.keep('restore')
        self.assertIn('export.json: sha256 ', out)
        self.assertIn('rc=1', out)
        self.assertFalse((self.s / 'export.json').exists())

    def test_no_recipient_is_a_need_in_the_dry_run_and_a_stop_on_apply(self):
        out = self.keep('save', recipient='', apply=0)
        self.assertIn('NEED  KEEP_GPG_RECIPIENT for the encrypted copies of', out)
        self.assertIn('rc=0 NEEDS=1', out)
        out = self.keep('save', recipient='')
        self.assertIn('rc=1', out)
        self.assertFalse((self.t / 'ev' / 'keep').exists())

    def test_the_read_phase_keeps_the_export(self):
        text = (HERE / 'window' / 'w4-migrate.sh').read_text()
        body = text[text.index('read_phase() {') :]
        body = body[: body.index('\n}\n')]
        self.assertLess(body.index('mark w4-export sha256'), body.index('step keep files keep_copy'))
        reset = text[text.index('reset_phase() {') :]
        self.assertLess(
            reset.index('step source-branch-create'), reset.index('step keep-source files keep_copy')
        )


def git(repo, *args):
    return subprocess.run(
        ['git', '-C', str(repo), '-c', 'user.name=t', '-c', 'user.email=t@example.invalid', *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


class FreightFor(Harness):
    def repo(self):
        r = self.t / 'origin'
        subprocess.run(['git', 'init', '-q', '-b', 'main', str(r)], check=True)
        for p in ('overlays/tb-prod/stalwart-configs', 'bases/stalwart-configs'):
            (r / p).mkdir(parents=True)
            (r / p / 'values.yaml').write_text('a: 1\n')
        git(r, 'add', '-A')
        git(r, 'commit', '-q', '-m', 'settings')
        merge = git(r, 'rev-parse', 'HEAD')
        git(r, 'checkout', '-q', '-b', 'env/tb-prod-thundermail-stalwart-configs-preview')
        (r / 'rendered.txt').write_text('x\n')
        git(r, 'add', '-A')
        git(r, 'commit', '-q', '-m', 'chore(stalwart-config): preview')
        preview = git(r, 'rev-parse', 'HEAD')
        git(r, 'checkout', '-q', 'main')
        clone = self.t / 'clone'
        subprocess.run(['git', 'clone', '-q', str(r), str(clone)], check=True)
        return r, clone, merge, preview

    def test_the_freight_of_a_merge_is_found_by_its_settings_trees(self):
        r, clone, merge, preview = self.repo()
        freight = json.dumps(
            {
                'items': [
                    {
                        'metadata': {'name': 'f-old', 'creationTimestamp': '2026-11-18T00:00:00Z'},
                        'origin': {'name': 'deploy-thundermail-tb-prod01-git'},
                        'commits': [{'id': '0' * 40}],
                    },
                    {
                        'metadata': {'name': 'f-new', 'creationTimestamp': '2026-11-19T00:00:00Z'},
                        'origin': {'name': 'deploy-thundermail-tb-prod01-git'},
                        'commits': [{'id': preview}],
                    },
                ]
            }
        )
        rules = [
            {'tool': 'kubectl', 'match': 'get freight -o json', 'out': freight},
            {'tool': 'git', 'match': '.', 'forward': False},
        ]
        real_git = shutil.which('git')
        (self.t / 'gitbin').mkdir()
        (self.t / 'gitbin' / 'git').symlink_to(real_git)
        out = self.fn(
            'w5-promote.sh',
            'freight_for %s; echo "rc=$? FREIGHT_FOUND=$FREIGHT_FOUND"' % merge,
            'PATH=%s:$PATH REPO_DIR=%s TMD_URL=%s PREVIEW_BRANCH=env/tb-prod-thundermail-stalwart-configs-preview '
            'CFG_PATHS="overlays/tb-prod/stalwart-configs bases/stalwart-configs" '
            'KARGO_PROJECT_CONFIGS=tb-stalwart-configs KARGO_WAREHOUSE_CONFIGS=deploy-thundermail-tb-prod01-git'
            % (self.t / 'gitbin', clone, r),
            rules=rules[:1],
        )
        self.assertIn('Freight f-new (preview commit %s) matches %s' % (preview, merge), out)
        self.assertIn('rc=0 FREIGHT_FOUND=f-new', out)
        script = (HERE / 'window' / 'freight-for.sh').read_text()
        self.assertIn('step freight-for freight match', script)
        out = self.fn(
            'freight-for.sh',
            'step freight-for freight match; finish; last_lines',
            'PATH=%s:$PATH REPO_DIR=%s TMD_URL=%s PREVIEW_BRANCH=env/tb-prod-thundermail-stalwart-configs-preview '
            'CFG_PATHS="overlays/tb-prod/stalwart-configs bases/stalwart-configs" SHA=%s '
            'KARGO_PROJECT_CONFIGS=tb-stalwart-configs KARGO_WAREHOUSE_CONFIGS=deploy-thundermail-tb-prod01-git'
            % (self.t / 'gitbin', clone, r, merge),
            rules=rules[:1],
        )
        self.assertIn('   freight-for done (freight=f-new)', out)
        self.assertEqual(
            out.rstrip().splitlines()[-2:],
            ['Freight f-new (preview commit %s) matches %s' % (preview, merge), 'f-new'],
            out,
        )
        p = subprocess.run(
            ['bash', str(HERE / 'window' / 'freight-for.sh'), 'nope'],
            capture_output=True,
            text=True,
            env={'PATH': '/usr/bin:/bin', 'HOME': str(self.t), 'TARGET_ENV': 'tb-stage'},
            timeout=60,
        )
        self.assertEqual(p.returncode, 64, p.stdout + p.stderr)


if __name__ == '__main__':
    unittest.main()
