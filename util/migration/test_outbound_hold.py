import calendar
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import time
import unittest

HERE = pathlib.Path(__file__).resolve().parent
SG = 'sg-0test0000000000001'
OPEN = {
    'SecurityGroupRuleId': 'sgr-open',
    'IsEgress': True,
    'IpProtocol': '-1',
    'FromPort': -1,
    'ToPort': -1,
    'CidrIpv4': '0.0.0.0/0',
    'Description': 'Allow all egress',
}
INGRESS = {
    'SecurityGroupRuleId': 'sgr-in',
    'IsEgress': False,
    'IpProtocol': 'tcp',
    'FromPort': 8080,
    'ToPort': 8080,
    'ReferencedGroupInfo': {'GroupId': SG},
}
CLOSED = {('tcp', 0, 24), ('tcp', 26, 65535), ('udp', 0, 65535), ('icmp', -1, -1)}
NODES = ['i-0node%d' % n for n in range(6)]
NODE_NAMES = ['mailstrom-prod-stalwart-%s' % n for n in (10, 11, 12, 13, 60, 61)]
LEGACY_DESC = 'Allow traffic from the instance out to the Internet'
PW = 'target-admin-pw-7f3a'
LEGACY_PW = 'legacy-admin-pw-91c2'

AWS = r"""#!{py}
import json, os, sys, time
args = sys.argv[1:]
open(os.environ['FAKE_LOG'], 'a').write('aws ' + ' '.join(args) + '\n')
p = os.environ['STATE']
st = json.load(open(p))
def arg(name):
    return args[args.index(name) + 1]
def multi(name):
    i = args.index(name) + 1
    out = []
    while i < len(args) and not args[i].startswith('--'):
        out.append(args[i]); i += 1
    return out
a = ' '.join(args)
for f in st.get('fail', []):
    if f in a:
        sys.stderr.write('An error occurred (Fake)\n'); sys.exit(255)
out = None
if 'eks describe-cluster' in a:
    out = st['cluster_vpc']
elif 'ec2 describe-security-groups' in a:
    out = st['sg_vpc']
elif 'ec2 describe-security-group-rules' in a:
    g = arg('--filters').split('Values=')[1]
    out = json.dumps({{'SecurityGroupRules': st['rules'].get(g, [])}})
elif 'ec2 authorize-security-group-egress' in a:
    g = arg('--group-id')
    for perm in json.loads(arg('--ip-permissions')):
        if perm['IpProtocol'] == '-1':
            at = time.time() + float(open(os.environ['FAKE_CLOCK']).read() or 0)
            for m in st.get('on_open', {{}}).values():
                if m['createdAt'] == 'now':
                    m['createdAt'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(at))
            st['queue'].update(st.pop('on_open', {{}}))
            for i in st.pop('attempts', {{}}):
                st.setdefault('delivered', []).append(i)
                st['queue'].pop(i, None)
        st['seq'] = st.get('seq', 0) + 1
        rng = perm['IpRanges'][0]
        r = {{'SecurityGroupRuleId': 'sgr-new%d' % st['seq'], 'IsEgress': True,
             'IpProtocol': perm['IpProtocol'],
             'FromPort': perm.get('FromPort', -1), 'ToPort': perm.get('ToPort', -1),
             'CidrIpv4': rng['CidrIp'], 'Description': rng.get('Description', '')}}
        st['rules'].setdefault(g, []).append(r)
    out = 'True'
elif 'ec2 revoke-security-group-egress' in a:
    g = arg('--group-id')
    ids = multi('--security-group-rule-ids')
    st['rules'][g] = [r for r in st['rules'][g] if r['SecurityGroupRuleId'] not in ids]
    out = 'True'
elif 'ec2 describe-network-interfaces' in a:
    g = arg('--filters').split('Values=')[1]
    enis = [{{'NetworkInterfaceId': 'eni-' + i['InstanceId'],
              'Attachment': {{'InstanceId': i['InstanceId']}}}}
            for i in st['instances'] for n in i['NetworkInterfaces']
            if any(x['GroupId'] == g for x in n['Groups'])]
    enis += [e for e in st.get('extra_enis', []) if e['group'] == g]
    out = json.dumps({{'NetworkInterfaces': enis}})
elif 'ec2 describe-instances' in a:
    ids = multi('--instance-ids')
    found = [i for i in st['instances'] if i['InstanceId'] in ids]
    out = json.dumps({{'Reservations': [{{'Instances': found}}]}})
else:
    sys.exit(254)
json.dump(st, open(p, 'w'))
print(out)
"""

KUBE = r"""#!{py}
import json, os, sys
args = sys.argv[1:]
open(os.environ['FAKE_LOG'], 'a').write('kubectl ' + ' '.join(args) + '\n')
st = json.load(open(os.environ['STATE']))
a = ' '.join(args)
if 'port-forward' in args:
    sys.exit(0)
if 'get securitygrouppolicy stalwart-server -o json' in a:
    print(json.dumps({{'spec': {{'securityGroups': {{'groupIds': st['sgp']}}}}}}))
elif 'get ds aws-node -o json' in a:
    env = [{{'name': 'POD_SECURITY_GROUP_ENFORCING_MODE', 'value': st['sg_mode']}},
           {{'name': 'ENABLE_POD_ENI', 'value': st.get('pod_eni', 'true')}}]
    pod = {{'containers': [{{'name': 'aws-node', 'env': env}}]}}
    print(json.dumps({{'spec': {{'template': {{'spec': pod}}}}}}))
elif '-l app=stalwart' in a and '-o json' in a:
    items = []
    for n in st.get('pods', []):
        ann = {{'vpc.amazonaws.com/pod-eni': '[{{"eniId":"eni-b"}}]'}}
        if n in st.get('no_eni', []):
            ann = {{}}
        phase = st.get('phase', {{}}).get(n, 'Running')
        meta = {{'name': n, 'annotations': ann}}
        items.append({{'metadata': meta, 'status': {{'phase': phase}}}})
    print(json.dumps({{'items': items}}))
else:
    sys.exit(1)
"""

CURL = r"""#!{py}
import json, os, sys, time
args = sys.argv[1:]
open(os.environ['FAKE_LOG'], 'a').write('curl ' + ' '.join(args) + '\n')
p = os.environ['STATE']
st = json.load(open(p))
url = [x for x in args if x.startswith('http')][-1]
if url.endswith('/healthz/live'):
    sys.exit(0)
user = ''
if '-K' in args:
    for line in open(args[args.index('-K') + 1]):
        if line.startswith('user = '):
            user = line.split('"')[1]
st.setdefault('users', []).append(user)
def iso(t):
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(t))
out = None
if '/api/queue/messages' in url:
    out = {{'data': {{'total': st['legacy_queue']}}}}
elif url.endswith('/jmap/session'):
    out = {{'primaryAccounts': {{'urn:stalwart:jmap': 'acc1'}}}}
elif url.endswith('/jmap'):
    req = json.loads(args[args.index('--data') + 1])
    m, a, tag = req['methodCalls'][0]
    q = st['queue']
    if m == 'x:QueuedMessage/query':
        ids = sorted(q)[a.get('position', 0):a.get('position', 0) + a.get('limit', 500)]
        res = {{'ids': ids}}
    elif m == 'x:QueuedMessage/get':
        res = {{'list': [dict(q[i], id=i) for i in a['ids'] if i in q]}}
    elif m == 'x:QueuedMessage/set':
        if st.get('set_error'):
            bad = {{k: {{'type': st['set_error']}} for k in a['update']}}
            res = {{'updated': {{}}, 'notUpdated': bad}}
        else:
            for k, v in a['update'].items():
                for r in [] if st.get('set_noop') else q[k]['recipients'].values():
                    if r['status']['@type'] != 'PermanentFailure':
                        r['retryDue'] = v['nextRetry']
            res = {{'updated': {{k: None for k in a['update']}}}}
    out = {{'methodResponses': [[m, res, tag]]}}
json.dump(st, open(p, 'w'))
print(json.dumps(out))
"""

DATE = r"""#!{py}
import os, sys, time
t = time.time() + float(open(os.environ['FAKE_CLOCK']).read() or 0)
args = sys.argv[1:]
fmt = '%a %b %d %H:%M:%S UTC %Y'
i = 0
while i < len(args):
    a = args[i]
    if a == '-d':
        d = args[i + 1]
        i += 1
        if d.startswith('@'):
            t = float(d[1:])
        else:
            n, unit = d.lstrip('+').split()
            t += int(n) * {{'hours': 3600, 'minutes': 60, 'seconds': 1}}[unit]
    elif a.startswith('+'):
        fmt = a[1:]
    elif a != '-u':
        sys.exit(1)
    i += 1
print(time.strftime(fmt.replace('%s', str(int(t))), time.gmtime(t)))
"""

SLEEP = r"""#!{py}
import calendar, copy, json, os, sys, time
n = float(sys.argv[1])
c = os.environ['FAKE_CLOCK']
clock = float(open(c).read() or 0) + n
open(c, 'w').write(str(clock))
p = os.environ['STATE']
st = json.load(open(p))
st.setdefault('sleeps', []).append(n)
now = time.time() + clock
def iso(t):
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(t))
def due(r):
    return calendar.timegm(time.strptime(r['retryDue'], '%Y-%m-%dT%H:%M:%SZ'))
def pending(m):
    live = ('Scheduled', 'TemporaryFailure')
    return [r for r in m['recipients'].values() if r['status']['@type'] in live]
q = st['queue']
att = st.setdefault('attempts', {{}})
for i in list(att):
    att[i]['left'] -= n
    if att[i]['left'] <= 0:
        m = att.pop(i)['snap']
        for r in pending(m):
            r['retryDue'] = iso(now + 120)
            r['status'] = {{'@type': 'TemporaryFailure'}}
        if i in q:
            q[i] = m
        st.setdefault('ended', []).append(i)
for i, m in st.pop('arrive', {{}}).items():
    m['createdAt'] = iso(now)
    q[i] = m
if st.get('scheduler'):
    for i, m in q.items():
        if i not in att and any(r['queueName'] != 'local' and due(r) <= now for r in pending(m)):
            att[i] = {{'left': st['attempt_secs'], 'snap': copy.deepcopy(m)}}
            st.setdefault('started', []).append(i)
json.dump(st, open(p, 'w'))
"""

SS = r"""#!{py}
import json, os, sys
st = json.load(open(os.environ['STATE']))
port = sys.argv[-1].rsplit(':', 1)[-1]
if port in st.get('ss', []):
    print('LISTEN 0 4096 127.0.0.1:%s 0.0.0.0:* users:(("session-manager",pid=4242,fd=9))' % port)
"""


def iso(t):
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(t))


def msg(due, queue='remote', status='TemporaryFailure'):
    return {
        'createdAt': iso(time.time() - 600),
        'recipients': {
            'rcpt@example.test': {
                'retryDue': iso(due),
                'status': {'@type': status},
                'queueName': queue,
            }
        },
    }


class Base(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp(prefix='.test-outbound-', dir=HERE.parent.parent))
        self.root.chmod(0o700)
        self.mig = self.root / 'mig'
        shutil.copytree(HERE, self.mig, ignore=shutil.ignore_patterns('__pycache__', '.test-*'))
        p = self.mig / 'targets' / 'tb-prod.sh'
        self.targets = p.read_text()
        p.write_text(self.targets.replace('REPLACE_ME_', 'filled_'))
        self.stubs = self.root / 'stubs'
        self.stubs.mkdir()
        for name, body in (
            ('aws', AWS),
            ('kubectl', KUBE),
            ('curl', CURL),
            ('ss', SS),
            ('date', DATE),
            ('sleep', SLEEP),
        ):
            (self.stubs / name).write_text(body.format(py=shutil.which('python3')))
            (self.stubs / name).chmod(0o755)
        (self.root / 'home').mkdir()
        self.ev = self.root / 'ev'
        self.log = self.root / 'fake.log'
        self.log.write_text('')
        self.clock = self.root / 'clock'
        self.clock.write_text('0')
        self.kc = self.root / 'kc'
        self.kc.write_text(
            'apiVersion: v1\ncurrent-context: prod\ncontexts:\n- name: prod\n  context:\n'
            '    cluster: arn:aws:eks:eu-central-1:1:cluster/mzla-eks-tb-prod01\n'
        )
        shm = self.root / 'shm'
        shm.mkdir(mode=0o700)
        self.envf = shm / 'cutover-tb-prod.env'
        self.envf.write_text(
            '\n'.join(
                [
                    'TARGET_ENV=tb-prod',
                    'TARGET_STALWART_SG=%s' % SG,
                    'KUBECONFIG_TARGET=%s' % self.kc,
                    'STALWART_URL=http://localhost:18081',
                    'STALWART_USER=admin-user',
                    'STALWART_PASSWORD=%s' % PW,
                    "LEGACY_STALWART_NODE_IDS='%s'" % ' '.join(NODES),
                    'OLD_STALWART_URL=https://127.0.0.1:18080',
                    'OLD_STALWART_USER=admin',
                    'OLD_STALWART_PASSWORD=%s' % LEGACY_PW,
                ]
            )
            + '\n'
        )
        self.envf.chmod(0o600)
        self.state_path = self.root / 'state.json'
        legacy_rules = {}
        instances = []
        for i, (nid, name) in enumerate(zip(NODES, NODE_NAMES)):
            g = 'sg-0node%d' % i
            legacy_rules[g] = [
                {
                    'SecurityGroupRuleId': 'sgr-legacy%d' % i,
                    'IsEgress': True,
                    'IpProtocol': 'tcp',
                    'FromPort': 0,
                    'ToPort': 65535,
                    'CidrIpv4': '0.0.0.0/0',
                    'Description': LEGACY_DESC,
                },
                dict(INGRESS, SecurityGroupRuleId='sgr-legacy-in%d' % i),
            ]
            instances.append(
                {
                    'InstanceId': nid,
                    'Tags': [{'Key': 'Name', 'Value': name}],
                    'NetworkInterfaces': [{'Groups': [{'GroupId': g}]}],
                }
            )
        self.save(
            {
                'cluster_vpc': 'vpc-prod',
                'sg_vpc': 'vpc-prod',
                'sgp': [SG],
                'sg_mode': 'strict',
                'pods': [],
                'rules': dict(legacy_rules, **{SG: [OPEN, INGRESS]}),
                'instances': instances,
                'queue': {},
                'legacy_queue': 0,
                'ss': ['18080'],
            }
        )

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def state(self):
        return json.loads(self.state_path.read_text())

    def save(self, st):
        self.state_path.write_text(json.dumps(st))

    def edit(self, **kw):
        st = self.state()
        st.update(kw)
        self.save(st)

    def run_tool(self, tool, *args, target='tb-prod', **extra):
        env = {
            'QUEUE_DRAIN_POLL_SECONDS': '300',
            'PATH': '%s:%s' % (self.stubs, os.environ['PATH']),
            'HOME': str(self.root / 'home'),
            'TARGET_ENV': target,
            'CUTOVER_ENV': str(self.envf),
            'CUTOVER_ATTEMPT': '1',
            'EVIDENCE_DIR': str(self.ev),
            'SECRET_DIR': str(self.root / 'secret'),
            'FAKE_LOG': str(self.log),
            'STATE': str(self.state_path),
            'FAKE_CLOCK': str(self.clock),
            **extra,
        }
        return subprocess.run(
            ['bash', str(self.mig / 'window' / tool), *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )

    def rows(self):
        f = self.ev / 'timing-tb-prod.tsv'
        return [line.split('\t')[0] for line in f.read_text().splitlines()] if f.exists() else []

    def add_row(self, name):
        self.ev.mkdir(exist_ok=True)
        with open(self.ev / 'timing-tb-prod.tsv', 'a') as f:
            f.write('%s\t2026-11-19T00:00:00Z\t2026-11-19T00:00:00Z\t\t\n' % name)

    def egress(self, g=SG):
        return {
            (r['IpProtocol'], r['FromPort'], r['ToPort'])
            for r in self.state()['rules'][g]
            if r['IsEgress']
        }

    def writes(self):
        return [
            line
            for line in self.log.read_text().splitlines()
            if 'authorize-' in line or 'revoke-' in line
        ]


class Hold25(Base):
    def test_set_dry_run_prints_the_writes_and_writes_nothing(self):
        p = self.run_tool('hold25.sh', 'set')
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn(
            '+ aws --profile mzla-tb-prod --region eu-central-1 ec2 '
            'authorize-security-group-egress --group-id %s --ip-permissions' % SG,
            p.stdout,
        )
        self.assertIn(
            'revoke-security-group-egress --group-id %s --security-group-rule-ids sgr-open' % SG,
            p.stdout,
        )
        self.assertIn('port25: open on %s' % SG, p.stdout)
        self.assertIn('HOLD25 dry-run: done', p.stdout)
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.egress(), {('-1', -1, -1)})
        self.assertNotIn('hold25-set', self.rows())

    def test_set_apply_closes_port_25_and_reads_it_back(self):
        p = self.run_tool('hold25.sh', 'set', '--apply')
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.egress(), CLOSED)
        self.assertIn('read back: port25: closed on %s' % SG, p.stdout)
        w = self.writes()
        self.assertEqual(len(w), 2)
        self.assertIn('authorize-security-group-egress', w[0])
        self.assertIn('revoke-security-group-egress', w[1])
        self.assertIn('hold25-set', self.rows())
        self.assertIn(
            ('tcp', 8080, 8080),
            {(r['IpProtocol'], r['FromPort'], r['ToPort']) for r in self.state()['rules'][SG]},
        )
        again = self.run_tool('hold25.sh', 'set', '--apply')
        self.assertEqual(again.returncode, 0, again.stdout)
        self.assertIn('nothing to write', again.stdout)
        self.assertEqual(len(self.writes()), 2)

    def test_set_apply_failed_authorize_keeps_the_open_rule_and_writes_no_row(self):
        self.edit(fail=['authorize-security-group-egress'])
        p = self.run_tool('hold25.sh', 'set', '--apply')
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(self.egress(), {('-1', -1, -1)})
        self.assertFalse(any('revoke-' in line for line in self.writes()))
        self.assertNotIn('hold25-set', self.rows())
        self.assertIn('stop-HOLD25', self.rows())

    def test_set_refuses_an_egress_rule_it_did_not_write(self):
        st = self.state()
        st['rules'][SG].append(
            dict(OPEN, SecurityGroupRuleId='sgr-x', IpProtocol='tcp', FromPort=443, ToPort=443)
        )
        self.save(st)
        p = self.run_tool('hold25.sh', 'set', '--apply')
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('sgr-x tcp|443|443|0.0.0.0/0', p.stdout)
        self.assertEqual(self.writes(), [])

    def test_apply_refuses_another_target(self):
        for kw, text in (
            ({'sgp': ['sg-other']}, "SecurityGroupPolicy stalwart-server lists 'sg-other'"),
            ({'sg_vpc': 'vpc-other'}, 'is in VPC vpc-other'),
            ({'sg_mode': 'standard'}, "POD_SECURITY_GROUP_ENFORCING_MODE is 'standard'"),
        ):
            with self.subTest(kw=kw):
                self.tearDown()
                self.setUp()
                self.edit(**kw)
                p = self.run_tool('hold25.sh', 'set', '--apply')
                self.assertNotEqual(p.returncode, 0)
                self.assertIn(text, p.stdout)
                self.assertEqual(self.writes(), [])
                self.assertEqual(self.egress(), {('-1', -1, -1)})
        self.kc.write_text(self.kc.read_text().replace('tb-prod01', 'tb-stage01'))
        p = self.run_tool('hold25.sh', 'set', '--apply')
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(self.writes(), [])
        p = self.run_tool('hold25.sh', 'set', '--apply', target='tb-stage')
        self.assertEqual(p.returncode, 64)
        self.assertEqual(self.writes(), [])

    def test_refuses_pods_the_security_group_does_not_cover(self):
        self.edit(pod_eni='false')
        p = self.run_tool('hold25.sh', 'set', '--apply')
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('aws-node ENABLE_POD_ENI=false', p.stdout)
        self.assertEqual(self.writes(), [])
        self.edit(pod_eni='true', pods=['stalwart-a', 'stalwart-b'], no_eni=['stalwart-b'])
        p = self.run_tool('hold25.sh', 'read')
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('pods without a branch ENI', p.stdout)
        self.assertIn('  stalwart-b', p.stdout)
        self.edit(no_eni=[], phase={'stalwart-a': 'Pending', 'stalwart-b': 'Pending'})
        p = self.run_tool('hold25.sh', 'read')
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertIn('stalwart pods: 2 (Running 0)', p.stdout)
        self.assertIn('queue: not read (no Running Stalwart pod)', p.stdout)

    def test_queue_credentials_go_to_a_loopback_url_only(self):
        self.queue()
        self.envf.write_text(
            self.envf.read_text().replace('localhost:18081', 'stalwart.example.test:18081')
        )
        p = self.run_tool('hold25.sh', 'queue-hold', '--apply')
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('is not loopback: credentials not sent', p.stdout)
        self.assertNotIn('users', self.state())

    def test_apply_refuses_while_targets_hold_a_placeholder(self):
        (self.mig / 'targets' / 'tb-prod.sh').write_text(self.targets)
        p = self.run_tool('hold25.sh', 'set', '--apply')
        self.assertEqual(p.returncode, 64)
        self.assertEqual(self.writes(), [])
        (self.mig / 'targets' / 'tb-prod.sh').write_text(
            self.targets.replace('REPLACE_ME_', 'filled_')
        )
        self.assertEqual(self.run_tool('hold25.sh', 'set', '--apply').returncode, 0)

    def test_read_without_a_pod_reads_the_rules_only(self):
        p = self.run_tool('hold25.sh', 'read')
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn('port25: open on %s' % SG, p.stdout)
        self.assertIn('queue: not read (no Running Stalwart pod)', p.stdout)
        self.run_tool('hold25.sh', 'set', '--apply')
        p = self.run_tool('hold25.sh', 'read')
        self.assertIn('port25: closed on %s' % SG, p.stdout)
        self.assertEqual(self.rows(), ['hold25-set'])

    def queue(self):
        now = time.time()
        self.edit(
            pods=['stalwart-a', 'stalwart-b'],
            queue={
                '11': msg(now - 60),
                '12': msg(now + 120, status='Scheduled'),
                '13': msg(now - 60, queue='local'),
                '14': msg(now - 60, status='PermanentFailure'),
            },
        )

    def due(self, i):
        r = self.state()['queue'][i]['recipients']['rcpt@example.test']['retryDue']
        return calendar.timegm(time.strptime(r, '%Y-%m-%dT%H:%M:%SZ'))

    def test_queue_hold_holds_remote_mail_only_and_reads_it_back(self):
        self.queue()
        dry = self.run_tool('hold25.sh', 'queue-hold')
        self.assertEqual(dry.returncode, 0, dry.stdout + dry.stderr)
        self.assertIn('x:QueuedMessage/set nextRetry=', dry.stdout)
        self.assertIn('ids=11,12', dry.stdout)
        self.assertLess(self.due('11'), time.time())
        p = self.run_tool('hold25.sh', 'queue-hold', '--apply')
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn('read back: queue: total=4 remote=2 held=2 not_held=0', p.stdout)
        for i in ('11', '12'):
            self.assertGreater(self.due(i), time.time() + 23 * 3600)
        self.assertLess(self.due('13'), time.time())
        self.assertIn('hold25-queue-hold', self.rows())
        self.assertIn('queue hold: on since', p.stdout)
        users = set(self.state()['users'])
        self.assertEqual(users, {'admin-user:' + PW})
        self.assertNotIn(PW, self.log.read_text())
        self.assertNotIn(PW, p.stdout)
        self.assertNotIn('rcpt@example.test', p.stdout)

    def test_queue_hold_stops_when_stalwart_refuses_the_update(self):
        self.queue()
        self.edit(set_error='forbidden')
        p = self.run_tool('hold25.sh', 'queue-hold', '--apply')
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('not updated:', p.stdout)
        self.assertNotIn('hold25-queue-hold', self.rows())

    def test_queue_hold_stops_when_the_read_back_shows_mail_not_held(self):
        self.queue()
        self.edit(set_noop=True)
        p = self.run_tool('hold25.sh', 'queue-hold', '--apply')
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('read back: 2 queued message(s) not held', p.stdout)
        self.assertNotIn('hold25-queue-hold', self.rows())

    def test_release_port25_needs_the_queue_hold_row_and_the_go(self):
        self.run_tool('hold25.sh', 'set', '--apply')
        self.queue()
        p = self.run_tool('hold25.sh', 'release-port25', '--apply', '--go', 'go 7.5')
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('no hold25-queue-hold row', p.stdout)
        self.assertEqual(self.egress(), CLOSED)
        self.add_row('hold25-queue-hold')
        p = self.run_tool('hold25.sh', 'release-port25', '--apply')
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('no operator go', p.stdout)
        self.assertEqual(self.egress(), CLOSED)
        dry = self.run_tool('hold25.sh', 'release-port25')
        self.assertEqual(dry.returncode, 0, dry.stdout)
        self.assertEqual(self.egress(), CLOSED)
        p = self.run_tool('hold25.sh', 'release-port25', '--apply', '--go', 'go 7.5')
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.egress(), {('-1', -1, -1)})
        self.assertEqual(
            [r['Description'] for r in self.state()['rules'][SG] if r['IsEgress']],
            ['Allow all egress'],
        )
        self.assertIn('read back: port25: open on %s' % SG, p.stdout)
        self.assertIn('read back: queue: total=4 remote=2 held=2 not_held=0', p.stdout)
        w = [line for line in self.writes() if 'release' not in line][-2:]
        self.assertIn('authorize-', w[0])
        self.assertIn('revoke-', w[1])
        self.assertEqual(self.rows()[-2:], ['go-HOLD25', 'hold25-release-port25'])

    def test_release_queue_releases_a_hold_older_than_the_held_threshold(self):
        now = time.time()
        self.edit(pods=['stalwart-a'], queue={'21': msg(now + 6 * 3600), '22': msg(now - 60)})
        self.add_row('hold25-release-port25')
        p = self.run_tool('hold25.sh', 'release-queue', '--apply', '--go', 'go 7.7')
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn('ids=21\n', p.stdout)
        self.assertLess(self.due('21'), time.time() + 60)

    def test_release_port25_leaves_mail_queued_after_the_release_unheld(self):
        self.run_tool('hold25.sh', 'set', '--apply')
        self.queue()
        self.add_row('hold25-queue-hold')
        self.assertEqual(self.run_tool('hold25.sh', 'queue-hold', '--apply').returncode, 0)
        late = msg(time.time() - 60)
        late['createdAt'] = iso(time.time() + 7200)
        self.edit(on_open={'15': late})
        p = self.run_tool('hold25.sh', 'release-port25', '--apply', '--go', 'go 7.5')
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn('held=2 not_held=1 queued_after_release=1', p.stdout)
        self.assertLess(self.due('15'), time.time())
        self.assertGreater(self.due('11'), time.time() + 23 * 3600)

    def fake_now(self):
        return time.time() + float(self.clock.read_text())

    def held_for_release(self):
        self.run_tool('hold25.sh', 'set', '--apply')
        self.queue()
        self.add_row('hold25-queue-hold')
        self.assertEqual(self.run_tool('hold25.sh', 'queue-hold', '--apply').returncode, 0)

    def in_flight(self, i, left, snap=None):
        st = self.state()
        st.setdefault('attempts', {})[i] = {'left': left, 'snap': snap or msg(time.time() - 60)}
        self.save(st)

    def test_release_port25_waits_out_an_attempt_that_overwrites_the_hold(self):
        self.held_for_release()
        self.in_flight('11', 200)
        self.edit(scheduler=True, attempt_secs=200)
        p = self.run_tool(
            'hold25.sh',
            'release-port25',
            '--apply',
            '--go',
            'go 7.5',
            QUEUE_DRAIN_POLL_SECONDS='60',
        )
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        st = self.state()
        self.assertEqual(st.get('delivered', []), [])
        self.assertEqual(st['ended'], ['11'])
        self.assertEqual(st.get('started', []), [])
        self.assertIn(
            'drain: held again, overwritten by a delivery attempt that ended: ids=11', p.stdout
        )
        self.assertIn('drain: unproven=0', p.stdout)
        self.assertGreaterEqual(sum(st['sleeps']), 200 + 1359)
        self.assertIn('read back: port25: open on %s' % SG, p.stdout)
        self.assertIn('read back: queue: total=4 remote=2 held=2 not_held=0', p.stdout)
        for i in ('11', '12'):
            self.assertGreater(self.due(i), self.fake_now() + 23 * 3600)
        self.assertLess(p.stdout.index('drain: done'), p.stdout.index('authorize-'))
        self.assertEqual(self.rows()[-2:], ['go-HOLD25', 'hold25-release-port25'])

    def test_release_port25_waits_longer_for_mail_to_several_domains(self):
        self.held_for_release()
        st = self.state()
        for i in ('11', '12'):
            r = st['queue'][i]['recipients']
            r['rcpt@other.test'] = dict(r['rcpt@example.test'])
        self.save(st)
        self.in_flight('12', 2000)
        p = self.run_tool('hold25.sh', 'release-port25', '--apply', '--go', 'go 7.5')
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.state().get('delivered', []), [])
        self.assertRegex(p.stdout, r'longest 27(19|20) s')
        self.assertIn('ids=12', p.stdout)
        self.assertGreater(self.due('12'), self.fake_now() + 23 * 3600)

    def test_release_port25_opens_at_once_for_mail_held_long_before(self):
        self.run_tool('hold25.sh', 'set', '--apply')
        now = time.time()
        self.edit(
            pods=['stalwart-a'], queue={'31': msg(now + 22 * 3600), '32': msg(now + 23 * 3600)}
        )
        self.add_row('hold25-queue-hold')
        p = self.run_tool('hold25.sh', 'release-port25', '--apply', '--go', 'go 7.5')
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn(
            'drain: queued before the drain: 2 remote, 0 still waiting (longest 0 s)', p.stdout
        )
        self.assertEqual(self.state().get('sleeps', []), [])
        self.assertNotIn('x:QueuedMessage/set', p.stdout)

    def test_release_port25_counts_mail_queued_during_the_drain_as_unproven(self):
        self.held_for_release()
        self.edit(arrive={'16': msg(time.time() - 60)})
        p = self.run_tool('hold25.sh', 'release-port25', '--apply', '--go', 'go 7.5')
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn('queued since: 1', p.stdout)
        self.assertIn('ids=16', p.stdout)
        self.assertIn('drain: unproven=1', p.stdout)
        self.assertGreater(self.due('16'), self.fake_now() + 23 * 3600)

    def test_release_port25_counts_mail_queued_between_the_drain_and_the_swap(self):
        self.held_for_release()
        gap = msg(time.time() - 60)
        gap['createdAt'] = 'now'
        self.edit(on_open={'17': gap})
        p = self.run_tool('hold25.sh', 'release-port25', '--apply', '--go', 'go 7.5')
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn('drain: unproven=0', p.stdout)
        self.assertIn(
            'release: 1 message(s) queued between the end of the drain and the port opening',
            p.stdout,
        )
        self.assertGreater(self.due('17'), self.fake_now() + 23 * 3600)

    def test_release_port25_keeps_port_25_closed_when_the_drain_runs_out(self):
        self.held_for_release()
        self.in_flight('11', 200)
        self.edit(scheduler=True, attempt_secs=200)
        p = self.run_tool(
            'hold25.sh',
            'release-port25',
            '--apply',
            '--go',
            'go 7.5',
            QUEUE_DRAIN_MAX_SECONDS='600',
            QUEUE_DRAIN_POLL_SECONDS='60',
        )
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('message(s) not proven held after 600 s; port 25 stays closed', p.stdout)
        self.assertEqual(self.egress(), CLOSED)
        self.assertEqual(self.state().get('delivered', []), [])
        self.assertNotIn('hold25-release-port25', self.rows())

    def test_release_port25_dry_run_prints_the_drain_and_does_not_wait(self):
        self.held_for_release()
        p = self.run_tool('hold25.sh', 'release-port25')
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertRegex(
            p.stdout, r'drain: --apply would wait up to 13(59|60) s, read the queue every 300 s'
        )
        self.assertEqual(self.state().get('sleeps', []), [])
        self.assertEqual(self.egress(), CLOSED)

    def test_release_queue_needs_the_port_release_row_and_releases_every_held_message(self):
        self.queue()
        self.add_row('hold25-queue-hold')
        self.assertEqual(self.run_tool('hold25.sh', 'queue-hold', '--apply').returncode, 0)
        p = self.run_tool('hold25.sh', 'release-queue', '--apply', '--go', 'go 7.7')
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('no hold25-release-port25 row', p.stdout)
        self.assertGreater(self.due('11'), time.time() + 23 * 3600)
        self.add_row('hold25-release-port25')
        p = self.run_tool('hold25.sh', 'release-queue', '--apply', '--go', 'go 7.7')
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn('read back: queue: total=4 remote=2 held=0 not_held=2', p.stdout)
        for i in ('11', '12'):
            self.assertLess(self.due(i), time.time() + 60)
        self.assertEqual(self.rows()[-1], 'hold25-release-queue')
        self.assertEqual(self.writes(), [])
        self.assertIn('queue hold: off', self.run_tool('hold25.sh', 'read').stdout)


class Block25(Base):
    def test_block_dry_run_prints_the_writes_and_names_its_gates(self):
        p = self.run_tool('block25.sh', 'block')
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        for i in range(6):
            self.assertIn(
                '+ aws --profile mzla-legacy --region eu-central-1 ec2 '
                'authorize-security-group-egress --group-id sg-0node%d' % i,
                p.stdout,
            )
            self.assertIn(
                'revoke-security-group-egress --group-id sg-0node%d '
                '--security-group-rule-ids sgr-legacy%d' % (i, i),
                p.stdout,
            )
        self.assertIn('NEED  row cut-ecs', p.stdout)
        self.assertIn('legacy outbound: blocked=0 open=6 groups=6', p.stdout)
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.rows()[-1:], [])

    def test_block_apply_gates(self):
        p = self.run_tool('block25.sh', 'block', '--apply', '--go', 'go 2.5')
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('row cut-ecs', p.stdout)
        self.add_row('cut-ecs')
        self.edit(legacy_queue=3)
        p = self.run_tool('block25.sh', 'block', '--apply', '--go', 'go 2.5')
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('the legacy queue holds 3 message(s)', p.stdout)
        self.edit(legacy_queue=0, ss=[])
        p = self.run_tool('block25.sh', 'block', '--apply', '--go', 'go 2.5')
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('the legacy queue was not read', p.stdout)
        self.edit(ss=['18080'])
        p = self.run_tool('block25.sh', 'block', '--apply')
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('no operator go', p.stdout)
        self.assertEqual(self.writes(), [])
        self.assertNotIn('legacy-outbound-block', self.rows())

    def test_block_apply_then_restore(self):
        self.add_row('cut-ecs')
        p = self.run_tool('block25.sh', 'block', '--apply', '--go', 'go 2.5')
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn('legacy outbound: blocked=6 open=0 groups=6', p.stdout)
        self.assertEqual(p.stdout.count('legacy queue total=0'), 2)
        for i in range(6):
            self.assertEqual(self.egress('sg-0node%d' % i), {('tcp', 0, 24), ('tcp', 26, 65535)})
        self.assertEqual(self.rows()[-2:], ['go-BLOCK25', 'legacy-outbound-block'])
        self.assertTrue(list(self.ev.glob('legacy-node-sg-before-*.json')))
        self.assertIn('restore --apply', (self.ev / 'block25-restore.txt').read_text())
        self.assertNotIn(LEGACY_PW, self.log.read_text() + p.stdout)
        n = len(self.writes())
        dry = self.run_tool('block25.sh', 'restore')
        self.assertEqual(dry.returncode, 0, dry.stdout)
        self.assertEqual(len(self.writes()), n)
        p = self.run_tool('block25.sh', 'restore', '--apply')
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn('legacy outbound: blocked=0 open=6 groups=6', p.stdout)
        for i in range(6):
            g = 'sg-0node%d' % i
            self.assertEqual(self.egress(g), {('tcp', 0, 65535)})
            self.assertEqual(
                [r['Description'] for r in self.state()['rules'][g] if r['IsEgress']], [LEGACY_DESC]
            )
        w = self.writes()[n:]
        self.assertEqual(len(w), 12)
        for i in range(0, 12, 2):
            self.assertIn('authorize-', w[i])
            self.assertIn('revoke-', w[i + 1])
        self.assertEqual(self.rows()[-1], 'legacy-outbound-restore')
        self.assertNotIn('go-', self.rows()[-1])

    def test_refuses_nodes_outside_the_plan(self):
        st = self.state()
        st['instances'][2]['Tags'][0]['Value'] = 'bastion'
        self.save(st)
        p = self.run_tool('block25.sh', 'read')
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("name 'bastion' does not match", p.stdout)
        self.tearDown()
        self.setUp()
        st = self.state()
        st['instances'][0]['NetworkInterfaces'].append({'Groups': [{'GroupId': 'sg-extra'}]})
        self.save(st)
        p = self.run_tool('block25.sh', 'read')
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('not one', p.stdout)
        self.tearDown()
        self.setUp()
        self.envf.write_text(self.envf.read_text().replace(' %s' % NODES[-1], ''))
        p = self.run_tool('block25.sh', 'read')
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('holds 5 ids', p.stdout)
        p = self.run_tool('block25.sh', 'read', target='tb-stage')
        self.assertEqual(p.returncode, 64)

    def test_refuses_shared_or_repeated_node_groups(self):
        self.edit(
            extra_enis=[
                {
                    'NetworkInterfaceId': 'eni-other',
                    'group': 'sg-0node2',
                    'Attachment': {'InstanceId': 'i-0bastion'},
                }
            ]
        )
        p = self.run_tool('block25.sh', 'block', '--apply', '--go', 'go 2.5')
        self.assertNotEqual(p.returncode, 0)
        self.assertIn(
            'sg-0node2 is also on network interfaces of other instances: eni-other', p.stdout
        )
        self.assertEqual(self.writes(), [])
        self.edit(extra_enis=[])
        st = self.state()
        st['instances'][1]['NetworkInterfaces'][0]['Groups'][0]['GroupId'] = 'sg-0node0'
        self.save(st)
        p = self.run_tool('block25.sh', 'read')
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('share groups (5 distinct)', p.stdout)

    def test_restore_refuses_an_egress_rule_it_did_not_write(self):
        st = self.state()
        st['rules']['sg-0node3'].append(
            dict(OPEN, SecurityGroupRuleId='sgr-x', IpProtocol='udp', FromPort=53, ToPort=53)
        )
        self.save(st)
        p = self.run_tool('block25.sh', 'restore', '--apply')
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('sg-0node3 sgr-x udp|53|53', p.stdout)
        self.assertEqual(self.writes(), [])


if __name__ == '__main__':
    unittest.main()
