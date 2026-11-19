import json
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
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
hang = st.get('hang', {}).get(cmd)
if hang and st.get('reads', 0) + (cmd == 'ecs list-tasks') >= hang[0]:
    time.sleep(hang[1])
if cmd in st.get('fail', []):
    sys.stderr.write('Error when retrieving token from sso: Token has expired and refresh failed\n')
    sys.exit(255)
if cmd == 'ecs describe-services':
    key = opt('--cluster') + '/' + opt('--services')
    d, r = st['services'][key]
    td = 'arn:aws:ecs:eu-central-1:1:task-definition/%s:1' % opt('--services')
    td = st.get('taskdefs', {}).get(key, td)
    if opt('--query') == 'services[0].desiredCount':
        out = str(d)
    elif 'taskDefinition' in opt('--query'):
        out = '%s\t%s\t%d' % (opt('--services'), td, d)
    else:
        out = '%d\t%d' % (d, r)
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
    if tok in st.get('hang_tok', {}):
        time.sleep(st['hang_tok'][tok])
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

    def snapshot(self, row=True, desired=None, revisions=True):
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
        if revisions:
            (self.t / 'ev' / 'ecs-revisions-before.txt').write_text(
                ''.join(
                    '%s\tarn:aws:ecs:eu-central-1:1:task-definition/%s:1\t2\t2\n'
                    % (t.split('/')[1], t.split('/')[1])
                    for t in TARGETS.split()
                )
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
        text, n = re.subn(
            r'(?m)^LEGACY_PG_SECRET_NAME=.*$', 'LEGACY_PG_SECRET_NAME=REPLACE_ME_X', tp.read_text()
        )
        self.assertEqual(n, 1)
        tp.write_text(text)
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

    def held_tsv(self):
        return list((self.t / 'ev').glob('celery-held-*.tsv'))

    def test_timed_read_records_what_is_still_held_and_exits_0(self):
        self.snapshot()
        retry = 'retry: Retry in 3600s: secret-arg@b.test'
        ev = done(ID1, 900) + [
            [600000, line(0, TASK, ID2, 'received')],
            [500000, line(0, TASK, ID2, retry)],
        ]
        third = '3' * 8 + ID1[8:]
        cyc = {STREAM + 'aaa': ev, STREAM + 'bbb': [[20000, line(0, TASK, third, 'received')]]}
        self.state(cycles=[cyc])
        rc, out = self.run_tool('timed', '--apply', CELERY_DRAIN_READ_FOR='4')
        self.assertEqual(rc, 0, out)
        self.assertIn('celery held=2 ', out)
        self.assertNotIn('STOP', out)
        f = self.held_tsv()
        self.assertEqual(len(f), 1, out)
        self.assertRegex(
            out,
            r'celery timed read: held=2 ignored=0 after \d+s of at most 4s; recorded in %s\n'
            % re.escape(str(f[0])),
        )
        rows = [r.split('\t') for r in f[0].read_text().splitlines()]
        self.assertEqual(rows[0], ['task', 'id', 'state', 'first_log_utc', 'last_log_utc', 'note'])
        self.assertEqual(
            [r[:3] for r in rows[1:]], [[TASK, ID2, 'retry'], [TASK, third, 'received']]
        )
        for r in rows[1:]:
            self.assertRegex(r[3] + r[4], r'^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ){2}$')
            self.assertEqual(r[5], 'held')
        self.assertLess(rows[1][3], rows[1][4])
        self.assertNotIn('secret', f[0].read_text() + out)
        self.assertEqual(self.rows()[-1], 'celery-read')
        self.assertTrue(self.timing().read_text().rstrip().endswith('held\t2'))
        self.assertNotIn('celery-drain', self.rows())
        self.assertEqual(list((self.t / 'ev').glob('*.raw')), [])

    def test_timed_read_ends_early_at_held_0_and_records_ignored_tasks(self):
        self.snapshot()
        held = {
            STREAM + 'aaa': done(ID1),
            STREAM + 'bbb': [[3600000, line(0, TASK, ID2, 'received')]],
        }
        self.state(cycles=[held])
        rc, out = self.run_tool(
            'timed', '--apply', CELERY_DRAIN_READ_FOR='600', CELERY_DRAIN_IGNORE=ID2
        )
        self.assertEqual(rc, 0, out)
        self.assertEqual(out.count('celery held='), 1, out)
        self.assertIn('celery timed read: held=0 ignored=1 after ', out)
        rows = self.held_tsv()[0].read_text().splitlines()
        self.assertEqual(len(rows), 2, rows)
        self.assertTrue(rows[1].startswith('%s\t%s\treceived\t' % (TASK, ID2)), rows)
        self.assertTrue(rows[1].endswith('\tignored'), rows)
        self.state()
        rc, out = self.run_tool('timed', '--apply', CELERY_DRAIN_READ_FOR='600', TS='t2')
        self.assertEqual(rc, 0, out)
        self.assertIn('celery timed read: held=0 ignored=0 ', out)
        t2 = self.t / 'ev' / 'celery-held-t2.tsv'
        self.assertEqual(len(t2.read_text().splitlines()), 1)

    def test_timed_read_that_failed_is_not_an_empty_read(self):
        self.snapshot()
        idle = [[30000, '[2026-11-19 07:00:00,000: INFO/MainProcess] Connected to redis://x']]
        cases = [
            ({'cycles': [{STREAM + 'aaa': idle, STREAM + 'bbb': idle}]}, 'no Celery task line'),
            (
                {'tasks': ['aaa', 'ccc']},
                'no log stream in %s for running Celery task(s): ccc' % GROUP,
            ),
            ({'garbled': [STREAM + 'bbb']}, 'logs get-log-events failed for %sbbb' % STREAM),
            ({'fail': ['logs get-log-events']}, 'logs get-log-events failed for'),
            ({'fail': ['ecs list-tasks']}, 'ecs list-tasks %s/%s failed' % (CL, SV)),
            (
                {
                    'tasks': [],
                    'services': {t: [2, 2] for t in TARGETS.split()} | {CL + '/' + SV: [2, 0]},
                },
                'no Celery task line',
            ),
        ]
        for n, (st, why) in enumerate(cases):
            self.state(**st)
            rc, out = self.run_tool('timed', '--apply', CELERY_DRAIN_READ_FOR='30', TS='f%d' % n)
            self.assertEqual(rc, 1, (st, out))
            self.assertIn(why, out)
            self.assertIn('STOP CELERY (apply): ', out)
            self.assertIn('a failed read is not an empty read', out)
            self.assertNotIn('celery timed read:', out)
        self.assertEqual(self.held_tsv(), [])
        self.assertNotIn('celery-read', self.rows())

    def test_wait_never_passes_a_read_with_no_task_line(self):
        self.snapshot()
        idle = [[30000, '[2026-11-19 07:00:00,000: INFO/MainProcess] Connected to redis://x']]
        self.state(cycles=[{STREAM + 'aaa': idle, STREAM + 'bbb': idle}])
        rc, out = self.run_tool('wait', '--apply', CELERY_DRAIN_TIMEOUT='0')
        self.assertEqual(rc, 1, out)
        self.assertIn('celery held=0 received=0 finished=0 ', out)
        self.assertIn('not a drained read: needs desired=running=tasks above 0, a task line', out)
        self.assertNotIn('celery-drain', self.rows())

    def test_timed_read_keeps_to_its_limit(self):
        self.snapshot()
        long = [[900000 - i * 1000, line(0, TASK, ID1, 'received')] for i in range(40)]
        self.state(cycles=[{STREAM + 'aaa': long, STREAM + 'bbb': done(ID2)}], slow=0.6)
        rc, out = self.run_tool('timed', '--apply', CELERY_DRAIN_READ_FOR='4')
        self.assertEqual(rc, 1, out)
        self.assertIn('the log history of %saaa ' % STREAM, out)
        self.assertIn('was not read to its end within 4s; raise CELERY_DRAIN_READ_FOR', out)
        self.assertIn('a failed read is not an empty read', out)
        self.assertNotIn('celery timed read:', out)
        pages = [ln for ln in self.log().splitlines() if 'get-log-events' in ln]
        self.assertLess(len(pages), 12, pages)
        self.assertEqual(self.held_tsv(), [])

    def test_a_slow_aws_call_cannot_hold_the_timed_read_past_its_limit(self):
        self.snapshot()
        held = {
            STREAM + 'aaa': done(ID1),
            STREAM + 'bbb': [[20000, line(0, TASK, ID2, 'received')]],
        }
        for cmd, why in (
            ('logs describe-log-streams', 'logs describe-log-streams did not answer within the 6s'),
            ('ecs list-tasks', 'ecs list-tasks %s/%s did not answer within the 6s' % (CL, SV)),
            ('logs get-log-events', 'was not read to its end within 6s'),
        ):
            self.state(cycles=[held], hang={cmd: [1, 30]})
            t0 = time.monotonic()
            rc, out = self.run_tool('timed', '--apply', CELERY_DRAIN_READ_FOR='6', TS=cmd[:4])
            self.assertEqual(rc, 1, out)
            self.assertLess(time.monotonic() - t0, 22, out)
            self.assertIn(why, out)
            self.assertIn('a failed read is not an empty read', out)
        self.assertEqual(self.held_tsv(), [])
        for cmd in ('logs describe-log-streams', 'logs get-log-events'):
            self.state(cycles=[held], hang={cmd: [2, 30]})
            t0 = time.monotonic()
            rc, out = self.run_tool(
                'timed', '--apply', CELERY_DRAIN_READ_FOR='8', TS='late' + cmd[:4]
            )
            self.assertEqual(rc, 0, out)
            self.assertLess(time.monotonic() - t0, 22, out)
            self.assertRegex(out, r'the read (at|stopped at) the limit')
            self.assertIn('celery timed read: held=1 ignored=0 ', out)
            rows = (self.t / 'ev' / ('celery-held-late%s.tsv' % cmd[:4])).read_text().splitlines()
            self.assertEqual([r.split('\t')[1] for r in rows[1:]], [ID2])

    def test_a_later_round_cut_at_the_limit_keeps_the_last_complete_read(self):
        self.snapshot()
        got = [20000, line(0, TASK, ID2, 'received')]
        idle = [15000, '[2026-11-19 07:00:00,000: INFO/MainProcess] Connected to redis://x']
        later = [got, [18000, line(0, TASK, ID2, 'succeeded in 1s: x')], idle]
        later.append([12000, line(0, TASK, ID2, 'received')])
        self.state(
            cycles=[
                {STREAM + 'aaa': done(ID1), STREAM + 'bbb': [got]},
                {STREAM + 'aaa': done(ID1), STREAM + 'bbb': later},
            ],
            hang_tok={'f3': 30},
        )
        rc, out = self.run_tool('timed', '--apply', CELERY_DRAIN_READ_FOR='6')
        self.assertEqual(rc, 0, out)
        self.assertIn(
            'the read at the limit did not finish; the counts and rows of the last complete '
            'read above stand',
            out,
        )
        self.assertEqual(out.count('celery held='), 1, out)
        self.assertIn('celery timed read: held=1 ignored=0 ', out)
        rows = self.held_tsv()[0].read_text().splitlines()
        self.assertEqual([r.split('\t')[1] for r in rows[1:]], [ID2])
        self.assertTrue(self.timing().read_text().rstrip().endswith('held\t1'))

    def test_timed_read_sleeps_no_further_than_its_limit(self):
        self.snapshot()
        (self.bin / 'sleep').write_text(
            '#!%s\necho "$@" >> %s\nexec %s 0.05\n'
            % (shutil.which('bash'), self.t / 'sleeps', shutil.which('sleep'))
        )
        held = {
            STREAM + 'aaa': done(ID1),
            STREAM + 'bbb': [[20000, line(0, TASK, ID2, 'received')]],
        }
        self.state(cycles=[held])
        rc, out = self.run_tool(
            'timed', '--apply', CELERY_DRAIN_READ_FOR='4', CELERY_DRAIN_INTERVAL='30'
        )
        self.assertEqual(rc, 0, out)
        self.assertIn('celery timed read: held=1 ', out)
        sleeps = (self.t / 'sleeps').read_text().split()
        self.assertTrue(sleeps, out)
        self.assertTrue(all(0 < int(x) <= 4 for x in sleeps), sleeps)

    def test_timed_read_goes_on_when_autoscaling_moved_a_desired_count(self):
        self.snapshot()
        self.state(services={**{t: [2, 2] for t in TARGETS.split()}, CL + '/' + SV: [9, 9]})
        rc, out = self.run_tool('timed', '--apply', CELERY_DRAIN_READ_FOR='5')
        self.assertEqual(rc, 0, out)
        self.assertIn('accounts-prod/%s task=%s:1 saved=%s:1 desired=9 saved=2' % (SV, SV, SV), out)
        self.assertEqual(out.count('service names and task definitions equal 1.5'), 2, out)
        self.assertEqual(
            out.count(
                'desired count differs from the 1.5 snapshot for %s: autoscaling moves it; '
                'the timed read goes on' % SV
            ),
            2,
            out,
        )
        self.assertNotIn('STOP', out)
        self.assertIn('celery timed read: held=0 ', out)
        self.assertEqual(self.rows()[-1], 'celery-read')
        self.snapshot(desired={SV: 9})
        rc, out = self.run_tool('timed', '--apply', CELERY_DRAIN_READ_FOR='5', TS='same')
        self.assertEqual(rc, 0, out)
        self.assertIn('desired counts equal the 1.5 snapshot', out)
        self.state(services={**{t: [2, 2] for t in TARGETS.split()}, CL + '/' + SV: [2, 2]})
        rc, out = self.run_tool('wait', '--apply', TS='w2')
        self.assertEqual(rc, 1, out)
        self.assertIn('desired counts differ from the 1.5 snapshot before the wait', out)

    def test_timed_read_stops_when_a_task_definition_or_service_differs_from_1_5(self):
        self.snapshot()
        new = 'arn:aws:ecs:eu-central-1:1:task-definition/%s:2' % SV
        self.state(taskdefs={CL + '/' + SV: new})
        rc, out = self.run_tool('timed', '--apply', CELERY_DRAIN_READ_FOR='5')
        self.assertEqual(rc, 1, out)
        self.assertIn('accounts-prod/%s task=%s:2 saved=%s:1 desired=2 saved=2' % (SV, SV, SV), out)
        self.assertIn(
            'STOP CELERY (apply): service names or task definitions differ from 1.5 '
            'before the timed read',
            out,
        )
        self.assertNotIn('get-log-events', self.log())
        self.assertEqual(self.held_tsv(), [])
        rev = self.t / 'ev' / 'ecs-revisions-before.txt'
        rev.write_text(''.join(ln + '\n' for ln in rev.read_text().splitlines() if SV not in ln))
        self.state()
        rc, out = self.run_tool('timed', '--apply', TS='r2')
        self.assertEqual(rc, 0, out)
        self.assertIn(
            'NOTE  %s names no service %s: task definitions are not compared here, '
            'only service names (2.2 compares both)' % (rev, SV),
            out,
        )
        self.assertIn('service names equal the 1.5 snapshot; task definitions not compared', out)
        rev.unlink()
        rc, out = self.run_tool('timed', TS='r3')
        self.assertEqual(rc, 0, out)
        self.assertIn(
            'NOTE  no %s from the ECS loop of 1.5: task definitions are not compared' % rev, out
        )
        self.state(taskdefs={CL + '/' + SV: new})
        rc, out = self.run_tool('timed', '--apply', TS='r4')
        self.assertEqual(rc, 0, out)
        self.assertIn('accounts-prod/%s task=%s:2 saved=- desired=2 saved=2' % (SV, SV), out)
        self.assertIn('celery timed read: held=0 ', out)
        self.assertEqual(self.rows()[-1], 'celery-read')

    def test_timed_dry_run_prints_the_plan_and_calls_nothing(self):
        rc, out = self.run_tool('timed')
        self.assertEqual(rc, 2, out)
        self.assertIn('NEED  no %s' % (self.t / 'ev' / 'cut-legacy-restore.txt'), out)
        self.snapshot()
        rc, out = self.run_tool('timed', CELERY_DRAIN_READ_FOR='240')
        self.assertEqual(rc, 0, out)
        self.assertIn('every 0s for at most 240s, ending early at held=0', out)
        self.assertIn(
            'the tasks still held to %s' % (self.t / 'ev' / 'celery-held-20261119T000000Z.tsv'), out
        )
        self.assertIn('row celery-read held <n>', out)
        self.assertIn('CELERY dry-run: done', out)
        self.assertEqual(self.log(), '')
        self.assertEqual(self.held_tsv(), [])
        rc, out = self.run_tool('timed', CELERY_DRAIN_READ_FOR='3m')
        self.assertEqual(rc, 64, out)

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
