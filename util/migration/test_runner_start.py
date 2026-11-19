import importlib
import json
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
ARN = 'arn:aws:eks:eu-central-1:000000000000:cluster/mzla-eks-tb-prod01'
PASSWORDS = ('kc-pw-123', 'acc-pw-123', 'legacy-stalwart-pw-123')
KUBE = r"""#!{py}
import json, os, sys
args = sys.argv[1:]
a = ' '.join(args)
open(os.environ['FAKE_LOG'], 'a').write('kubectl ' + a + '\n')
open(os.environ['FAKE_LOG'] + '.json', 'a').write(json.dumps(['kubectl'] + args) + '\n')
p = os.environ['KUBE_STATE']
st = json.load(open(p))
out, rc = '', 0
i = args.index('-n') + 2 if '-n' in args else 2
verb = args[i] if i < len(args) else ''
rest = args[i + 1:]
if verb == 'get' and rest[0].startswith('securitygrouppolicies'):
    out = json.dumps({{'items': st['sgps']}})
elif verb == 'get' and rest[:2] == ['secret', 'stalwart-postgresql-admin-password']:
    out = 'yes' if st.get('target_key', True) else ''
elif verb == 'get' and rest[:2] == ['configmap', 'stalwart-config']:
    out = json.dumps({{'host': st.get('cm_host', 'ep-cut-pooler.example.test')}})
elif verb == 'get' and rest[:2] == ['pod', 'runner'] and '--ignore-not-found' in rest:
    out = 'pod/runner' if st.get('pod') else ''
elif verb == 'get' and rest[0] == 'secret' and '--ignore-not-found' in rest:
    out = 'secret/' + rest[1] if st.get('secret') else ''
elif verb == 'get' and rest[:3] == ['pod', 'runner', '-o'] and rest[3] == 'json':
    if not st.get('pod'):
        sys.stderr.write('Error from server (NotFound): pods "runner" not found\n')
        rc = 1
    else:
        eni = st.get('eni', True)
        lim = {{'vpc.amazonaws.com/pod-eni': '1'}} if eni else {{}}
        ann = {{'vpc.amazonaws.com/pod-eni': '[{{"eniId":"eni-x"}}]'}} if eni else {{}}
        pg = {{'name': 'postgres', 'resources': {{'limits': lim}}}}
        spec = {{'nodeName': 'n1', 'containers': [pg, {{'name': 'python'}}]}}
        if st.get('automount') is not None:
            spec['automountServiceAccountToken'] = st['automount']
        r = st.get('restarts', 0)
        cs = [{{'name': n, 'ready': True, 'restartCount': r}} for n in ('postgres', 'python')]
        status = {{'containerStatuses': cs}}
        out = json.dumps({{'metadata': {{'annotations': ann}}, 'spec': spec, 'status': status}})
elif verb == 'get' and rest[:1] == ['node']:
    out = st.get('role', 'mail')
elif verb == 'get' and rest[:1] == ['pods,jobs,cronjobs']:
    out = '\n'.join(st.get('pods', ['pod/runner'] if st.get('pod') else []))
elif verb == 'create':
    f = rest[rest.index('-f') + 1]
    text = open(f).read()
    open(os.environ['CREATED'], 'a').write(text + '\n---\n')
    if '"kind": "Secret"' in text:
        st['secret'] = True
        if st.get('term'):
            json.dump(st, open(p, 'w'))
            os.kill(os.getppid(), 15)
            sys.exit(1)
    else:
        st['pod'] = True
        st['automount'] = False if 'automountServiceAccountToken: false' in text else None
elif verb == 'wait':
    rc = 0 if st.get('pod') else 1
elif verb == 'delete':
    if not st.get('sticky'):
        st['pod' if rest[0] == 'pod' else 'secret'] = False
elif verb == 'exec':
    cmd = args[args.index('--') + 1:]
    if cmd[0] == 'pg_isready':
        out = cmd[2] + ':5432 - ' + st.get('isready', 'accepting connections')
    elif cmd[0] == 'sh':
        out = st.get('pgpass', '0\nnokey')
    elif cmd[0] == 'psql':
        host, user = cmd[cmd.index('-h') + 1], cmd[cmd.index('-U') + 1]
        db = cmd[cmd.index('-d') + 1]
        out = st.get('logins', {{}}).get(host, db + '|' + user)
else:
    rc = 1
json.dump(st, open(p, 'w'))
if out:
    print(out)
sys.exit(rc)
"""
ENV = {
    'TARGET_ENV': 'tb-prod',
    'STALWART_TARGET_PG_HOST': 'ep-cut.example.test',
    'STALWART_TARGET_PG_USER': 'stalwart_owner',
    'STALWART_TARGET_PG_DB': 'stalwart',
    'STALWART_LEGACY_PG_HOST': 'legacy-stalwart.example.test',
    'STALWART_LEGACY_PG_PASSWORD': PASSWORDS[2],
    'KC_LEGACY_PG_HOST': 'kc.example.test',
    'KC_LEGACY_PG_USER': 'keycloak_user',
    'KC_LEGACY_PG_PASSWORD': PASSWORDS[0],
    'ACCOUNTS_LEGACY_PG_HOST': 'accounts.example.test',
    'ACCOUNTS_LEGACY_PG_USER': 'accounts_user',
    'ACCOUNTS_LEGACY_PG_NAME': 'accounts',
    'ACCOUNTS_LEGACY_PG_PASSWORD': PASSWORDS[1],
}
SGPS = [
    {
        'metadata': {'name': 'stalwart-server'},
        'spec': {
            'podSelector': {'matchLabels': {'app': 'stalwart'}},
            'securityGroups': {'groupIds': ['sg-srv']},
        },
    },
    {
        'metadata': {'name': 'stalwart-config-applier'},
        'spec': {
            'podSelector': {'matchLabels': {'app.kubernetes.io/name': 'stalwart-config-applier'}},
            'securityGroups': {'groupIds': ['sg-srv']},
        },
    },
]
NETNS = (
    shutil.which('unshare')
    and subprocess.run(['unshare', '-rn', 'true'], capture_output=True).returncode == 0
)


class RunnerStart(unittest.TestCase):
    def setUp(self):
        self.t = pathlib.Path(tempfile.mkdtemp())
        self.t.chmod(0o700)
        self.mig = self.t / 'mig'
        shutil.copytree(HERE, self.mig, ignore=shutil.ignore_patterns('__pycache__', 'test_*.py'))
        tp = self.mig / 'targets' / 'tb-prod.sh'
        tp.write_text(re.sub(r'REPLACE_ME_[A-Z_]+', 'legacy-pg', tp.read_text()))
        self.bin = self.t / 'bin'
        self.bin.mkdir()
        (self.bin / 'kubectl').write_text(KUBE.format(py=sys.executable))
        (self.bin / 'kubectl').chmod(0o755)
        self.kc = self.t / 'kc-prod'
        self.kubeconfig(ARN)
        self.ev = self.t / 'ev'
        self.ev.mkdir(mode=0o700)
        self.secret_dir = self.t / 'shm' / 'secret'
        self.state(sgps=SGPS)
        self.login_at(time.time())

    def tearDown(self):
        shutil.rmtree(self.t, ignore_errors=True)

    def kubeconfig(self, cluster):
        self.kc.write_text(
            'apiVersion: v1\nkind: Config\ncurrent-context: prod\n'
            'contexts:\n- name: prod\n  context: {cluster: %s, user: u}\n'
            'clusters:\n- name: %s\n  cluster: {server: https://127.0.0.1:1}\n'
            'users:\n- name: u\n  user: {}\n' % (cluster, cluster)
        )

    def state(self, **st):
        (self.t / 'kube.json').write_text(json.dumps(st))

    def kube(self):
        return json.loads((self.t / 'kube.json').read_text())

    def login_at(self, at):
        (self.ev / 'sso-login-at.txt').write_text('%d\n' % at)

    def run_tool(self, *args, target='tb-prod', env_extra=None):
        envf = self.t / 'cutover.env'
        vals = dict(ENV, TARGET_ENV=target, **(env_extra or {}))
        envf.write_text(''.join("%s='%s'\n" % kv for kv in vals.items()))
        envf.chmod(0o600)
        env = {
            'PATH': '%s:%s' % (self.bin, os.environ['PATH']),
            'HOME': str(self.t),
            'TARGET_ENV': target,
            'TS': '20261119T010000Z',
            'CUTOVER_ENV': str(envf),
            'EVIDENCE_DIR': str(self.ev),
            'SECRET_DIR': str(self.secret_dir),
            'TIMING_TSV': str(self.ev / 'timing.tsv'),
            'KUBECONFIG_TARGET': str(self.kc),
            'FAKE_LOG': str(self.t / 'fake.log'),
            'KUBE_STATE': str(self.t / 'kube.json'),
            'CREATED': str(self.t / 'created.txt'),
        }
        cmd = ['bash', str(self.mig / 'window' / 'runner-start.sh'), *args]
        if NETNS:
            cmd = ['unshare', '-rn', *cmd]
        p = subprocess.run(cmd, capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL)
        return p.returncode, p.stdout + p.stderr

    def argv(self):
        p = self.t / 'fake.log.json'
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def calls(self):
        p = self.t / 'fake.log'
        return p.read_text().splitlines() if p.exists() else []

    def writes(self):
        return [
            c
            for c in self.calls()
            if re.search(r' -n thundermail (create|delete|apply|patch|scale) ', c)
        ]

    def rows(self):
        p = self.ev / 'timing.tsv'
        return [x.split('\t')[0] for x in p.read_text().splitlines()] if p.exists() else []

    def created(self):
        p = self.t / 'created.txt'
        return p.read_text() if p.exists() else ''

    def test_dry_run_prints_the_apply_writes_and_writes_nothing(self):
        rc, out = self.run_tool()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.rows(), [])
        planned = [shlex.split(x[4:]) for x in out.splitlines() if x.startswith('  + kubectl ')]
        self.assertEqual(
            [c[5] for c in planned if c[5] in ('create', 'wait')], ['create'] * 2 + ['wait']
        )
        self.assertIn('+ env SECRET_DIR=%s TS=20261119T010000Z bash ' % self.secret_dir, out)
        self.assertIn('runner-pod.sh --secret', out)
        gates = len(self.argv())
        rc, out2 = self.run_tool('--apply')
        self.assertEqual(rc, 0, out2)
        ran = self.argv()[2 * gates :]
        self.assertEqual(planned, [c for c in ran if c[3:6] != ['get', 'node', 'n1']])
        self.assertIn('  + kubectl --kubeconfig %s get node n1' % self.kc, out2)
        self.assertEqual(self.rows(), ['runner-start'])

    def test_an_interrupt_leaves_no_secret_file(self):
        self.state(sgps=SGPS, term=True)
        rc, out = self.run_tool('--apply')
        self.assertEqual(rc, 1, out)
        self.assertIn('interrupted by signal', out)
        self.assertEqual(list(self.secret_dir.iterdir()), [])

    def test_dry_run_reports_need_and_apply_stops_on_each_gate(self):
        cases = {
            'no policy': dict(sgps=SGPS[:1]),
            'other groups': dict(
                sgps=[SGPS[0], json.loads(json.dumps(SGPS[1]).replace('sg-srv', 'sg-node'))]
            ),
            'no target key': dict(sgps=SGPS, target_key=False),
            'other host': dict(sgps=SGPS, cm_host='ep-old-pooler.example.test'),
            'runner exists': dict(sgps=SGPS, pod=True),
            'secret exists': dict(sgps=SGPS, secret=True),
        }
        for name, st in cases.items():
            with self.subTest(name):
                self.state(**st)
                rc, out = self.run_tool()
                self.assertEqual(rc, 2, out)
                self.assertRegex(out, r'(?m)^NEED  ')
                (self.t / 'fake.log').unlink()
                rc, out = self.run_tool('--apply')
                self.assertEqual(rc, 1, out)
                self.assertIn('gate(s) NEED above', out)
                self.assertEqual(self.writes(), [])
                self.assertNotIn('runner-start', self.rows())
        self.state(sgps=SGPS)
        rc, out = self.run_tool('--apply')
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(self.writes()), 2)

    def test_session_gate(self):
        self.login_at(time.time() - 14400 + 20 * 60)
        rc, out = self.run_tool()
        self.assertEqual(rc, 2, out)
        self.assertRegex(out, r'NEED  session minutes left: (19|20), under 45')
        (self.ev / 'sso-login-at.txt').unlink()
        rc, out = self.run_tool()
        self.assertIn('NEED  no %s/sso-login-at.txt' % self.ev, out)
        self.login_at(time.time())
        rc, out = self.run_tool()
        self.assertRegex(out, r'HAVE  session minutes left: (240|239)')

    def test_refuses_another_target_or_cluster(self):
        rc, out = self.run_tool('--apply', target='tb-stage')
        self.assertEqual(rc, 64, out)
        self.assertIn('starts the tb-prod runner only, not tb-stage', out)
        self.kubeconfig('arn:aws:eks:eu-central-1:000000000000:cluster/mzla-eks-tb-dev01')
        rc, out = self.run_tool('--apply')
        self.assertEqual(rc, 64, out)
        self.assertIn('is not cluster mzla-eks-tb-prod01', out)
        self.assertEqual(self.calls(), [])
        self.kubeconfig(ARN)
        rc, out = self.run_tool('--apply')
        self.assertEqual(rc, 0, out)

    def test_no_password_on_a_command_line_in_a_log_or_left_on_disk(self):
        rc, out = self.run_tool('--apply')
        self.assertEqual(rc, 0, out)
        created = self.created()
        self.assertIn(PASSWORDS[0], created)
        self.assertIn(PASSWORDS[1], created)
        secret = json.loads(created.split('\n---\n')[0])
        self.assertEqual(
            sorted(secret['stringData']), ['accounts', 'accounts-host', 'keycloak', 'keycloak-host']
        )
        self.assertNotIn(PASSWORDS[2], created)
        logs = '\n'.join(self.calls()) + out
        logs += ''.join(p.read_text() for p in self.ev.iterdir() if p.is_file())
        for pw in PASSWORDS:
            self.assertNotIn(pw, logs)
        self.assertEqual(list(self.secret_dir.iterdir()), [])
        self.assertTrue(all(' apply ' not in c for c in self.calls()))

    def test_refusal_when_the_key_list_names_legacy_stalwart(self):
        tp = self.mig / 'targets' / 'tb-prod.sh'
        tp.write_text(
            tp.read_text().replace(
                "RUNNER_LEGACY_PG_KEYS='keycloak accounts'",
                "RUNNER_LEGACY_PG_KEYS='stalwart keycloak accounts'",
            )
        )
        rc, out = self.run_tool()
        self.assertEqual(rc, 2, out)
        self.assertIn('the tb-prod runner gets no legacy Stalwart credential', out)
        rc, out = self.run_tool('--apply')
        self.assertEqual(rc, 1, out)
        self.assertEqual(self.writes(), [])

    def test_manifest_is_admitted_with_policy_label_node_and_no_token(self):
        rc, out = self.run_tool()
        self.assertEqual(rc, 0, out)
        m = (self.ev / 'runner-pod-20261119T010000Z.yaml').read_text()
        self.assertRegex(m, r'(?m)^spec:\n  automountServiceAccountToken: false\n')
        self.assertIn('    app.kubernetes.io/name: stalwart-config-applier\n', m)
        self.assertIn('  nodeSelector:\n    role: mail\n', m)
        self.assertIn('        secretName: legacy-pg\n', m)
        self.assertNotIn('serviceAccountName', m)
        raw = (self.mig / 'runner-pod.yaml').read_text()
        self.assertNotIn('automountServiceAccountToken', raw)

    def test_checks_after_start_fail_and_write_no_row(self):
        cases = {
            'no branch interface': dict(eni=False),
            'other node': dict(role='general'),
            'restart': dict(restarts=1),
            'kc login': dict(logins={'kc.example.test': 'error: password authentication failed'}),
            'branch login': dict(logins={'ep-cut.example.test': 'stalwart|someone'}),
            'legacy down': dict(isready='no response'),
            'legacy password line': dict(pgpass='1\nnokey'),
            'other pod': dict(pods=['pod/runner', 'pod/stalwart-abc']),
            'a job': dict(pods=['pod/runner', 'job.batch/stalwart-config-apply']),
        }
        for name, st in cases.items():
            with self.subTest(name):
                self.state(sgps=SGPS, **st)
                (self.ev / 'timing.tsv').unlink(missing_ok=True)
                rc, out = self.run_tool('--apply')
                self.assertEqual(rc, 1, out)
                self.assertRegex(out, r'(?m)^FAIL  ')
                self.assertNotIn('runner-start', self.rows())
                self.assertIn('stop-RUNNER', self.rows())
        self.state(sgps=SGPS)
        (self.ev / 'timing.tsv').unlink()
        rc, out = self.run_tool('--apply')
        self.assertEqual(rc, 0, out)
        for line in (
            'PASS  node role mail',
            'PASS  vpc.amazonaws.com/pod-eni limit 1: a SecurityGroupPolicy matched',
            'PASS  service account default',
            'PASS  no service account token',
            'PASS  legacy Stalwart database: accepting connections',
            'PASS  no legacy Stalwart password in the runner',
            'PASS  cutover-branch login: stalwart|stalwart_owner',
            'PASS  keycloak login: keycloak|keycloak_user',
            'PASS  accounts login: accounts|accounts_user',
            'PASS  zero: thundermail 1 is pod/runner, no other pod, Job or CronJob',
            'RUNNER apply: done',
        ):
            self.assertIn(line, out)
        self.assertEqual(self.rows(), ['runner-start'])

    def test_check_mode_reads_only_and_names_other_pods(self):
        self.state(sgps=SGPS, pod=True, automount=False, pods=['pod/runner', 'pod/stalwart-abc'])
        rc, out = self.run_tool('check')
        self.assertEqual(rc, 0, out)
        self.assertIn('INFO  zero: thundermail has pod/runner and pod/stalwart-abc', out)
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.rows(), [])
        self.state(sgps=SGPS)
        (self.t / 'fake.log').unlink()
        rc, out = self.run_tool('check')
        self.assertEqual(rc, 1, out)
        self.assertIn('FAIL  no runner pod', out)
        self.assertFalse([c for c in self.calls() if ' exec ' in c])

    def test_remove(self):
        self.state(sgps=SGPS, pod=True, secret=True)
        rc, out = self.run_tool('remove')
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.writes(), [])
        self.assertIn(
            '-n thundermail delete pod runner --ignore-not-found --wait=true --timeout=120s', out
        )
        self.assertIn('-n thundermail delete secret legacy-pg --ignore-not-found', out)
        self.secret_dir.mkdir(parents=True)
        (self.secret_dir / 'legacy-pg-20261118T000000Z.yaml').write_text('{}')
        (self.secret_dir / 'other.yaml').write_text('{}')
        self.state(sgps=SGPS, pod=True, secret=True, sticky=True)
        rc, out = self.run_tool('remove', '--apply')
        self.assertEqual(rc, 1, out)
        self.assertIn('FAIL  runner pod or Secret legacy-pg still there', out)
        self.assertNotIn('runner-removed', self.rows())
        self.state(sgps=SGPS, pod=True, secret=True)
        rc, out = self.run_tool('remove', '--apply')
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(self.writes()), 4)
        self.assertIn('PASS  runner pod and Secret legacy-pg gone', out)
        self.assertEqual(self.rows()[-1], 'runner-removed')
        self.assertFalse(self.kube().get('pod') or self.kube().get('secret'))
        self.assertEqual([x.name for x in self.secret_dir.iterdir()], ['other.yaml'])

    def test_usage(self):
        rc, out = self.run_tool('stop')
        self.assertEqual(rc, 64, out)
        self.assertIn('unknown mode: stop', out)
        rc, out = self.run_tool('start', 'check')
        self.assertEqual(rc, 64, out)


class W4RunnerHint(importlib.import_module('test_pr3_w4').Prod):
    def test_w4_names_the_tb_prod_start_command(self):
        self.kube({'runner': 'Pending'})
        body = 'APPLY=0; preflight 2>&1 | head -1'
        out = self.w4(body)
        want = 'NEED  runner pod not Running: bash %s/window/runner-start.sh --apply before W4'
        self.assertIn(want % self.MIG, out)
        out = self.w4(body, target='tb-stage')
        self.assertIn('NEED  runner pod not Running: apply util/migration/runner-pod.yaml', out)
        self.assertNotIn('runner-start.sh', out)


if __name__ == '__main__':
    unittest.main()
