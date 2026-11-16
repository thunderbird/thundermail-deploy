#!/usr/bin/env bash
set -euo pipefail

overlay=${1:?usage: $0 <overlay dir>}
engine=${CONTAINER_ENGINE:-$(command -v podman || command -v docker)}
image=${NGINX_IMAGE:-docker.io/library/nginx:1.31}
python=${PYTHON_IMAGE:-docker.io/library/python:3.13-slim}
name=jmap-filter-$$
containers=("$name-edge" "$name-mock" "$name-wsecho" "$name-eventsource")
work=$(mktemp -d)
cleanup() {
  "$engine" kill "${containers[@]}" >/dev/null 2>&1 || true
  "$engine" rm -f "${containers[@]}" >/dev/null 2>&1 || true
  "$engine" network rm "$name" >/dev/null 2>&1 || true
  rm -rf "$work"
}
trap cleanup EXIT

kubectl kustomize "$overlay" > "$work/rendered.yaml"
mkdir -p "$work/etc" "$work/ssl" "$work/py"
yq 'select(.kind == "ConfigMap" and .metadata.name == "nginx-config") | .data["nginx.conf"]' "$work/rendered.yaml" > "$work/etc/nginx.conf"
yq 'select(.kind == "ConfigMap" and .metadata.name == "nginx-config") | .data["jmap.js"]' "$work/rendered.yaml" > "$work/etc/jmap.js"
pp=()
grep -Eq 'listen +443 +ssl +proxy_protocol' "$work/etc/nginx.conf" && pp=(--haproxy-protocol)
push_total=$(grep -oP 'limit_conn jmap_push_total \K[0-9]+' "$work/etc/nginx.conf" | sort -u || true)
[[ $push_total =~ ^[0-9]+$ ]] || { echo "$overlay: expected one jmap_push_total value, got '${push_total}'" >&2; exit 1; }
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj /CN=test -keyout "$work/ssl/tls.key" -out "$work/ssl/tls.crt" 2>/dev/null
for path in $(yq 'select(.kind == "Deployment" and .metadata.name == "nginx") | .spec.template.spec.volumes[].projected.sources[].secret.items[]?.path' "$work/rendered.yaml"); do
  case $path in
    *.crt) cp "$work/ssl/tls.crt" "$work/etc/$path" ;;
    *.key) cp "$work/ssl/tls.key" "$work/etc/$path" ;;
  esac
done
yq -e 'select(.kind == "Deployment" and .metadata.name == "nginx") | .spec.template.spec.volumes[].projected.sources[].configMap.items[]? | select(.key == "jmap.js")' "$work/rendered.yaml" > /dev/null

cat > "$work/mock.conf" <<'EOF'
events { worker_connections 4096; }
http {
  server {
    listen 443 ssl;
    ssl_certificate /etc/nginx/ssl/tls.crt;
    ssl_certificate_key /etc/nginx/ssl/tls.key;
    client_max_body_size 0;
    location / {
      return 200 "UPSTREAM $request_method $request_uri upgrade=[$http_upgrade] auth=[$http_authorization] len=[$content_length] xff=[$http_x_forwarded_for] fwd=[$http_forwarded]";
    }
    location = /jmap/soak {
      proxy_pass http://wsecho:8081;
    }
    location = /jmap/eventsource {
      proxy_pass http://wsecho:8082;
      proxy_buffering off;
      proxy_read_timeout 1h;
    }
    location = /jmap/ws {
      proxy_pass http://wsecho:8080;
      proxy_http_version 1.1;
      proxy_set_header Upgrade $http_upgrade;
      proxy_set_header Connection upgrade;
    }
  }
}
stream {
  server { listen 25; return "x"; }
  server { listen 465; return "x"; }
  server { listen 587; return "x"; }
  server { listen 993; return "x"; }
  server { listen 4190; return "x"; }
}
EOF

cat > "$work/py/wsframe.py" <<'EOF'
import os, struct

def xor(data, key):
    if not key or key == b'\0\0\0\0':
        return data
    return bytes(b ^ key[i % 4] for i, b in enumerate(data))

def frame(opcode, payload, fin=True, mask=True, rsv=0, length=None, key=None):
    """Build one frame. length overrides the declared length; key=b'\\0\\0\\0\\0' skips the XOR (fast soak)."""
    n = len(payload) if length is None else length
    first, bit = (0x80 if fin else 0) | rsv | opcode, 0x80 if mask else 0
    if n < 126:
        header = bytes([first, bit | n])
    elif n < 65536:
        header = bytes([first, bit | 126]) + struct.pack('>H', n)
    else:
        header = bytes([first, bit | 127]) + struct.pack('>Q', n)
    if not mask:
        return header + payload
    key = os.urandom(4) if key is None else key
    return header + key + xor(payload, key)

def read_frame(stream):
    """Read one frame: (fin, opcode, unmasked payload), or None at end of stream."""
    head = stream.read(2)
    if len(head) < 2:
        return None
    n = head[1] & 0x7f
    if n == 126:
        n = struct.unpack('>H', stream.read(2))[0]
    elif n == 127:
        n = struct.unpack('>Q', stream.read(8))[0]
    key = stream.read(4) if head[1] & 0x80 else None
    return bool(head[0] & 0x80), head[0] & 0x0f, xor(stream.read(n), key)
EOF

cat > "$work/py/wsecho.py" <<'EOF'
import base64, hashlib, json, socket, threading, time
from wsframe import frame, read_frame

def headers(stream):
    """Read request headers into a dict of lower-case name -> value."""
    result = {}
    while (line := stream.readline()) not in (b'\r\n', b''):
        name, _, value = line.partition(b':')
        result[name.strip().lower()] = value.strip()
    return result

def handle(conn):
    """8080: WebSocket echo. Replies 'ECHO auth=<Authorization> <first 40 bytes>' per message."""
    stream = conn.makefile('rb')
    request = headers(stream)
    for name in (b'x-forwarded-for', b'forwarded'):
        if name in request:
            print('HEADER', name.decode(), request[name].decode(), flush=True)
    auth = request.get(b'authorization', b'')
    accept = base64.b64encode(hashlib.sha1(request.get(b'sec-websocket-key', b'') + b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest())
    conn.sendall(b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ' + accept + b'\r\n\r\n')
    message = b''
    while (f := read_frame(stream)):
        fin, opcode, data = f
        if opcode == 8:
            conn.sendall(frame(8, data, mask=False))
            break
        if opcode == 9:
            conn.sendall(frame(10, data, mask=False))
            continue
        message += data
        if fin:
            try:
                names = ' '.join(call[0] for call in json.loads(message).get('methodCalls', [])) or '-'
            except ValueError:
                names = 'INVALID'
            print('RECEIVED', names, flush=True)
            conn.sendall(frame(1, b'ECHO auth=' + auth + b' ' + message[:40], mask=False))
            message = b''
    conn.close()

def slow(conn):
    """8081: reads the whole body, waits 2 s, answers 200 'slow'. Holds limit_conn slots for the soak."""
    stream = conn.makefile('rb')
    stream.read(int(headers(stream).get(b'content-length', 0)))
    time.sleep(2)
    conn.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 4\r\nConnection: close\r\n\r\nslow')
    conn.close()

def sse(conn):
    """8082: EventSource. One event at once, a ping after 65 s (past nginx's default 60 s read timeout)."""
    headers(conn.makefile('rb'))
    conn.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-cache\r\nConnection: close\r\n\r\n'
                 b'event: state\ndata: {}\n\n')
    time.sleep(65)
    conn.sendall(b'event: ping\ndata: {}\n\n')
    conn.close()

def serve(port, handler):
    server = socket.create_server(('0.0.0.0', port))
    def accept():
        while True:
            threading.Thread(target=handler, args=(server.accept()[0],), daemon=True).start()
    threading.Thread(target=accept, daemon=True).start()

serve(8080, handle)
serve(8081, slow)
serve(8082, sse)
print('READY', flush=True)
threading.Event().wait()
EOF

cat > "$work/py/client.py" <<'EOF'
import json, os, socket, ssl, struct, sys, threading, time
from wsframe import frame, read_frame

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
failures = echoed = blocked = 0
CLOSED_1008 = (8, b'\x03\xf0')

def report(desc, ok, detail):
    global failures
    failures += 0 if ok else 1
    print(f"{'ok   ' if ok else 'FAIL '} {desc:<45} {detail}")

def tls(timeout=3):
    sock = socket.create_connection(('edge', 443), timeout=timeout)
    if os.environ.get('PROXY_PROTOCOL') == '1':
        src, dst = sock.getsockname(), sock.getpeername()
        sock.sendall(f'PROXY TCP4 {src[0]} {dst[0]} {src[1]} {dst[1]}\r\n'.encode())
    return ctx.wrap_socket(sock)

def http_post(path, credential, body, timeout):
    """POST on a fresh connection; returns the raw response, or b'error <exception>'."""
    try:
        s = tls(timeout)
        s.sendall(b'POST %s HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nConnection: close\r\n'
                  b'Authorization: Bearer %s\r\nContent-Length: %d\r\n\r\n' % (path, credential, len(body)) + body)
        response = b''
        while chunk := s.recv(65536):
            response += chunk
        return response
    except OSError as e:
        return ('error ' + type(e).__name__).encode()

def status(response):
    return response.split(b'\r\n')[0].decode() or 'empty'

def connect(path='/jmap/ws', timeout=3):
    sock = tls(timeout)
    sock.sendall((f'GET {path} HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
                  'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n'
                  'Authorization: Bearer test\r\nX-Forwarded-For: 6.6.6.6\r\nForwarded: for=6.6.6.6\r\n\r\n').encode())
    stream = sock.makefile('rb')
    first = stream.readline()
    while stream.readline() not in (b'\r\n', b''):
        pass
    assert b' 101 ' in first, first
    return sock, stream

def read(stream):
    """One reply as (opcode, payload); ('eof', b'') if nginx closed, ('timeout', b'') if nothing came in 3 s."""
    try:
        f = read_frame(stream)
        return ('eof', b'') if f is None else f[1:]
    except (OSError, ValueError, struct.error):
        return 'timeout', b''

def request(*calls):
    return json.dumps({'@type': 'Request', 'id': 'r', 'using': ['urn:ietf:params:jmap:core'], 'methodCalls': list(calls)}).encode()

def is_echo(f):
    return f[0] == 1 and f[1].startswith(b'ECHO auth=Bearer test ')

echo = lambda got: is_echo(got[-1])
closed = lambda got: got[-1] == CLOSED_1008

def case(desc, frames, expect, path='/jmap/ws'):
    """Send frames on a new WebSocket (None = read a reply there), read one final reply, check expect(replies)."""
    global echoed, blocked
    sock, stream = connect(path)
    got = []
    for f in frames:
        if f is None:
            got.append(read(stream))
        else:
            sock.sendall(f)
    got.append(read(stream))
    sock.close()
    echoed += sum(1 for f in got if is_echo(f))
    blocked += got.count(CLOSED_1008)
    report('ws ' + desc, expect(got), [(op, data[:24]) for op, data in got])

def ws_cases():
    user = request(['Mailbox/get', {'accountId': 'a'}, '0'], ['Email/query', {'accountId': 'a'}, '1'])
    admin = request(['Mailbox/get', {'accountId': 'a'}, '0'], ['x:Account/get', {}, '1'])
    padded = request(['Email/set', {'pad': 'a' * 2000}, '0'])
    case('user request', [frame(1, user)], echo)
    case('user request on /jmap/ws?basic=x', [frame(1, user)], echo, '/jmap/ws?basic=x')
    case('push enable', [frame(1, b'{"@type":"WebSocketPushEnable","dataTypes":null}')], echo)
    case('ping', [frame(9, b'hi')], lambda got: got[-1] == (10, b'hi'))
    case('user split into 1000 fragments', [frame(1, padded[:1], fin=False)] + [frame(0, padded[i:i + 1], fin=False) for i in range(1, 999)] + [frame(0, padded[999:])], echo)
    case('fragmented user with ping between', [frame(1, user[:20], fin=False), frame(9, b'p'), None, frame(0, user[20:])],
         lambda got: got[0] == (10, b'p') and echo(got))
    case('70KB user request', [frame(1, request(['Email/set', {'pad': 'a' * 70000}, '0']))], echo)
    case('user then admin on one connection', [frame(1, user), None, frame(1, admin)], lambda got: is_echo(got[0]) and closed(got))
    case('admin in batch', [frame(1, admin)], closed)
    case('admin on /jmap/ws?basic=x', [frame(1, admin)], closed, '/jmap/ws?basic=x')
    case('admin split across fragments', [frame(1, admin[:60], fin=False), frame(0, admin[60:])], closed)
    case('admin with escaped x\\u003a', [frame(1, admin.replace(b'x:Account', b'x\\u003aAccount'))], closed)
    case('admin in binary frame', [frame(2, admin)], closed)
    case('invalid JSON', [frame(1, b'{"methodCalls":[["Core/echo"')], closed)
    case('unmasked frame', [frame(1, user, mask=False)], closed)
    case('RSV1 (compressed) frame', [frame(1, user, rsv=0x40)], closed)
    case('continuation without start', [frame(0, user)], closed)
    case('declared length over 1 MiB', [frame(1, b'', length=1048577)], closed)
    case('1100 empty continuation frames', [frame(1, b'{', fin=False)] + [frame(0, b'', fin=False)] * 1100, closed)

def http_soak():
    """20 concurrent ~1 MB bodies of 349000 empty arrays (worst case for the JSON filter) from one address.
    The slow upstream holds each slot 2 s, so limit_conn jmap_ip 16 must let exactly 16 through."""
    dense = b'{"methodCalls":[["Core/echo",{"a":[' + b','.join([b'[]'] * 349000) + b']},"0"]]}'
    statuses = []
    def post(i):
        response = http_post(b'/jmap//soak', b'soak-%d' % i, dense, 60)
        result = status(response)
        if result == 'HTTP/1.1 200 OK' and not response.endswith(b'\r\n\r\nslow'):
            result += ' without the slow upstream body'
        statuses.append(result)
    threads = [threading.Thread(target=post, args=(i,)) for i in range(20)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    ok_count = statuses.count('HTTP/1.1 200 OK')
    report('http soak 20 concurrent 1MB dense bodies', ok_count == 16 and statuses.count('HTTP/1.1 429 Too Many Requests') == 4,
           f'{len(dense)} bytes each: {sorted(set(statuses))}, {ok_count} x 200')

def ws_soak():
    """50 x 900 KB messages on one socket: the frame filter must not grow memory per message."""
    global echoed
    soak = request(['Email/set', {'pad': 'a' * 900000}, '0'])
    sock, stream = connect(timeout=30)
    count = 0
    for _ in range(50):
        try:
            sock.sendall(frame(1, soak, key=b'\0\0\0\0'))
        except OSError:
            break
        if not is_echo(read(stream)):
            break
        count += 1
    sock.close()
    echoed += count
    report('ws soak 50 x 900KB on one socket', count == 50, f'{count} of 50 echoed')

def burst(n, threads, credential):
    """n tiny POSTs to /jmap from `threads` workers; credential is formatted with the request index."""
    responses = []
    def worker(indexes):
        for i in indexes:
            responses.append(http_post(b'/jmap', credential.format(i).encode(), b'{"methodCalls":[]}', 10))
    pool = [threading.Thread(target=worker, args=(range(t, n, threads),)) for t in range(threads)]
    start = time.time()
    [t.start() for t in pool]
    [t.join() for t in pool]
    return responses, time.time() - start

def rate_case(desc, result):
    """Expect burst (200) + 1 to pass, plus what 20 r/s refills while the burst ran (+5 slack); the rest 429."""
    responses, elapsed = result
    codes = [status(r) for r in responses]
    ok, limited = codes.count('HTTP/1.1 200 OK'), codes.count('HTTP/1.1 429 Too Many Requests')
    allowed = 201 + 20 * elapsed + 5
    report(desc, min(len(codes), 201) <= ok <= allowed and ok + limited == len(codes),
           f'{ok} x 200, {limited} x 429, {len(codes) - ok - limited} other in {elapsed:.1f} s (at most {allowed:.0f} allowed)')

def rate_limits():
    rate_case('limit_req: 400 requests, 400 credentials, one address', burst(400, 8, 'rotate-{}'))
    time.sleep(11)
    result = burst(400, 2, 'one')
    rate_case('limit_req: 400 requests, one credential', result)
    limited = next((r for r in result[0] if r.startswith(b'HTTP/1.1 429')), b'')
    head, _, body = limited.partition(b'\r\n\r\n')
    report('429 is JMAP problem JSON with Retry-After', b'Content-Type: application/problem+json' in head and b'Retry-After: 1' in head
           and b'"type":"urn:ietf:params:jmap:error:limit"' in body, head.split(b'\r\n')[0].decode() + ' ' + body[:60].decode())

def push_open(path, credential, extra=''):
    sock = tls(30)
    sock.sendall((f'GET {path} HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
                  f'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n'
                  f'Authorization: Bearer {credential}\r\nAccept: text/event-stream\r\n{extra}\r\n').encode())
    head = b''
    while b'\r\n\r\n' not in head and (chunk := sock.recv(4096)):
        head += chunk
    return status(head), sock

def park_sessions(n, prefix):
    """Open n WebSocket sessions at once, each parking an unfinished 1 MiB message; returns (codes, open sockets)."""
    park = frame(1, b'a' * 524288, fin=False, key=b'\0\0\0\0') + frame(0, b'a' * 524288, key=b'\0\0\0\0')[:-1]
    held, codes, lock = [], [], threading.Lock()
    def session(i):
        try:
            code, sock = push_open('/jmap/ws', f'{prefix}-{i}')
            if code == 'HTTP/1.1 101 Switching Protocols':
                sock.sendall(park)
                with lock:
                    held.append(sock)
            else:
                sock.close()
        except OSError as e:
            code = 'error ' + type(e).__name__
        with lock:
            codes.append(code)
    threads = [threading.Thread(target=session, args=(i,)) for i in range(n)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    return codes, held

def push_hold(n):
    """One of several containers (one address each) that together exceed the pod-wide push cap."""
    codes, held = park_sessions(n, 'hold-' + socket.gethostname())
    open('/sync/' + socket.gethostname(), 'w').close()
    deadline = time.time() + 120
    while not os.path.exists('/sync/release') and time.time() < deadline:
        time.sleep(0.1)
    [s.close() for s in held]
    print('PUSHHOLD', codes.count('HTTP/1.1 101 Switching Protocols'), codes.count('HTTP/1.1 429 Too Many Requests'), len(codes))

def push_bodies():
    for path in ('/jmap/ws', '/jmap/eventsource'):
        for extra in ('Content-Length: 10', 'Transfer-Encoding: chunked'):
            try:
                code, sock = push_open(path, 'held', extra + '\r\n')
                sock.close()
            except OSError as e:
                code = 'error ' + type(e).__name__
            report(f'push {path} announcing a body ({extra.split(":")[0]})', code == 'HTTP/1.1 400 Bad Request', code)

def push_caps():
    codes, held = park_sessions(520, 'push')
    upgraded, limited = codes.count('HTTP/1.1 101 Switching Protocols'), codes.count('HTTP/1.1 429 Too Many Requests')
    report('push cap: 520 ws sessions from one address', upgraded == 32 and limited == 488,
           f'{upgraded} x 101, {limited} x 429 of {len(codes)}')
    code, sock = push_open('/jmap/eventsource?types=*&closeafter=no&ping=65', 'push-sse')
    sock.close()
    report('push cap: eventsource shares the address cap', code == 'HTTP/1.1 429 Too Many Requests', code)
    time.sleep(2)
    [s.close() for s in held]
    for _ in range(50):
        time.sleep(0.1)
        code, sock = push_open('/jmap/ws', 'push-after')
        sock.close()
        if code == 'HTTP/1.1 101 Switching Protocols':
            break
    report('push cap: slots free once sessions close', code == 'HTTP/1.1 101 Switching Protocols', code)

def eventsource():
    """The first event must not be buffered, and the stream must outlive nginx's default 60 s read timeout."""
    sock = tls(90)
    sock.sendall(b'GET /jmap/eventsource?types=*&closeafter=no&ping=65 HTTP/1.1\r\nHost: localhost\r\n'
                 b'Authorization: Bearer test\r\nAccept: text/event-stream\r\n\r\n')
    start, data, seen = time.time(), b'', {}
    while 'ping' not in seen:
        try:
            chunk = sock.recv(4096)
        except OSError:
            break
        if not chunk:
            break
        data += chunk
        for event in ('state', 'ping'):
            if event not in seen and b'event: ' + event.encode() in data:
                seen[event] = time.time() - start
    sock.close()
    report('eventsource first event arrives promptly', seen.get('state', 99) < 3, f"state after {seen.get('state', 'never')} s")
    report('eventsource survives a 65 s gap to the ping', seen.get('ping', 0) > 60, f"ping after {seen.get('ping', 'never')} s")

if sys.argv[1:] == ['eventsource']:
    eventsource()
elif sys.argv[1:2] == ['push-hold']:
    push_hold(int(sys.argv[2]))
else:
    ws_cases()
    http_soak()
    ws_soak()
    rate_limits()
    push_bodies()
    push_caps()
    print('ECHOED', echoed)
    print('BLOCKED', blocked)
sys.exit(1 if failures else 0)
EOF

wait_until() {
  local _
  for _ in {1..100}; do
    "$@" > /dev/null 2>&1 && return
    sleep 0.1
  done
  echo "timed out waiting for: $*" >&2
  return 1
}
wsecho_ready() { "$engine" logs "$name-wsecho" 2>&1 | grep '^READY'; }

"$engine" network create "$name" > /dev/null
"$engine" run -d --name "$name-wsecho" --network "$name" --network-alias wsecho -v "$work/py:/py:ro,z" "$python" python -u /py/wsecho.py > /dev/null
"$engine" run -d --name "$name-mock" --network "$name" --network-alias stalwart-https --network-alias stalwart-mail \
  -v "$work/mock.conf:/etc/nginx/nginx.conf:ro,z" -v "$work/ssl:/etc/nginx/ssl:ro,z" "$image" > /dev/null
wait_until "$engine" exec "$name-mock" curl -sk -o /dev/null https://localhost/
"$engine" run --rm --network "$name" -v "$work/etc:/etc/nginx:ro,z" --entrypoint nginx "$image" -t -q
"$engine" run -d --name "$name-edge" --network "$name" --network-alias edge --memory=1g --memory-swap=1g \
  -v "$work/etc:/etc/nginx:ro,z" "$image" > /dev/null
wait_until "$engine" exec "$name-edge" curl -skf "${pp[@]}" -o /dev/null https://localhost/.well-known/jmap
wait_until wsecho_ready
"$engine" run -d --name "$name-eventsource" --network "$name" -e PROXY_PROTOCOL=${#pp[@]} -v "$work/py:/py:ro,z" "$python" python -u /py/client.py eventsource > /dev/null

"$engine" exec "$name-edge" sh -c '
  head -c 900000 /dev/zero | tr "\0" a > /tmp/pad
  { printf "{\"pad\":\""; cat /tmp/pad; printf "\",\"methodCalls\":[[\"Mailbox/get\",{},\"0\"]]}"; } > /tmp/big-user
  sed "s#Mailbox/get#x:Account/get#" /tmp/big-user > /tmp/big-admin
  head -c 1100000 /dev/zero | tr "\0" a > /tmp/huge
  head -c 5000000 /dev/zero > /tmp/5mb
  head -c 53000000 /dev/zero > /tmp/53mb'

failures=0
pass() { printf 'ok    %-45s %s\n' "$1" "$2"; }
fail() { printf 'FAIL  %-45s %s\n' "$1" "$2"; failures=$((failures + 1)); }
result() {
  local desc=$1 detail=$2
  shift 2
  if "$@"; then pass "$desc" "$detail"; else fail "$desc" "$detail"; fi
}

check() {
  local code=$1 upstream=$2 desc=$3
  shift 3
  local response got size out len
  response=$("$engine" exec "$name-edge" curl -sk "${pp[@]}" -w '\n%{http_code} %{size_upload}' \
    -H 'Authorization: Bearer test' -H 'X-Forwarded-For: 6.6.6.6' -H 'Forwarded: for=6.6.6.6' "$@" https://localhost/ || true)
  out=${response%$'\n'*}
  read -r got size <<< "${response##*$'\n'}"
  len=${size%.*}
  [[ $len == 0 ]] && len=
  if [[ $got == "$code" && ( -z $upstream || $out == "UPSTREAM $upstream upgrade=[] auth=[Bearer test] len=[$len] xff=[127.0.0.1] fwd=[]" ) ]]; then
    pass "$desc" "$got"
  else
    fail "$desc" "expected $code $upstream, got $got ${out:0:160}"
  fi
}

json=(-H 'Content-Type: application/json')
user='{"using":["urn:ietf:params:jmap:core","urn:ietf:params:jmap:mail"],"methodCalls":[["Mailbox/get",{"accountId":"a"},"0"],["Email/query",{"accountId":"a"},"1"]]}'
mixed='{"using":["urn:ietf:params:jmap:core","urn:ietf:params:jmap:mail"],"methodCalls":[["Mailbox/get",{"accountId":"a"},"0"],["x:Account/get",{},"1"]]}'

check 200 'POST /jmap' 'user batch POST /jmap' "${json[@]}" --request-target /jmap -d "$user"
check 200 'POST /jmap/' 'user POST /jmap/ with urn:stalwart:jmap' "${json[@]}" --request-target /jmap/ \
  -d '{"using":["urn:ietf:params:jmap:core","urn:stalwart:jmap"],"methodCalls":[["Core/echo",{},"0"]]}'
check 200 'POST /jmap/?q=1' 'user POST /jmap/?q=1 keeps raw URI' "${json[@]}" --request-target '/jmap/?q=1' -d "$user"
check 200 'POST /jmap' 'user 900KB body' "${json[@]}" --request-target /jmap --data-binary @/tmp/big-user
check 403 '' 'mixed user/admin batch' "${json[@]}" --request-target /jmap -d "$mixed"
check 403 '' 'admin on POST /jmap/' "${json[@]}" --request-target /jmap/ -d '{"methodCalls":[["x:Domain/set",{},"0"]]}'
check 403 '' 'admin on POST /jmap?q=1' "${json[@]}" --request-target '/jmap?q=1' -d "$mixed"
check 403 '' 'admin on POST /jmap#/upload/a' "${json[@]}" --request-target '/jmap#/upload/a' -d "$mixed"
check 403 '' 'admin on POST /jmap//upload/a' "${json[@]}" --request-target /jmap//upload/a -d "$mixed"
check 403 '' 'admin on POST /jmap//../upload/a' "${json[@]}" --request-target /jmap//../upload/a -d "$mixed"
check 403 '' 'admin on absolute-form target' "${json[@]}" --request-target https://localhost/jmap -d "$mixed"
check 403 '' 'escaped x\u003a method name' "${json[@]}" --request-target /jmap -d '{"methodCalls":[["x\u003aAccount/get",{},"0"]]}'
check 403 '' 'escaped \u0078: method name' "${json[@]}" --request-target /jmap -d '{"methodCalls":[["\u0078:Account/get",{},"0"]]}'
check 200 'POST /jmap' 'user call with "x:" in arguments' "${json[@]}" --request-target /jmap -d '{"methodCalls":[["Core/echo",{"x:k":"x:v"},"0"]]}'
check 403 '' 'duplicate methodCalls, admin last' "${json[@]}" --request-target /jmap \
  -d '{"methodCalls":[["Core/echo",{},"0"]],"methodCalls":[["x:Account/get",{},"1"]]}'
check 403 '' 'admin chunked body' "${json[@]}" -H 'Transfer-Encoding: chunked' --request-target /jmap -d "$mixed"
check 403 '' 'admin 900KB body' "${json[@]}" --request-target /jmap --data-binary @/tmp/big-admin
check 403 '' 'invalid JSON' "${json[@]}" --request-target /jmap -d '{"methodCalls":[["x:Account/get",{},"0"]'
check 403 '' 'methodCalls not an array' "${json[@]}" --request-target /jmap -d '{"methodCalls":{"0":["x:Account/get",{},"0"]}}'
check 403 '' 'empty body' "${json[@]}" --request-target /jmap -X POST
check 413 '' 'JMAP API body over 1m' "${json[@]}" --request-target /jmap --data-binary @/tmp/huge

for target in //jmap /./jmap /jmap/. /%6Amap /%6amap /JMAP '/jmap;x' /jmap%2F; do
  check 200 "POST $target" "not a Stalwart JMAP route: $target" "${json[@]}" --request-target "$target" -d "$mixed"
done

check 200 'POST /jmap/upload/a/' 'upload POST /jmap/upload/a/' -H 'Content-Type: application/octet-stream' \
  --request-target /jmap/upload/a/ -d "$mixed"
check 200 'POST /jmap/upload/a/' '5MB upload POST /jmap/upload/a/' -H 'Content-Type: application/octet-stream' \
  --request-target /jmap/upload/a/ --data-binary @/tmp/5mb
check 200 'PUT /dav/cal/u/default/e.ics' '5MB CalDAV PUT' -X PUT -H 'Content-Type: text/calendar' \
  --request-target /dav/cal/u/default/e.ics --data-binary @/tmp/5mb
check 413 '' '53MB upload over 50m' -H 'Content-Type: application/octet-stream' \
  --request-target /jmap/upload/a/ --data-binary @/tmp/53mb
check 200 'GET /jmap/download/a/b/My%20file.pdf?accept=application/pdf' 'download GET /jmap/download' \
  --request-target '/jmap/download/a/b/My%20file.pdf?accept=application/pdf'
check 200 'GET /jmap/session' 'session GET /jmap/session' --request-target /jmap/session
check 200 'GET /.well-known/jmap' 'GET /.well-known/jmap' --request-target /.well-known/jmap
check 200 'GET /jmap/eventsource/' 'eventsource drops Upgrade header' \
  -H 'Connection: Upgrade' -H 'Upgrade: websocket' --request-target /jmap/eventsource/
check 200 'PROPFIND /dav/cal/u/' 'PROPFIND /dav/cal/u/' -X PROPFIND --request-target /dav/cal/u/
check 200 'POST /auth/token' 'POST /auth/token' -d grant_type=password --request-target /auth/token
check 200 'GET /API/account' 'not a Stalwart route: /API/account' --request-target /API/account

blocked_paths=(
  /admin /api/account /account /metrics/prometheus
  /.jmap-api /.jmap-ws /.jmap-sse /.jmap-error-413 /.jmap-error-429
  /api/../x /api/account/../../x /api/live/delivery/../../../x /api/live/delivery/example.com/../../../../x
  /api/schema/../../x /api/discover/a@b.c/../../../x /metrics/prometheus/../../x /admin/../x /admin/index.html/../../x
  /account/../x /api/%2e%2e/x /api/%2E%2E/x /api/.%2e/x /metrics/prometheus/%2e%2e/%2e%2e/x '/api/..;/x'
  /%61pi/account //api/account /./api/account '/api;/account' /api%2Faccount /x/../api/account
)
for path in "${blocked_paths[@]}"; do
  check 404 '' "GET $path" --request-target "$path"
done
check 404 '' 'POST /api/auth/../../x' -d x --request-target /api/auth/../../x
check 404 '' 'POST /api/../jmap' "${json[@]}" -d "$mixed" --request-target /api/../jmap

conn=$("$engine" exec "$name-edge" sh -c "seq 6 | xargs -P 6 -I{} curl -sk ${pp[*]} -o /dev/null -w '%{http_code}\n' \
  -H 'Content-Type: application/json' -H 'Authorization: Bearer conn' -d '{\"methodCalls\":[]}' https://localhost/jmap//soak" | sort | uniq -c | tr -s ' \n' ' ')
result 'limit_conn: 6 slow requests, one credential' "$conn" test "$conn" = ' 4 200 2 429 '

shape=$("$engine" exec "$name-edge" sh -c "curl -sk ${pp[*]} -D - -H 'Content-Type: application/json' --data-binary @/tmp/huge https://localhost/jmap")
if [[ $shape == *'HTTP/1.1 413'* && $shape == *'Content-Type: application/problem+json'* && $shape == *'"limit":"maxSizeRequest"'* ]]; then
  pass '413 is JMAP problem JSON' "$(head -1 <<< "$shape" | tr -d '\r')"
else
  fail '413 is JMAP problem JSON' "${shape:0:160}"
fi

blocked_ws_log_lines() { "$engine" logs "$name-edge" 2>&1 | grep -c 'blocked JMAP WebSocket session' || true; }
before=$(blocked_ws_log_lines)
"$engine" exec "$name-edge" curl -sk "${pp[@]}" -m 3 -o /dev/null --request-target /jmap/ws "${json[@]}" -d "$mixed" https://localhost/ || true
after=$(blocked_ws_log_lines)
result 'POST body on /jmap/ws is not forwarded' "$((after - before)) blocked sessions logged" test "$after" = "$before"

client=0
"$engine" run --rm --network "$name" -e PROXY_PROTOCOL=${#pp[@]} -v "$work/py:/py:ro,z" "$python" python -u /py/client.py > "$work/client.log" 2>&1 || client=$?
grep -v '^ECHOED\|^BLOCKED' "$work/client.log"
[[ $client -eq 0 ]] || failures=$((failures + 1))
[[ $("$engine" wait "$name-eventsource") == 0 ]] || failures=$((failures + 1))
"$engine" logs "$name-eventsource" 2>&1

# One more address x 32 sessions than the pod-wide cap (jmap_push_total) allows: it must stop the rest.
holders=$((push_total / 32 + 1))
mkdir -p "$work/sync"
for i in $(seq "$holders"); do
  containers+=("$name-hold-$i")
  "$engine" run -d --name "$name-hold-$i" --network "$name" -e PROXY_PROTOCOL=${#pp[@]} -v "$work/py:/py:ro,z" -v "$work/sync:/sync:z" "$python" python -u /py/client.py push-hold 32 > /dev/null &
done
wait
for _ in {1..600}; do
  [[ $(find "$work/sync" -type f | wc -l) -ge $holders ]] && break
  sleep 0.1
done
touch "$work/sync/release"
read -r upgraded limited total < <(for i in $(seq "$holders"); do "$engine" wait "$name-hold-$i" > /dev/null; "$engine" logs "$name-hold-$i" 2>&1; done |
  awk '/^PUSHHOLD/ { u += $2; l += $3; t += $4 } END { print u + 0, l + 0, t + 0 }')
result "push cap: $((holders * 32)) ws sessions from $holders addresses" "$upgraded x 101, $limited x 429 of $total (cap $push_total)" \
  test "$upgraded:$limited:$total" = "$push_total:32:$((holders * 32))"

echoed=$(sed -n 's/^ECHOED //p' "$work/client.log")
received=$("$engine" logs "$name-wsecho" 2>&1 | grep '^RECEIVED' || true)
if grep -q 'x:\|INVALID' <<< "$received"; then
  fail 'ws upstream received only user messages' "$(grep 'x:\|INVALID' <<< "$received" | head -3 | tr '\n' ' ')"
elif [[ $(grep -c . <<< "$received") != "$echoed" ]]; then
  fail 'ws upstream received only user messages' "received $(grep -c . <<< "$received"), echoed ${echoed:-none}"
else
  pass 'ws upstream received only user messages' "$echoed received"
fi

headers=$("$engine" logs "$name-wsecho" 2>&1 | grep '^HEADER' || true)
result 'ws upstream got rewritten X-Forwarded-For' "$(sort -u <<< "$headers" | head -3 | tr '\n' ' ')" \
  test -n "$headers" -a "${headers//6.6.6.6/}" = "$headers"

closes=$(sed -n 's/^BLOCKED //p' "$work/client.log")
blocked=$(( $(blocked_ws_log_lines) - after ))
result 'blocked ws sessions are logged' "$blocked log lines for ${closes:-no} 1008 closes" test "$blocked" = "${closes:-none}"

oom=$("$engine" exec "$name-edge" sh -c 'grep "^oom_kill " /sys/fs/cgroup/memory.events' | cut -d' ' -f2 || true)
killed=$("$engine" logs "$name-edge" 2>&1 | grep -c 'exited on signal' || true)
peak=$("$engine" exec "$name-edge" cat /sys/fs/cgroup/memory.peak || true)
result 'edge (1g limit) no OOM kill, peak < 768 MiB' "oom_kill=${oom:-unknown} workers killed=$killed peak=${peak:-unknown}" \
  test "$oom:$killed" = 0:0 -a "${peak:-unknown}" -lt 805306368

echo "$failures failure(s)"
[[ $failures -eq 0 ]]
