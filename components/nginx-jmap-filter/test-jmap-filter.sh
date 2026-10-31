#!/usr/bin/env bash
set -euo pipefail

overlay=${1:?usage: $0 <overlay dir>}
engine=${CONTAINER_ENGINE:-$(command -v podman || command -v docker)}
image=${NGINX_IMAGE:-docker.io/library/nginx:1.31}
python=${PYTHON_IMAGE:-docker.io/library/python:3.13-slim}
name=jmap-filter-$$
work=$(mktemp -d)
trap '"$engine" rm -f "$name-edge" "$name-mock" "$name-wsecho" >/dev/null 2>&1 || true; "$engine" network rm "$name" >/dev/null 2>&1 || true; rm -rf "$work"' EXIT

kubectl kustomize "$overlay" > "$work/rendered.yaml"
mkdir -p "$work/etc" "$work/ssl" "$work/py"
yq 'select(.kind == "ConfigMap" and .metadata.name == "nginx-config") | .data["nginx.conf"]' "$work/rendered.yaml" > "$work/etc/nginx.conf"
yq 'select(.kind == "ConfigMap" and .metadata.name == "nginx-config") | .data["jmap.js"]' "$work/rendered.yaml" > "$work/etc/jmap.js"
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj /CN=test -keyout "$work/ssl/tls.key" -out "$work/ssl/tls.crt" 2>/dev/null
for path in $(yq 'select(.kind == "Deployment" and .metadata.name == "nginx") | .spec.template.spec.volumes[].projected.sources[].secret.items[]?.path' "$work/rendered.yaml"); do
  case $path in
    *.crt) cp "$work/ssl/tls.crt" "$work/etc/$path" ;;
    *.key) cp "$work/ssl/tls.key" "$work/etc/$path" ;;
  esac
done
yq -e 'select(.kind == "Deployment" and .metadata.name == "nginx") | .spec.template.spec.volumes[].projected.sources[].configMap.items[]? | select(.key == "jmap.js")' "$work/rendered.yaml" > /dev/null

cat > "$work/mock.conf" <<'EOF'
events {}
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

cat > "$work/py/wsecho.py" <<'EOF'
import base64, hashlib, json, socket, struct, threading, time

def send(conn, opcode, payload):
    n = len(payload)
    if n < 126:
        header = bytes([0x80 | opcode, n])
    elif n < 65536:
        header = bytes([0x80 | opcode, 126]) + struct.pack('>H', n)
    else:
        header = bytes([0x80 | opcode, 127]) + struct.pack('>Q', n)
    conn.sendall(header + payload)

def unmask(data, mask):
    if mask == b'\0\0\0\0':
        return data
    return bytes(b ^ mask[i % 4] for i, b in enumerate(data))

def handle(conn):
    stream = conn.makefile('rb')
    key = auth = b''
    while (line := stream.readline()) not in (b'\r\n', b''):
        name, _, value = line.partition(b':')
        if name.lower() == b'sec-websocket-key':
            key = value.strip()
        elif name.lower() == b'authorization':
            auth = value.strip()
        elif name.lower() in (b'x-forwarded-for', b'forwarded'):
            print('HEADER', name.lower().decode(), value.strip().decode(), flush=True)
    accept = base64.b64encode(hashlib.sha1(key + b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest())
    conn.sendall(b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ' + accept + b'\r\n\r\n')
    message = b''
    while len(head := stream.read(2)) == 2:
        n = head[1] & 0x7f
        if n == 126:
            n = struct.unpack('>H', stream.read(2))[0]
        elif n == 127:
            n = struct.unpack('>Q', stream.read(8))[0]
        mask = stream.read(4) if head[1] & 0x80 else b'\0\0\0\0'
        data = unmask(stream.read(n), mask)
        opcode = head[0] & 0x0f
        if opcode == 8:
            send(conn, 8, data)
            break
        if opcode == 9:
            send(conn, 10, data)
            continue
        message += data
        if head[0] & 0x80:
            try:
                names = ' '.join(call[0] for call in json.loads(message).get('methodCalls', [])) or '-'
            except ValueError:
                names = 'INVALID'
            print('RECEIVED', names, flush=True)
            send(conn, 1, b'ECHO auth=' + auth + b' ' + message[:40])
            message = b''
    conn.close()

def slow(conn):
    stream = conn.makefile('rb')
    length = 0
    while (line := stream.readline()) not in (b'\r\n', b''):
        name, _, value = line.partition(b':')
        if name.lower() == b'content-length':
            length = int(value)
    stream.read(length)
    time.sleep(2)
    conn.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 4\r\nConnection: close\r\n\r\nslow')
    conn.close()

def sse(conn):
    stream = conn.makefile('rb')
    while stream.readline() not in (b'\r\n', b''):
        pass
    conn.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-cache\r\nConnection: close\r\n\r\n'
                 b'event: state\ndata: {}\n\n')
    time.sleep(65)
    conn.sendall(b'event: ping\ndata: {}\n\n')
    conn.close()

def serve(port, handler):
    server = socket.create_server(('0.0.0.0', port))
    while True:
        threading.Thread(target=handler, args=(server.accept()[0],), daemon=True).start()

threading.Thread(target=serve, args=(8081, slow), daemon=True).start()
threading.Thread(target=serve, args=(8082, sse), daemon=True).start()
serve(8080, handle)
EOF

cat > "$work/py/client.py" <<'EOF'
import json, os, socket, ssl, struct, sys, threading, time

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

def tls(timeout=3):
    return ctx.wrap_socket(socket.create_connection(('edge', 443), timeout=timeout))

def connect(path='/jmap/ws', timeout=3):
    sock = tls(timeout)
    sock.sendall((f'GET {path} HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
                  'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n'
                  'Authorization: Bearer test\r\nX-Forwarded-For: 6.6.6.6\r\nForwarded: for=6.6.6.6\r\n\r\n').encode())
    stream = sock.makefile('rb')
    status = stream.readline()
    while stream.readline() not in (b'\r\n', b''):
        pass
    assert b' 101 ' in status, status
    return sock, stream

def frame(opcode, payload, fin=True, mask=True, rsv=0, length=None, key=None):
    n = len(payload) if length is None else length
    bit = 0x80 if mask else 0
    if n < 126:
        header = bytes([(0x80 if fin else 0) | rsv | opcode, bit | n])
    elif n < 65536:
        header = bytes([(0x80 if fin else 0) | rsv | opcode, bit | 126]) + struct.pack('>H', n)
    else:
        header = bytes([(0x80 if fin else 0) | rsv | opcode, bit | 127]) + struct.pack('>Q', n)
    if not mask:
        return header + payload
    key = os.urandom(4) if key is None else key
    return header + key + (payload if key == b'\0\0\0\0' else bytes(b ^ key[i % 4] for i, b in enumerate(payload)))

def read(stream):
    try:
        head = stream.read(2)
        if len(head) < 2:
            return 'eof', b''
        n = head[1] & 0x7f
        if n == 126:
            n = struct.unpack('>H', stream.read(2))[0]
        elif n == 127:
            n = struct.unpack('>Q', stream.read(8))[0]
        return head[0] & 0x0f, stream.read(n)
    except (OSError, ValueError):
        return 'timeout', b''

def request(*calls):
    return json.dumps({'@type': 'Request', 'id': 'r', 'using': ['urn:ietf:params:jmap:core'], 'methodCalls': list(calls)}).encode()

user = request(['Mailbox/get', {'accountId': 'a'}, '0'], ['Email/query', {'accountId': 'a'}, '1'])
admin = request(['Mailbox/get', {'accountId': 'a'}, '0'], ['x:Account/get', {}, '1'])
failures = 0
echoed = 0

def is_echo(f):
    return f[0] == 1 and f[1].startswith(b'ECHO auth=Bearer test ')

def report(desc, ok, detail):
    global failures
    failures += 0 if ok else 1
    print(f"{'ok   ' if ok else 'FAIL '} {desc:<45} {detail}")

def case(desc, frames, expect, path='/jmap/ws'):
    global echoed
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
    report('ws ' + desc, expect(got), [(op, data[:24]) for op, data in got])

sse_result = {}
def sse():
    sock = tls(90)
    sock.sendall(b'GET /jmap/eventsource?types=*&closeafter=no&ping=65 HTTP/1.1\r\nHost: localhost\r\n'
                 b'Authorization: Bearer test\r\nAccept: text/event-stream\r\n\r\n')
    start, data = time.time(), b''
    while 'ping' not in sse_result:
        try:
            chunk = sock.recv(4096)
        except OSError:
            break
        if not chunk:
            break
        data += chunk
        if 'state' not in sse_result and b'event: state' in data:
            sse_result['state'] = time.time() - start
        if b'event: ping' in data:
            sse_result['ping'] = time.time() - start
    sock.close()
sse_thread = threading.Thread(target=sse)
sse_thread.start()

echo = lambda got: is_echo(got[-1])
closed = lambda got: got[-1] == (8, b'\x03\xf0')

case('user request', [frame(1, user)], echo)
case('user request on /jmap/ws?basic=x', [frame(1, user)], echo, '/jmap/ws?basic=x')
case('push enable', [frame(1, b'{"@type":"WebSocketPushEnable","dataTypes":null}')], echo)
case('ping', [frame(9, b'hi')], lambda got: got[-1] == (10, b'hi'))
padded = request(['Email/set', {'pad': 'a' * 2000}, '0'])
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

dense = b'{"methodCalls":[["Core/echo",{"a":[' + b','.join([b'[]'] * 349000) + b']},"0"]]}'
statuses = []
def post(i):
    try:
        s = tls(60)
        s.sendall(b'POST /jmap//soak HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nConnection: close\r\n'
                  b'Authorization: Bearer soak-%d\r\nContent-Length: %d\r\n\r\n' % (i, len(dense)) + dense)
        response = b''
        while chunk := s.recv(65536):
            response += chunk
        status = response.split(b'\r\n')[0].decode() or 'empty'
        if status == 'HTTP/1.1 200 OK' and not response.endswith(b'\r\n\r\nslow'):
            status += ' without the slow upstream body'
        statuses.append(status)
    except OSError as e:
        statuses.append('error ' + type(e).__name__)
threads = [threading.Thread(target=post, args=(i,)) for i in range(20)]
[t.start() for t in threads]
[t.join() for t in threads]
ok_count = statuses.count('HTTP/1.1 200 OK')
report('http soak 20 concurrent 1MB dense bodies', ok_count == 16 and statuses.count('HTTP/1.1 429 Too Many Requests') == 4,
       f'{len(dense)} bytes each: {sorted(set(statuses))}, {ok_count} x 200')

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
    responses = []
    def worker(indexes):
        for i in indexes:
            try:
                s = tls(10)
                s.sendall(b'POST /jmap HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nConnection: close\r\n'
                          b'Authorization: Bearer %s\r\nContent-Length: 18\r\n\r\n{"methodCalls":[]}' % credential.format(i).encode())
                response = b''
                while chunk := s.recv(65536):
                    response += chunk
                responses.append(response)
            except OSError as e:
                responses.append(('error ' + type(e).__name__).encode())
    pool = [threading.Thread(target=worker, args=(range(t, n, threads),)) for t in range(threads)]
    start = time.time()
    [t.start() for t in pool]
    [t.join() for t in pool]
    return responses, time.time() - start

def rate_case(desc, result):
    responses, elapsed = result
    codes = [r.split(b'\r\n')[0].decode() for r in responses]
    ok, limited = codes.count('HTTP/1.1 200 OK'), codes.count('HTTP/1.1 429 Too Many Requests')
    allowed = 201 + 20 * elapsed + 5
    report(desc, min(len(codes), 201) <= ok <= allowed and ok + limited == len(codes),
           f'{ok} x 200, {limited} x 429, {len(codes) - ok - limited} other in {elapsed:.1f} s (at most {allowed:.0f} allowed)')

rate_case('limit_req: 400 requests, 400 credentials, one address', burst(400, 8, 'rotate-{}'))
time.sleep(11)
result = burst(400, 2, 'one')
rate_case('limit_req: 400 requests, one credential', result)
responses = result[0]
limited = next((r for r in responses if r.startswith(b'HTTP/1.1 429')), b'')
head, _, body = limited.partition(b'\r\n\r\n')
report('429 is JMAP problem JSON with Retry-After', b'Content-Type: application/problem+json' in head and b'Retry-After: 1' in head
       and b'"type":"urn:ietf:params:jmap:error:limit"' in body, head.split(b'\r\n')[0].decode() + ' ' + body[:60].decode())

sse_thread.join()
report('eventsource first event arrives promptly', sse_result.get('state', 99) < 3, f"state after {sse_result.get('state', 'never')} s")
report('eventsource survives a 65 s gap to the ping', sse_result.get('ping', 0) > 60, f"ping after {sse_result.get('ping', 'never')} s")

print('ECHOED', echoed)
sys.exit(1 if failures else 0)
EOF

"$engine" network create "$name" > /dev/null
"$engine" run -d --name "$name-wsecho" --network "$name" --network-alias wsecho -v "$work/py:/py:ro,Z" "$python" python -u /py/wsecho.py > /dev/null
"$engine" run -d --name "$name-mock" --network "$name" --network-alias stalwart-https --network-alias stalwart-mail \
  -v "$work/mock.conf:/etc/nginx/nginx.conf:ro,Z" -v "$work/ssl:/etc/nginx/ssl:ro,Z" "$image" > /dev/null
sleep 1
"$engine" run --rm --network "$name" -v "$work/etc:/etc/nginx:ro,Z" --entrypoint nginx "$image" -t -q
"$engine" run -d --name "$name-edge" --network "$name" --network-alias edge --memory=1g --memory-swap=1g \
  -v "$work/etc:/etc/nginx:ro,Z" "$image" > /dev/null
sleep 1

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

check() {
  local code=$1 upstream=$2 desc=$3
  shift 3
  local got size out len
  read -r got size < <("$engine" exec "$name-edge" curl -sk -o /tmp/out -w '%{http_code} %{size_upload}\n' \
    -H 'Authorization: Bearer test' -H 'X-Forwarded-For: 6.6.6.6' -H 'Forwarded: for=6.6.6.6' "$@" https://localhost/ || true)
  out=$("$engine" exec "$name-edge" cat /tmp/out)
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
for path in /admin /api/account /account /metrics/prometheus /.jmap-api /.jmap-ws /.jmap-sse /.jmap-error-413 /.jmap-error-429 \
  /api/../x /api/account/../../x /api/live/delivery/../../../x /api/live/delivery/example.com/../../../../x \
  /api/schema/../../x /api/discover/a@b.c/../../../x /metrics/prometheus/../../x /admin/../x /admin/index.html/../../x \
  /account/../x /api/%2e%2e/x /api/%2E%2E/x /api/.%2e/x /metrics/prometheus/%2e%2e/%2e%2e/x '/api/..;/x' \
  /%61pi/account //api/account /./api/account '/api;/account' /api%2Faccount /x/../api/account; do
  check 404 '' "GET $path" --request-target "$path"
done
check 404 '' 'POST /api/auth/../../x' -d x --request-target /api/auth/../../x
check 404 '' 'POST /api/../jmap' "${json[@]}" -d "$mixed" --request-target /api/../jmap

conn=$("$engine" exec "$name-edge" sh -c "seq 6 | xargs -P 6 -I{} curl -sk -o /dev/null -w '%{http_code}\n' \
  -H 'Content-Type: application/json' -H 'Authorization: Bearer conn' -d '{\"methodCalls\":[]}' https://localhost/jmap//soak" | sort | uniq -c | tr -s ' \n' ' ')
if [[ $conn == ' 4 200 2 429 ' ]]; then pass 'limit_conn: 6 slow requests, one credential' "$conn"; else fail 'limit_conn: 6 slow requests, one credential' "$conn"; fi

shape=$("$engine" exec "$name-edge" sh -c "curl -sk -D - -H 'Content-Type: application/json' --data-binary @/tmp/huge https://localhost/jmap")
if [[ $shape == *'HTTP/1.1 413'* && $shape == *'Content-Type: application/problem+json'* && $shape == *'"limit":"maxSizeRequest"'* ]]; then
  pass '413 is JMAP problem JSON' "$(head -1 <<< "$shape" | tr -d '\r')"
else
  fail '413 is JMAP problem JSON' "${shape:0:160}"
fi
"$engine" exec "$name-edge" curl -sk -m 3 -o /dev/null --request-target /jmap/ws "${json[@]}" -d "$mixed" https://localhost/ || true

client=0
"$engine" run --rm --network "$name" -v "$work/py:/py:ro,Z" "$python" python -u /py/client.py > "$work/client.log" 2>&1 || client=$?
grep -v '^ECHOED' "$work/client.log"
[[ $client -eq 0 ]] || failures=$((failures + 1))
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
if [[ -n $headers && $headers != *6.6.6.6* ]]; then pass 'ws upstream got rewritten X-Forwarded-For' "$(sort -u <<< "$headers" | tr '\n' ' ')"; else fail 'ws upstream got rewritten X-Forwarded-For' "$(sort -u <<< "$headers" | head -3 | tr '\n' ' ')"; fi

blocked=$("$engine" logs "$name-edge" 2>&1 | grep -c 'blocked JMAP WebSocket session' || true)
if [[ $blocked -eq 12 ]]; then pass 'blocked ws sessions are logged' "$blocked log lines"; else fail 'blocked ws sessions are logged' "$blocked log lines"; fi

oom=$("$engine" exec "$name-edge" sh -c 'grep "^oom_kill " /sys/fs/cgroup/memory.events' | cut -d' ' -f2 || true)
killed=$("$engine" logs "$name-edge" 2>&1 | grep -c 'exited on signal' || true)
if [[ $oom == 0 && $killed == 0 ]]; then pass 'edge (--memory=1g) had no OOM kill' "oom_kill=$oom"; else fail 'edge (--memory=1g) had no OOM kill' "oom_kill=${oom:-unknown} workers killed=$killed"; fi

echo "$failures failure(s)"
[[ $failures -eq 0 ]]
