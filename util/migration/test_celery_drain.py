import json
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
REAL = (
    'awk basename bash cat chmod cut date dirname env find grep head id jq ls mkdir mktemp '
    'printf realpath rm sed sort tail tee timeout tr true false uniq wc setsid'
).split()
AWS = r"""#!PY
import json, os, sys, time
args = sys.argv[1:]
open(os.environ['FAKE_LOG'], 'a').write('aws ' + ' '.join(args) + '\n')
p = os.environ['AWS_STATE']
st = json.load(open(p))
if 'get-log-events' in args:
    time.sleep(st.get('slow', 0))
now = int(time.time() * 1000)
def opt(name):
    return args[args.index(name) + 1] if name in args else None
sub = args[args.index('--region') + 2:]
cmd = ' '.join(sub[:2])
out, rc = '', 0
if cmd == 'ecs describe-services':
    d, r = st['services'][opt('--cluster') + '/' + opt('--services')]
    out = str(d) if opt('--query') == 'services[0].desiredCount' else '%d\t%d' % (d, r)
elif cmd == 'ecs list-tasks':
    st['reads'] = st.get('reads', 0) + 1
    out = '\t'.join('arn:aws:ecs:x:task/c/' + t for t in st['tasks'])
elif cmd == 'logs describe-log-groups':
    out = '\t'.join(g for g in st['groups'] if g.startswith(opt('--log-group-name-prefix')))
elif cmd == 'logs describe-log-streams':
    out = '\t'.join(st['streams']) + '\nNone'
elif cmd == 'logs get-log-events':
    cycles = st['cycles']
    ev = cycles[min(st.get('reads', 1), len(cycles)) - 1].get(opt('--log-stream-name'), [])
    since = int(opt('--start-time') or 0)
    ev = [{'timestamp': now - a, 'message': m} for a, m in ev if now - a >= since]
    tok = opt('--next-token')
    if opt('--log-stream-name') in st.get('empty_first', []) and tok in (None, 'e0'):
        if tok is None:
            print(json.dumps({'events': [], 'nextForwardToken': 'e0'}))
            sys.exit(0)
        tok = None
    if opt('--log-stream-name') in st.get('garbled', []):
        print('<html>throttled</html>')
        sys.exit(0)
    start = int(tok[1:]) if tok else 0
    page = ev[start:start + 2]
    nxt = 'f%d' % (start + len(page)) if page else (tok or 'f0')
    out = json.dumps({'events': page, 'nextForwardToken': nxt})
else:
    rc = 1
json.dump(st, open(p, 'w'))
if out:
    print(out)
sys.exit(rc)
"""
CL = 'accounts-prod'
SV = 'accounts-prod-afc-accounts-celery-prod'
TARGETS = (
    'accounts-prod-fargate-accounts/accounts-prod-fargate-accounts '
    'accounts-prod/accounts-prod-afc-accounts-celery-prod '
    'accounts-prod/accounts-prod-afc-accounts-flower-prod '
    'accounts-prod-fargate-keycloak/accounts-prod-fargate-keycloak'
)
GROUP = 'accounts-prod-afc-accounts-loggroup-celery-prod'
STREAM = 'accounts/prod/celery-prod//celery/'
TASK = 'thunderbird_accounts.mail.tasks.create_stalwart_account'
ID1 = '11111111-1111-4111-8111-111111111111'
ID2 = '22222222-2222-4222-8222-222222222222'


def line(t, name, tid, word):
    return '[2026-11-19 07:00:00,000: INFO/MainProcess] Task %s[%s] %s' % (name, tid, word)


def done(tid, ago=60):
    return [
        [ago * 1000, line(0, TASK, tid, 'received')],
        [(ago - 5) * 1000, line(0, TASK, tid, 'succeeded in 0.10s: secret-retval-a@b.test')],
    ]


class Base(unittest.TestCase):
    def setUp(self):
        self.t = pathlib.Path(tempfile.mkdtemp(prefix='celery-drain-test-'))
        self.addCleanup(shutil.rmtree, self.t, True)
        self.t.chmod(0o700)
        self.mig = self.t / 'migration'
        shutil.copytree(HERE, self.mig, ignore=shutil.ignore_patterns('__pycache__', 'test_*.py'))
        tp = self.mig / 'targets' / 'tb-prod.sh'
        tp.write_text(re.sub(r'REPLACE_ME_[A-Z_]+', 'filled-for-test', tp.read_text()))
        self.bin = self.t / 'bin'
        self.bin.mkdir()
        for name in REAL:
            (self.bin / name).symlink_to(shutil.which(name))
        (self.bin / 'sleep').write_text(
            '#!%s\nexec %s 0.05\n' % (shutil.which('bash'), shutil.which('sleep'))
        )
        (self.bin / 'aws').write_text(AWS.replace('PY', sys.executable, 1))
        for n in ('sleep', 'aws'):
            (self.bin / n).chmod(0o755)
        for d in ('ev', 'shm', 'home'):
            (self.t / d).mkdir(mode=0o700)
        (self.t / 'kc-hub').write_text('')
        self.env_file = self.t / 'shm' / 'cutover.env'
        self.env_file.write_text(
            "TARGET_ENV='tb-prod'\nLEGACY_ECS_TARGETS='%s'\nKUBECONFIG_HUB='%s'\n"
            % (TARGETS, self.t / 'kc-hub')
        )
        self.env_file.chmod(0o600)
        self.state()

    def state(self, **kw):
        st = {
            'services': {t: [2, 2] for t in TARGETS.split()},
            'tasks': ['aaa', 'bbb'],
            'groups': [
                GROUP,
                'accounts-prod-afc-accounts-loggroup-flower-prod',
                'accounts-prod-fargate-keycloak-fargate-logs',
            ],
            'streams': [STREAM + 'aaa', STREAM + 'bbb', STREAM + 'old'],
            'cycles': [{STREAM + 'aaa': done(ID1), STREAM + 'bbb': done(ID2)}],
        }
        st.update(kw)
        (self.t / 'aws.json').write_text(json.dumps(st))

    def snapshot(self, row=True, desired=None):
        snap = self.t / 'ev' / 'ecs-before-x.json'
        svcs = []
        for t in TARGETS.split():
            c, s = t.split('/')
            d = 2
            if desired and s in desired:
                d = desired[s]
            svcs.append(
                {
                    'cluster': c,
                    'serviceName': s,
                    'desiredCount': d,
                    'runningCount': d,
                    'status': 'ACTIVE',
                }
            )
        snap.write_text(json.dumps({'services': svcs, 'scalable': []}))
        (self.t / 'ev' / 'cut-legacy-restore.txt').write_text(
            'bash %s/restore-legacy-sg.sh sg.json --dry-run\nbash %s/restore-ecs.sh %s --dry-run\n'
            % (self.mig, self.mig, snap)
        )
        if row:
            with open(self.timing(), 'a') as f:
                f.write('restore-W2\t2026-11-19T00:00:00Z\t2026-11-19T00:00:00Z\tfile\tx\n')

    def stage_env(self):
        f = self.t / 'shm' / 'stage.env'
        f.write_text(
            self.env_file.read_text().replace("TARGET_ENV='tb-prod'", "TARGET_ENV='tb-stage'")
        )
        f.chmod(0o600)
        return str(f)

    def timing(self):
        return self.t / 'ev' / 'timing-tb-prod.tsv'

    def rows(self):
        p = self.timing()
        return [ln.split('\t')[0] for ln in p.read_text().splitlines()] if p.exists() else []

    def log(self):
        p = self.t / 'fake.log'
        return p.read_text() if p.exists() else ''

    def run_tool(self, *args, script='window/celery-drain.sh', **env):
        e = {
            'PATH': str(self.bin),
            'HOME': str(self.t / 'home'),
            'LANG': 'C.UTF-8',
            'TARGET_ENV': 'tb-prod',
            'FAKE_LOG': str(self.t / 'fake.log'),
            'AWS_STATE': str(self.t / 'aws.json'),
            'EVIDENCE_DIR': str(self.t / 'ev'),
            'SECRET_DIR': str(self.t / 'shm'),
            'CUTOVER_ENV': str(self.env_file),
            'TS': '20261119T000000Z',
            'CELERY_DRAIN_INTERVAL': '0',
            'CELERY_DRAIN_TIMEOUT': '5',
        }
        e.update(env)
        p = subprocess.run(
            [shutil.which('bash'), str(self.mig / script), *args],
            capture_output=True,
            text=True,
            env=e,
            cwd=self.t,
            timeout=120,
        )
        return p.returncode, p.stdout + p.stderr


class CeleryDrain(Base):
    def test_dry_run_prints_the_reads_and_calls_nothing(self):
        rc, out = self.run_tool('read')
        self.assertEqual(rc, 0, out)
        self.assertIn('logs get-log-events --log-group-name', out)
        self.assertIn('ecs describe-services --cluster accounts-prod --services ' + SV, out)
        self.assertIn('CELERY dry-run: done', out)
        self.assertEqual(self.log(), '')
        rc, out = self.run_tool('read', '--apply')
        self.assertEqual(rc, 0, out)
        self.assertIn('aws --profile mzla-legacy-ro', self.log())

    def test_only_read_only_calls_with_the_ro_profile(self):
        self.snapshot()
        self.run_tool('read', '--apply')
        self.run_tool('wait', '--apply')
        calls = [ln for ln in self.log().splitlines() if ln]
        self.assertTrue(calls)
        self.assertEqual(writes(calls), [])
        self.assertEqual(
            writes(calls + ['aws --profile mzla-legacy ecs update-service --desired-count 0']),
            ['aws --profile mzla-legacy ecs update-service --desired-count 0'],
        )

    def test_refuses_other_targets_and_placeholders(self):
        rc, out = self.run_tool('read', TARGET_ENV='tb-stage', CUTOVER_ENV=self.stage_env())
        self.assertEqual(rc, 64, out)
        self.assertIn('TARGET_ENV=tb-stage has none', out)
        self.assertEqual(list((self.t / 'ev').iterdir()), [])
        rc, _ = self.run_tool('read', '--bogus')
        self.assertNotEqual(rc, 0)
        rc, out = self.run_tool('drain')
        self.assertEqual(rc, 64, out)
        tp = self.mig / 'targets' / 'tb-prod.sh'
        tp.write_text(
            tp.read_text().replace(
                'LEGACY_PG_SECRET_NAME=filled-for-test', 'LEGACY_PG_SECRET_NAME=REPLACE_ME_X'
            )
        )
        rc, out = self.run_tool('read', '--apply')
        self.assertEqual(rc, 64, out)
        rc, out = self.run_tool('read')
        self.assertEqual(rc, 2, out)

    def test_read_counts_held_tasks_and_prints_no_log_content(self):
        held = [[30000, line(0, TASK, ID2, 'received')]]
        self.state(cycles=[{STREAM + 'aaa': done(ID1), STREAM + 'bbb': held}])
        rc, out = self.run_tool('read', '--apply')
        self.assertEqual(rc, 0, out)
        self.assertRegex(
            out,
            r'celery held=1 received=2 finished=1 newest_log_age=\d+s desired=2 running=2 tasks=2',
        )
        self.assertIn('held %s received' % TASK, out)
        self.assertNotIn('secret-retval', out)
        self.assertNotIn(
            'secret-retval', ''.join(p.read_text() for p in (self.t / 'ev').glob('*.log'))
        )
        self.state()
        rc, out = self.run_tool('read', '--apply')
        self.assertIn('celery held=0 received=2 finished=2', out)
        self.assertEqual(self.rows(), [])

    def test_an_empty_page_before_the_end_does_not_end_the_read(self):
        held = [[30000, line(0, TASK, ID2, 'received')]]
        self.state(
            cycles=[{STREAM + 'aaa': done(ID1), STREAM + 'bbb': held}], empty_first=[STREAM + 'bbb']
        )
        rc, out = self.run_tool('read', '--apply')
        self.assertEqual(rc, 0, out)
        self.assertIn('celery held=1 ', out)

    def test_an_unreadable_log_page_stops_the_read(self):
        self.state(garbled=[STREAM + 'bbb'])
        rc, out = self.run_tool('read', '--apply')
        self.assertEqual(rc, 1, out)
        self.assertIn('logs get-log-events failed for %sbbb' % STREAM, out)
        self.assertNotIn('celery held=', out)

    def test_log_text_never_leaves_jq(self):
        (self.bin / 'sort').unlink()
        (self.bin / 'sort').write_text(
            '#!%s\n%s -a "$SORT_IN" | %s "$@"\n'
            % (shutil.which('bash'), shutil.which('tee'), shutil.which('sort'))
        )
        (self.bin / 'sort').chmod(0o755)
        big = 'secret-' + 'x' * 300000
        ev = [
            [60000, line(0, TASK, ID1, 'received')],
            [50000, line(0, TASK, ID1, 'succeeded in 1s: ' + big)],
        ]
        self.state(cycles=[{STREAM + 'aaa': ev, STREAM + 'bbb': done(ID2)}])
        rc, out = self.run_tool('read', '--apply', SORT_IN=str(self.t / 'sort.in'))
        self.assertEqual(rc, 0, out)
        self.assertIn('celery held=0 received=2 finished=2', out)
        seen = (self.t / 'sort.in').read_text()
        self.assertIn(' %s %s received' % (TASK, ID1), seen)
        self.assertNotIn('secret', seen + out)

    def test_a_task_received_hours_ago_and_unfinished_stays_held(self):
        old = [[3 * 3600 * 1000, line(0, TASK, ID2, 'received')]]
        self.state(cycles=[{STREAM + 'aaa': done(ID1), STREAM + 'bbb': old}])
        rc, out = self.run_tool('read', '--apply')
        self.assertEqual(rc, 0, out)
        self.assertIn('celery held=1 ', out)
        self.assertRegex(out, r'held %s received 10(799|800|801)s' % re.escape(TASK))

    def test_each_stream_is_read_from_its_head_once_then_by_token(self):
        self.snapshot()
        held = {
            STREAM + 'aaa': done(ID1),
            STREAM + 'bbb': [[20000, line(0, TASK, ID2, 'received')]],
        }
        fin = {
            STREAM + 'aaa': done(ID1),
            STREAM + 'bbb': held[STREAM + 'bbb']
            + [[10000, line(0, TASK, ID2, 'succeeded in 1s: x')]],
        }
        self.state(cycles=[held, held, fin])
        rc, out = self.run_tool('wait', '--apply')
        self.assertEqual(rc, 0, out)
        reads = [ln for ln in self.log().splitlines() if 'get-log-events' in ln]
        self.assertNotIn('--start-time', ''.join(reads))
        for st in ('aaa', 'bbb'):
            mine = [ln for ln in reads if '--log-stream-name %s%s ' % (STREAM, st) in ln]
            self.assertEqual(sum('--next-token' not in ln for ln in mine), 1, mine)
            self.assertGreater(len(mine), 3)

    def test_a_retry_or_a_page_boundary_keeps_a_task_held(self):
        ev = done(ID1, 300) + [
            [200000, line(0, TASK, ID2, 'received')],
            [100000, line(0, TASK, ID2, 'retry: Retry in 3600s: boom')],
        ]
        self.state(cycles=[{STREAM + 'aaa': ev, STREAM + 'bbb': done('3' * 8 + ID1[8:])}])
        rc, out = self.run_tool('read', '--apply')
        self.assertIn('celery held=1 ', out)
        self.assertIn('held %s retry' % TASK, out)
        ev[-1] = [100000, line(0, TASK, ID2, 'succeeded in 1s: x')]
        self.state(cycles=[{STREAM + 'aaa': ev, STREAM + 'bbb': done('3' * 8 + ID1[8:])}])
        rc, out = self.run_tool('read', '--apply')
        self.assertIn('celery held=0 ', out)

    def test_wait_passes_when_held_reaches_zero_and_writes_the_row(self):
        self.snapshot()
        held = {
            STREAM + 'aaa': done(ID1),
            STREAM + 'bbb': [[20000, line(0, TASK, ID2, 'received')]],
        }
        fin = {
            STREAM + 'aaa': done(ID1),
            STREAM + 'bbb': held[STREAM + 'bbb']
            + [[10000, line(0, TASK, ID2, 'succeeded in 1s: x')]],
        }
        self.state(cycles=[held, held, fin])
        rc, out = self.run_tool('wait', '--apply')
        self.assertEqual(rc, 0, out)
        self.assertEqual(out.count('celery held=1 '), 2, out)
        self.assertIn('celery drained: held=0, desired counts unchanged', out)
        self.assertIn('desired counts equal the 1.5 snapshot', out)
        self.assertEqual(self.rows()[-1], 'celery-drain')
        self.assertIn('celery-drain\t', self.timing().read_text())
        self.assertTrue(self.timing().read_text().rstrip().endswith('held\t0'))

    def test_wait_stops_at_the_timeout_without_the_row(self):
        self.snapshot()
        held = {
            STREAM + 'aaa': done(ID1),
            STREAM + 'bbb': [[20000, line(0, TASK, ID2, 'received')]],
        }
        self.state(cycles=[held])
        rc, out = self.run_tool('wait', '--apply', CELERY_DRAIN_TIMEOUT='0')
        self.assertEqual(rc, 1, out)
        self.assertIn('STOP CELERY (apply): held=1 after 0s', out)
        self.assertNotIn('celery-drain', self.rows())
        self.assertIn('stop-CELERY', self.rows())

    def test_stale_logs_or_missing_workers_never_pass(self):
        self.snapshot()
        self.state(cycles=[{STREAM + 'aaa': done(ID1, 900), STREAM + 'bbb': done(ID2, 900)}])
        rc, out = self.run_tool('wait', '--apply', CELERY_DRAIN_TIMEOUT='0')
        self.assertEqual(rc, 1, out)
        self.assertIn('celery held=0 ', out)
        self.assertIn('not a drained read', out)
        self.assertNotIn('celery-drain', self.rows())
        self.state(services={**{t: [2, 2] for t in TARGETS.split()}, CL + '/' + SV: [2, 1]})
        rc, out = self.run_tool('wait', '--apply', CELERY_DRAIN_TIMEOUT='0')
        self.assertEqual(rc, 1, out)
        self.assertIn('not a drained read', out)
        self.state(
            tasks=['aaa', 'ccc'],
            services={**{t: [2, 2] for t in TARGETS.split()}, CL + '/' + SV: [2, 2]},
        )
        rc, out = self.run_tool('read', '--apply')
        self.assertEqual(rc, 1, out)
        self.assertIn('no log stream in %s for running Celery task(s): ccc' % GROUP, out)
        self.state(tasks=['aaa'])
        rc, out = self.run_tool('wait', '--apply', CELERY_DRAIN_TIMEOUT='0')
        self.assertEqual(rc, 1, out)
        self.assertIn('desired=2 running=2 tasks=1', out)
        self.assertIn('not a drained read', out)
        self.state()
        rc, out = self.run_tool('wait', '--apply', CELERY_DRAIN_TIMEOUT='0')
        self.assertEqual(rc, 0, out)

    def test_wait_needs_the_snapshot_of_step_1_5(self):
        rc, out = self.run_tool('wait')
        self.assertEqual(rc, 2, out)
        self.assertIn('NEED  no %s' % (self.t / 'ev' / 'cut-legacy-restore.txt'), out)
        rc, out = self.run_tool('wait', '--apply')
        self.assertEqual(rc, 1, out)
        self.assertNotIn('logs ', self.log())
        self.snapshot(row=False)
        rc, out = self.run_tool('wait', '--apply')
        self.assertEqual(rc, 1, out)
        self.assertIn('no restore-W2 row after the last cut-ecs row', out)
        with open(self.timing(), 'a') as f:
            f.write('restore-W2\tx\tx\tfile\tx\ncut-ecs\tx\tx\tdesired\t0\n')
        rc, out = self.run_tool('wait', '--apply')
        self.assertEqual(rc, 1, out)
        self.assertIn('no restore-W2 row after the last cut-ecs row', out)
        self.snapshot()
        rc, out = self.run_tool('wait')
        self.assertEqual(rc, 0, out)
        self.assertIn('ECS snapshot of 1.5: ', out)
        self.assertIn('CELERY dry-run: done', out)

    def test_wait_stops_when_a_desired_count_differs_from_the_snapshot(self):
        self.snapshot(desired={SV: 10})
        rc, out = self.run_tool('wait', '--apply')
        self.assertEqual(rc, 1, out)
        self.assertIn('accounts-prod/%s desired=2 saved=10' % SV, out)
        self.assertIn('desired counts differ from the 1.5 snapshot before the wait', out)
        self.assertNotIn('get-log-events', self.log())
        self.assertNotIn('celery-drain', self.rows())

    def test_a_task_past_the_hard_time_limit_is_finished(self):
        hard = (
            '[2026-11-19 07:00:00,000: ERROR/MainProcess] '
            'Hard time limit (300.0s) exceeded for %s[%s]'
        )
        ev = [[400000, line(0, TASK, ID2, 'received')], [100000, hard % (TASK, ID2)]]
        self.state(cycles=[{STREAM + 'aaa': done(ID1), STREAM + 'bbb': ev}])
        rc, out = self.run_tool('read', '--apply')
        self.assertEqual(rc, 0, out)
        self.assertIn('celery held=0 received=2 finished=2 ', out)
        ev.append([50000, line(0, TASK, ID2, 'received')])
        self.state(cycles=[{STREAM + 'aaa': done(ID1), STREAM + 'bbb': ev}])
        rc, out = self.run_tool('read', '--apply')
        self.assertIn('celery held=1 ', out)

    def test_the_ignore_list_is_printed_and_passes_only_its_tasks(self):
        self.snapshot()
        held = {
            STREAM + 'aaa': done(ID1) + [[20000, line(0, TASK, ID1[:-1] + '2', 'received')]],
            STREAM + 'bbb': [[3 * 3600 * 1000, line(0, TASK, ID2, 'received')]],
        }
        self.state(cycles=[held])
        rc, out = self.run_tool('wait', '--apply', CELERY_DRAIN_TIMEOUT='0')
        self.assertEqual(rc, 1, out)
        self.assertIn('CELERY_DRAIN_IGNORE: none', out)
        self.assertIn('celery held=2 ', out)
        self.assertRegex(out, r'held %s received 10(799|800|801)s id=%s\n' % (re.escape(TASK), ID2))
        rc, out = self.run_tool(
            'wait', '--apply', CELERY_DRAIN_TIMEOUT='0', CELERY_DRAIN_IGNORE=ID2
        )
        self.assertEqual(rc, 1, out)
        self.assertIn('CELERY_DRAIN_IGNORE: %s' % ID2, out)
        self.assertIn('celery held=1 ', out)
        self.assertRegex(
            out,
            r'ignored %s received 10(799|800|801)s id=%s \(CELERY_DRAIN_IGNORE\)'
            % (re.escape(TASK), ID2),
        )
        self.assertNotIn('celery-drain', self.rows())
        ign = ID2 + ',' + ID1[:-1] + '2'
        rc, out = self.run_tool('wait', '--apply', CELERY_DRAIN_IGNORE=ign)
        self.assertEqual(rc, 0, out)
        self.assertIn('CELERY_DRAIN_IGNORE: ' + ign, out)
        self.assertRegex(out, r'celery held=0 .* ignored=2\n')
        self.assertEqual(self.rows()[-1], 'celery-drain')
        log = ''.join(p.read_text() for p in (self.t / 'ev').glob('*.log'))
        self.assertIn('CELERY_DRAIN_IGNORE: ' + ign, log)
        self.assertIn('ignored %s received' % TASK, log)
        rc, out = self.run_tool('read', CELERY_DRAIN_IGNORE='x; rm -rf /')
        self.assertEqual(rc, 64, out)
        self.assertIn('CELERY_DRAIN_IGNORE takes Celery task ids', out)

    def test_events_logged_during_the_read_are_fresh_not_negative(self):
        self.snapshot()
        live = [[0, line(0, TASK, ID1, 'received')], [0, line(0, TASK, ID1, 'succeeded in 0s: x')]]
        self.state(cycles=[{STREAM + 'aaa': live, STREAM + 'bbb': done(ID2)}], slow=1.5)
        rc, out = self.run_tool('read', '--apply')
        self.assertEqual(rc, 0, out)
        self.assertRegex(out, r'celery held=0 received=2 finished=2 newest_log_age=[0-9]+s ')
        self.assertNotIn('not a drained read', out)
        rc, out = self.run_tool('wait', '--apply', CELERY_DRAIN_TIMEOUT='0')
        self.assertEqual(rc, 0, out)
        self.assertEqual(out.count('celery held='), 1, out)

    def test_one_celery_service_and_one_celery_log_group(self):
        self.env_file.write_text(
            self.env_file.read_text().replace(
                'accounts-prod/accounts-prod-afc-accounts-flower-prod',
                'accounts-prod/accounts-prod-afc-accounts-celery-two',
            )
        )
        rc, out = self.run_tool('read')
        self.assertEqual(rc, 1, out)
        self.assertIn('names 2 Celery services, not 1', out)
        self.setUp()
        self.state(groups=[GROUP, GROUP + '-old'])
        rc, out = self.run_tool('read', '--apply')
        self.assertEqual(rc, 1, out)
        self.assertIn('-celery-: 2, not 1', out)


WRITE = re.compile(
    r'^aws (?:--\S+ \S+ )*(?:ecs|logs|application-autoscaling) (?!describe-|list-|get-)\S+'
)


def writes(calls):
    return [c for c in calls if not c.startswith('aws --profile mzla-legacy-ro ') or WRITE.match(c)]


if __name__ == '__main__':
    unittest.main()
