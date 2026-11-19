import importlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
W4 = importlib.import_module('test_pr3_w4')

HERE = pathlib.Path(__file__).resolve().parent
FAKE_AWS = r"""#!{py}
import json, os, sys, uuid
args = sys.argv[1:]
open(os.environ['FAKE_LOG'], 'a').write(json.dumps(args) + '\n')
st = json.load(open(os.environ['AWS_STATE']))
def val(flag):
    return args[args.index(flag) + 1]
def ids_after(flag):
    out = []
    for x in args[args.index(flag) + 1:]:
        if x.startswith('--'):
            break
        out.append(x)
    return out
assert args[0] == '--profile' and args[2] == '--region', args
svc, op = args[4], args[5]
if svc == 'sts':
    if not st.get('session', True):
        sys.stderr.write('Token has expired\n'); sys.exit(255)
    print('arn:aws:sts::1:assumed-role/AWSReservedSSO_AdministratorAccess_x/op')
elif svc == 'ec2' and op == 'describe-instances' and st.get('describe_fail'):
    sys.stderr.write('UnauthorizedOperation\n'); sys.exit(254)
elif svc == 'ssm' and op == 'describe-instance-information' and st.get('ping_fail'):
    sys.stderr.write('ThrottlingException\n'); sys.exit(254)
elif svc == 'ec2' and op == 'describe-instances':
    for i in ids_after('--instance-ids'):
        n = st['nodes'].get(i)
        if n and not n.get('hidden'):
            print('\t'.join([i, n.get('state', 'running'), n['name'], n.get('svc', 'None')]))
elif svc == 'ssm' and op == 'describe-instance-information':
    for i in val('--filters').split('Values=', 1)[1].split(','):
        n = st['nodes'].get(i)
        if n and n.get('ping', 'Online') != 'absent':
            print(i + '\t' + n.get('ping', 'Online'))
elif svc == 'ssm' and op == 'send-command':
    if st.get('send_fail'):
        sys.stderr.write('AccessDenied\n'); sys.exit(254)
    cid = st.get('cid', str(uuid.uuid4()))
    params = json.loads(val('--parameters'))
    st['sent'] = {{'cid': cid, 'ids': ids_after('--instance-ids'), 'params': params}}
    print(cid)
elif svc == 'ssm' and op == 'get-command-invocation':
    i = val('--instance-id')
    assert val('--command-id') == st['sent']['cid']
    seq = st['inv'][i]
    r = seq.pop(0) if len(seq) > 1 else seq[0]
    if r == 'ok':
        c = st['sent']['params']['commands'][0]
        r = ['Success', 0, 'active\n' if c.endswith('systemctl is-active thundermail') else '']
    if r == 'missing':
        json.dump(st, open(os.environ['AWS_STATE'], 'w'))
        sys.stderr.write('InvocationDoesNotExist\n'); sys.exit(254)
    out = {{'Status': r[0], 'ResponseCode': r[1], 'StandardOutputContent': r[2]}}
    if len(r) > 3:
        out['StandardErrorContent'] = r[3]
    print(json.dumps(out))
else:
    sys.exit(2)
json.dump(st, open(os.environ['AWS_STATE'], 'w'))
"""

NODES = {
    'i-0000000000000a010': (
        'mailstrom-prod-stalwart-10',
        'https,imaps,lmtp,managesieve,smtp,smtps,submission',
    ),
    'i-0000000000000a011': (
        'mailstrom-prod-stalwart-11',
        'https,imaps,lmtp,managesieve,smtp,smtps,submission',
    ),
    'i-0000000000000a012': (
        'mailstrom-prod-stalwart-12',
        'https,imaps,lmtp,managesieve,smtp,smtps,submission',
    ),
    'i-0000000000000a013': (
        'mailstrom-prod-stalwart-13',
        'https,imaps,lmtp,managesieve,smtp,smtps,submission',
    ),
    'i-0000000000000a060': ('mailstrom-prod-stalwart-60', 'management'),
    'i-0000000000000a061': ('mailstrom-prod-stalwart-61', 'management'),
}
MAIL = sorted(i for i, (_, s) in NODES.items() if s != 'management')
ALL = sorted(NODES)
OK = 'ok'
STOPPED = ['Success', 0, 'inactive\ndisabled\n']
RUNNING = ['Success', 0, 'active\nenabled\n']
CUT = ['cut-ecs', '2026-11-19T04:00:00Z', '2026-11-19T04:00:00Z', 'tasks', '0']
DRAINED = ['queue-drain', '2026-11-19T04:40:00Z', '2026-11-19T04:40:00Z', 'queued', '0']
READ_DONE = ['w4-read-done', '2026-11-19T04:41:00Z', '2026-11-19T04:41:00Z', 'export', 'x']
START = 'systemctl enable --now thundermail && sleep 5 && systemctl is-active thundermail'


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.t = pathlib.Path(tempfile.mkdtemp(prefix='.test-legacy-stalwart-', dir=HERE))
        self.addCleanup(shutil.rmtree, self.t, True)
        self.t.chmod(0o700)
        mig = self.t / 'mig'
        (mig / 'window').mkdir(parents=True)
        for f in ('target.sh', 'lib.sh', 'timing.sh'):
            shutil.copy(HERE / f, mig / f)
        shutil.copytree(HERE / 'targets', mig / 'targets')
        shutil.copytree(
            HERE / 'verify', mig / 'verify', ignore=shutil.ignore_patterns('*.py', '__pycache__')
        )
        for f in ('legacy-stalwart.sh', 'gate.sh'):
            shutil.copy(HERE / 'window' / f, mig / 'window')
        self.mig = mig
        self.fill_placeholders()
        self.bin = self.t / 'bin'
        self.bin.mkdir()
        (self.bin / 'aws').write_text(FAKE_AWS.format(py=sys.executable))
        (self.bin / 'aws').chmod(0o755)
        for d in ('ev', 'secret', 'home', 'shm'):
            (self.t / d).mkdir(mode=0o700)
        self.tsv = self.t / 'ev' / 'timing.tsv'
        self.state({})
        self.envfile()

    def fill_placeholders(self, fill=True):
        src = (HERE / 'targets' / 'tb-prod.sh').read_text()
        if fill:
            src = re.sub(r'REPLACE_ME_[A-Z_]+', 'filled-for-test', src)
        else:
            src += 'LEGACY_TEST_VALUE=REPLACE_ME_LEGACY_TEST_VALUE\n'
        (self.mig / 'targets' / 'tb-prod.sh').write_text(src)
        v = self.mig / 'verify' / 'tb-prod.env'
        if v.exists() and fill:
            v.write_text(re.sub(r'REPLACE_ME_[A-Z_]+', 'filled-for-test', v.read_text()))

    def envfile(self, ids=' '.join(ALL), target='tb-prod'):
        f = self.t / 'shm' / 'cutover-tb-prod.env'
        f.write_text("TARGET_ENV='%s'\nLEGACY_STALWART_NODE_IDS='%s'\n" % (target, ids))
        f.chmod(0o600)

    def state(self, over, inv=None):
        nodes = {i: {'name': n, 'svc': s} for i, (n, s) in NODES.items()}
        for i, v in over.get('nodes', {}).items():
            nodes[i].update(v)
        st = {'nodes': nodes, 'inv': {i: [OK] for i in NODES}}
        st.update({k: v for k, v in over.items() if k != 'nodes'})
        if inv:
            st['inv'].update(inv)
        (self.t / 'aws.json').write_text(json.dumps(st))

    def rows(self, *rows):
        self.tsv.write_text(''.join('\t'.join(r) + '\n' for r in rows))

    def calls(self, op=None):
        p = self.t / 'fake.log'
        out = [json.loads(ln) for ln in p.read_text().splitlines()] if p.exists() else []
        return [c for c in out if op is None or op in c]

    def sent(self):
        return json.loads((self.t / 'aws.json').read_text()).get('sent')

    def run_tool(self, *args, target='tb-prod', **extra):
        env = {
            'PATH': '%s:%s' % (self.bin, os.environ['PATH']),
            'HOME': str(self.t / 'home'),
            'LANG': 'C.UTF-8',
            'TARGET_ENV': target,
            'CUTOVER_ENV': str(self.t / 'shm' / 'cutover-tb-prod.env'),
            'EVIDENCE_DIR': str(self.t / 'ev'),
            'SECRET_DIR': str(self.t / 'secret'),
            'TIMING_TSV': str(self.tsv),
            'TS': '20261119T000000Z',
            'FAKE_LOG': str(self.t / 'fake.log'),
            'AWS_STATE': str(self.t / 'aws.json'),
            'LEGACY_SSM_POLL': '0',
            'LEGACY_SSM_WAIT': '2',
        }
        env.update(extra)
        cmd = [shutil.which('bash'), str(self.mig / 'window' / 'legacy-stalwart.sh'), *args]
        if (
            subprocess.run([shutil.which('unshare'), '-rn', 'true'], capture_output=True).returncode
            == 0
        ):
            cmd = [shutil.which('unshare'), '-rn', *cmd]
        p = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=60)
        return p.returncode, p.stdout + p.stderr

    def tsv_text(self):
        return self.tsv.read_text() if self.tsv.exists() else ''

    def stop_rows(self):
        return [
            x.split('\t')
            for x in self.tsv_text().splitlines()
            if x.startswith('stop-legacy-stalwart\t')
        ]


class LegacyStalwart(Sandbox):
    def test_refuses_other_target_and_bad_usage(self):
        rc, out = self.run_tool('read', target='tb-stage')
        self.assertEqual(rc, 64, out)
        self.assertIn('TARGET_ENV=tb-stage has none', out)
        for args in ((), ('reboot',), ('read', 'stop'), ('read', '--force')):
            rc, out = self.run_tool(*args)
            self.assertEqual(rc, 64, (args, out))
        self.assertEqual(self.calls(), [])
        rc, out = self.run_tool('read')
        self.assertEqual(rc, 0, out)

    def test_apply_refuses_with_placeholder_left(self):
        self.fill_placeholders(fill=False)
        rc, out = self.run_tool('start', '--apply')
        self.assertEqual(rc, 64, out)
        self.assertIn('refusing: placeholder value', out)
        self.assertEqual(self.calls(), [])
        self.fill_placeholders()
        rc, out = self.run_tool('start', '--apply')
        self.assertEqual(rc, 0, out)

    def test_dry_run_is_default_and_sends_nothing(self):
        for mode in ('restart', 'start', 'read', 'stop'):
            rc, out = self.run_tool(mode)
            self.assertEqual(rc, 2 if mode == 'stop' else 0, out)
            self.assertIn('ssm send-command --document-name AWS-RunShellScript', out)
            self.assertIn('legacy-stalwart %s (dry-run)' % mode, out)
        self.assertEqual(self.calls('send-command'), [])
        self.assertEqual(self.calls('get-command-invocation'), [])
        self.assertEqual(self.tsv_text(), '')
        rc, out = self.run_tool('start', '--apply')
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(self.calls('send-command')), 1)

    def test_dry_run_prints_the_apply_command(self):
        rc, out = self.run_tool('restart', '--dry-run')
        self.assertEqual(rc, 0, out)
        self.assertIn('legacy-stalwart restart dry-run: done (4 node(s))', out)
        plan = [ln for ln in out.splitlines() if ' send-command ' in ln][0]
        rc, out = self.run_tool('restart', '--apply')
        self.assertEqual(rc, 0, out)
        self.assertIn(plan, out.splitlines())

    def test_restart_targets_the_mail_nodes_only(self):
        rc, out = self.run_tool('restart', '--apply')
        self.assertEqual(rc, 0, out)
        s = self.sent()
        self.assertEqual(sorted(s['ids']), MAIL)
        self.assertEqual(s['params']['commands'], ['systemctl restart thundermail'])
        self.assertIn('node mailstrom-prod-stalwart-60 i-0000000000000a060 management skip', out)
        self.assertIn('node mailstrom-prod-stalwart-10 i-0000000000000a010 mail target', out)
        self.assertEqual(
            len([ln for ln in out.splitlines() if re.match(r'node \S+ \S+ Success rc=0$', ln)]), 4
        )
        self.assertIn('legacy-stalwart restart apply: done (4 node(s))', out)
        self.assertEqual(self.tsv_text(), '')
        self.state({'nodes': {'i-0000000000000a060': {'svc': 'smtp'}}})
        rc, out = self.run_tool('restart', '--apply')
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(self.sent()['ids']), 5)

    def test_restart_refuses_after_the_stop_row_of_this_cut(self):
        cut = ['cut-ecs', 'a', 'b', '', '']
        stop = ['stop-legacy-stalwart', 'a', 'b', 'nodes', '6']
        self.rows(cut, stop)
        rc, out = self.run_tool('restart', '--apply')
        self.assertEqual(rc, 2, out)
        self.assertIn('NEED  a stop-legacy-stalwart row follows the last cut-ecs row', out)
        self.assertIsNone(self.sent())
        self.rows(stop, cut)
        rc, out = self.run_tool('restart', '--apply')
        self.assertEqual(rc, 0, out)
        self.assertEqual(sorted(self.sent()['ids']), MAIL)

    def test_needs_the_env_file(self):
        (self.t / 'shm' / 'cutover-tb-prod.env').unlink()
        rc, out = self.run_tool('start', '--apply', LEGACY_STALWART_NODE_IDS=' '.join(ALL))
        self.assertEqual(rc, 2, out)
        self.assertIn('NEED  no env file', out)
        self.assertIsNone(self.sent())
        self.envfile()
        rc, out = self.run_tool('start', '--apply')
        self.assertEqual(rc, 0, out)

    def test_start_and_stop_commands_on_all_six(self):
        self.rows(CUT, DRAINED, READ_DONE)
        for mode, cmd in (
            ('start', START),
            ('stop', 'systemctl disable --now thundermail'),
        ):
            rc, out = self.run_tool(mode, '--apply')
            self.assertEqual(rc, 0, out)
            s = self.sent()
            self.assertEqual(sorted(s['ids']), ALL)
            self.assertEqual(s['params']['commands'], [cmd])
            self.assertIn('legacy-stalwart %s apply: done (6 node(s))' % mode, out)
        self.assertNotIn('stop-legacy-stalwart', self.tsv_text())

    def test_stop_gate_is_window_gate_sh_stop_legacy(self):
        q3 = ['queue-drain', 'a', 'b', 'queued', '3']
        w4stop = ['stop-W4', 'a', 'b', 'reason', 'x']
        rerun = ['w4-read-start', 'a', 'b', 'export', 'x']
        refused = (
            ((), 'no timing file'),
            ((CUT,), 'no w4-read-done row after the last cut-ecs row'),
            ((DRAINED,), 'no cut-ecs row'),
            ((DRAINED, READ_DONE), 'no cut-ecs row'),
            ((CUT, DRAINED), 'no w4-read-done row'),
            ((CUT, q3, READ_DONE), 'the last queue-drain row has value 3, not 0'),
            ((DRAINED, READ_DONE, CUT), 'no w4-read-done row'),
            ((CUT, READ_DONE, DRAINED), 'no queue-drain row before the w4-read-done row'),
            ((CUT, DRAINED, READ_DONE, rerun, w4stop), 'a read phase started at'),
        )
        for rows, why in refused:
            if rows:
                self.rows(*rows)
            elif self.tsv.exists():
                self.tsv.unlink()
            rc, out = self.run_tool('stop', '--apply')
            self.assertEqual(rc, 2, (rows, out))
            self.assertIn('gate: WAIT  stop-legacy: ', out)
            self.assertIn(why, out)
            self.assertIn('NEED  window/gate.sh stop-legacy does not pass', out)
            self.assertIsNone(self.sent(), rows)
            rc, out = self.run_tool('stop')
            self.assertEqual(rc, 2, (rows, out))
            self.assertIn('legacy-stalwart stop dry-run: 1 NEED item(s) above', out)
        self.rows(CUT, DRAINED, READ_DONE, w4stop)
        rc, out = self.run_tool('stop', '--apply')
        self.assertEqual(rc, 0, out)
        self.assertIn(
            'gate: GO    stop-legacy: queue-drain 0 at 2026-11-19T04:40:00Z, w4-read-done at '
            '2026-11-19T04:41:00Z; legacy Stalwart may be stopped',
            out,
        )
        self.assertEqual(sorted(self.sent()['ids']), ALL)

    def test_stop_before_the_window_sends_nothing(self):
        self.rows(DRAINED)
        rc, out = self.run_tool('stop', '--apply')
        self.assertEqual(rc, 2, out)
        self.assertIsNone(self.sent())
        self.assertEqual(self.calls('send-command'), [])

    def test_read_writes_the_stop_row_only_when_all_six_are_stopped(self):
        self.rows(CUT, DRAINED, READ_DONE)
        rc, out = self.run_tool('read', '--apply')
        self.assertEqual(rc, 3, out)
        self.assertIn('summary: /=6', out)
        self.assertIn('no stop-legacy-stalwart row', out)
        self.state({}, inv={i: [STOPPED] for i in ALL[:5]} | {ALL[5]: [RUNNING]})
        rc, out = self.run_tool('read', '--apply')
        self.assertEqual(rc, 3, out)
        self.assertIn(
            '1 of 6 node(s) not inactive (or failed) and disabled: no stop-legacy-stalwart row', out
        )
        self.assertIn(
            'legacy-stalwart read apply: mixed, the 6 node(s) are neither all stopped nor all '
            'active and enabled',
            out,
        )
        self.assertEqual(self.stop_rows(), [])
        self.state(
            {},
            inv={i: [STOPPED] for i in ALL[:5]} | {ALL[5]: [['Success', 0, 'inactive\nenabled\n']]},
        )
        rc, out = self.run_tool('read', '--apply')
        self.assertEqual(rc, 3, out)
        self.assertEqual(self.stop_rows(), [])
        self.state({}, inv={i: [STOPPED] for i in ALL})
        rc, out = self.run_tool('read', '--apply')
        self.assertEqual(rc, 0, out)
        s = self.sent()
        self.assertEqual(sorted(s['ids']), ALL)
        self.assertEqual(
            s['params']['commands'],
            ['systemctl is-active thundermail; systemctl is-enabled thundermail; exit 0'],
        )
        self.assertIn('gate: GO    stop-legacy: ', out)
        self.assertIn(
            'node mailstrom-prod-stalwart-61 i-0000000000000a061 Success rc=0 inactive disabled',
            out,
        )
        self.assertIn('summary: inactive/disabled=6', out)
        self.assertIn(
            'all 6 nodes inactive (or failed) and disabled: stop-legacy-stalwart row written', out
        )
        self.assertIn('legacy-stalwart read apply: done (6 node(s))', out)
        rows = self.stop_rows()
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0][3:], ['nodes', '6'])
        for t in rows[0][1:3]:
            self.assertRegex(t, r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$')

    def test_read_counts_a_failed_disabled_unit_as_stopped(self):
        self.rows(CUT, DRAINED, READ_DONE)
        failed = ['Success', 0, 'failed\ndisabled\n']
        self.state({}, inv={i: [STOPPED] for i in ALL[:5]} | {ALL[5]: [failed]})
        rc, out = self.run_tool('read', '--apply')
        self.assertEqual(rc, 0, out)
        self.assertIn('summary: failed/disabled=1 inactive/disabled=5', out)
        self.assertEqual(len(self.stop_rows()), 1)

    def test_read_writes_no_row_while_the_gate_waits(self):
        self.state({}, inv={i: [STOPPED] for i in ALL})
        for rows in ((), (CUT,), (CUT, DRAINED), (DRAINED, READ_DONE)):
            self.rows(*rows)
            rc, out = self.run_tool('read', '--apply')
            self.assertEqual(rc, 0, (rows, out))
            self.assertIn('gate: WAIT  stop-legacy: ', out)
            self.assertIn(
                'all 6 nodes inactive (or failed) and disabled, but window/gate.sh stop-legacy '
                'waits: no stop-legacy-stalwart row',
                out,
            )
            self.assertEqual(self.stop_rows(), [], rows)

    def test_read_exit_code_tells_mixed_from_uniform(self):
        for inv, rc_want in (
            ({i: [RUNNING] for i in ALL}, 0),
            ({i: [STOPPED] for i in ALL}, 0),
            ({i: [RUNNING] for i in ALL[:5]} | {ALL[5]: [['Success', 0, 'failed\nenabled\n']]}, 3),
            ({i: [RUNNING] for i in ALL[:5]} | {ALL[5]: [STOPPED]}, 3),
        ):
            self.state({}, inv=inv)
            rc, out = self.run_tool('read', '--apply')
            self.assertEqual(rc, rc_want, (inv, out))
        self.assertIn('summary: active/enabled=5 inactive/disabled=1', out)

    def test_read_prints_active_enabled_for_the_rollback_check(self):
        self.state({}, inv={i: [RUNNING] for i in ALL})
        rc, out = self.run_tool('read', '--apply')
        self.assertEqual(rc, 0, out)
        self.assertIn('summary: active/enabled=6', out)
        self.assertEqual(len(re.findall(r'(?m)^node \S+ \S+ Success rc=0 active enabled$', out)), 6)
        self.assertEqual(self.tsv_text(), '')

    def test_fails_when_a_node_failed_or_did_not_answer(self):
        cases = (
            {ALL[2]: [['Failed', 1, '']]},
            {ALL[2]: [['TimedOut', -1, '']]},
            {ALL[2]: [['InProgress', -1, '']]},
            {ALL[2]: ['missing']},
        )
        for inv in cases:
            self.state({}, inv={i: [STOPPED] for i in ALL} | inv)
            rc, out = self.run_tool('read', '--apply')
            self.assertEqual(rc, 1, (inv, out))
            self.assertIn(
                'STOP legacy-stalwart read (apply): 1 of 6 node(s) failed or did not answer', out
            )
            self.assertEqual(self.tsv_text(), '', inv)
        self.assertIn('node mailstrom-prod-stalwart-12 i-0000000000000a012 no-answer rc=-', out)
        self.rows(CUT, DRAINED, READ_DONE)
        self.state(
            {},
            inv={i: [STOPPED] for i in ALL}
            | {ALL[2]: ['missing', ['InProgress', -1, ''], STOPPED]},
        )
        rc, out = self.run_tool('read', '--apply')
        self.assertEqual(rc, 0, out)
        self.assertIn('stop-legacy-stalwart row written', out)

    def test_read_with_extra_output_writes_no_row(self):
        extra = ['Success', 0, 'inactive\ndisabled\nwarning\n']
        self.rows(CUT, DRAINED, READ_DONE)
        self.state({}, inv={i: [STOPPED] for i in ALL} | {ALL[3]: [extra]})
        rc, out = self.run_tool('read', '--apply')
        self.assertEqual(rc, 3, out)
        self.assertIn('1 of 6 node(s) not inactive (or failed) and disabled', out)
        self.assertEqual(self.stop_rows(), [])

    def test_apply_prints_the_invocation_read(self):
        self.state({'cid': '01234567-89ab-cdef-0123-456789abcdef'})
        rc, out = self.run_tool('start', '--apply')
        self.assertEqual(rc, 0, out)
        self.assertIn(
            'ssm get-command-invocation --command-id 01234567-89ab-cdef-0123-456789abcdef', out
        )
        self.assertEqual(len(self.calls('get-command-invocation')), 6)

    def test_malformed_command_id_stops(self):
        self.state({'cid': 'not-a-command-id'})
        rc, out = self.run_tool('start', '--apply')
        self.assertEqual(rc, 1, out)
        self.assertIn('ssm send-command returned no command id', out)
        self.assertEqual(self.calls('get-command-invocation'), [])

    def test_failed_reads_are_needs(self):
        cases = (
            ({'describe_fail': True}, 'ec2 describe-instances failed: UnauthorizedOperation'),
            ({'ping_fail': True}, 'ssm describe-instance-information failed: ThrottlingException'),
            ({'nodes': {ALL[4]: {'hidden': True}}}, 'ec2 describe-instances returned 0 row(s)'),
        )
        for st, msg in cases:
            for mode in ('--apply', '--dry-run'):
                self.state(st)
                rc, out = self.run_tool('start', mode)
                self.assertEqual(rc, 2, (st, out))
                self.assertIn('NEED  ' + msg if 'returned' not in msg else msg, out)
                self.assertIsNone(self.sent(), st)

    def test_restart_stops_without_a_mail_node(self):
        self.state({'nodes': {i: {'svc': 'management'} for i in MAIL}})
        rc, out = self.run_tool('restart', '--apply')
        self.assertEqual(rc, 1, out)
        self.assertIn('no node carries mail', out)
        self.assertIsNone(self.sent())

    def test_failed_node_prints_its_stderr(self):
        bad = ['Failed', 1, '', 'Failed to stop thundermail.service: Access denied\n']
        self.state({}, inv={ALL[0]: [bad]})
        rc, out = self.run_tool('start', '--apply')
        self.assertEqual(rc, 1, out)
        self.assertIn(
            'stderr mailstrom-prod-stalwart-10 i-0000000000000a010: '
            'Failed to stop thundermail.service: Access denied',
            out,
        )
        self.assertNotIn('stderr mailstrom-prod-stalwart-11', out)

    def test_start_voids_the_stop_row_before_it_sends(self):
        cut = ['cut-ecs', 'a', 'b', '', '']
        stop = ['stop-legacy-stalwart', 'a', 'b', 'nodes', '6']
        self.rows(cut, stop)
        self.state({'send_fail': True})
        rc, out = self.run_tool('start', '--apply')
        self.assertEqual(rc, 1, out)
        last = self.tsv_text().splitlines()[-1].split('\t')
        self.assertEqual((last[0], last[3:]), ('start-legacy-stalwart', ['nodes', '6']))
        self.state({})
        rc, out = self.run_tool('restart', '--apply')
        self.assertEqual(rc, 0, out)
        self.rows(cut, stop)
        rc, out = self.run_tool('start')
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.tsv_text().count('start-legacy-stalwart'), 0)
        rc, out = self.run_tool('restart', '--apply')
        self.assertEqual(rc, 2, out)

    def test_start_sends_to_the_reachable_nodes_then_stops_naming_the_rest(self):
        lost = ALL[1]
        for st, why in (
            (
                {'nodes': {lost: {'ping': 'ConnectionLost'}}},
                'SSM PingStatus ConnectionLost, not Online',
            ),
            ({'nodes': {lost: {'ping': 'absent'}}}, 'SSM PingStatus absent, not Online'),
            (
                {'nodes': {lost: {'state': 'stopped', 'ping': 'absent'}}},
                'instance state stopped, not running; SSM PingStatus absent, not Online',
            ),
        ):
            self.state(st)
            rc, out = self.run_tool('start')
            self.assertEqual(rc, 0, out)
            self.assertIn(
                'node mailstrom-prod-stalwart-11 %s mail unreachable (%s)' % (lost, why), out
            )
            self.assertIn(
                'not reachable: mailstrom-prod-stalwart-11 %s; --apply sends to the other 5 '
                'node(s), then exits 1 naming these' % lost,
                out,
            )
            self.assertIsNone(self.sent())
            rc, out = self.run_tool('start', '--apply')
            self.assertEqual(rc, 1, (st, out))
            self.assertEqual(sorted(self.sent()['ids']), [i for i in ALL if i != lost])
            self.assertEqual(self.sent()['params']['commands'], [START])
            self.assertEqual(
                len(re.findall(r'(?m)^node \S+ \S+ Success rc=0 active$', out)), 5, out
            )
            self.assertIn('thundermail active on all 5 node(s) sent to', out)
            self.assertIn(
                'STOP legacy-stalwart start (apply): not sent to 1 of 6 node(s) SSM cannot reach: '
                'mailstrom-prod-stalwart-11 %s; thundermail stays disabled there and a reboot does '
                'not start it: run systemctl enable --now thundermail on each by other means'
                % lost,
                out,
            )
            self.assertNotIn('apply: done', out)
            row = [x.split('\t') for x in self.tsv_text().splitlines()][-1]
            self.assertEqual((row[0], row[3:]), ('start-legacy-stalwart', ['nodes', '5']))

    def test_start_command_fails_when_enable_fails_on_a_running_unit(self):
        rc, out = self.run_tool('start', '--apply')
        self.assertEqual(rc, 0, out)
        cmd = self.sent()['params']['commands'][0]
        node = self.t / 'node'
        node.mkdir()
        (node / 'systemctl').write_text(
            '#!/bin/sh\n'
            'case "$1" in\n'
            '  enable) echo "Failed to enable unit" >&2; exit "$ENABLE_RC" ;;\n'
            '  is-active) echo active ;;\n'
            'esac\n'
        )
        (node / 'sleep').write_text('#!/bin/sh\n')
        for f in ('systemctl', 'sleep'):
            (node / f).chmod(0o755)
        for enable_rc, want in (('1', 1), ('0', 0)):
            p = subprocess.run(
                [shutil.which('bash'), '-c', cmd],
                capture_output=True,
                text=True,
                env={'PATH': '%s:/usr/bin:/bin' % node, 'ENABLE_RC': enable_rc},
                timeout=30,
            )
            self.assertEqual(p.returncode, want, (enable_rc, p.stdout, p.stderr))
            self.assertEqual(p.stdout, 'active\n' if want == 0 else '')

    def test_start_refuses_when_ssm_reaches_no_node(self):
        self.state({'nodes': {i: {'ping': 'ConnectionLost'} for i in ALL}})
        rc, out = self.run_tool('start', '--apply')
        self.assertEqual(rc, 1, out)
        self.assertIn('STOP legacy-stalwart start (apply): SSM reaches none of the 6 nodes', out)
        self.assertIsNone(self.sent())

    def test_start_exit_code_proves_every_node_active(self):
        cases = (
            {ALL[3]: [['Failed', 3, 'inactive\n']]},
            {ALL[3]: [['Failed', 3, 'failed\n']]},
            {ALL[3]: [['Success', 0, 'activating\n']]},
            {ALL[3]: [['Success', 0, '']]},
        )
        for inv in cases:
            self.state({}, inv=inv)
            rc, out = self.run_tool('start', '--apply')
            self.assertEqual(rc, 1, (inv, out))
            self.assertIn(
                'STOP legacy-stalwart start (apply): 1 of 6 node(s) failed or did not answer', out
            )
            self.assertNotIn('thundermail active on all', out)
        self.state({})
        rc, out = self.run_tool('start', '--apply')
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(re.findall(r'(?m)^node \S+ \S+ Success rc=0 active$', out)), 6, out)
        self.assertIn('thundermail active on all 6 node(s) sent to', out)
        self.assertIn('legacy-stalwart start apply: done (6 node(s))', out)

    def test_send_failure_stops(self):
        self.state({'send_fail': True})
        rc, out = self.run_tool('start', '--apply')
        self.assertEqual(rc, 1, out)
        self.assertIn('ssm send-command failed: AccessDenied', out)
        self.assertEqual(self.calls('get-command-invocation'), [])

    def test_needs_before_sending(self):
        cases = (
            (
                {'nodes': {ALL[1]: {'ping': 'ConnectionLost'}}},
                'SSM PingStatus ConnectionLost, not Online',
            ),
            ({'nodes': {ALL[1]: {'ping': 'absent'}}}, 'SSM PingStatus absent, not Online'),
            ({'nodes': {ALL[1]: {'state': 'stopped'}}}, 'instance state stopped, not running'),
            (
                {'nodes': {ALL[1]: {'name': 'bastion'}}},
                "Name tag 'bastion' does not match mailstrom-prod-stalwart-*",
            ),
            ({'nodes': {ALL[1]: {'svc': 'None'}}}, 'no postboot.stalwart.node_services tag'),
            ({'session': False}, 'no AWS session for mzla-legacy'),
        )
        for st, msg in cases:
            for mode in ('read', 'stop', 'restart'):
                self.state(st)
                rc, out = self.run_tool(mode, '--apply')
                self.assertEqual(rc, 2, (mode, st, out))
                self.assertIn('NEED  ', out)
                self.assertIn(msg, out)
                self.assertIsNone(self.sent(), (mode, st))
        self.state({}, inv={i: [RUNNING] for i in ALL})
        rc, out = self.run_tool('read', '--apply')
        self.assertEqual(rc, 0, out)

    def test_needs_six_node_ids_from_the_env_file(self):
        for ids in (
            '',
            ' '.join(ALL[:5]),
            ' '.join(ALL[:5] + ALL[:1]),
            ' '.join(ALL[:5] + ['10.0.0.1']),
        ):
            self.envfile(ids=ids)
            rc, out = self.run_tool('read', '--apply')
            self.assertEqual(rc, 2, (ids, out))
            self.assertIn('NEED  LEGACY_STALWART_NODE_IDS', out)
            self.assertEqual(self.calls(), [])
        self.envfile()
        self.state({}, inv={i: [RUNNING] for i in ALL})
        rc, out = self.run_tool('read', '--apply')
        self.assertEqual(rc, 0, out)

    def test_evidence_log_has_the_run(self):
        rc, out = self.run_tool('read')
        self.assertEqual(rc, 0, out)
        log = self.t / 'ev' / 'legacy-stalwart-read-dry-run-20261119T000000Z.log'
        self.assertIn('legacy-stalwart read dry-run: done (6 node(s))', log.read_text())


class ResetAfterStart(W4.Prod):
    def preflight(self, rows):
        self.rows(rows)
        self.kube({'replicas': 0, 'pods': []})
        return self.w4(
            'mkdir -p "$S"; echo "{}" > "$S/export.json"\n'
            'reset_preflight; echo "rc=$? stop=$STOP_AT"'
        )

    def test_a_start_after_the_stop_row_refuses_the_reset(self):
        start = ['start-legacy-stalwart', 'x', '2026-11-19T04:40:00Z', 'nodes', '6']
        ok = [W4.CUT, W4.EXPORT, W4.READ, W4.STOPPED]
        out = self.preflight(ok + [start])
        self.assertIn('a start-legacy-stalwart row follows the stop-legacy-stalwart row', out)
        self.assertIn('rc=1', out)
        out = self.preflight(ok + [start, W4.STOPPED])
        self.assertIn('a start-legacy-stalwart row follows the w4-export row', out)
        self.assertIn('run --phase read first', out)
        self.assertIn('rc=1', out)
        out = self.preflight(ok + [start, W4.EXPORT, W4.READ, W4.STOPPED])
        self.assertIn('rc=0 stop=2026-11-19T04:30:00Z', out)
        out = self.preflight([W4.CUT, start] + ok[1:])
        self.assertIn('rc=0', out)

    def test_a_start_after_the_export_makes_the_read_phase_dump_again(self):
        body = W4.ReadPhase.BODY + 'read_phase; echo "rc=$?"'
        self.rows([W4.CUT])
        out = self.w4(body)
        self.assertIn('rc=0', out)
        out = self.w4(body)
        self.assertIn('reusing the export of this cut', out)
        tsv = self.t / 'ev' / 'timing.tsv'
        start = ['start-legacy-stalwart', 'x', '2026-11-19T04:40:00Z', 'nodes', '6']
        tsv.write_text(tsv.read_text() + '\t'.join(start) + '\n')
        out = self.w4(body)
        self.assertIn('a start-legacy-stalwart row follows the w4-export row', out)
        self.assertNotIn('reusing the export of this cut', out)
        self.assertIn('DUMP', out.split())
        out = self.w4(body)
        self.assertIn('reusing the export of this cut', out)


if __name__ == '__main__':
    unittest.main()
