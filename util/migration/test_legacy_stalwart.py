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
    if r == 'missing':
        json.dump(st, open(os.environ['AWS_STATE'], 'w'))
        sys.stderr.write('InvocationDoesNotExist\n'); sys.exit(254)
    print(json.dumps({{'Status': r[0], 'ResponseCode': r[1], 'StandardOutputContent': r[2]}}))
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
OK = ['Success', 0, '']
STOPPED = ['Success', 0, 'inactive\ndisabled\n']
RUNNING = ['Success', 0, 'active\nenabled\n']


class LegacyStalwart(unittest.TestCase):
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
        shutil.copy(HERE / 'window' / 'legacy-stalwart.sh', mig / 'window')
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
        self.rows(['cut-ecs', 'a', 'b', '', ''], ['queue-drain', 'a', 'b', 'queued', '0'])
        for mode, cmd in (
            ('start', 'systemctl enable --now thundermail'),
            ('stop', 'systemctl disable --now thundermail'),
        ):
            rc, out = self.run_tool(mode, '--apply')
            self.assertEqual(rc, 0, out)
            s = self.sent()
            self.assertEqual(sorted(s['ids']), ALL)
            self.assertEqual(s['params']['commands'], [cmd])
            self.assertIn('legacy-stalwart %s apply: done (6 node(s))' % mode, out)
        self.assertNotIn('stop-legacy-stalwart', self.tsv_text())

    def test_stop_gate_needs_a_zero_queue_drain_row_of_this_cut(self):
        cut = ['cut-ecs', 'a', 'b', '', '']
        refused = (
            (),
            (cut,),
            (cut, ['queue-drain', 'a', 'b', 'queued', '3']),
            (cut, ['queue-drain', 'a', 'b', 'queued', '0'], ['stop-W4', 'a', 'b', 'reason', 'x']),
            (['queue-drain', 'a', 'b', 'queued', '0'], cut),
            (cut, ['dryrun-queue-drain', 'a', 'b', 'queued', '0']),
            (
                cut,
                ['queue-drain', 'a', 'b', 'queued', '0'],
                ['queue-drain', 'a', 'b', 'queued', '2'],
            ),
        )
        for rows in refused:
            self.rows(*rows)
            rc, out = self.run_tool('stop', '--apply')
            self.assertEqual(rc, 2, (rows, out))
            self.assertIn('NEED  the last queue-drain row of this cut', out)
            self.assertIsNone(self.sent(), rows)
            rc, out = self.run_tool('stop')
            self.assertEqual(rc, 2, (rows, out))
            self.assertIn('legacy-stalwart stop dry-run: 1 NEED item(s) above', out)
        self.rows(
            cut,
            ['queue-drain', 'a', 'b', 'queued', '3'],
            ['stop-W4', 'a', 'b', 'reason', 'x'],
            ['queue-drain', 'a', 'b', 'queued', '0'],
        )
        rc, out = self.run_tool('stop', '--apply')
        self.assertEqual(rc, 0, out)
        self.assertIn('gate: the last queue-drain row of this cut reads 0', out)
        self.assertEqual(sorted(self.sent()['ids']), ALL)

    def test_read_writes_the_stop_row_only_when_all_six_are_stopped(self):
        rc, out = self.run_tool('read', '--apply')
        self.assertEqual(rc, 0, out)
        self.assertIn('summary: /=6', out)
        self.assertIn('no stop-legacy-stalwart row', out)
        self.state({}, inv={i: [STOPPED] for i in ALL[:5]} | {ALL[5]: [RUNNING]})
        rc, out = self.run_tool('read', '--apply')
        self.assertEqual(rc, 0, out)
        self.assertIn('1 of 6 node(s) not inactive and disabled: no stop-legacy-stalwart row', out)
        self.assertEqual(self.tsv_text(), '')
        self.state(
            {},
            inv={i: [STOPPED] for i in ALL[:5]} | {ALL[5]: [['Success', 0, 'inactive\nenabled\n']]},
        )
        rc, out = self.run_tool('read', '--apply')
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.tsv_text(), '')
        self.state({}, inv={i: [STOPPED] for i in ALL})
        rc, out = self.run_tool('read', '--apply')
        self.assertEqual(rc, 0, out)
        s = self.sent()
        self.assertEqual(sorted(s['ids']), ALL)
        self.assertEqual(
            s['params']['commands'],
            ['systemctl is-active thundermail; systemctl is-enabled thundermail; exit 0'],
        )
        self.assertIn(
            'node mailstrom-prod-stalwart-61 i-0000000000000a061 Success rc=0 inactive disabled',
            out,
        )
        self.assertIn('summary: inactive/disabled=6', out)
        self.assertIn('all 6 nodes inactive and disabled: stop-legacy-stalwart row written', out)
        rows = [ln.split('\t') for ln in self.tsv_text().splitlines()]
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0][0], 'stop-legacy-stalwart')
        self.assertEqual(rows[0][3:], ['nodes', '6'])
        for t in rows[0][1:3]:
            self.assertRegex(t, r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$')

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
        self.state({}, inv={i: [STOPPED] for i in ALL} | {ALL[3]: [extra]})
        rc, out = self.run_tool('read', '--apply')
        self.assertEqual(rc, 0, out)
        self.assertIn('1 of 6 node(s) not inactive and disabled', out)
        self.assertEqual(self.tsv_text(), '')

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
            self.state(st)
            rc, out = self.run_tool('start', '--apply')
            self.assertEqual(rc, 2, (st, out))
            self.assertIn('NEED  ', out)
            self.assertIn(msg, out)
            self.assertIsNone(self.sent(), st)
        self.state({})
        rc, out = self.run_tool('start', '--apply')
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
        rc, out = self.run_tool('read', '--apply')
        self.assertEqual(rc, 0, out)

    def test_evidence_log_has_the_run(self):
        rc, out = self.run_tool('read')
        self.assertEqual(rc, 0, out)
        log = self.t / 'ev' / 'legacy-stalwart-read-dry-run-20261119T000000Z.log'
        self.assertIn('legacy-stalwart read dry-run: done (6 node(s))', log.read_text())


if __name__ == '__main__':
    unittest.main()
