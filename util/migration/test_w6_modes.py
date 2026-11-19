import shutil
import sys
import unittest

import test_window as tw

FAKE_RUN_ALL = (
    'printf "%s\\n" "$*" >> "$EVIDENCE_DIR/run_all.args"\n'
    'echo "== 02 directory"\necho "  PASS  account ids: source 1, target 1"\n'
)


class W6Script(tw.Harness):
    def w6(self, args, run_all=FAKE_RUN_ALL, **values):
        mig = self.t / 'mig'
        shutil.copytree(self.MIG, mig, ignore=shutil.ignore_patterns('__pycache__', 'test_*.py'))
        (mig / 'check-prereqs.sh').write_text('echo "HAVE 1  NEED 0"\n')
        (mig / 'verify' / 'run_all.sh').write_text(run_all)
        (self.t / 'secret').mkdir(mode=0o700)
        env = self.t / 'cutover.env'
        env.write_text(''.join("%s='%s'\n" % kv for kv in {**tw.DryRun.ENV, **values}.items()))
        env.chmod(0o600)
        e = self.env(CUTOVER_ENV=str(env), VENV_PY=sys.executable, CF_API_TOKEN_AGENT='cf-token')
        cmd = 'bash %s/window/w6-verify.sh %s </dev/null; echo "rc=$?"\n' % (mig, args)
        return self.netns(cmd, rules=tw.DryRun.RULES, mock=tw.DryRun.MOCK, env=e)

    def run_all_args(self):
        p = self.t / 'ev' / 'run_all.args'
        return p.read_text().splitlines() if p.exists() else None

    def test_compare_only_runs_the_compare_group_and_no_folder_count(self):
        out = self.w6('--dry-run --compare-only', TEST_SENDER_APP_PASSWORD='', TARGET_NLB_DNS='')
        self.assertRegex(out, r'(?m)^rc=0$')
        self.assertIn('verify/run_all.sh --group compare > ', out)
        self.assertIn('== dryrun-verify-compare\n', out)
        self.assertNotIn('imap-jmap-folders', out)
        self.assertNotIn('PENDING  TEST_SENDER_APP_PASSWORD', out)
        self.assertNotIn('PENDING  TARGET_NLB_DNS', out)
        self.assertEqual(self.run_all_args(), ['--group compare'])

    def test_folders_only_runs_the_folder_count_alone(self):
        out = self.w6('--folders-only --dry-run')
        self.assertRegex(out, r'(?m)^rc=0$')
        self.assertIn('== dryrun-imap-jmap-folders\n', out)
        self.assertNotIn('run_all.sh', out)
        self.assertNotIn('dryrun-verify', out)
        self.assertIsNone(self.run_all_args())

    def test_a_piece_dry_run_prints_the_command_and_does_not_run_it(self):
        out = self.w6('--piece bodies --resume')
        self.assertRegex(out, r'(?m)^rc=0$')
        self.assertIn('verify/run_all.sh --piece bodies --resume > ', out)
        self.assertIn('== dryrun-verify-bodies\n', out)
        self.assertNotIn('imap-jmap-folders', out)
        self.assertIsNone(self.run_all_args())

    def test_bad_flags_are_refused_before_anything_runs(self):
        for args, msg in (
            ('--resume', '--resume needs --piece'),
            ('--compare-only --folders-only', 'use one of --compare-only, --folders-only, --piece'),
            ('--piece nope', '--piece takes buckets, counts or bodies'),
        ):
            with self.subTest(args=args):
                self.setUp()
                out = self.w6(args)
                self.assertIn(msg, out)
                self.assertIn('rc=64\n', out)
                self.assertFalse((self.t / 'ev' / 'timing.tsv').exists())


class W6Apply(tw.Harness):
    def apply(self, mode, run_all, extra=''):
        mig = self.t / 'fix'
        (mig / 'verify').mkdir(parents=True)
        (mig / 'verify' / 'tb-stage.env').write_text('')
        (mig / 'verify' / 'expected-warn.txt').write_text('extra on target:\n')
        (mig / 'verify' / 'run_all.sh').write_text(
            'printf "%s\\n" "$*" >> "$EVIDENCE_DIR/run_all.args"\n' + run_all
        )
        text = (self.MIG / 'window' / 'w6-verify.sh').read_text().splitlines()
        main = '\n'.join(text[text.index('prereqs') :])
        setup = (
            'MIG_DIR=%s VERIFY_OUT=%s/out VERIFY_PIECES_DIR=%s/pieces '
            'EXPECTED_WARN=%s/verify/expected-warn.txt UNEXPECTED_WARN=0 SUITE_FAILED="" '
            'SOURCE_MISSING="" SUITE_RAN=0\n%s\n'
            'prereqs() { :; }; helpers() { :; }; port_forward() { :; }; live() { :; }\n'
            'folders() { echo "folders ran"; VALUE=3; }' % (mig, self.t, self.t, mig, mode)
        )
        return self.fn('w6-verify.sh', main + '\n' + extra, setup)

    def rows(self):
        p = self.t / 'ev' / 'timing.tsv'
        return [x.split('\t')[0] for x in p.read_text().splitlines()] if p.exists() else []

    COMPARE = 'W6_MODE=compare RUN_ARGS=(--group compare) VSTEP=verify-compare'
    PIECE = 'W6_MODE=piece W6_PIECE=bodies RUN_ARGS=(--piece bodies --resume) VSTEP=verify-bodies'

    def test_compare_passes_without_the_folder_count(self):
        out = self.apply(self.COMPARE, 'echo "  PASS  bodies downloaded (random sample): 1/1"\n')
        self.assertIn('run_all exit=0 PASS=1 WARN=0 (not in expected-warn.txt: 0) FAIL=0', out)
        self.assertIn('TEST apply: done', out)
        self.assertNotIn('folders ran', out)
        self.assertEqual(self.rows(), ['verify-compare', 'verify-compare'])

    def test_a_failed_compare_stops_and_skips_the_folder_count(self):
        out = self.apply(self.COMPARE, 'echo "  FAIL  messages: missing on target 1"; exit 1\n')
        self.assertIn('STOP TEST (apply): verify: run_all exit=1 FAIL=1', out)
        self.assertNotIn('imap-jmap-folders runs next', out)
        self.assertNotIn('folders ran', out)

    def test_an_empty_source_credential_stops_without_running_the_suite(self):
        mode = (
            self.COMPARE + '\nSOURCE_MISSING=" STALWART_SOURCE_PG_HOST STALWART_SOURCE_PG_PASSWORD"'
        )
        out = self.apply(mode, 'echo "  PASS  x 1"\n')
        self.assertIn(
            'STOP TEST (apply): verify: empty in %s/cutover.env: STALWART_SOURCE_PG_HOST '
            'STALWART_SOURCE_PG_PASSWORD (branch tb-prod-cutover-source' % self.t,
            out,
        )
        self.assertFalse((self.t / 'ev' / 'run_all.args').exists())

    def test_a_piece_with_batches_left_is_a_need(self):
        line = 'echo "== piece bodies: 3 batches; passed 1, failed 0 [], left 2 (resume key /p)"\n'
        out = self.apply(self.PIECE, line)
        self.assertEqual((self.t / 'ev' / 'run_all.args').read_text(), '--piece bodies --resume\n')
        self.assertIn('== piece bodies: 3 batches; passed 1, failed 0 [], left 2', out)
        self.assertIn(
            'NEED  piece bodies: 2 batch(es) left; rerun with --piece bodies --resume', out
        )
        self.assertIn('TEST apply: 1 NEED item(s) above', out)
        self.assertNotIn('folders ran', out)

    def test_a_finished_piece_is_done(self):
        line = 'echo "== piece bodies: 3 batches; passed 3, failed 0 [], left 0 (resume key /p)"\n'
        out = self.apply(self.PIECE, line)
        self.assertNotIn('NEED  piece', out)
        self.assertIn('TEST apply: done', out)
        self.assertEqual(self.rows(), ['verify-bodies', 'verify-bodies'])


if __name__ == '__main__':
    unittest.main()
