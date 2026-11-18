import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
TOOL = HERE / 'body-update.py'
spec = importlib.util.spec_from_file_location('body_update', TOOL)
bu = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bu)

FAKE_GH = """#!{py}
import json, os, shutil, sys
d = os.environ['FAKE_DIR']
a = sys.argv[1:]
open(os.path.join(d, 'gh.log'), 'a').write(' '.join(a) + '\\n')
if a[:2] == ['issue', 'view']:
    p = os.path.join(d, 'issue-%s.md' % a[2])
    if not os.path.exists(p):
        sys.exit(1)
    print(json.dumps({{'body': open(p, newline='').read()}}))
elif a[:2] == ['issue', 'edit']:
    shutil.copy(a[a.index('--body-file') + 1], os.path.join(d, 'edited-%s.md' % a[2]))
elif a[:2] == ['pr', 'view']:
    p = os.path.join(d, 'pr.json')
    if not os.path.exists(p):
        sys.exit(1)
    sys.stdout.write(open(p).read())
else:
    sys.exit(3)
"""

ISSUER = '- [ ] Issuer `https://auth.tb.pro/realms/tbpro` through the tb-prod load balancer'
ROWS = '- [ ] Rows `rb-identity` and `repoint-keycloak` exist in the #1738 timing log'
ALB = '- [ ] PLACEHOLDER: `$TBPROD_AUTH_ALB_IP` variable name from #1723'
HAND = (
    '# PLACEHOLDER: `bash util/migration/window/unpin-hpa.sh --apply` from #1723'
    ' replaces the [hand] lines above'
)
GATE_BODY = '\n'.join(
    [
        '## Before the window',
        '',
        'Staged when, GATE `go-T7`:',
        ROWS,
        '- [ ] Secret `keycloak-customer-admin-client` exists',
        ALB,
        '',
        'Staged when, GATE `go-W4`:',
        '- [ ] Runner pod Running',
        '',
        '## GATE go-T7: merge the Keycloak repoint',
        '',
        '### Steps',
        '```',
        '# lane: lead',
        'MIN=1     # [hand] until unpin-hpa.sh is delivered',
        'STEP=hpa-unpin t unpin     # [hand] until unpin-hpa.sh is delivered',
        HAND,
        ISSUER,
        '```',
        '',
        '### Pass when',
        ISSUER,
        '- [ ] JWKS key ids equal the capture taken in #1739',
        '',
        '## GATE go-T8: repoint Accounts',
        '',
        '### Pass when',
        ISSUER,
        '',
    ]
)
PARENT_BODY = '\n'.join(
    [
        '## Execution order',
        '',
        '- Entry gate of the #1738 rehearsal, one box per condition:',
        '  - [ ] Build PRs merged.',
        '  - [ ] The identity certificate is `ISSUED`.',
        '- Phase rules',
        '  - [ ] Build PRs merged.',
        '- [ ] PLACEHOLDER: body writer from #1723',
        '',
    ]
)
EVIDENCE = '\n'.join(
    [
        '# go-T7',
        'GATE go-T7: PASS',
        'PASS\t' + ISSUER[6:] + '\tissuer.txt',
        'FAIL\tJWKS key ids equal the capture taken in #1739',
        'PASS  Rows `rb-identity` and `repoint-keycloak` exist in the #1738 timing log',
        'PASS: Runner pod Running',
        '- PASS: Build PRs merged.',
        '',
    ]
)
PR = 'https://github.com/thunderbird/thundermail-deploy/pull/999'
COMMENT = '\n'.join(
    [
        'Closes these placeholder lines:',
        '```placeholders',
        'issue 1743',
        '- ' + HAND,
        '+ bash util/migration/window/unpin-hpa.sh --apply',
        '- ' + ALB,
        '+ - [ ] `$TBPROD_AUTH_ALB_IP` is written by build-cutover-env.sh',
        'issue https://github.com/thunderbird/platform-infrastructure/issues/1719',
        '- - [ ] PLACEHOLDER: body writer from #1723',
        '+ - [ ] `util/migration/body-update.py`',
        '```',
        '',
    ]
)


def plus(out):
    return [x[1:] for x in out.splitlines() if x.startswith('+') and not x.startswith('+++')]


def minus(out):
    return [x[1:] for x in out.splitlines() if x.startswith('-') and not x.startswith('---')]


class Harness(unittest.TestCase):
    def setUp(self):
        self.d = pathlib.Path(tempfile.mkdtemp(prefix='body-update-test-'))
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        bins = self.d / 'bin'
        bins.mkdir()
        (bins / 'gh').write_text(FAKE_GH.format(py=sys.executable))
        (bins / 'gh').chmod(0o755)
        self.issue('1743', GATE_BODY)
        self.issue('1719', PARENT_BODY)
        (self.d / 'go-T7.md').write_text(EVIDENCE)
        self.pr([{'body': 'Merged at abc.'}, {'body': COMMENT}])

    def issue(self, n, body):
        with open(self.d / f'issue-{n}.md', 'w', newline='') as f:
            f.write(body)

    def pr(self, comments):
        (self.d / 'pr.json').write_text(json.dumps({'comments': comments}))

    def run_tool(self, *args):
        env = dict(os.environ, FAKE_DIR=str(self.d), PATH=f'{self.d / "bin"}:{os.environ["PATH"]}')
        return subprocess.run(
            [sys.executable, str(TOOL), *args], capture_output=True, text=True, env=env
        )

    def tick(self, *extra):
        return self.run_tool(
            '--tick', 'go-T7', '--evidence', str(self.d / 'go-T7.md'), '--issue', '1743', *extra
        )

    def edits(self):
        log = self.d / 'gh.log'
        if not log.exists():
            return []
        return [x for x in log.read_text().splitlines() if x.startswith('issue edit')]

    def edited(self, n):
        with open(self.d / f'edited-{n}.md', newline='') as f:
            return f.read()


class Tick(Harness):
    def test_dry_run_ticks_only_pass_lines_in_scope(self):
        p = self.tick('--dry-run')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(
            plus(p.stdout),
            [ROWS.replace('[ ]', '[x]'), ISSUER.replace('[ ]', '[x]'), '  - [x] Build PRs merged.'],
        )
        self.assertEqual(minus(p.stdout), [ROWS, ISSUER, '  - [ ] Build PRs merged.'])
        self.assertIn('body size: %d characters' % len(GATE_BODY), p.stdout)
        self.assertEqual(self.edits(), [])
        self.assertIn('Runner pod Running', p.stderr)

    def test_real_run_writes_the_dry_run_result(self):
        dry = self.tick('--dry-run')
        real = self.tick()
        self.assertEqual(real.returncode, 0, real.stderr)
        self.assertTrue(real.stdout.startswith(dry.stdout))
        self.assertEqual(len(self.edits()), 2)
        for n, body in (('1743', GATE_BODY), ('1719', PARENT_BODY)):
            self.assertIn(bu.diff((bu.REPO, n), body, self.edited(n)), dry.stdout)
        self.assertEqual(self.edited('1743').count('- [x]'), 2)
        self.assertEqual(self.edited('1719').count('- [x]'), 1)

    def test_gate_not_pass_leaves_its_boxes(self):
        (self.d / 'go-T7.md').write_text(
            EVIDENCE.replace('GATE go-T7: PASS', 'GATE go-T7: STOPPED')
        )
        p = self.tick('--dry-run')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn(ISSUER.replace('[ ]', '[x]'), plus(p.stdout))
        self.assertIn(ROWS.replace('[ ]', '[x]'), plus(p.stdout))
        self.assertIn('not reported PASS', p.stderr)

    def test_no_box_matched_exits_non_zero(self):
        (self.d / 'go-T7.md').write_text('GATE go-T7: PASS\nPASS\tsomething else\n')
        p = self.tick('--dry-run')
        self.assertEqual(p.returncode, 1)
        self.assertIn('no box ticked', p.stderr)

    def test_crlf_body_keeps_line_ends(self):
        self.issue('1743', GATE_BODY.replace('\n', '\r\n'))
        p = self.tick('--parent', 'none')
        self.assertEqual(p.returncode, 0, p.stderr)
        got = self.edited('1743')
        self.assertEqual(got.count('\r\n'), GATE_BODY.count('\n'))
        self.assertEqual(got.count('- [x]'), 2)

    def test_empty_evidence_exits_non_zero(self):
        (self.d / 'go-T7.md').write_text('\n')
        p = self.tick('--dry-run')
        self.assertEqual(p.returncode, 1)
        self.assertEqual(self.edits(), [])


class Close(Harness):
    def test_dry_run_replaces_listed_lines_and_deletes_hand_lines(self):
        p = self.run_tool('--close', PR, '--dry-run')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(
            minus(p.stdout),
            [
                ALB,
                'MIN=1     # [hand] until unpin-hpa.sh is delivered',
                'STEP=hpa-unpin t unpin     # [hand] until unpin-hpa.sh is delivered',
                HAND,
                '- [ ] PLACEHOLDER: body writer from #1723',
            ],
        )
        self.assertEqual(
            plus(p.stdout),
            [
                '- [ ] `$TBPROD_AUTH_ALB_IP` is written by build-cutover-env.sh',
                'bash util/migration/window/unpin-hpa.sh --apply',
                '- [ ] `util/migration/body-update.py`',
            ],
        )
        self.assertEqual(self.edits(), [])

    def test_real_run_writes_the_dry_run_result(self):
        dry = self.run_tool('--close', PR, '--dry-run')
        real = self.run_tool('--close', PR)
        self.assertEqual(real.returncode, 0, real.stderr)
        self.assertTrue(real.stdout.startswith(dry.stdout))
        for n, body in (('1743', GATE_BODY), ('1719', PARENT_BODY)):
            self.assertIn(bu.diff((bu.REPO, n), body, self.edited(n)), dry.stdout)
        self.assertNotIn('[hand]', self.edited('1743'))

    def test_unknown_placeholder_writes_nothing(self):
        self.pr([{'body': COMMENT.replace('body writer from #1723', 'no such line')}])
        p = self.run_tool('--close', PR)
        self.assertEqual(p.returncode, 1)
        self.assertIn('0 lines equal placeholder', p.stderr)
        self.assertEqual(self.edits(), [])

    def test_no_comment_exits_non_zero(self):
        self.pr([])
        p = self.run_tool('--close', PR, '--dry-run')
        self.assertEqual(p.returncode, 1)
        self.assertIn('has no comment', p.stderr)

    def test_failed_pr_read_exits_non_zero(self):
        (self.d / 'pr.json').unlink()
        p = self.run_tool('--close', PR, '--dry-run')
        self.assertEqual(p.returncode, 1)
        self.assertIn('failed', p.stderr)

    def test_result_above_limit_refuses(self):
        self.issue('1743', GATE_BODY + 'x' * (bu.LIMIT - len(GATE_BODY) - 10))
        self.pr([{'body': COMMENT.replace('+ bash', '+ ' + 'y' * 400 + ' bash')}])
        p = self.run_tool('--close', PR)
        self.assertEqual(p.returncode, 1)
        self.assertIn('would be', p.stderr)
        self.assertEqual(self.edits(), [])


class Reads(Harness):
    def test_empty_body_exits_non_zero(self):
        self.issue('1743', '  \n')
        for p in (self.tick('--dry-run'), self.run_tool('--close', PR, '--dry-run')):
            self.assertEqual(p.returncode, 1)
            self.assertIn('empty body', p.stderr)
        self.assertEqual(self.edits(), [])

    def test_failed_issue_read_exits_non_zero(self):
        (self.d / 'issue-1719.md').unlink()
        p = self.tick()
        self.assertEqual(p.returncode, 1)
        self.assertIn('gh issue view 1719', p.stderr)
        self.assertEqual(self.edits(), [])

    def test_body_above_limit_refuses_in_every_mode(self):
        self.issue('1743', GATE_BODY + 'x' * bu.LIMIT)
        for p in (self.tick('--dry-run'), self.run_tool('--close', PR, '--dry-run')):
            self.assertEqual(p.returncode, 1)
            self.assertIn('above 65536', p.stderr)

    def test_help_states_the_hand_form(self):
        p = self.run_tool('--help')
        self.assertEqual(p.returncode, 0)
        self.assertIn('#1690 and #1694 have no mode', p.stdout)

    def test_usage_error_exits_2(self):
        self.assertEqual(self.run_tool('--tick', 'go-T7').returncode, 2)


if __name__ == '__main__':
    unittest.main()
