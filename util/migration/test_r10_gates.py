import importlib
import json
import pathlib
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
        self.assertEqual(rc, 2)
        self.assertIn('stop-W4 at x after the read phase', out)

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


if __name__ == '__main__':
    unittest.main()
