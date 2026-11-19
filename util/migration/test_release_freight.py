import importlib
import json
import pathlib
import re
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
Base = importlib.import_module('test_celery_drain').Base

FAKE = r"""#!PY
import json, os, sys
name, args = os.path.basename(sys.argv[0]), sys.argv[1:]
open(os.environ['FAKE_LOG'], 'a').write(name + ' ' + ' '.join(args) + '\n')
st = json.load(open(os.environ['REL_STATE']))
if name == 'skopeo' and args[0] == 'list-tags':
    print(json.dumps({'Tags': st['tags'][args[1][len('docker://'):]]}))
elif name == 'skopeo' and args[0] == 'inspect':
    print(st['digests'][args[-1][len('docker://'):]])
elif name == 'gh':
    sha = args[1].split('head_sha=')[1].split('&')[0]
    print(st.get('runs', {}).get(sha, 'completed/success'))
elif name == 'kubectl':
    a = ' '.join(args)
    if not a.startswith('--kubeconfig ') or ' get ' not in a:
        sys.exit(9)
    if ' get stage tb-prod -o json' in a:
        print(json.dumps(st['stage']))
    elif ' get freight -o json' in a:
        print(json.dumps({'items': st['freight']}))
    else:
        sys.exit(1)
else:
    sys.exit(1)
"""
A = 'ghcr.io/thunderbird/thunderbird-accounts'
K = 'ghcr.io/thunderbird/thunderbird-accounts-keycloak'
AD = 'sha256:' + 'a' * 64
KD = 'sha256:' + 'b' * 64


def freight(name, ad=AD, kd=KD, wh='accounts-pair', verified=('tb-dev', 'tb-stage')):
    return {
        'metadata': {'name': name, 'labels': {'kargo.akuity.io/alias': name + '-alias'}},
        'alias': name + '-alias',
        'origin': {'kind': 'Warehouse', 'name': wh},
        'images': [
            {'repoURL': A, 'digest': ad, 'tag': 'latest'},
            {'repoURL': K, 'digest': kd, 'tag': 'latest'},
        ],
        'status': {'verifiedIn': {v: {} for v in verified}, 'currentlyIn': {'tb-stage': {}}},
    }


STAGE = {
    'spec': {
        'requestedFreight': [
            {
                'origin': {'kind': 'Warehouse', 'name': 'accounts-pair'},
                'sources': {'stages': ['tb-stage']},
            }
        ]
    }
}


class ReleaseFreight(Base):
    def setUp(self):
        super().setUp()
        for n in ('skopeo', 'gh', 'kubectl'):
            (self.bin / n).write_text(FAKE.replace('PY', sys.executable, 1))
            (self.bin / n).chmod(0o755)
        (self.bin / 'git').symlink_to(shutil.which('git'))
        self.repo = self.t / 'tba'
        g = ['git', '-C', str(self.repo), '-c', 'user.name=t', '-c', 'user.email=t@example.test']
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        self.c = []
        for i in range(3):
            subprocess.run(g + ['commit', '-q', '--allow-empty', '-m', 'c%d' % i], check=True)
            self.c.append(
                subprocess.run(
                    ['git', '-C', str(self.repo), 'rev-parse', 'HEAD'],
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout.strip()
            )
        self.rel(tags={A: ['latest', self.c[2], self.c[1]], K: [self.c[0], 'latest']})

    def rel(self, tags=None, freights=None, runs=None, stage=None, digests=None):
        st = {
            'tags': tags or self.st['tags'],
            'digests': digests
            or {
                A + ':' + self.c[2]: AD,
                A + ':' + self.c[1]: 'sha256:' + 'c' * 64,
                K + ':' + self.c[0]: KD,
            },
            'freight': freights
            if freights is not None
            else [freight('f-old', ad='sha256:' + 'c' * 64), freight('f-rel')],
            'runs': runs or {},
            'stage': stage or STAGE,
        }
        self.st = st
        (self.t / 'rel.json').write_text(json.dumps(st))

    def go(self, *args, **env):
        return self.run_tool(
            *args,
            script='window/release-freight.sh',
            REL_STATE=str(self.t / 'rel.json'),
            TBA_DIR=str(self.repo),
            **env,
        )

    def test_dry_run_prints_the_reads_and_calls_nothing(self):
        rc, out = self.go(self.c[2])
        self.assertEqual(rc, 0, out)
        self.assertIn('+ git -C %s rev-list --first-parent %s' % (self.repo, self.c[2]), out)
        self.assertIn('+ skopeo list-tags docker://%s' % K, out)
        self.assertIn('get freight -o json', out)
        self.assertIn('RELEASE dry-run: done', out)
        self.assertEqual(self.log(), '')
        rc, out = self.go(self.c[2], '--apply')
        self.assertEqual(rc, 0, out)
        self.assertIn('skopeo list-tags', self.log())

    def test_finds_the_freight_with_both_digests(self):
        rc, out = self.go(self.c[2], '--apply')
        self.assertEqual(rc, 0, out)
        self.assertIn('%s <- :%s (%s)' % (A, self.c[2], AD), out)
        self.assertIn('%s <- :%s (%s)' % (K, self.c[0], KD), out)
        self.assertIn('Stage tb-prod: Warehouse accounts-pair, verified in: tb-stage', out)
        self.assertIn(
            'Freight f-rel (f-rel-alias) accounts=%s keycloak=%s '
            'verified_in=tb-dev,tb-stage currently_in=tb-stage' % (AD, KD),
            out,
        )
        self.assertEqual(out.splitlines()[-1], 'f-rel')
        kube = [ln for ln in self.log().splitlines() if ln.startswith('kubectl ')]
        self.assertEqual(len(kube), 2)
        self.assertTrue(
            all(
                ln.startswith('kubectl --kubeconfig %s -n tb-accounts get ' % (self.t / 'kc-hub'))
                for ln in kube
            )
        )
        rc, out = self.go(self.c[1], '--apply')
        self.assertEqual(rc, 0, out)
        self.assertEqual(out.splitlines()[-1], 'f-old')

    def test_no_freight_is_a_clear_stop(self):
        self.rel(freights=[freight('f-old', ad='sha256:' + 'c' * 64)])
        rc, out = self.go(self.c[2], '--apply')
        self.assertEqual(rc, 1, out)
        self.assertIn(
            'no Freight from Warehouse accounts-pair carries accounts %s and keycloak %s'
            % (AD, KD),
            out,
        )
        self.assertIn(
            'STOP RELEASE (apply): no Freight to promote to tb-prod for %s' % self.c[2], out
        )
        self.rel(freights=[freight('f-other', wh='other-warehouse')])
        rc, out = self.go(self.c[2], '--apply')
        self.assertEqual(rc, 1, out)
        self.assertIn('no Freight from Warehouse accounts-pair carries', out)
        self.rel(freights=[freight('f-a'), freight('f-b')])
        rc, out = self.go(self.c[2], '--apply')
        self.assertEqual(rc, 1, out)
        self.assertIn('2 Freight from Warehouse accounts-pair carry both digests: f-a f-b', out)

    def test_freight_not_verified_upstream_stops(self):
        self.rel(freights=[freight('f-rel', verified=('tb-dev',))])
        rc, out = self.go(self.c[2], '--apply')
        self.assertEqual(rc, 1, out)
        self.assertIn(
            'Freight f-rel is not verified in tb-stage, so Stage tb-prod cannot take it', out
        )

    def test_a_failed_image_build_does_not_fall_back_to_an_older_image(self):
        self.rel(tags={A: [self.c[1]], K: [self.c[0]]}, runs={self.c[2]: 'completed/failure'})
        rc, out = self.go(self.c[2], '--apply')
        self.assertEqual(rc, 1, out)
        self.assertIn('publish-images for %s is completed/failure' % self.c[2], out)
        self.rel(runs={})
        rc, out = self.go(self.c[2], '--apply')
        self.assertEqual(rc, 0, out)
        self.assertIn('%s <- :%s' % (A, self.c[1]), out)
        self.assertEqual(out.splitlines()[-1], 'f-old')

    def test_bad_input_refuses(self):
        rc, out = self.go('abc123')
        self.assertEqual(rc, 64, out)
        rc, out = self.go(self.c[2], TARGET_ENV='tb-stage', CUTOVER_ENV=self.stage_env())
        self.assertEqual(rc, 64, out)
        self.assertIn('TARGET_ENV=tb-stage has none', out)
        rc, out = self.go('f' * 40, '--apply')
        self.assertEqual(rc, 1, out)
        self.assertIn('is not a commit in %s (TBA_DIR)' % self.repo, out)
        self.assertNotIn('skopeo', self.log())
        self.rel(tags={A: ['latest'], K: [self.c[0]]})
        rc, out = self.go(self.c[2], '--apply')
        self.assertEqual(rc, 1, out)
        self.assertIn('no %s:<sha> tag on the first-parent history of %s' % (A, self.c[2]), out)
        tp = self.mig / 'targets' / 'tb-prod.sh'
        text, n = re.subn(
            r'(?m)^LEGACY_PG_SECRET_NAME=.*$', 'LEGACY_PG_SECRET_NAME=REPLACE_ME_X', tp.read_text()
        )
        self.assertEqual(n, 1)
        tp.write_text(text)
        rc, out = self.go(self.c[2], '--apply')
        self.assertEqual(rc, 64, out)


if __name__ == '__main__':
    unittest.main()
