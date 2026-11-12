import base64
import http.server
import json
import pathlib
import re
import shutil
import socketserver
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest

HERE = pathlib.Path(__file__).resolve().parent
TOOLS = (
    'awk base64 basename bash cat chmod cp curl cut dirname env find grep head id jq kill ls mkdir '
    'mktemp mv openssl paste perl printf rm sed seq setsid sha256sum sort ss stat swaks tail tee '
    'timeout touch tr true false uniq wc xargs'
).split()
FAKES = ('kubectl', 'gh', 'aws', 'git', 'stalwart-cli', 'podman', 'rclone')
M = 'mail.stage-thundermail.com'
FAKE = """#!{py}
import json, os, re, sys, time
name, args = os.path.basename(sys.argv[0]), ' '.join(sys.argv[1:])
open(os.environ['FAKE_LOG'], 'a').write(name + ' ' + args + '\\n')
for rule in json.load(open(os.environ['FAKE_RULES'])):
    if rule['tool'] == name and re.search(rule['match'], args):
        time.sleep(rule.get('sleep', 0))
        if rule.get('serve'):
            import http.server

            class Ok(http.server.BaseHTTPRequestHandler):
                def do_GET(self):
                    self.send_response(200)
                    self.end_headers()

            http.server.HTTPServer(('127.0.0.1', rule['serve']), Ok).serve_forever()
        sys.stdout.write(rule.get('out', ''))
        sys.exit(rule.get('rc', 0))
sys.exit(1)
"""
FAKE_DATE = """#!{bash}
[ "$*" = "+%s" ] || exec {date} "$@"
n=$(( $(cat "$FAKE_CLOCK" 2>/dev/null || echo 1800000000) + 5 ))
echo "$n" > "$FAKE_CLOCK"
echo "$n"
"""


def defs(path):
    out, keep = [], False
    for line in pathlib.Path(path).read_text().splitlines():
        if re.match(r'[A-Za-z_][A-Za-z0-9_]*\(\) \{', line):
            keep = True
            out.append(line)
        elif keep and (line == '' or line[0] in ' \t' or line == '}'):
            out.append(line)
            keep = line != '}'
        else:
            keep = False
    return '\n'.join(out) + '\n'


def doh(ip):
    return {'code': 200, 'body': json.dumps({'Answer': [{'type': 1, 'data': ip, 'TTL': 60}]})}


class Harness(unittest.TestCase):
    MIG = HERE

    @classmethod
    def setUpClass(cls):
        if not shutil.which('unshare'):
            raise unittest.SkipTest('needs unshare')
        cls.root = pathlib.Path(tempfile.mkdtemp(prefix='window-test-'))
        r = cls.root
        bins = r / 'bin'
        bins.mkdir()
        for t in TOOLS:
            if shutil.which(t):
                (bins / t).symlink_to(shutil.which(t))
        (bins / 'python3').symlink_to(sys.executable)
        for t in FAKES:
            (bins / t).write_text(FAKE.format(py=sys.executable))
            (bins / t).chmod(0o755)
        (bins / 'date').write_text(
            FAKE_DATE.format(bash=shutil.which('bash'), date=shutil.which('date'))
        )
        (bins / 'sleep').write_text(
            '#!%s\nexec %s 0.2\n' % (shutil.which('bash'), shutil.which('sleep'))
        )
        for t in ('date', 'sleep'):
            (bins / t).chmod(0o755)
        ext = r / 'ext'
        ext.write_text(
            'subjectAltName=DNS:api.cloudflare.com,DNS:*.stage-thundermail.com,DNS:%s,IP:1.1.1.1,IP:127.0.0.1\n'
            % M
        )
        ec = ['-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:prime256v1', '-nodes']
        for args in (
            [
                'req',
                '-x509',
                *ec,
                '-keyout',
                'ca.key',
                '-out',
                'ca.pem',
                '-subj',
                '/CN=test-ca',
                '-days',
                '2',
            ],
            ['req', *ec, '-keyout', 'leaf.key', '-out', 'leaf.csr', '-subj', '/CN=' + M],
            [
                'x509',
                '-req',
                '-in',
                'leaf.csr',
                '-CA',
                'ca.pem',
                '-CAkey',
                'ca.key',
                '-CAcreateserial',
                '-out',
                'leaf.pem',
                '-days',
                '2',
                '-extfile',
                'ext',
            ],
        ):
            subprocess.run(['openssl', *args], cwd=r, check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def setUp(self):
        self.t = pathlib.Path(tempfile.mkdtemp(dir=self.root))
        self.t.chmod(0o700)

    def env(self, **extra):
        r = self.root
        e = {
            'PATH': str(r / 'bin'),
            'HOME': str(self.t),
            'LANG': 'C.UTF-8',
            'SSL_CERT_FILE': str(r / 'ca.pem'),
            'CURL_CA_BUNDLE': str(r / 'ca.pem'),
            'FAKE_LOG': str(self.t / 'fake.log'),
            'FAKE_RULES': str(self.t / 'rules.json'),
            'FAKE_CLOCK': str(self.t / 'clock'),
            'EVIDENCE_DIR': str(self.t / 'ev'),
            'SECRET_DIR': str(self.t / 'secret'),
            'TIMING_TSV': str(self.t / 'ev' / 'timing.tsv'),
            'TS': '20261112T000000Z',
        }
        e.update(extra)
        return e

    def netns(self, script, rules=(), mock=None, env=None, stdin=''):
        t = self.t
        (t / 'rules.json').write_text(json.dumps(list(rules)))
        (t / 'ev').mkdir(exist_ok=True, mode=0o700)
        (t / 'secret').mkdir(exist_ok=True, mode=0o700)
        (t / 'hosts').write_text('127.0.0.1 localhost api.cloudflare.com\n')
        (t / 'nsswitch.conf').write_text('hosts: files\n')
        ip, mnt = shutil.which('ip'), shutil.which('mount')
        pre = '%s --bind %s/hosts /etc/hosts || exit 97\n' % (mnt, t)
        pre += '%s --bind %s/nsswitch.conf /etc/nsswitch.conf || exit 97\n' % (mnt, t)
        pre += '%s link set lo up || exit 97\n' % ip
        pre += '%s addr add 1.1.1.1/32 dev lo || exit 97\n' % ip
        if mock is not None:
            cfg = {
                'cert': str(self.root / 'leaf.pem'),
                'key': str(self.root / 'leaf.key'),
                'ready': str(t / 'ready'),
                'log': str(t / 'mock.log'),
                **mock,
            }
            (t / 'mock.json').write_text(json.dumps(cfg))
            pre += (
                "%s %s --mock %s >%s 2>&1 &\nMOCK=$!\ntrap 'kill $MOCK' EXIT\n"
                'for i in $(seq 400); do [ -e %s ] && break; %s 0.05; done\n'
            ) % (
                sys.executable,
                __file__,
                t / 'mock.json',
                t / 'mock.err',
                t / 'ready',
                shutil.which('sleep'),
            )
        (t / 'driver.sh').write_text(pre + script)
        p = subprocess.run(
            [shutil.which('unshare'), '-rnm', shutil.which('bash'), str(t / 'driver.sh')],
            input=stdin,
            capture_output=True,
            text=True,
            env=env or self.env(),
            timeout=300,
        )
        return p.stdout + p.stderr

    def fn(self, script, body, setup='', **kw):
        mig = self.MIG
        src = (
            defs(mig / 'lib.sh')
            + defs(mig / 'timing.sh')
            + defs(mig / 'window' / 'lib.sh')
            + defs(mig / 'window' / script)
        )
        (self.t / 'defs.sh').write_text(src)
        head = (
            'set -u\nAPPLY=1 KEY=TEST SUF="" NEEDS=0 PF="" PF_OK=0 GO="" SECRET_NAMES=""\n'
            'MIG_DIR=%s ENV_FILE=%s/cutover.env LOG=%s/run.log RESTORE=%s/restore.txt\n'
            'KS=(kubectl --kubeconfig /dev/null) KH=(kubectl --kubeconfig /dev/null)\n'
            '. %s/defs.sh\n' % (mig, self.t, self.t, self.t, self.t)
        )
        return self.netns(head + setup + '\n' + body + '\n', **kw)

    def mock_log(self):
        p = self.t / 'mock.log'
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


class T7Issuer(Harness):
    RULES = [
        {'tool': 'kubectl', 'match': 'get sts keycloak-customer', 'out': '2'},
        {'tool': 'kubectl', 'match': 'rollout status', 'out': 'rolled out\n'},
    ]
    SETUP = 'NS=keycloak-customer ISSUER_HOST=auth.stage-thundermail.com'
    URL = '/realms/tbpro/.well-known/openid-configuration'

    def mock(self, *bodies):
        return {
            'https': {
                '/dns-query?name=auth.stage-thundermail.com': [doh('127.0.0.1')],
                self.URL: list(bodies),
            }
        }

    def test_issuer_read_is_retried_past_a_non_json_body(self):
        good = {
            'code': 200,
            'body': json.dumps({'issuer': 'https://auth.stage-thundermail.com/realms/tbpro'}),
        }
        html = {'code': 502, 'body': '<html>502 Bad Gateway</html>'}
        out = self.fn(
            't7.sh',
            'repoint; echo "rc=$?"',
            self.SETUP,
            rules=self.RULES,
            mock=self.mock(html, html, good),
        )
        self.assertIn('issuer: https://auth.stage-thundermail.com/realms/tbpro', out)
        self.assertIn('rc=0', out)

    def test_failure_prints_the_last_code_and_body(self):
        bad = {'code': 503, 'body': 'upstream connect error or disconnect'}
        out = self.fn(
            't7.sh', 'repoint; echo "rc=$?"', self.SETUP, rules=self.RULES, mock=self.mock(bad)
        )
        self.assertIn('rc=1', out)
        self.assertIn('last HTTP 503', out)
        self.assertIn('upstream connect error', out)


class W5AccountsCheck(Harness):
    BODY = 'accounts_check; echo "rc=$? needs=$NEEDS"'

    def check(self, code, body, *rows):
        tsv = ''.join(
            '%s\t2026-11-12T0%d:00:00Z\t2026-11-12T0%d:00:00Z\t\t\n' % (r, i, i)
            for i, r in enumerate(rows)
        )
        (self.t / 'ev').mkdir(mode=0o700)
        (self.t / 'ev' / 'timing.tsv').write_text(tsv)
        rules = [
            {'tool': 'kubectl', 'match': 'exec deploy/accounts-web', 'out': '%s\n%s' % (body, code)}
        ]
        return self.fn('w5-promote.sh', self.BODY, rules=rules)

    def test_401_before_the_bundle_write_is_pending(self):
        out = self.check(401, '{}', 'cut-ecs')
        self.assertIn('PENDING  the #1539 Accounts bundle write', out)
        self.assertIn('rc=0 needs=1', out)

    def test_401_after_the_bundle_write_fails(self):
        out = self.check(401, '{}', 'cut-ecs', 'put-accounts-bundle')
        self.assertIn('rc=1 needs=0', out)
        self.assertNotIn('PENDING  the', out)

    def test_bundle_row_from_an_earlier_cut_does_not_count(self):
        out = self.check(401, '{}', 'put-accounts-bundle', 'cut-ecs')
        self.assertIn('rc=0 needs=1', out)

    def test_200_passes(self):
        session = json.dumps({'primaryAccounts': {'urn:stalwart:jmap': 'a'}})
        out = self.check(200, session, 'cut-ecs')
        self.assertIn('rc=0 needs=0', out)


class RestartProbe(Harness):
    UP = {'10025': {'starttls': True}, '10587': {'starttls': True}, '10465': {'implicit': True}}
    RULES = [
        {'tool': 'kubectl', 'match': 'rollout (restart|status) deploy/stalwart$|--timeout=600s$'},
        {
            'tool': 'kubectl',
            'match': 'get pod -l app=stalwart-nginx',
            'out': 'pod/stalwart-nginx-1\n',
        },
        {'tool': 'kubectl', 'match': 'port-forward pod/stalwart-nginx-1', 'sleep': 60},
    ]

    def probe(self, smtp, imap=None, setup='', rules=()):
        mock = {'smtp': smtp, 'imap': {'10993': {'implicit': True}} if imap is None else imap}
        return self.fn(
            'lib.sh',
            'restart_stalwart; echo "rc=$? pf_ok=$PF_OK"',
            setup,
            rules=[*rules, *self.RULES],
            mock=mock,
        )

    def test_restart_then_all_four_ports_verified(self):
        out = self.probe(self.UP)
        self.assertIn('Stalwart restarted; 25 465 587 993 answer', out)
        self.assertIn('rc=0', out)
        log = (self.t / 'fake.log').read_text()
        self.assertLess(
            log.index('rollout restart deploy/stalwart'),
            log.index('rollout status deploy/stalwart'),
        )
        self.assertIn(
            'port-forward pod/stalwart-nginx-1 10025:25 10465:465 10587:587 10993:993', log
        )

    def test_a_dead_listener_fails_the_step(self):
        out = self.probe({k: v for k, v in self.UP.items() if k != '10587'}, imap={})
        self.assertIn('no verified TLS through pod/stalwart-nginx-1 on: 587 993', out)
        self.assertIn('rc=1', out)

    def test_port_25_without_starttls_fails(self):
        out = self.probe({**self.UP, '10025': {}})
        self.assertIn('on: 25', out)
        self.assertIn('rc=1', out)

    def test_the_stalwart_port_forward_is_reopened(self):
        rules = [
            {
                'tool': 'kubectl',
                'match': 'get pod -l app=stalwart --field',
                'out': 'pod/stalwart-new\n',
            },
            {
                'tool': 'kubectl',
                'match': 'port-forward pod/stalwart-new 18080:8080',
                'serve': 18080,
            },
        ]
        setup = 'STALWART_URL=http://127.0.0.1:18080 PF_OK=1'
        out = self.probe(self.UP, setup=setup, rules=rules)
        self.assertIn('port-forward pod/stalwart-new -> http://127.0.0.1:18080', out)
        self.assertIn('rc=0 pf_ok=1', out)


class SenderPassword(Harness):
    USER = 'user@stage-thundermail.com'
    SETUP = 'TEST_USER=%s TARGET_NLB_DNS=nlb.example.test M=%s OPERATOR_ENV=%%s/operator.env' % (
        USER,
        M,
    )
    BODY = 'capture; echo "rc=$?"; cat "$ENV_FILE" 2>/dev/null | grep -c TEST_SENDER_APP_PASSWORD'

    def run_capture(self, pw, imap_pw=None, smtp_pw=None, operator=None, stdin=''):
        session = {'code': 200, 'body': '{}', 'auth': '%s:%s' % (self.USER, pw)}
        mock = {
            'https': {
                '/dns-query?name=nlb.example.test': [doh('127.0.0.1')],
                '/jmap/session': [session],
            },
            'imap': {'993': {'implicit': True, 'password': imap_pw or pw}},
            'smtp': {'587': {'starttls': True, 'password': smtp_pw or pw}},
        }
        if operator is not None:
            (self.t / 'operator.env').write_text(
                "export TEST_SENDER_APP_PASSWORD='%s'\n" % operator
            )
            (self.t / 'operator.env').chmod(0o600)
        return self.fn(
            'test-sender-password.sh', self.BODY, self.SETUP % self.t, mock=mock, stdin=stdin
        )

    def test_value_from_the_operator_env_file_is_proven_and_stored(self):
        out = self.run_capture('app-pw-1', operator='app-pw-1')
        self.assertIn(
            'from %s/operator.env: JMAP 443, IMAP LOGIN 993 and SMTP AUTH 587 ok' % self.t, out
        )
        self.assertIn('rc=0\n1', out)
        self.assertIn({'port': 993, 'login': True}, self.mock_log())

    def test_jmap_alone_is_not_enough(self):
        out = self.run_capture('app-pw-2', imap_pw='other', smtp_pw='other', stdin='app-pw-2\n')
        self.assertIn('failed on: 993 (IMAP LOGIN) 587 (SMTP AUTH): password not stored', out)
        self.assertIn('rc=1\n0', out)

    def test_stdin_still_works(self):
        out = self.run_capture('app-pw-3', stdin='app-pw-3\n')
        self.assertIn('from stdin: JMAP 443', out)
        self.assertIn('rc=0\n1', out)


class W7Verify(Harness):
    SETUP = (
        'Z=stage-thundermail.com M=%s NLB_IP=127.0.0.1 TARGET_NLB_DNS=nlb.example.test FAILS=0 '
        'TABLE=%%s/table.txt '
        'SUBJECT=w7-verify-1 TEST_MAILBOX=box@example.test INBOUND_FROM=w7@sender.example.test '
        'TEST_USER=u@stage-thundermail.com\n'
        ': > "$TABLE"' % M
    )

    def verify(self, body, mock=None):
        return self.fn('w7-verify.sh', body, self.SETUP % self.t, mock=mock)

    def test_no_port_25_egress_fails_the_gate(self):
        out = self.verify(
            'chk inbound-test-user inbound u@stage-thundermail.com; gate; echo "gate passed"'
        )
        self.assertRegex(out, r'NEED  inbound-test-user +NEED port 25 egress')
        self.assertIn('STOP TEST (apply): passed=0 not passed=1 (FAIL=0 NEED=1)', out)
        self.assertNotIn('gate passed', out)

    UP = {
        'smtp': {'25': {'starttls': True}, '587': {'starttls': True}, '465': {'implicit': True}},
        'imap': {'993': {'implicit': True}},
        'https': {
            '/dns-query?name=nlb.example.test': [doh('127.0.0.1')],
            '/.well-known/mta-sts.txt': [{'code': 200, 'body': 'version: STSv1\nmode: testing\n'}],
        },
    }

    def no_starttls_on_25(self):
        return {**self.UP, 'smtp': {**self.UP['smtp'], '25': {}}}

    def test_probes_fail_against_a_port_25_listener_without_starttls(self):
        out = self.verify(
            'TARGET_NLB_DNS=nlb.example.test; probes; gate; echo "gate passed"',
            self.no_starttls_on_25(),
        )
        self.assertRegex(
            out, r'FAIL  smtp-25-starttls +EHLO on 127.0.0.1:25 does not list STARTTLS'
        )
        self.assertIn('STOP TEST (apply): passed=4 not passed=1 (FAIL=1 NEED=0)', out)
        self.assertNotIn('gate passed', out)

    def test_inbound_refuses_a_server_without_starttls(self):
        out = self.verify(
            'chk inbound-test-user inbound u@stage-thundermail.com', self.no_starttls_on_25()
        )
        self.assertRegex(out, r'FAIL  inbound-test-user +swaks to u@stage-thundermail.com failed')
        self.assertEqual([r for r in self.mock_log() if r.get('port') == 25], [])

    def test_starttls_on_25_with_a_verified_certificate_passes(self):
        body = 'TARGET_NLB_DNS=nlb.example.test; probes; chk inbound-test-user inbound '
        body += 'u@stage-thundermail.com'
        out = self.verify(body + '; gate; echo "gate passed"', self.UP)
        self.assertRegex(out, r'PASS  smtp-25-starttls +EHLO on 25 lists STARTTLS; TLS verified')
        self.assertIn('gate passed', out)
        self.assertTrue([r for r in self.mock_log() if r.get('port') == 25 and r['tls']])

    def test_inbound_sends_from_inbound_from(self):
        self.verify('chk inbound-test-user inbound u@stage-thundermail.com', self.UP)
        sent = [r for r in self.mock_log() if r.get('port') == 25]
        self.assertEqual([r['from'] for r in sent], ['w7@sender.example.test'])

    def test_unset_inbound_from_stops_before_any_mail(self):
        out = self.verify('INBOUND_FROM=""; mail; echo "rc=$?"', self.UP)
        self.assertIn('NEED  INBOUND_FROM is empty or TEST_MAILBOX', out)
        self.assertIn('rc=1', out)
        self.assertEqual([r for r in self.mock_log() if r.get('port') == 25], [])

    def test_no_forged_sender_in_util_migration(self):
        hits = [
            str(f.relative_to(self.MIG))
            for f in self.MIG.rglob('*.sh')
            if re.search(r'--from "?\$\{?TEST_MAILBOX', f.read_text())
        ]
        self.assertEqual(hits, [])

    def delivery(self, box, received):
        email = {
            'id': 'm1',
            'subject': 'w7-verify-1',
            'mailboxIds': {box: True},
            'received': received,
        }
        jmap = {'auth': 'u@stage-thundermail.com:app-pw', 'inbox': 'inbox1', 'emails': [email]}
        mock = {'https': {}, 'jmap': jmap}
        return self.verify('TEST_SENDER_APP_PASSWORD=app-pw; chk first-delivery delivery', mock)

    TLS = ['from w7-check.local (host [192.0.2.7]) by mail (Stalwart SMTP) with ESMTPS id 1; Thu']
    PLAIN = ['from w7-check.local (host [192.0.2.7]) by mail (Stalwart SMTP) with ESMTP id 1; Thu']

    def test_delivery_to_junk_fails(self):
        out = self.delivery('junk1', self.TLS)
        self.assertRegex(
            out, r'FAIL  first-delivery +inbound test message after 300s: not in the Inbox'
        )

    def test_delivery_without_tls_fails(self):
        out = self.delivery('inbox1', self.PLAIN)
        self.assertRegex(
            out, r'FAIL  first-delivery +.*in the Inbox, no TLS in its top Received header'
        )

    def test_delivery_to_the_inbox_over_tls_passes(self):
        out = self.delivery('inbox1', self.TLS)
        self.assertRegex(out, r'PASS  first-delivery +delivered to the Inbox')


class DryRun(Harness):
    ENV = {
        'KC_LEGACY_PG_USER': 'u',
        'KC_LEGACY_PG_PASSWORD': 'pw-kc',
        'KCD_DIR': '/nonexistent',
        'KUBECONFIG_STAGE': '/dev/null',
        'KUBECONFIG_HUB': '/dev/null',
        'T7_PR': '1',
        'T7_REVERT_PR': '2',
        'STALWART_URL': 'http://127.0.0.1:18080',
        'STALWART_USER': 'admin',
        'STALWART_PASSWORD': 'pw-st',
        'KARGO_PROJECT_CONFIGS': 'p',
        'KARGO_STAGE_CONFIGS': 's',
        'KARGO_WAREHOUSE_CONFIGS': 'w',
        'STALWART_ADMIN_ACCOUNT_ID': 'a',
        'TEST_USER': 'user@stage-thundermail.com',
        'CF_ZONE_ID': 'z',
        'TARGET_NLB_DNS': 'nlb.example.test',
        'FLIP_PR': '3',
        'SPF_FINAL_TXT': 'v=spf1 -all',
        'TEST_MAILBOX': 'box@example.test',
        'TEST_SENDER': 'user@stage-thundermail.com',
        'TEST_SENDER_APP_PASSWORD': 'pw-app',
        'T6_SHA': 'c1',
        'INBOUND_FROM': 'w7@sender.example.test',
        'LEGACY_BLOB_BUCKET': 'l',
        'TARGET_BLOB_BUCKET': 't',
    }

    CF = '/client/v4/zones/z'
    OK = {
        'code': 200,
        'body': json.dumps({'success': True, 'result': [], 'result_info': {'total_pages': 1}}),
    }
    MOCK = {
        'https': {
            '/dns-query?name=nlb.example.test': [doh('127.0.0.1')],
            '/dns-query': [{'code': 200, 'body': '{}'}],
            CF: [
                {
                    'code': 200,
                    'body': json.dumps(
                        {'success': True, 'result': {'name': 'stage-thundermail.com'}}
                    ),
                }
            ],
            CF + '/dns_records': [OK],
            CF + '/dns_records/export': [
                {'code': 200, 'body': 'stage-thundermail.com. 300 IN A 192.0.2.1'}
            ],
            CF + '/rulesets': [
                {'code': 200, 'body': json.dumps({'success': True, 'result': {'rules': []}})}
            ],
        }
    }
    FREIGHT = {
        'items': [
            {
                'metadata': {'name': 'f1', 'creationTimestamp': '2026-11-01T00:00:00Z'},
                'origin': {'name': 'w'},
                'commits': [{'id': 'c1'}],
            }
        ]
    }
    RULES = [
        {
            'tool': 'aws',
            'match': 'describe-secret',
            'out': json.dumps({'VersionIdsToStages': {'v1': ['AWSCURRENT']}}),
        },
        {'tool': 'aws', 'match': 'get-secret-value', 'out': json.dumps({'SecretString': '{}'})},
        {'tool': 'gh', 'match': 'pr view .*state,isDraft', 'out': 'OPEN false APPROVED c1\n'},
        {'tool': 'git', 'match': 'fetch'},
        {'tool': 'git', 'match': 'rev-parse', 'out': 'tree1\n'},
        {'tool': 'kubectl', 'match': 'get freight -o json', 'out': json.dumps(FREIGHT)},
        {'tool': 'kubectl', 'match': 'get stage s', 'out': 'f0'},
    ]

    def dry(self, script):
        mig = self.t / 'mig'
        shutil.copytree(self.MIG, mig, ignore=shutil.ignore_patterns('__pycache__', 'test_*.py'))
        (mig / 'check-prereqs.sh').write_text('echo "HAVE 1  NEED 0"\n')
        (self.t / 'secret').mkdir(mode=0o700)
        (self.t / 'secret' / 'realm-before-1.json').write_text('{}')
        env = self.t / 'cutover.env'
        env.write_text(''.join("%s='%s'\n" % kv for kv in self.ENV.items()))
        env.chmod(0o600)
        e = self.env(CUTOVER_ENV=str(env), VENV_PY=sys.executable, CF_API_TOKEN_AGENT='cf-token')
        cmd = 'bash %s/window/%s --dry-run; echo "rc=$?"\n' % (mig, script)
        return self.netns(cmd, rules=self.RULES, mock=self.MOCK, env=e)

    def test_every_changed_script_dry_runs_to_exit_0(self):
        for script in (
            't7.sh',
            'w5-promote.sh',
            'w6-verify.sh',
            'w7.sh',
            'w7-verify.sh',
            'test-sender-password.sh',
        ):
            with self.subTest(script=script):
                self.setUp()
                out = self.dry(script)
                self.assertTrue(out.rstrip().endswith('rc=0'), out[-1500:])

    def test_w5_and_w7_restart_stalwart_after_the_promote(self):
        for script, before in (
            ('w5-promote.sh', '== dryrun-settings-promote'),
            ('w7.sh', 'job/stalwart-config-apply; restart'),
        ):
            with self.subTest(script=script):
                self.setUp()
                out = self.dry(script)
                self.assertIn('rollout restart deploy/stalwart', out[out.index(before) :])
                self.assertIn('stalwart-nginx pod 10025:25 10465:465 10587:587 10993:993', out)


class Mock:
    def __init__(self, cfg):
        self.cfg, self.lock = cfg, threading.Lock()
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ctx.load_cert_chain(cfg['cert'], cfg['key'])

    def log(self, **rec):
        with self.lock, open(self.cfg['log'], 'a') as f:
            f.write(json.dumps(rec) + '\n')

    def next(self, key):
        with self.lock:
            q = self.cfg['https'][key]
            return q.pop(0) if len(q) > 1 else q[0]

    def jmap(self, req):
        j = self.cfg['jmap']
        want = 'Basic ' + base64.b64encode(j['auth'].encode()).decode()
        if req.headers.get('Authorization') != want:
            return 401, '{}'
        if req.command == 'GET':
            api = 'https://%s/jmap' % M
            return 200, json.dumps(
                {'primaryAccounts': {'urn:ietf:params:jmap:mail': 'acc'}, 'apiUrl': api}
            )
        calls = json.loads(req.rfile.read(int(req.headers['Content-Length'])))['methodCalls']
        out, last = [], []
        for name, args, tag in calls:
            if name == 'Mailbox/query':
                out.append([name, {'ids': [j['inbox']]}, tag])
            elif name == 'Email/query':
                last = [e['id'] for e in j['emails'] if args['filter']['subject'] in e['subject']]
                out.append([name, {'ids': last}, tag])
            elif name == 'Email/get':
                ids = args.get('ids', last)
                rows = [
                    {
                        'id': e['id'],
                        'subject': e['subject'],
                        'mailboxIds': e['mailboxIds'],
                        'header:Received:asText:all': e['received'],
                    }
                    for e in j['emails']
                    if e['id'] in ids
                ]
                out.append([name, {'list': rows}, tag])
        return 200, json.dumps({'methodResponses': out})

    def run(self):
        mock = self
        socketserver.ThreadingTCPServer.allow_reuse_address = True
        socketserver.ThreadingTCPServer.daemon_threads = True

        class Web(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def reply(self, code, body, ctype='application/json'):
                b = body.encode()
                self.send_response(code)
                self.send_header('Content-Type', ctype)
                self.send_header('Content-Length', str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def handle_any(self):
                mock.log(port=443, method=self.command, path=self.path)
                if 'jmap' in mock.cfg and self.path.startswith('/jmap'):
                    return self.reply(*mock.jmap(self))
                for key in sorted(mock.cfg.get('https', {}), key=len, reverse=True):
                    if self.path.startswith(key):
                        r = mock.next(key)
                        if (
                            'auth' in r
                            and self.headers.get('Authorization')
                            != 'Basic ' + base64.b64encode(r['auth'].encode()).decode()
                        ):
                            return self.reply(401, '{}')
                        return self.reply(r['code'], r['body'], r.get('type', 'application/json'))
                self.reply(404, 'not found', 'text/plain')

            do_GET = do_POST = handle_any

        servers = []
        if 'https' in self.cfg or 'jmap' in self.cfg:
            web = http.server.ThreadingHTTPServer(('0.0.0.0', 443), Web)
            web.socket = self.ctx.wrap_socket(web.socket, server_side=True)
            servers.append(web)
        for port, conf in self.cfg.get('smtp', {}).items():
            servers.append(self.tcp(int(port), conf, self.smtp))
        for port, conf in self.cfg.get('imap', {}).items():
            servers.append(self.tcp(int(port), conf, self.imap))
        for s in servers:
            threading.Thread(target=s.serve_forever, daemon=True).start()
        open(self.cfg['ready'], 'w').close()
        threading.Event().wait()

    def tcp(self, port, conf, fn):
        mock = self

        class H(socketserver.BaseRequestHandler):
            def handle(self):
                sock = self.request
                if conf.get('implicit'):
                    sock = mock.ctx.wrap_socket(sock, server_side=True)
                fn(sock, conf, port)

        return socketserver.ThreadingTCPServer(('0.0.0.0', port), H)

    def smtp(self, sock, conf, port):
        tls, f, rec = bool(conf.get('implicit')), sock.makefile('rwb'), {'port': port, 'rcpt': []}

        def send(*lines):
            f.write(''.join(x + '\r\n' for x in lines).encode())
            f.flush()

        send('220 mock ESMTP')
        while True:
            line = f.readline().decode(errors='replace').rstrip('\r\n')
            cmd = line.upper()
            if not line:
                return
            if cmd.startswith(('EHLO', 'HELO')):
                ext = (['STARTTLS'] if conf.get('starttls') and not tls else []) + (
                    ['AUTH PLAIN'] if tls else []
                )
                send(*['250-mock'] + ['250-' + x for x in ext] + ['250 8BITMIME'])
            elif cmd == 'STARTTLS' and conf.get('starttls') and not tls:
                send('220 go ahead')
                sock = self.ctx.wrap_socket(sock, server_side=True)
                f, tls = sock.makefile('rwb'), True
            elif cmd.startswith('AUTH PLAIN'):
                arg = line.split()[2] if len(line.split()) > 2 else None
                if arg is None:
                    send('334 ')
                    arg = f.readline().decode().strip()
                pw = base64.b64decode(arg).split(b'\0')[-1].decode()
                rec['auth'] = pw == conf.get('password')
                send('235 ok' if rec['auth'] else '535 bad credentials')
            elif cmd.startswith('MAIL FROM'):
                rec['from'] = line.split(':', 1)[1].strip(' <>')
                send('250 ok')
            elif cmd.startswith('RCPT TO'):
                rec['rcpt'].append(line.split(':', 1)[1].strip(' <>'))
                send('250 ok')
            elif cmd == 'DATA':
                send('354 go')
                body = []
                while (x := f.readline().decode(errors='replace')) not in ('.\r\n', '.\n', ''):
                    body.append(x)
                rec['tls'] = tls
                rec['subject'] = next(
                    (x.split(':', 1)[1].strip() for x in body if x.lower().startswith('subject:')),
                    '',
                )
                self.log(**rec)
                send('250 queued')
            elif cmd == 'QUIT':
                send('221 bye')
                return
            else:
                send('250 ok' if cmd.startswith(('RSET', 'NOOP')) else '502 no')

    def imap(self, sock, conf, port):
        f = sock.makefile('rwb')
        f.write(b'* OK mock IMAP\r\n')
        f.flush()
        while line := f.readline().decode(errors='replace').rstrip('\r\n'):
            tag, cmd, *rest = line.split(' ', 2) + ['']
            cmd = cmd.upper()
            if cmd == 'LOGIN':
                ok = re.findall(r'"((?:[^"\\]|\\.)*)"', rest[0])[1:2] == [conf.get('password')]
                self.log(port=port, login=ok)
                f.write(
                    ('%s %s\r\n' % (tag, 'OK logged in' if ok else 'NO bad credentials')).encode()
                )
            elif cmd == 'LIST':
                f.write(
                    b''.join(
                        b'* LIST () "/" "box%d"\r\n' % i for i in range(conf.get('folders', 1))
                    )
                )
                f.write(('%s OK\r\n' % tag).encode())
            elif cmd == 'LOGOUT':
                f.write(('* BYE\r\n%s OK\r\n' % tag).encode())
                f.flush()
                return
            else:
                f.write(('%s BAD\r\n' % tag).encode())
            f.flush()


if __name__ == '__main__' and sys.argv[1:2] == ['--mock']:
    Mock(json.load(open(sys.argv[2]))).run()
elif __name__ == '__main__':
    unittest.main()
