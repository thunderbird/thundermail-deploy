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
FAKES = ('kubectl', 'gh', 'aws', 'stalwart-cli', 'podman', 'rclone')
M = 'mail.stage-thundermail.com'
FAKE = """#!{py}
import json, os, re, sys, time
name, args = os.path.basename(sys.argv[0]), ' '.join(sys.argv[1:])
open(os.environ['FAKE_LOG'], 'a').write(name + ' ' + args + '\\n')
for rule in json.load(open(os.environ['FAKE_RULES'])):
    if rule['tool'] == name and re.search(rule['match'], args):
        time.sleep(rule.get('sleep', 0))
        sys.stdout.write(rule.get('out', ''))
        sys.exit(rule.get('rc', 0))
sys.exit(1)
"""
FAKE_DATE = """#!{bash}
[ "$*" = "+%s" ] || exec {date} "$@"
n=$(( $(cat "$FAKE_CLOCK" 2>/dev/null || echo 1800000000) + 30 ))
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
        (bins / 'sleep').write_text('#!%s\nexit 0\n' % shutil.which('bash'))
        for t in ('date', 'sleep'):
            (bins / t).chmod(0o755)
        ext = r / 'ext'
        ext.write_text(
            'subjectAltName=DNS:*.stage-thundermail.com,DNS:%s,IP:1.1.1.1,IP:127.0.0.1\n' % M
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
        pre = '%s link set lo up; %s addr add 1.1.1.1/32 dev lo\n' % (
            shutil.which('ip'),
            shutil.which('ip'),
        )
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
            [shutil.which('unshare'), '-rn', shutil.which('bash'), str(t / 'driver.sh')],
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
                for key in mock.cfg.get('https', {}):
                    if self.path.startswith(key):
                        r = mock.next(key)
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
