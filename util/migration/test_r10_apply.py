import importlib
import json
import re
import pathlib
import sys
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
w4 = importlib.import_module('test_pr3_w4')

KUBE = r"""#!{py}
import json, os, sys
a = sys.argv[1:]
j = ' '.join(a)
open(os.environ['FAKE_LOG'], 'a').write('kubectl ' + j + '\n')
st = json.load(open(os.environ['APPLY_STATE']))
t = os.environ['APPLY_DIR']
out, rc = '', 0
if 'get pods -l app=stalwart -o json' in j:
    out = json.dumps({{'items': st['pods']}})
elif ' create -f -' in ' ' + j:
    open(t + '/created.yaml', 'w').write(sys.stdin.read())
elif ' wait --for=condition=Ready ' in j:
    rc = st.get('ready_rc', 0)
elif ' attach -i -c cli ' in j:
    open(t + '/attached.bin', 'wb').write(sys.stdin.buffer.read())
    import time
    time.sleep(st.get('attach_sleep', 0))
    rc = st.get('attach_rc', 0)
elif ' patch pod ' in j:
    open(t + '/patched', 'a').write(j + '\n')
    st['phase'] = 'Failed'
elif ' logs --timestamps -c cli ' in j and st.get('ts_logs_rc'):
    rc = st['ts_logs_rc']
elif ' describe pod ' in j:
    out = 'Events:\n  Warning  Failed  ImagePullBackOff\n'
elif '{{.status.phase}}' in j:
    out = st.get('phase', 'Succeeded')
elif ' logs --timestamps -c cli ' in j or ' logs -c cli ' in j:
    out = st['log']
elif 'terminated.exitCode' in j:
    out = str(st.get('code', 0))
elif ' delete pod ' in j:
    open(t + '/deleted', 'a').write(j + '\n')
else:
    rc = 1
json.dump(st, open(os.environ['APPLY_STATE'], 'w'))
if out:
    sys.stdout.write(out)
sys.exit(rc)
"""


def ready(ip, name='stalwart-5d9f-a'):
    return {
        'metadata': {'name': name},
        'status': {'podIP': ip, 'conditions': [{'type': 'Ready', 'status': 'True'}]},
    }


SCHEMA_HASH = 'Pzykh-dZ4p6qVJ7wVulQvyF7-1aRB8fI6GcjdJMWK2Q'
SCHEMA = (
    '2026-11-19T05:09:55.900000000Z [debug] -> GET http://10.0.0.5:8080/api/schema\n'
    '2026-11-19T05:09:55.950000000Z [debug] <- GET http://10.0.0.5:8080/api/schema status=302 Found '
    'content-type=(none) content-length=0 content-encoding=identity\n'
    '2026-11-19T05:09:55.960000000Z [debug] -> GET http://10.0.0.5:8080/api/schema/%s\n'
    '2026-11-19T05:09:56.000000000Z [debug] <- GET http://10.0.0.5:8080/api/schema/%s status=200 OK '
    'content-type=application/json content-length=150150 content-encoding=gzip\n'
) % (SCHEMA_HASH, SCHEMA_HASH)
LOG_OK = SCHEMA + (
    '2026-11-19T05:10:00.000000001Z Plan: 0 destroy, 4 update, 5 create, 0 upsert, 0 reconcile (11980 objects)\n'
    '2026-11-19T05:10:00.100000000Z [debug] -> POST http://10.0.0.5:8080/jmap\n'
    '2026-11-19T05:10:33.600000000Z [debug] <- POST http://10.0.0.5:8080/jmap status=200 OK content-type=application/json content-length=9 content-encoding=identity\n'
    '2026-11-19T05:10:33.700000000Z [debug] -> POST http://10.0.0.5:8080/jmap\n'
    '2026-11-19T05:10:40.700000000Z [debug] <- POST http://10.0.0.5:8080/jmap status=200 OK content-type=application/json content-length=9 content-encoding=identity\n'
    '2026-11-19T05:19:15.000000000Z Done: 0 destroyed, 4 updated, 11972 created (0 failed)\n'
)
LOG_FAIL = SCHEMA + (
    '2026-11-19T05:10:00.000000001Z Plan: 0 destroy, 4 update, 5 create, 0 upsert, 0 reconcile (11980 objects)\n'
    "2026-11-19T05:11:00.000000000Z ✗ create Account: u1@a.example.test: primaryKeyViolation\n"
    '2026-11-19T05:11:00.100000000Z Done: 0 destroyed, 0 updated, 500 created (1 failed)\n'
    '2026-11-19T05:11:00.200000000Z error: 1 operation failed\n'
)


class InClusterApply(w4.Prod):
    def setUp(self):
        super().setUp()
        (self.t / 'bin' / 'kubectl').write_text(KUBE.format(py=sys.executable))
        (self.t / 'secret').mkdir(exist_ok=True, mode=0o700)
        (self.t / 'secret' / 'export.json').write_text(
            '{"@type":"update","object":"X"}\n' * 4 + '{"@type":"create","object":"Domain"}\n' * 5
        )
        self.state({'pods': [ready('10.0.0.5')], 'log': LOG_OK})

    def state(self, st):
        (self.t / 'apply.json').write_text(json.dumps(st))

    def env(self, **extra):
        e = super().env(**extra)
        e['APPLY_STATE'] = str(self.t / 'apply.json')
        e['APPLY_DIR'] = str(self.t)
        return e

    def apply(self, rows=None, wait=30):
        self.wait = wait

        self.rows(rows or [w4.CUT, ['pre-delete', 'x', 'x', 'deleted', '9']])
        return self.w4(
            'export STALWART_PASSWORD=recovery-pw-1; APPLY_POD_WAIT=%d\n'
            'port_forward() { echo PF-OK; }\n'
            'prod_apply; echo "rc=$? VALUE=${VALUE:-}"' % self.wait
        )

    def kube_log(self):
        return [c for c in self.calls('kubectl')]

    def test_the_export_goes_over_stdin_to_a_pod_next_to_stalwart(self):
        out = self.apply()
        self.assertIn('rc=0 VALUE=0', out)
        created = (self.t / 'created.yaml').read_text()
        self.assertIn('value: http://10.0.0.5:8080', created)
        self.assertIn(
            'image: ghcr.io/stalwartlabs/cli:1.0.13@sha256:'
            '9a1e07307d6f890a193322c4b737fc0868405f0f8486a19922cbf362f43884fd',
            created,
        )
        self.assertIn('name: stalwart-recovery-admin', created)
        self.assertIn('app.kubernetes.io/name: stalwart-config-applier', created)
        self.assertNotIn('app: stalwart', created)
        self.assertNotIn('recovery-pw-1', created)
        self.assertEqual(
            (self.t / 'attached.bin').read_bytes(), (self.t / 'secret' / 'export.json').read_bytes()
        )
        log = '\n'.join(self.kube_log())
        self.assertNotIn('recovery-pw-1', log)
        self.assertNotIn('export.json', log)
        self.assertNotIn('"object"', log)
        self.assertIn('delete pod stalwart-apply-20261112t000000z', (self.t / 'deleted').read_text())
        self.assertIn(
            'apply: pod/stalwart-apply-20261112t000000z exit=0 attach=0 errors=0 requests=4 '
            'longest request 33.5s (the CLI gives up after 60s)',
            out,
        )
        self.assertIn('Done: 0 destroyed, 4 updated, 11972 created (0 failed)', out)
        names = [r[0] for r in self.timing()]
        self.assertEqual(names[-2:], ['apply-start', 'export-applied'])
        self.assertTrue((self.t / 'ev' / 'apply-20261112T000000Z.log').exists())

    def test_a_failed_batch_deletes_the_pod_and_a_rerun_refuses(self):
        self.state({'pods': [ready('10.0.0.5')], 'log': LOG_FAIL, 'code': 1})
        out = self.apply()
        self.assertIn('rc=1 VALUE=1', out)
        self.assertIn('delete pod stalwart-apply-', (self.t / 'deleted').read_text())
        self.assertNotIn('export-applied', [r[0] for r in self.timing()])
        ev = (self.t / 'ev' / 'apply-20261112T000000Z.log').read_text()
        self.assertNotIn('u1@a.example.test', ev)
        self.assertIn('<addr>', ev)
        self.assertIn('u1@a.example.test', (self.t / 'secret' / 'apply.log').read_text())
        rows = [list(r) for r in self.timing()]
        out = self.apply(rows)
        self.assertIn('an apply started after this pre-delete and did not finish', out)
        self.assertIn("--phase reset --apply --go '<words>'", out)
        self.assertIn('rc=1', out)

    def test_a_cut_stream_fails_and_a_pod_that_never_got_ready_allows_a_rerun(self):
        short = LOG_OK.replace('4 update, 5 create', '4 update, 3 create')
        self.state({'pods': [ready('10.0.0.5')], 'log': short})
        out = self.apply()
        self.assertIn(
            'the CLI planned destroy=0 update=4 create=3 upsert=0 reconcile=0, export.json holds '
            'destroy=0 update=4 create=5 upsert=0 reconcile=0: the export did not arrive whole',
            out,
        )
        self.assertIn('rc=1', out)
        self.state({'pods': [ready('10.0.0.5')], 'log': LOG_OK, 'ready_rc': 1})
        (self.t / 'ev' / 'timing.tsv').unlink()
        out = self.apply()
        self.assertIn('not Ready within 300s', out)
        self.assertNotIn('apply-start', [r[0] for r in self.timing()])
        self.state({'pods': [ready('10.0.0.5')], 'log': LOG_OK})
        out = self.apply([list(r) for r in self.timing()])
        self.assertIn('rc=0 VALUE=0', out)

    def test_the_reads_after_the_apply_get_a_port_forward_also_on_a_rerun(self):
        out = self.apply()
        self.assertLess(out.index('apply: pod/'), out.index('PF-OK'))
        out = self.apply([list(r) for r in self.timing()])
        self.assertIn('the export is already applied after this pre-delete', out)
        self.assertIn('PF-OK', out)
        self.assertIn('rc=0', out)

    def test_the_wait_bounds_the_attach_and_a_failure_keeps_the_pod_status(self):
        import time

        self.state({'pods': [ready('10.0.0.5')], 'log': LOG_OK, 'attach_sleep': 20, 'phase': 'Running'})
        t0 = time.monotonic()
        out = self.apply(wait=2)
        self.assertLess(time.monotonic() - t0, 15, out)
        self.assertIn('still Running 2s after the export was sent', out)
        self.assertIn('rc=1', out)
        diag = (self.t / 'ev' / 'apply-pod-20261112T000000Z.txt').read_text()
        self.assertIn('ImagePullBackOff', diag)
        self.assertTrue((self.t / 'secret' / 'apply.log').exists())
        self.assertIn('delete pod stalwart-apply-', (self.t / 'deleted').read_text())

    def test_no_schema_answer_in_the_log_means_no_row_and_no_send(self):
        self.state({'pods': [ready('10.0.0.5')], 'log': LOG_OK.replace(SCHEMA, '')})
        self.rows([w4.CUT, ['pre-delete', 'x', 'x', 'deleted', '9']])
        out = self.w4(
            'APPLY_POD_WAIT=30 APPLY_SCHEMA_WAIT=1; port_forward() { echo PF-OK; }\n'
            'prod_apply; echo "rc=$?"'
        )
        self.assertIn(
            'log shows no status=200 for GET /api/schema or its /api/schema/<hash> redirect within 1s: '
            'the CLI did not reach Stalwart at '
            'http://10.0.0.5:8080 (security group of the pod, credential or URL); nothing sent',
            out,
        )
        self.assertIn('rc=1', out)
        self.assertFalse((self.t / 'attached.bin').exists())
        self.assertNotIn('apply-start', [r[0] for r in self.timing()])
        self.assertIn('delete pod stalwart-apply-', (self.t / 'deleted').read_text())
        self.state({'pods': [ready('10.0.0.5')], 'log': LOG_OK})
        out = self.apply([list(r) for r in self.timing()])
        self.assertIn('reached Stalwart (GET /api/schema/<hash> status=200); sending ', out)
        self.assertIn('rc=0 VALUE=0', out)

    def test_the_schema_check_reads_the_redirect_and_the_hashed_answer(self):
        cases = (
            ('redirect then hashed 200', SCHEMA, True),
            ('a 200 on /api/schema itself', SCHEMA.replace('/api/schema/' + SCHEMA_HASH, '/api/schema'), True),
            ('the redirect alone', SCHEMA.split('\n')[1] + '\n', False),
            ('a 401 on the hashed path', SCHEMA.replace('status=200 OK', 'status=401 Unauthorized'), False),
        )
        for case, log, ok in cases:
            with self.subTest(case=case):
                self.state({'pods': [ready('10.0.0.5')], 'log': log})
                out = self.w4('schema_seen stalwart-apply-x; echo "seen=$?"')
                self.assertIn('seen=%d' % (0 if ok else 1), out)

    def test_a_failed_attach_before_the_plan_writes_a_row_the_rerun_accepts(self):
        self.state(
            {'pods': [ready('10.0.0.5')], 'log': SCHEMA, 'attach_rc': 1, 'phase': 'Running'}
        )
        out = self.apply()
        self.assertIn('the pod was stopped and its final log has no Plan: line: nothing was applied', out)
        self.assertIn('activeDeadlineSeconds', (self.t / 'patched').read_text())
        self.assertIn('apply-unsent row written', out)
        self.assertIn('rc=1', out)
        self.assertNotIn('still Running', out)
        self.assertIn('delete pod stalwart-apply-', (self.t / 'deleted').read_text())
        names = [r[0] for r in self.timing()]
        self.assertEqual(names[-2:], ['apply-start', 'apply-unsent'])
        self.state({'pods': [ready('10.0.0.5')], 'log': LOG_OK})
        out = self.apply([list(r) for r in self.timing()])
        self.assertNotIn('an apply started after this pre-delete and did not finish', out)
        self.assertIn('rc=0 VALUE=0', out)
        self.state(
            {'pods': [ready('10.0.0.5')], 'log': LOG_FAIL, 'attach_rc': 1, 'code': 1}
        )
        (self.t / 'ev' / 'timing.tsv').unlink()
        out = self.apply()
        self.assertNotIn('apply-unsent', [r[0] for r in self.timing()])
        out = self.apply([list(r) for r in self.timing()])
        self.assertIn('an apply started after this pre-delete and did not finish', out)

    def test_an_unreadable_log_after_a_failed_attach_keeps_the_rerun_blocked(self):
        self.state(
            {'pods': [ready('10.0.0.5')], 'log': SCHEMA, 'attach_rc': 1, 'phase': 'Running', 'ts_logs_rc': 1}
        )
        out = self.apply(wait=2)
        self.assertIn('could not be read after the failed attach: the apply-start row stays', out)
        self.assertIn('rc=1', out)
        self.assertFalse((self.t / 'patched').exists())
        self.assertNotIn('apply-unsent', [r[0] for r in self.timing()])
        self.state({'pods': [ready('10.0.0.5')], 'log': LOG_OK})
        out = self.apply([list(r) for r in self.timing()])
        self.assertIn('an apply started after this pre-delete and did not finish', out)

    def test_no_apply_start_row_means_no_send(self):
        self.rows([w4.CUT, ['pre-delete', 'x', 'x', 'deleted', '9']])
        out = self.w4(
            'APPLY_POD_WAIT=30; port_forward() { echo PF-OK; }\n'
            'mark() { return 1; }\nprod_apply; echo "rc=$?"'
        )
        self.assertIn('could not write the apply-start row', out)
        self.assertIn('rc=1', out)
        self.assertFalse((self.t / 'attached.bin').exists())
        self.assertIn('delete pod stalwart-apply-', (self.t / 'deleted').read_text())

    def test_not_exactly_one_ready_pod_creates_nothing(self):
        self.state({'pods': [ready('10.0.0.5'), ready('10.0.0.6', 'b')], 'log': LOG_OK})
        out = self.apply()
        self.assertIn('not exactly one Ready stalwart pod with an IPv4 pod IP: no apply pod', out)
        self.assertIn('rc=1', out)
        self.assertFalse((self.t / 'created.yaml').exists())
        self.assertNotIn('apply-start', [r[0] for r in self.timing()])

    def test_the_dry_run_prints_the_plan_and_runs_nothing(self):
        self.rows([w4.CUT])
        out = self.w4('APPLY=0 APPLY_POD_WAIT=30; prod_apply; echo "rc=$?"')
        self.assertIn('rc=0', out)
        self.assertIn('attach -i -c cli pod/stalwart-apply-20261112t000000z <', out)
        self.assertFalse((self.t / 'created.yaml').exists())

    def test_the_pod_manifest_holds_no_secret_and_stays_off_services(self):
        text = (HERE / 'apply-pod.yaml').read_text()
        self.assertNotIn('password:', text.lower())
        self.assertIn('automountServiceAccountToken: false', text)
        self.assertIn('readOnlyRootFilesystem: true', text)
        self.assertIn('stdinOnce: true', text)
        mounts = re.findall(r'(?m)^ +mountPath: (\S+)$', text)
        self.assertEqual(mounts, ['/tmp'])
        env = dict(re.findall(r'(?m)^ +- name: (\S+)\n +value: (\S+)$', text))
        for name in ('HOME', 'XDG_CACHE_HOME'):
            with self.subTest(name=name):
                self.assertIn(name, env)
                self.assertTrue(env[name] == '/tmp' or env[name].startswith('/tmp/'), env[name])
        for svc in (HERE.parent.parent / 'bases' / 'stalwart-mail').glob('service*.yaml'):
            self.assertNotIn('stalwart-config-applier', svc.read_text())


if __name__ == '__main__':
    unittest.main()
