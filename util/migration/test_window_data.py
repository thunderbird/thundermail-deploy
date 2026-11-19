import json
import pathlib
import re
import shutil
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_dup_check import stub_path  # noqa: E402
from test_window import Harness  # noqa: E402

FAKE_RCLONE = """#!{py}
import hashlib, json, os, pathlib, shutil, sys
args = sys.argv[1:]
open(os.environ['FAKE_LOG'], 'a').write('rclone ' + ' '.join(args) + '\\n')
root = pathlib.Path(os.environ['FAKE_S3'])


def where(remote):
    name, _, bucket = remote.partition(':')
    return root / name / bucket


def opt(name):
    return args[args.index(name) + 1]


def listing(remote):
    with open(root / 'listings', 'a') as f:
        f.write(remote + '\\n')
    return {{p.name: p.read_bytes() for p in where(remote).iterdir()}}


if args[0] == 'lsd':
    for b in sorted((root / args[1].rstrip(':')).iterdir()):
        print('          -1 2026-11-18 00:00:00        -1 ' + b.name)
elif args[0] == 'copy':
    src, dst = listing(args[1]), listing(args[2])
    n = size = 0
    with open(opt('--log-file'), 'a') as log:
        for k, v in sorted(src.items()):
            have = dst.get(k)
            same = have == v if '--checksum' in args else have is not None and len(have) == len(v)
            if same:
                continue
            shutil.copyfile(where(args[1]) / k, where(args[2]) / k)
            n, size = n + 1, size + len(v)
            log.write(json.dumps({{'level': 'info', 'msg': 'Copied (new)', 'object': k}}) + '\\n')
        log.write('not json\\n')
        stats = {{'transfers': n, 'bytes': size, 'errors': 0, 'checks': len(src) - n}}
        log.write(json.dumps({{'level': 'notice', 'msg': 'stats', 'stats': stats}}) + '\\n')
elif args[0] == 'check':
    src, dst = listing(args[1]), listing(args[2])
    miss = sorted(k for k in src if k not in dst)
    diff = sorted(k for k in src if k in dst and dst[k] != src[k])
    err = ['broken'] if os.environ.get('FAKE_CHECK_ERROR') else []
    for flag, keys in (('--missing-on-dst', miss), ('--differ', diff), ('--error', err)):
        pathlib.Path(opt(flag)).write_text(''.join(k + '\\n' for k in keys))
    comb = [('+ ' if k in miss else '* ' if k in diff else '= ') + k for k in sorted(src)]
    pathlib.Path(opt('--combined')).write_text(''.join(c + '\\n' for c in comb))
    sys.exit(1 if miss or diff or err else 0)
else:
    sys.exit(2)
"""

FAKE_KUBECTL = """#!{py}
import json, os, pathlib, sys
args = ' '.join(sys.argv[1:])
open(os.environ['FAKE_LOG'], 'a').write('kubectl ' + args + '\\n')
seq = json.load(open(os.environ['KUBE_SEQ']))
keys = {{'get deploy/stalwart -o json': 'deploy', 'get pods -l app=stalwart -o json': 'pods'}}
key = next((v for k, v in keys.items() if k in args), None)
if key is None:
    sys.exit(1)
n = pathlib.Path(os.environ['KUBE_SEQ'] + '.' + key)
i = int(n.read_text()) if n.exists() else 0
n.write_text(str(i + 1))
print(json.dumps(seq[key][min(i, len(seq[key]) - 1)]))
"""

STATE_OK = (
    'deploy replicas=1 ready=1 updated=1 total=1 observed=true recovery_env=true '
    'pods=1 ready=1 recovery=1'
)
GET = re.compile(
    r'^kubectl --kubeconfig /dev/null -n thundermail get '
    r'(deploy/stalwart|pods -l app=stalwart) -o json$'
)


def container(recovery=True):
    env = [{'name': 'STALWART_RECOVERY_MODE_PORT', 'value': '8080'}]
    if recovery:
        env.append({'name': 'STALWART_RECOVERY_MODE', 'value': 'true'})
    return {'name': 'stalwart', 'env': env}


def deploy(replicas, ready, recovery=True, observed=2):
    return {
        'metadata': {'generation': 2},
        'spec': {
            'replicas': replicas,
            'template': {'spec': {'containers': [container(recovery)]}},
        },
        'status': {
            'observedGeneration': observed,
            'replicas': replicas,
            'readyReplicas': ready,
            'updatedReplicas': replicas,
        },
    }


def pod(name, ready=True, recovery=True, kind='ReplicaSet', owner='stalwart-5d9f'):
    return {
        'metadata': {'name': name, 'ownerReferences': [{'kind': kind, 'name': owner}]},
        'spec': {'containers': [container(recovery)]},
        'status': {'conditions': [{'type': 'Ready', 'status': 'True' if ready else 'False'}]},
    }


def pods(*items):
    return {'items': list(items)}


class Data(Harness):
    ENV = {
        'LEGACY_BLOB_BUCKET': 'legacy-b',
        'TARGET_BLOB_BUCKET': 'target-b',
        'RCLONE_CONFIG_LEG_TYPE': 's3',
        'RCLONE_CONFIG_LEG_PROFILE': 'leg-ro',
        'RCLONE_CONFIG_TGT_TYPE': 's3',
        'RCLONE_CONFIG_TGT_PROFILE': 'tgt',
    }

    def setUp(self):
        super().setUp()
        b = self.t / 'bin'
        b.mkdir()
        for name, src in (('rclone', FAKE_RCLONE), ('kubectl', FAKE_KUBECTL)):
            (b / name).write_text(src.format(py=sys.executable))
            (b / name).chmod(0o755)
        self.s3 = self.t / 's3'
        for side, bucket in (('leg', 'legacy-b'), ('tgt', 'target-b')):
            (self.s3 / side / bucket).mkdir(parents=True)

    def put(self, side, key, data):
        bucket = 'legacy-b' if side == 'leg' else 'target-b'
        (self.s3 / side / bucket / key).write_bytes(data)

    def get(self, side, key):
        bucket = 'legacy-b' if side == 'leg' else 'target-b'
        p = self.s3 / side / bucket / key
        return p.read_bytes() if p.exists() else None

    def env(self, **extra):
        e = super().env(**extra)
        e['PATH'] = '%s:%s' % (self.t / 'bin', e['PATH'])
        e['FAKE_S3'] = str(self.s3)
        e['KUBE_SEQ'] = str(self.t / 'kube.json')
        path = stub_path(self.t / 'stubs')
        if path:
            e['PYTHONPATH'] = path
        return e

    def listings(self):
        p = self.s3 / 'listings'
        return p.read_text().splitlines() if p.exists() else []

    def calls(self, tool):
        p = self.t / 'fake.log'
        lines = p.read_text().splitlines() if p.exists() else []
        return [x for x in lines if x.startswith(tool + ' ')]

    def rows(self):
        p = self.t / 'ev' / 'timing.tsv'
        return [x.split('\t') for x in p.read_text().splitlines()] if p.exists() else []

    def w3(self, *flags, check_error=False):
        mig = self.t / 'mig'
        if not mig.exists():
            ignore = shutil.ignore_patterns('__pycache__', 'test_*.py')
            shutil.copytree(self.MIG, mig, ignore=ignore)
        (mig / 'check-prereqs.sh').write_text('echo "HAVE 1  NEED 0"\n')
        env = self.t / 'cutover.env'
        env.write_text(''.join("%s='%s'\n" % kv for kv in self.ENV.items()))
        env.chmod(0o600)
        e = self.env(CUTOVER_ENV=str(env))
        if check_error:
            e['FAKE_CHECK_ERROR'] = '1'
        cmd = 'bash %s/window/w3-blob-copy.sh %s; echo "rc=$?"\n' % (mig, ' '.join(flags))
        return self.netns(cmd, env=e)

    def kube(self, deploys, pod_lists):
        (self.t / 'kube.json').write_text(json.dumps({'deploy': deploys, 'pods': pod_lists}))

    def w4(self, body, setup='', target='tb-stage'):
        text = (self.MIG / 'window' / 'w4-migrate.sh').read_text()
        top = re.findall(r'(?m)^(?:NS|S|POD|RECOVERY_[A-Z]+|REVERT)=.*$', text)
        head = '\n'.join(top) + '\nPY=%s\nSTATE=""\n' % sys.executable
        for k, v in self.ENV.items():
            head += 'export %s=%s\n' % (k, v)
        return self.fn('w4-migrate.sh', body, setup=head + setup, env=self.env(TARGET_ENV=target))


class W3Delta(Data):
    def presync(self):
        for i in range(5):
            self.put('leg', 'blob%d' % i, b'data %d' % i)
        for i in range(3):
            self.put('tgt', 'blob%d' % i, b'data %d' % i)
        self.put('leg', 'STALWART_WEBADMIN', b'bundle-new')
        self.put('tgt', 'STALWART_WEBADMIN', b'bundle-old')
        self.put('tgt', 'only-on-target', b'x')

    def test_apply_copies_the_delta_by_checksum_into_the_presynced_bucket(self):
        self.presync()
        out = self.w3('--apply', '--delta')
        self.assertRegex(out, r'(?m)^rc=0$')
        self.assertIn('rclone check exit=0 differences=0 missing=0 differ=0 errors=0', out)
        self.assertIn('W3 apply: done', out)
        self.assertNotIn('target bucket is not empty', out)
        self.assertEqual(self.get('tgt', 'STALWART_WEBADMIN'), b'bundle-new')
        self.assertEqual(self.get('tgt', 'blob4'), b'data 4')
        self.assertEqual(self.get('tgt', 'only-on-target'), b'x')
        rc = self.calls('rclone')
        self.assertEqual([c.split()[1] for c in rc], ['lsd', 'lsd', 'copy', 'check'])
        self.assertIn('--checksum', rc[2])
        self.assertIn('--one-way', rc[3])
        for c in rc:
            self.assertNotIn('--fast-list', c)
        self.assertEqual(len(self.listings()), 4)
        rows = {(r[0], r[3]): r[4] for r in self.rows()}
        self.assertEqual(rows[('snap-blobs', 'mode')], 'delta')
        self.assertEqual(rows[('blob-copy', 'objects')], '3')
        self.assertEqual(rows[('blob-copy', 'bytes')], '22')
        self.assertEqual(rows[('blob-check', 'differences')], '0')
        restore = (self.t / 'ev' / 'w3-blob-copy-restore.txt').read_text()
        self.assertTrue(restore.startswith('none staged: delta mode'), restore)
        self.assertNotIn('rclone delete', restore)
        combined = list((self.t / 'ev').glob('blobs-check-apply-*-combined.txt'))
        self.assertEqual(len(combined), 1)
        self.assertEqual(len(combined[0].read_text().splitlines()), 6)

    def test_a_rerun_copies_nothing_and_passes(self):
        self.presync()
        self.w3('--apply', '--delta')
        out = self.w3('--apply', '--delta')
        self.assertRegex(out, r'(?m)^rc=0$')
        self.assertIn('restore already staged for this cut', out)
        self.assertEqual(
            [r[4] for r in self.rows() if r[:1] == ['blob-copy'] and r[3] == 'objects'], ['3', '0']
        )

    def test_a_staged_delete_restore_stops_delta_mode(self):
        self.presync()
        (self.t / 'ev').mkdir(mode=0o700)
        restore = self.t / 'ev' / 'w3-blob-copy-restore.txt'
        restore.write_text('rclone delete tgt:target-b --dry-run\n')
        (self.t / 'ev' / 'timing.tsv').write_text('restore-W3\ta\tb\tfile\tx\n')
        out = self.w3('--apply', '--delta')
        self.assertIn('delta mode does not run on top of it', out)
        self.assertRegex(out, r'(?m)^rc=1$')
        self.assertEqual(self.calls('rclone')[2:], [])

    def test_dry_run_lists_no_objects(self):
        self.presync()
        out = self.w3('--dry-run', '--delta')
        self.assertRegex(out, r'(?m)^rc=0$')
        self.assertEqual(self.listings(), [])
        self.assertEqual([c.split()[1] for c in self.calls('rclone')], ['lsd', 'lsd'])
        self.assertRegex(out, r'\+ rclone copy leg:legacy-b tgt:target-b --checksum ')
        self.assertRegex(out, r'\+ rclone check leg:legacy-b tgt:target-b --one-way ')
        self.assertEqual(self.get('tgt', 'STALWART_WEBADMIN'), b'bundle-old')

    def test_check_errors_with_no_difference_do_not_record_0(self):
        self.presync()
        out = self.w3('--apply', '--delta', check_error=True)
        self.assertIn('STOP W3 (apply): blob-check failed', out)
        self.assertRegex(out, r'(?m)^rc=1$')
        self.assertIn('rclone check exit=1 differences=0 missing=0 differ=0 errors=1', out)
        rows = [r[4] for r in self.rows() if r[0] == 'blob-check']
        self.assertEqual(rows, ['errors-exit-1'])


class W4BlobRecheck(Data):
    def test_recheck_uses_the_delta_copy_and_check(self):
        self.put('leg', 'blob0', b'new')
        self.put('tgt', 'blob0', b'old')
        self.put('leg', 'blob1', b'b')
        (self.t / 'ev').mkdir(mode=0o700)
        (self.t / 'ev' / 'timing.tsv').write_text(
            'cut-ecs\ta\tb\t\t\nsnap-blobs\ta\tb\tmode\tdelta\nblob-check\ta\tb\tdifferences\t0\n'
        )
        out = self.w4('blob_recheck; echo "rc=$? VALUE=$VALUE"')
        self.assertIn('rc=0 VALUE=0', out)
        self.assertEqual(self.get('tgt', 'blob0'), b'new')
        rc = self.calls('rclone')
        self.assertEqual([c.split()[1] for c in rc], ['copy', 'check'])
        self.assertIn('--checksum', rc[0])
        self.assertIn('--one-way', rc[1])
        self.assertNotIn('--fast-list', ' '.join(rc))
        self.assertEqual(len(self.listings()), 4)

    def test_both_windows_call_the_same_delta_script(self):
        for name in ('w3-blob-copy.sh', 'w4-migrate.sh'):
            text = (self.MIG / 'window' / name).read_text()
            with self.subTest(script=name):
                self.assertIn('blob-delta.sh" copy', text)
                self.assertIn('blob-delta.sh" check', text)
        w4 = (self.MIG / 'window' / 'w4-migrate.sh').read_text()
        cmd = re.search(r'(?m)(^\s*|[(|;&]\s*)rclone (copy|check|sync|lsf|size|delete)\b', w4)
        self.assertIsNone(cmd)


class W4Recovery(Data):
    def boot(self, wait=600):
        return self.w4(
            'schema_ok() { true; }\nRECOVERY_WAIT=%d\nrecovery_boot; echo "rc=$?"' % wait
        )

    def test_waits_for_the_merged_recovery_pr(self):
        self.kube(
            [deploy(0, 0), deploy(0, 0), deploy(1, 0), deploy(1, 1)],
            [
                pods(),
                pods(),
                pods(pod('stalwart-5d9f-a', ready=False)),
                pods(pod('stalwart-5d9f-a')),
            ],
        )
        out = self.boot()
        self.assertIn(
            'merge the recovery-mode PR (thundermail-deploy PR <TM_RECOVERY_PR>) now', out
        )
        self.assertIn(STATE_OK + '\nrc=0', out)
        gets = self.calls('kubectl')
        self.assertEqual(len(gets), 8)
        for c in gets:
            self.assertRegex(c, GET)

    def test_two_pods_time_out_with_the_state_and_the_pr(self):
        self.kube(
            [deploy(2, 2)],
            [pods(pod('stalwart-5d9f-a'), pod('stalwart-5d9f-b'))],
        )
        out = self.w4(
            'schema_ok() { true; }\nTM_RECOVERY_PR=42\nRECOVERY_PR="thundermail-deploy PR 42"\n'
            'RECOVERY_WAIT=30\nrecovery_boot; echo "rc=$?"'
        )
        self.assertIn(
            'after 30s not one Ready stalwart pod in recovery mode: deploy replicas=2', out
        )
        self.assertIn('pods=2 ready=2 recovery=2; is thundermail-deploy PR 42 merged', out)
        self.assertIn('rc=1', out)

    def test_a_second_pod_of_another_owner_blocks_the_wait(self):
        self.kube(
            [deploy(1, 1)],
            [pods(pod('stalwart-5d9f-a'), pod('stray', kind='Job', owner='stalwart-config-apply'))],
        )
        out = self.boot(wait=30)
        self.assertIn('pods=2 ready=1 recovery=1', out)
        self.assertIn('rc=1', out)

    def test_names_in_errors_are_masked(self):
        (self.t / 'secret').mkdir(mode=0o700)
        lines = (
            'error: principal "o\'neil" has no domain and no default domain is set\n',
            'error: duplicate email address x@example.com claimed by both '
            'User 7 (bob) and Group 9 (ops)\n',
        )
        for i, line in enumerate(lines):
            (self.t / 'secret' / ('e%d' % i)).write_text(line)
        out = self.w4('first_error "$S/e0"; first_error "$S/e1"')
        self.assertIn("principal '<value>' has no domain", out)
        self.assertIn('address <address> claimed by both User 7 (<name>) and Group 9 (<name>)', out)
        self.assertNotIn('neil', out)
        self.assertNotIn('bob', out)

    def test_a_pod_without_the_recovery_variable_does_not_count(self):
        self.kube([deploy(1, 1, recovery=False)], [pods(pod('stalwart-5d9f-a', recovery=False))])
        out = self.boot(wait=30)
        self.assertIn('recovery_env=unset pods=1 ready=1 recovery=0', out)
        self.assertIn('rc=1', out)

    def test_an_unsynced_rollout_does_not_count(self):
        self.kube([deploy(1, 1, observed=1)], [pods(pod('stalwart-5d9f-a'))])
        out = self.boot(wait=30)
        self.assertIn('observed=false', out)
        self.assertIn('rc=1', out)

    def test_w4_ends_in_recovery_mode_after_the_recheck_row(self):
        self.kube([deploy(1, 1)], [pods(pod('stalwart-5d9f-a'))])
        (self.t / 'ev').mkdir(mode=0o700)
        (self.t / 'ev' / 'timing.tsv').write_text(
            'db-dump-legacy\ta\tb\tbytes\t9\nblob-recheck\ta\tb\tdifferences\t0\n'
        )
        out = self.w4('recovery_hold; echo "rc=$?"')
        self.assertIn('W4 ends in recovery mode; leaving it is a PR (W 5.1)', out)
        self.assertIn('rc=0', out)
        self.assertEqual(len(self.calls('kubectl')), 2)

    def test_no_kubectl_write_and_the_restore_names_the_pr_revert(self):
        text = (self.MIG / 'window' / 'w4-migrate.sh').read_text()
        for line in text.splitlines():
            if 'KS[@]' in line:
                with self.subTest(line=line[:80]):
                    self.assertNotRegex(line, r'"\$\{NS\[@\]\}" (scale|set env|patch|apply)\b')
        self.assertNotIn('STALWART_RECOVERY_MODE=true', text)
        self.assertNotIn('STALWART_RECOVERY_MODE-', text)
        out = self.w4(
            'TM_RECOVERY_PR=42\nRECOVERY_PR="thundermail-deploy PR 42"\n'
            + re.search(r'(?ms)^if \[ "\$TARGET_ENV" = tb-prod \]; then\n  printf .*?^fi$', text).group(0)
            + '\ncat "$RESTORE"'
        )
        self.assertNotIn('kubectl scale', out)
        self.assertNotIn(' set env ', out)
        self.assertIn('revert the recovery-mode PR (thundermail-deploy PR', out)


class W4Duplicates(Data):
    def write(self, principals, settings=None):
        (self.t / 'secret').mkdir(mode=0o700)
        (self.t / 'secret' / 'principals.json').write_text(json.dumps(principals))
        (self.t / 'secret' / 'settings.json').write_text(json.dumps(settings or {}))

    def test_every_duplicate_by_class_then_stop_before_convert(self):
        self.write(
            [
                {
                    'id': 1,
                    'type': 'individual',
                    'name': 'a@example.com',
                    'emails': ['b@example.com'],
                },
                {'id': 2, 'type': 'individual', 'name': 'b@example.com', 'emails': []},
                {'id': 3, 'type': 'group', 'name': 'g@example.com', 'emails': ['c@example.com']},
                {'id': 4, 'type': 'list', 'name': 'c@example.com', 'emails': []},
            ]
        )
        out = self.w4('step duplicates duplicates duplicates\nstep convert export_lines convert')
        self.assertIn('  class Group:alias + MailingList:primary 1', out)
        self.assertIn('  class User:alias + User:primary 1', out)
        self.assertIn('  total 2', out)
        self.assertIn('2 duplicate address(es) by class above', out)
        self.assertIn('STOP TEST (apply): duplicates failed', out)
        self.assertNotIn('== convert', out)
        self.assertNotIn('@example.com', out.replace('dup_check.py', ''))
        listed = (self.t / 'secret' / 'duplicates.txt').read_text()
        self.assertIn('\tb@example.com\t', listed)
        self.assertIn('\tc@example.com\t', listed)
        self.assertIn(['duplicates', 'duplicates', '2'], [[r[0], r[3], r[4]] for r in self.rows()])

    def test_convert_reports_its_own_error_not_a_duplicate(self):
        self.write([{'id': 1, 'type': 'individual', 'name': 'a@example.com', 'emails': []}])
        out = self.w4('step duplicates duplicates duplicates\nstep convert export_lines convert')
        self.assertIn('duplicates done (duplicates=0)', out)
        self.assertIn(
            'convert exit 1: DataStore could not be built (storage.data missing or invalid); '
            'full text in',
            out,
        )
        self.assertNotIn('1166', out)

    def test_another_convert_error_in_the_check_is_masked_in_the_log(self):
        self.write([{'type': 'individual', 'name': 'noid@example.com', 'emails': []}])
        out = self.w4('step duplicates duplicates duplicates')
        self.assertIn("dup_check.py exit 1: user '<value>' has no id; full text in", out)
        self.assertNotIn('noid@', out)


class W4LegacyDb(Data):
    BODY = (
        'export STALWART_LEGACY_PG_HOST=legacy.example.test STALWART_LEGACY_PG_USER=u '
        'STALWART_LEGACY_PG_DB=d\n'
        'task_queue; echo "rc=$?"; dump_legacy; echo "rc=$?"; echo "needs=$NEEDS"'
    )
    REFUSED = (
        '%sreads the legacy Stalwart database: refused on tb-prod, whose runner holds no '
        'credential for it (the cutover branch replaces this step)'
    )

    def test_prod_apply_refuses_both_legacy_steps(self):
        out = self.w4(self.BODY, target='tb-prod')
        self.assertIn(self.REFUSED % 'task-queue ' + '\nrc=1\n', out)
        self.assertIn(self.REFUSED % 'db-dump-legacy ' + '\nrc=1\n', out)
        self.assertIn('needs=0', out)
        self.assertEqual(self.calls('kubectl'), [])
        self.assertNotIn('legacy.example.test', out)

    def test_prod_dry_run_lists_them_as_need(self):
        out = self.w4('APPLY=0\n' + self.BODY, target='tb-prod')
        self.assertIn('NEED  ' + self.REFUSED % 'task-queue ' + '\nrc=0\n', out)
        self.assertIn('NEED  ' + self.REFUSED % 'db-dump-legacy ' + '\nrc=0\n', out)
        self.assertIn('needs=2', out)

    def test_stage_dry_run_still_prints_the_legacy_commands(self):
        out = self.w4('APPLY=0\n' + self.BODY)
        self.assertNotIn('refused', out)
        self.assertIn('psql -h legacy.example.test -U u -d d -Atc', out)
        self.assertIn('pg_dump -Fc --no-owner -h legacy.example.test', out)
        self.assertIn('needs=0', out)


if __name__ == '__main__':
    unittest.main()
