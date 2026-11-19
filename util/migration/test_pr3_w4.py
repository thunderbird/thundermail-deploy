import json
import pathlib
import re
import shutil
import subprocess
import sys
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_target import Sandbox  # noqa: E402
from test_window import Harness  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent
KUBE = r"""#!{py}
import json, os, re, sys
AUTH = 'psql: error: FATAL:  password authentication failed for user "o"' + chr(10) + 'rc=2'
args = sys.argv[1:]
a = ' '.join(args)
open(os.environ['FAKE_LOG'], 'a').write('kubectl ' + a + '\n')
p = os.environ['KUBE_STATE']
st = json.load(open(p))
out, rc = '', 0
if 'get deploy/stalwart -o jsonpath={{.spec.replicas}}' in a:
    out = str(st.get('replicas', 0))
elif 'get pod runner -o jsonpath={{.status.phase}}' in a:
    out = st.get('runner', 'Running')
elif 'get pods -l app=stalwart -o json' in a:
    out = json.dumps({{'items': st.get('pods', [])}})
elif 'get deploy/stalwart -o json' in a:
    out = json.dumps(st['deploy'])
elif ' exec -i runner -c postgres -- psql ' in ' ' + a + ' ' and '-1' in args:
    lines = []
    for i, x in enumerate(args):
        if x == '-c' and args[i - 1] != 'runner':
            sql = args[i + 1]
            if sql.startswith('select count'):
                lines.append(str(st.get('count', 0)))
            else:
                lines.append('DELETE %d' % st.get('delete', st.get('count', 0)))
                st['count'] = st.get('left', 0)
    out = chr(10).join(lines)
elif ' exec -i runner -c postgres -- psql ' in ' ' + a + ' ':
    sql = args[-1]
    if 'select count(*) from k' in sql:
        out = str(st.get('count', 0))
    elif sql.startswith('delete from k'):
        out = 'DELETE %d' % st.get('delete', st.get('count', 0))
        st['count'] = st.get('left', 0)
    elif "from p where k=" in sql:
        out = st.get('schema', '00000005')
    elif sql == 'select 1':
        out = st.get('login', '1')
    else:
        rc = 1
elif ' exec -i runner -c postgres -- sh -c ' in ' ' + a + ' ':
    i = args.index('--')
    script, host = args[i + 3], args[i + 5]
    if 'awk' in script:
        out = st.get('prod_cutover', AUTH)
    elif 'read -r PGPASSWORD' in script:
        pw = sys.stdin.readline().rstrip('\n')
        st.setdefault('seen_pw', []).append(pw)
        if host == st.get('source_host'):
            out = st.get('source_login', '1\nrc=0')
        else:
            out = st.get('prod_source', AUTH)
else:
    rc = 1
json.dump(st, open(p, 'w'))
if out:
    print(out)
sys.exit(rc)
"""
NB = r"""#!{py}
import json, os, sys
args = sys.argv[1:]
open(os.environ['FAKE_LOG'], 'a').write('nb ' + ' '.join(args) + '\n')
st = json.load(open(os.environ['NB_STATE']))
cmd = [x for x in args if x in ('reset', 'source', 'hosts', 'show', 'check')][0]
if cmd == 'hosts':
    print('parent_host=' + st['parent_host'])
    print('branch_host=' + st['branch_host'])
elif cmd == 'check':
    print('branch tb-prod-cutover state=ready parent_lsn=0/9 x')
    print('branch tb-prod-cutover-source state=ready parent_lsn=%s x' % st.get('source_lsn', '0/9'))
    if st.get('source_lsn', '0/9') != '0/9':
        print('branch check FAILED: parent_lsn differs: '
              'tb-prod-cutover=0/9 tb-prod-cutover-source=0/8')
        sys.exit(1)
    print('branch check: both ready, later than the legacy stop x, parent_lsn 0/9')
elif cmd == 'reset':
    print('before: branch tb-prod-cutover state=ready '
          'last_reset_at=2026-11-01T00:00:00Z parent_lsn=0/1 x')
    if '--apply' in args:
        print('after: branch tb-prod-cutover state=ready '
              'last_reset_at=%s parent_lsn=0/9 x' % st['reset_at'])
        sys.exit(st.get('reset_rc', 0))
elif cmd == 'source':
    if '--apply' in args:
        out = args[args.index('--out') + 1]
        open(out, 'w').write(
            'STALWART_SOURCE_PG_HOST=src.example.test\nSTALWART_SOURCE_PG_DB=stalwart\n'
            'STALWART_SOURCE_PG_USER=stalwart_owner\nSTALWART_SOURCE_PG_PASSWORD=src-pw-123\n'
        )
        print('created: branch tb-prod-cutover-source state=ready '
              'created_at=%s x endpoint=read_only' % st['created_at'])
"""
TOP = re.compile(
    r'(?m)^(?:NS|S|POD|RECOVERY_[A-Z]+|REVERT|RULE|TPSQL|NB|WANT_SCHEMA|CUTOVER_BRANCH|SOURCE_BRANCH|APPLY_ARG)=.*$'
)
AUTH = 'psql: error: FATAL:  password authentication failed for user "o"\nrc=2'
ARN = 'arn:aws:eks:eu-central-1:1:cluster/mzla-eks-tb-prod01'


def deploy(replicas=1, ready=1):
    c = {'name': 'stalwart', 'env': [{'name': 'STALWART_RECOVERY_MODE', 'value': 'true'}]}
    return {
        'metadata': {'generation': 2},
        'spec': {'replicas': replicas, 'template': {'spec': {'containers': [c]}}},
        'status': {
            'observedGeneration': 2,
            'replicas': replicas,
            'readyReplicas': ready,
            'updatedReplicas': replicas,
        },
    }


def pod(start):
    c = {'name': 'stalwart', 'env': [{'name': 'STALWART_RECOVERY_MODE', 'value': 'true'}]}
    return {
        'metadata': {
            'name': 'stalwart-5d9f-a',
            'ownerReferences': [{'kind': 'ReplicaSet', 'name': 'stalwart-5d9f'}],
        },
        'spec': {'containers': [c]},
        'status': {'startTime': start, 'conditions': [{'type': 'Ready', 'status': 'True'}]},
    }


class Prod(Harness):
    def setUp(self):
        super().setUp()
        b = self.t / 'bin'
        b.mkdir()
        for name, src in (('kubectl', KUBE), ('nb', NB)):
            (b / name).write_text(src.format(py=sys.executable))
            (b / name).chmod(0o755)
        if shutil.which('yq'):
            (b / 'yq').symlink_to(shutil.which('yq'))
        (self.t / 'kc-prod').write_text(
            'apiVersion: v1\nkind: Config\ncurrent-context: prod\n'
            'contexts:\n- name: prod\n  context: {cluster: %s, user: u}\n'
            'clusters:\n- name: %s\n  cluster: {server: https://127.0.0.1:1}\n'
            'users:\n- name: u\n  user: {}\n' % (ARN, ARN)
        )
        self.kube({})
        self.nb(
            {
                'parent_host': 'prod.example.test',
                'branch_host': 'cut.example.test',
                'reset_at': '2026-11-19T05:00:00Z',
                'created_at': '2026-11-19T05:00:05Z',
            }
        )

    def kube(self, st):
        st.setdefault('source_host', 'src.example.test')
        (self.t / 'kube.json').write_text(json.dumps(st))

    def kube_state(self):
        return json.loads((self.t / 'kube.json').read_text())

    def nb(self, st):
        (self.t / 'nb.json').write_text(json.dumps(st))

    def env(self, **extra):
        e = super().env(**extra)
        e['PATH'] = '%s:%s' % (self.t / 'bin', e['PATH'])
        e['KUBE_STATE'] = str(self.t / 'kube.json')
        e['NB_STATE'] = str(self.t / 'nb.json')
        e['KUBECONFIG_TARGET'] = str(self.t / 'kc-prod')
        return e

    def rows(self, text):
        (self.t / 'ev').mkdir(exist_ok=True, mode=0o700)
        (self.t / 'ev' / 'timing.tsv').write_text(''.join('\t'.join(r) + '\n' for r in text))

    def timing(self):
        p = self.t / 'ev' / 'timing.tsv'
        return [x.split('\t') for x in p.read_text().splitlines()] if p.exists() else []

    def calls(self, tool):
        p = self.t / 'fake.log'
        return (
            [x for x in p.read_text().splitlines() if x.startswith(tool + ' ')]
            if p.exists()
            else []
        )

    def w4(self, body, setup='', target='tb-prod'):
        text = (self.MIG / 'window' / 'w4-migrate.sh').read_text()
        head = (
            'export STALWART_TARGET_PG_HOST=cut.example.test '
            'STALWART_TARGET_PG_USER=stalwart_owner '
            'STALWART_TARGET_PG_DB=stalwart TM_RECOVERY_PR=197\n'
            'KS=(kubectl --kubeconfig %s)\n' % (self.t / 'kc-prod')
        )
        head += '\n'.join(TOP.findall(text)) + '\n'
        head += 'PY=%s\nSTATE=""\nNB=(nb)\n' % sys.executable
        return self.fn('w4-migrate.sh', body, setup=head + setup, env=self.env(TARGET_ENV=target))


CUT = ['cut-ecs', '2026-11-19T04:00:00Z', '2026-11-19T04:00:00Z', 'tasks', '0']
EXPORT = ['w4-export', '2026-11-19T04:19:00Z', '2026-11-19T04:19:00Z', 'file', 'x']
READ = ['w4-read-done', '2026-11-19T04:20:00Z', '2026-11-19T04:20:00Z', 'export', 'x']
STOPPED = ['stop-legacy-stalwart', '2026-11-19T04:30:00Z', '2026-11-19T04:30:00Z', 'nodes', '6']


class ReadPhase(Prod):
    BODY = (
        'dump() { echo DUMP; }; duplicates() { echo DUP; }\n'
        'convert() { echo CONVERT; DROPPED=0; }\n'
        'retarget() { echo RETARGET; }; queue_drain() { echo QUEUE; }\n'
        'mkdir -p "$S"; echo "{}" > "$S/export.json"\n'
    )

    def test_order_marks_and_a_queue_rerun_reads_the_queue_only(self):
        self.rows([CUT])
        out = self.w4(self.BODY + 'read_phase; echo "rc=$?"')
        order = [x for x in out.split() if x in ('DUMP', 'DUP', 'CONVERT', 'RETARGET', 'QUEUE')]
        self.assertEqual(order, ['DUMP', 'DUP', 'CONVERT', 'RETARGET', 'QUEUE'])
        self.assertIn('legacy-read phase done', out)
        names = [r[0] for r in self.timing()]
        self.assertLess(names.index('w4-export'), names.index('queue-drain'))
        self.assertEqual(names[-1], 'read-done')
        self.assertIn('w4-read-done', names)
        out = self.w4(self.BODY + 'read_phase; echo "rc=$?"')
        self.assertIn('reusing the export of this cut', out)
        self.assertEqual([x for x in out.split() if x in ('DUMP', 'QUEUE')], ['QUEUE'])
        out = self.w4(self.BODY + 'GO=again\nconvert() { echo CONVERT; return 1; }\nread_phase')
        self.assertIn('DUMP', out.split())
        out = self.w4(self.BODY + 'read_phase; echo "rc=$?"')
        self.assertNotIn('reusing the export of this cut', out)
        self.assertIn('DUMP', out.split())
        self.rows([CUT, EXPORT, READ, ['w4-export-start', 'b', 'b', 'file', 'x'], STOPPED])
        self.kube({'replicas': 0, 'pods': []})
        out = self.w4('mkdir -p "$S"; echo "{}" > "$S/export.json"\nreset_preflight; echo "rc=$?"')
        self.assertIn('the export of the last read run did not pass its checks', out)
        self.assertIn('rc=1', out)

    def test_the_tb_prod_dump_keeps_the_password_off_the_command_line(self):
        text = (self.MIG / 'window' / 'w4-migrate.sh').read_text()
        prod = re.search(
            r'(?m)^\[ "\$TARGET_ENV" != tb-prod \] \|\| DUMP_CMD=\((.*)\)$', text
        ).group(1)
        self.assertIn('dump_legacy.py', prod)
        self.assertNotIn('PASSWORD', prod)


class ResetPhase(Prod):
    def preflight(self, rows, **kube):
        self.rows(rows)
        self.kube(dict({'replicas': 0, 'pods': []}, **kube))
        return self.w4(
            'mkdir -p "$S"; echo "{}" > "$S/export.json"\n'
            'reset_preflight; echo "rc=$? stop=$STOP_AT"'
        )

    def test_the_reset_needs_the_read_phase_then_the_stop_and_no_pod(self):
        out = self.preflight([CUT, EXPORT, READ, STOPPED])
        self.assertIn('rc=0 stop=2026-11-19T04:30:00Z', out)
        out = self.preflight([CUT, STOPPED])
        self.assertIn('no w4-read-done row after the last cut-ecs row', out)
        self.assertIn('rc=1', out)
        out = self.preflight([CUT, EXPORT, READ])
        self.assertIn('no stop-legacy-stalwart row after the last cut-ecs row', out)
        out = self.preflight([CUT, STOPPED, EXPORT, READ])
        self.assertIn('the stop-legacy-stalwart row comes before the w4-read-done row', out)
        out = self.preflight([STOPPED, EXPORT, READ, CUT])
        self.assertIn('rc=1', out)
        out = self.preflight([READ, STOPPED])
        self.assertIn('no cut-ecs row in', out)
        self.assertIn('rc=1', out)
        out = self.preflight(
            [CUT, EXPORT, READ, STOPPED], replicas=1, pods=[pod('2026-11-19T04:40:00Z')]
        )
        self.assertIn('the reset needs deploy/stalwart at 0 replicas and no app=stalwart pod', out)

    def test_reset_and_source_mark_rows_and_replace_the_env_names(self):
        (self.t / 'cutover.env').write_text("STALWART_SOURCE_PG_HOST='old'\nKEEP='x'\n")
        out = self.w4(
            'APPLY_ARG=(--apply); STOP_AT=2026-11-19T04:30:00Z\n'
            'neon_reset; echo "rc=$?"; source_branch; echo "rc=$?"'
        )
        self.assertIn('rc=0\n', out)
        self.assertNotIn('src-pw-123', out)
        self.assertIn('STALWART_SOURCE_PG_PASSWORD (len=10) in ', out)
        env = (self.t / 'cutover.env').read_text()
        self.assertNotIn("'old'", env)
        self.assertIn("KEEP='x'", env)
        self.assertIn("STALWART_SOURCE_PG_HOST='src.example.test'", env)
        rows = {r[0]: r[4] for r in self.timing()}
        self.assertEqual(rows['reset'], '2026-11-19T05:00:00Z')
        self.assertEqual(rows['source-branch'], '2026-11-19T05:00:05Z')
        kept = (self.t / 'ev' / 'window-values.tsv').read_text()
        self.assertIn('STALWART_SOURCE_PG_HOST\tsrc.example.test\n', kept)
        self.assertIn('STALWART_SOURCE_PG_USER\tstalwart_owner\n', kept)
        self.assertIn('STALWART_SOURCE_PG_DB\tstalwart\n', kept)
        self.assertNotIn('src-pw-123', kept)
        self.nb({'reset_at': '2026-11-19T05:00:00Z', 'reset_rc': 1})
        (self.t / 'ev' / 'timing.tsv').unlink()
        out = self.w4('APPLY_ARG=(--apply); STOP_AT=x\nneon_reset; echo "rc=$?"')
        self.assertIn('rc=1', out)
        self.assertNotIn('reset', [r[0] for r in self.timing()])

    def test_schema_other_than_5_after_the_reset_stops(self):
        self.kube({'schema': '00000006'})
        out = self.w4('schema_before; echo "rc=$?"')
        self.assertIn('schema 00000006 after the reset, want 00000005', out)
        self.assertIn('rc=1', out)
        self.kube({'schema': '00000005'})
        self.assertIn('rc=0', self.w4('schema_before; echo "rc=$?"'))

    def guard(self, **kube):
        self.kube(kube)
        return self.w4(
            'export STALWART_SOURCE_PG_HOST=src.example.test '
            'STALWART_SOURCE_PG_USER=stalwart_owner STALWART_SOURCE_PG_DB=stalwart '
            'STALWART_SOURCE_PG_PASSWORD=src-pw-123\nguard; echo "rc=$?"'
        )

    def test_the_guard_passes_only_on_password_authentication_failed(self):
        out = self.guard()
        self.assertIn('GUARD PASS\nrc=0', out)
        self.assertEqual(self.kube_state()['seen_pw'], ['src-pw-123', 'src-pw-123'])
        self.assertNotIn('src-pw-123', '\n'.join(self.calls('kubectl')))
        self.assertIn('guard', [r[0] for r in self.timing()])
        for case, kube in (
            ('timeout', {'prod_cutover': 'psql: error: connection to server timed out\nrc=2'}),
            ('accepted', {'prod_cutover': '1\nrc=0'}),
            ('other', {'prod_cutover': 'psql: error: no pg_hba.conf entry\nrc=2'}),
            ('source timeout', {'prod_source': 'psql: error: timeout expired\nrc=2'}),
            (
                'source login',
                {'source_login': 'psql: error: FATAL: password authentication failed\nrc=2'},
            ),
            ('branch login', {'login': 'psql: error: could not connect'}),
        ):
            with self.subTest(case=case):
                (self.t / 'ev' / 'timing.tsv').unlink(missing_ok=True)
                out = self.guard(**kube)
                self.assertNotIn('GUARD PASS', out)
                self.assertIn('rc=1', out)
                self.assertNotIn('guard', [r[0] for r in self.timing()])

    def test_the_guard_refuses_the_production_host_as_target(self):
        self.nb({'parent_host': 'cut.example.test', 'branch_host': 'cut.example.test'})
        out = self.guard()
        self.assertIn('STALWART_TARGET_PG_HOST is the production endpoint', out)

    def test_branch_check_stops_on_a_different_lsn(self):
        out = self.w4('APPLY=1; STOP_AT=x\nbranch_check; echo "rc=$? VALUE=$VALUE"')
        self.assertIn('rc=0 VALUE=0/9', out)
        self.nb({'source_lsn': '0/8'})
        out = self.w4('APPLY=1; STOP_AT=x\nbranch_check; echo "rc=$?"')
        self.assertIn('branch check FAILED: parent_lsn differs', out)
        self.assertIn('rc=1', out)

    def test_pre_delete_prints_delete_n_then_0(self):
        self.kube({'count': 1423546, 'replicas': 0, 'pods': []})
        out = self.w4('predelete; echo "rc=$?"')
        self.assertIn('links to delete: 1423546\nDELETE 1423546\n0\nrc=0', out)
        self.assertEqual({r[0]: r[4] for r in self.timing()}['pre-delete'], '1423546')
        (self.t / 'ev' / 'timing.tsv').unlink()
        self.kube({'count': 5, 'delete': 4, 'left': 1, 'replicas': 0, 'pods': []})
        out = self.w4('predelete; echo "rc=$?"')
        self.assertIn('the pre-delete removed 4 row(s) of 5, 1 left', out)
        self.assertNotIn('pre-delete', [r[0] for r in self.timing()])
        self.kube({'count': 5, 'replicas': 1, 'pods': [pod('2026-11-19T05:00:00Z')]})
        out = self.w4('predelete; echo "rc=$?"')
        self.assertIn('a Stalwart pod exists or deploy/stalwart is not at 0: no pre-delete', out)
        psql = [c for c in self.calls('kubectl') if ' -1 -c ' in c]
        self.assertEqual(len(psql), 2)


class BootPhase(Prod):
    PRE = ['pre-delete', '2026-11-19T05:10:00Z', '2026-11-19T05:10:00Z', 'deleted', '1']

    def test_the_wait_is_an_hour_and_takes_only_a_pod_started_after_the_pre_delete(self):
        text = (self.MIG / 'window' / 'w4-migrate.sh').read_text()
        self.assertIn(
            'RECOVERY_WAIT=${RECOVERY_WAIT:-$([ "$TARGET_ENV" = tb-prod ] '
            '&& echo 3600 || echo 1800)}',
            text,
        )
        self.rows([CUT, self.PRE])
        self.kube({'deploy': deploy(), 'pods': [pod('2026-11-19T05:00:00Z')], 'schema': '00000006'})
        out = self.w4('RECOVERY_WAIT=30\nprod_recovery_boot; echo "rc=$?"')
        self.assertIn('is not later than the pre-delete at 2026-11-19T05:10:00Z', out)
        self.assertIn('resume with: bash', out)
        self.assertIn('--phase boot --apply', out)
        self.assertIn('rc=1', out)
        self.kube({'deploy': deploy(), 'pods': [pod('2026-11-19T05:20:00Z')], 'schema': '00000006'})
        out = self.w4('prod_recovery_boot; echo "rc=$? VALUE=$VALUE"')
        self.assertIn('rc=0 VALUE=00000006', out)

    def test_boot_resumes_only_after_reset_guard_and_pre_delete(self):
        r = ['reset', '2026-11-19T05:00:00Z', '2026-11-19T05:00:00Z', 'last_reset_at', 'x']
        g = ['guard', '2026-11-19T05:05:00Z', '2026-11-19T05:05:00Z', 'result', 'pass']
        self.rows([CUT, r, g, self.PRE])
        self.assertIn(
            'resuming after the pre-delete of 2026-11-19T05:10:00Z; no second reset',
            self.w4('boot_resume'),
        )
        for rows in ([CUT, r, g], [CUT, g, r, self.PRE], [r, g, self.PRE, CUT]):
            with self.subTest(rows=[x[0] for x in rows]):
                self.rows(rows)
                self.assertIn('nothing to resume', self.w4('boot_resume; echo "rc=$?"'))

    def test_an_applied_export_is_not_applied_again_on_resume(self):
        a = ['export-applied', '2026-11-19T05:30:00Z', '2026-11-19T05:30:00Z', 'errors', '0']
        self.rows([CUT, self.PRE, a])
        out = self.w4('apply_export() { echo APPLIED; }\nprod_apply; echo "rc=$?"')
        self.assertIn('the export is already applied after this pre-delete', out)
        self.assertNotIn('APPLIED', out)


class Script(Prod):
    ENV = {
        'OLD_STALWART_URL': 'http://127.0.0.1:18080',
        'OLD_STALWART_USER': 'admin',
        'OLD_STALWART_PASSWORD': 'legacy-pw-99',
        'STALWART_URL': 'http://127.0.0.1:18081',
        'STALWART_USER': 'admin',
        'STALWART_PASSWORD': 'pw-st',
        'STALWART_TARGET_PG_HOST': 'cut.example.test',
        'STALWART_TARGET_PG_USER': 'stalwart_owner',
        'STALWART_TARGET_PG_DB': 'stalwart',
        'TARGET_BLOB_BUCKET': 't',
        'REDIS_URL': 'redis://r.example.test:6379',
        'NEON_PROJECT_STALWART_LEGACY': 'proj',
        'NEON_LEGACY_STALWART_BRANCH': 'br-prod',
        'TM_RECOVERY_PR': '197',
        'TARGET_ENV': 'tb-prod',
    }

    def run_w4(self, *args, target='tb-prod', rows=None):
        mig = self.t / 'mig'
        if not mig.exists():
            shutil.copytree(
                self.MIG, mig, ignore=shutil.ignore_patterns('__pycache__', 'test_*.py')
            )
            tp = mig / 'targets' / 'tb-prod.sh'
            tp.write_text(re.sub(r'REPLACE_ME_[A-Z_]+', 'filled-for-test', tp.read_text()))
            (mig / 'check-prereqs.sh').write_text(
                'echo "prereqs $*" >> "$FAKE_LOG"; echo "HAVE 1  NEED 0"\n'
            )
            text = (mig / 'window' / 'w4-migrate.sh').read_text()
            (mig / 'window' / 'w4-migrate.sh').write_text(
                text.replace(
                    'NB=(python3 "$MIG_DIR/neon_branch.py"', 'NB=(nb "$MIG_DIR/neon_branch.py"'
                )
            )
        if rows is not None:
            self.rows(rows)
        env = self.t / 'cutover.env'
        vals = dict(self.ENV, TARGET_ENV=target)
        env.write_text(''.join("%s='%s'\n" % kv for kv in vals.items()))
        env.chmod(0o600)
        e = self.env(CUTOVER_ENV=str(env), VENV_PY=sys.executable, TARGET_ENV=target)
        cmd = 'bash %s/window/w4-migrate.sh %s; echo "rc=$?"\n' % (mig, ' '.join(args))
        return self.netns(cmd, env=e)

    def test_phases_exist_on_tb_prod_only(self):
        out = self.run_w4('--phase', 'read', '--dry-run', target='tb-stage')
        self.assertIn('W4 on tb-stage runs without --phase', out)
        out = self.run_w4('--phase', 'drop', '--dry-run')
        self.assertIn("--phase is read, reset or boot, not 'drop'", out)
        out = self.run_w4('--apply')
        self.assertIn(
            'on tb-prod --apply needs --phase read, --phase reset or --phase boot\nrc=64', out
        )

    def test_the_read_phase_needs_no_target_database_names(self):
        for k in ('STALWART_TARGET_PG_HOST', 'STALWART_TARGET_PG_USER', 'STALWART_TARGET_PG_DB'):
            self.ENV = {x: v for x, v in self.ENV.items() if x != k}
        out = self.run_w4('--phase', 'read', '--dry-run')
        self.assertNotIn('unbound variable', out)
        self.assertIn('== dryrun-dump', out)
        self.assertIn('== dryrun-queue-drain', out)
        self.assertNotIn('== dryrun-neon-reset', out)

    def test_the_tb_prod_dry_run_uses_both_gates_and_no_dump_or_restore(self):
        self.kube({'replicas': 0, 'pods': [], 'deploy': deploy(0, 0)})
        out = self.run_w4('--dry-run')
        self.assertTrue(out.rstrip().endswith(('rc=0', 'rc=2')), out[-2000:])
        prereqs = [
            x for x in (self.t / 'fake.log').read_text().splitlines() if x.startswith('prereqs')
        ]
        self.assertEqual(prereqs, ['prereqs --for go-W4-read', 'prereqs --for go-W4-reset'])
        for word in (
            'rclone',
            'pg_dump',
            'pg_restore',
            'DROP SCHEMA',
            'restore-target-db',
            'task-queue',
            'db-dump-legacy',
            'legacy-pw-99',
            '--password',
        ):
            self.assertNotIn(word, out)
        for line in (
            '== dryrun-dump',
            '== dryrun-queue-drain',
            '== dryrun-neon-reset',
            '== dryrun-guard-check',
            '== dryrun-predelete',
            'merge the recovery-mode PR (thundermail-deploy PR 197) now',
            '== dryrun-recovery-hold',
        ):
            self.assertIn(line, out)
        self.assertLess(out.index('== dryrun-queue-drain'), out.index('== dryrun-neon-reset'))
        restore = (self.t / 'ev' / 'w4-migrate-restore.dry-run.txt').read_text()
        self.assertIn('--phase boot --apply', restore)
        self.assertNotIn('restore-target-db', restore)

    def test_a_second_reset_needs_go_and_boot_needs_a_reset(self):
        self.kube({'replicas': 0, 'pods': []})
        (self.t / 'secret').mkdir(exist_ok=True, mode=0o700)
        (self.t / 'secret' / 'export.json').write_text('{}\n')
        reset = [
            'reset',
            '2026-11-19T05:00:00Z',
            '2026-11-19T05:00:00Z',
            'last_reset_at',
            'x',
        ]
        out = self.run_w4('--phase', 'reset', '--apply', rows=[CUT, EXPORT, READ, STOPPED, reset])
        self.assertIn('was already reset in this cut: a second reset needs', out)
        self.assertNotIn('nb ', '\n'.join(self.calls('nb')).replace('nb hosts', ''))
        out = self.run_w4('--phase', 'boot', '--apply', rows=[CUT, EXPORT, READ, STOPPED])
        self.assertIn('STOP W4 (apply): --phase boot has nothing to resume', out)
        prereqs = [
            x for x in (self.t / 'fake.log').read_text().splitlines() if x.startswith('prereqs')
        ]
        self.assertEqual(set(prereqs), {'prereqs --for go-W4-reset'})


class Gates(Sandbox):
    def prereqs(self, gid, env='tb-prod'):
        self.rules([{'tool': 'curl', 'match': '.', 'out': '000'}])
        rc, out = self.run_sh([str(HERE / 'check-prereqs.sh'), '--for', gid], TARGET_ENV=env)
        return rc, re.findall(r'(?m)^(?:HAVE|NEED|SKIP)  (\S+)', out)

    def test_the_reset_gate_needs_no_legacy_admin_api(self):
        rc, rows = self.prereqs('go-W4-reset')
        self.assertNotEqual(rc, 64)
        for name in (
            'stalwart-admin-legacy',
            'legacy-stalwart-pg-secret',
            'legacy-db-passwords',
            'tool-session-manager-plugin',
            'tool-rclone',
            'sso-mzla-legacy',
        ):
            self.assertNotIn(name, rows)
        for name in (
            'neon-api-key+neonctl',
            'stalwart-target-db-owner',
            'stalwart-recovery-admin',
            'tool-stalwart-cli',
            'kube-tb-prod',
            'checkout',
        ):
            self.assertIn(name, rows)
        rc, rows = self.prereqs('go-W4-read')
        for name in ('stalwart-admin-legacy', 'tool-session-manager-plugin', 'sso-mzla-legacy'):
            self.assertIn(name, rows)
        for name in (
            'neon-api-key+neonctl',
            'stalwart-recovery-admin',
            'legacy-stalwart-pg-secret',
        ):
            self.assertNotIn(name, rows)

    def test_tb_prod_start_reads_no_production_stalwart_password(self):
        self.assertNotIn('legacy-stalwart-pg-secret', self.prereqs('start')[1])
        self.assertIn('legacy-stalwart-pg-secret', self.prereqs('start', env='tb-stage')[1])
        self.assertNotIn('cf-token-shared-external-dns', self.prereqs('go-open')[1])
        self.assertIn('cf-token-shared-external-dns', self.prereqs('go-open', env='tb-stage')[1])


HOLD = """import fcntl, sys, time
f = open(sys.argv[1], 'a')
fcntl.flock(f, fcntl.LOCK_EX)
open(sys.argv[2], 'w').write('held')
time.sleep(float(sys.argv[4]))
open(sys.argv[3], 'a').write(sys.argv[5])
"""


class EnvLock(Sandbox):
    def setUp(self):
        super().setUp()
        if shutil.which('flock') and not (self.bin / 'flock').exists():
            (self.bin / 'flock').symlink_to(shutil.which('flock'))
        (self.t / 'hold.py').write_text(HOLD)

    def hold(self, env, seconds, text):
        held = self.t / 'held'
        p = subprocess.Popen(
            [
                sys.executable,
                str(self.t / 'hold.py'),
                str(env) + '.lock',
                str(held),
                str(env),
                str(seconds),
                text,
            ]
        )
        deadline = time.time() + 10
        while not held.exists() and time.time() < deadline:
            time.sleep(0.02)
        return p

    def test_setenv_waits_for_another_writer(self):
        env = self.t / 'shm' / 'cutover.env'
        env.write_text("A='1'\n")
        env.chmod(0o600)
        p = self.hold(env, 1.0, "B='from-the-other-lane'\n")
        script = 'set -u\nAPPLY=1; ENV_FILE=%s\n. %s/target.sh\n%s\nsetenv C 3\n' % (
            env,
            HERE,
            re.search(
                r'(?ms)^env_locked\(\) \{.*?^setenv_secret',
                (HERE / 'window' / 'lib.sh').read_text(),
            )
            .group(0)
            .rsplit('\n', 1)[0],
        )
        rc, out = self.run_sh(['-c', script], TARGET_ENV='tb-prod')
        p.wait(10)
        self.assertEqual(rc, 0, out)
        self.assertEqual(env.read_text(), "A='1'\nB='from-the-other-lane'\nC='3'\n")
        (self.t / 'shm' / 'cutover.env.lock').unlink()
        (self.t / 'shm' / 'cutover.env.lock').mkdir()
        rc, out = self.run_sh(
            ['-c', script.replace('setenv C 3', 'setenv D 4; echo "rc=$?"')], TARGET_ENV='tb-prod'
        )
        self.assertIn('cannot open the env file lock', out)
        self.assertIn('rc=1', out)
        self.assertNotIn('D=', env.read_text())

    def test_check_prereqs_does_not_read_without_its_lock(self):
        env = self.t / 'shm' / 'cutover.env'
        env.write_text("TARGET_ENV='tb-prod'\nTMD_CHECKOUT='/elsewhere'\nWINDOW_SHA='abc'\n")
        env.chmod(0o600)
        (self.t / 'shm' / 'cutover.env.lock').mkdir()
        self.rules([{'tool': 'curl', 'match': '.', 'out': '000'}])
        rc, out = self.run_sh(
            [str(HERE / 'check-prereqs.sh'), '--for', 'go-W4-reset'], TARGET_ENV='tb-prod'
        )
        self.assertRegex(out, r'(?m)^NEED  checkout +TMD_CHECKOUT or WINDOW_SHA not in ')

    def test_check_prereqs_reads_the_env_file_after_a_writer(self):
        env = self.t / 'shm' / 'cutover.env'
        env.write_text("TARGET_ENV='tb-prod'\n")
        env.chmod(0o600)
        p = self.hold(env, 1.5, "TMD_CHECKOUT='/elsewhere'\nWINDOW_SHA='abc'\n")
        self.rules([{'tool': 'curl', 'match': '.', 'out': '000'}])
        t0 = time.time()
        rc, out = self.run_sh(
            [str(HERE / 'check-prereqs.sh'), '--for', 'go-W4-reset'], TARGET_ENV='tb-prod'
        )
        took = time.time() - t0
        p.wait(10)
        self.assertGreater(took, 1.2, out)
        self.assertRegex(out, r'(?m)^NEED  checkout +cwd is not TMD_CHECKOUT$')


if __name__ == '__main__':
    unittest.main()
