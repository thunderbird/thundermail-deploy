#!/usr/bin/env bash
set -euo pipefail

overlay=$(cd "$(dirname "$0")/.." && pwd)
engine=$(command -v podman || command -v docker)
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
      return 200 "UPSTREAM $request_method $request_uri upgrade=[$http_upgrade]";
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
import base64, hashlib, json, socket, struct, threading

def send(conn, opcode, payload):
    n = len(payload)
    if n < 126:
        header = bytes([0x80 | opcode, n])
    elif n < 65536:
        header = bytes([0x80 | opcode, 126]) + struct.pack('>H', n)
    else:
        header = bytes([0x80 | opcode, 127]) + struct.pack('>Q', n)
    conn.sendall(header + payload)

def handle(conn):
    stream = conn.makefile('rb')
    key = b''
    while (line := stream.readline()) not in (b'\r\n', b''):
        if line.lower().startswith(b'sec-websocket-key:'):
            key = line.split(b':', 1)[1].strip()
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
        data = bytes(b ^ mask[i % 4] for i, b in enumerate(stream.read(n)))
        opcode = head[0] & 0x0f
        if opcode == 8:
            send(conn, 8, data)
            break
        if opcode == 9:
            send(conn, 10, data)
            continue
        message += data
        if head[0] & 0x80:
            calls = json.loads(message).get('methodCalls', [])
            print('RECEIVED', ' '.join(call[0] for call in calls) or '-', flush=True)
            send(conn, 1, b'ECHO ' + message[:40])
            message = b''
    conn.close()

server = socket.create_server(('0.0.0.0', 8080))
while True:
    threading.Thread(target=handle, args=(server.accept()[0],), daemon=True).start()
EOF

cat > "$work/py/wsclient.py" <<'EOF'
import json, os, socket, ssl, struct, sys

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

def connect(path='/jmap/ws'):
    sock = ctx.wrap_socket(socket.create_connection(('edge', 443), timeout=3))
    sock.sendall((f'GET {path} HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
                  'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n'
                  'Authorization: Bearer test\r\n\r\n').encode())
    stream = sock.makefile('rb')
    status = stream.readline()
    while stream.readline() not in (b'\r\n', b''):
        pass
    assert b' 101 ' in status, status
    return sock, stream

def frame(opcode, payload, fin=True, mask=True, rsv=0, length=None):
    n = len(payload) if length is None else length
    bit = 0x80 if mask else 0
    if n < 126:
        header = bytes([(0x80 if fin else 0) | rsv | opcode, bit | n])
    elif n < 65536:
        header = bytes([(0x80 if fin else 0) | rsv | opcode, bit | 126]) + struct.pack('>H', n)
    else:
        header = bytes([(0x80 if fin else 0) | rsv | opcode, bit | 127]) + struct.pack('>Q', n)
    key = os.urandom(4) if mask else b''
    return header + key + (bytes(b ^ key[i % 4] for i, b in enumerate(payload)) if mask else payload)

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

def case(desc, frames, expect, path='/jmap/ws'):
    global failures
    sock, stream = connect(path)
    got = []
    for f in frames:
        if f is None:
            got.append(read(stream))
        else:
            sock.sendall(f)
    got.append(read(stream))
    sock.close()
    ok = expect(got)
    failures += 0 if ok else 1
    print(f"{'ok   ' if ok else 'FAIL '} {('ws ' + desc):<45} {[(op, data[:24]) for op, data in got]}")

echo = lambda got: got[-1][0] == 1 and got[-1][1].startswith(b'ECHO ')
closed = lambda got: got[-1] in ((8, b'\x03\xf0'), ('eof', b''))

case('user request', [frame(1, user)], echo)
case('user request on /jmap/ws?basic=x', [frame(1, user)], echo, '/jmap/ws?basic=x')
case('push enable', [frame(1, b'{"@type":"WebSocketPushEnable","dataTypes":null}')], echo)
case('ping', [frame(9, b'hi')], lambda got: got[-1] == (10, b'hi'))
padded = request(['Email/set', {'pad': 'a' * 2000}, '0'])
case('user split into 1000 fragments', [frame(1, padded[:1], fin=False)] + [frame(0, padded[i:i + 1], fin=False) for i in range(1, 999)] + [frame(0, padded[999:])], echo)
case('fragmented user with ping between', [frame(1, user[:20], fin=False), frame(9, b'p'), None, frame(0, user[20:])],
     lambda got: got[0] == (10, b'p') and echo(got))
case('70KB user request', [frame(1, request(['Email/set', {'pad': 'a' * 70000}, '0']))], echo)
case('user then admin on one connection', [frame(1, user), None, frame(1, admin)], lambda got: echo(got[:1]) and closed(got))
case('admin in batch', [frame(1, admin)], closed)
case('admin on /jmap/ws?basic=x', [frame(1, admin)], closed, '/jmap/ws?basic=x')
case('admin split across fragments', [frame(1, admin[:60], fin=False), frame(0, admin[60:])], closed)
case('admin with escaped x\\u003a', [frame(1, admin.replace(b'x:Account', b'x\\u003aAccount'))], closed)
case('admin in binary frame', [frame(2, admin)], closed)
case('invalid JSON', [frame(1, b'{"methodCalls":[["x:Account/get"')], closed)
case('unmasked frame', [frame(1, user, mask=False)], closed)
case('RSV1 (compressed) frame', [frame(1, user, rsv=0x40)], closed)
case('continuation without start', [frame(0, user)], closed)
case('declared length over 1 MiB', [frame(1, b'', length=1048577)], closed)
case('1100 empty continuation frames', [frame(1, b'{', fin=False)] + [frame(0, b'', fin=False)] * 1100, closed)
sys.exit(1 if failures else 0)
EOF

"$engine" network create "$name" > /dev/null
"$engine" run -d --name "$name-wsecho" --network "$name" --network-alias wsecho -v "$work/py:/py:ro,Z" "$python" python -u /py/wsecho.py > /dev/null
"$engine" run -d --name "$name-mock" --network "$name" --network-alias stalwart-https --network-alias stalwart-mail \
  -v "$work/mock.conf:/etc/nginx/nginx.conf:ro,Z" -v "$work/ssl:/etc/nginx/ssl:ro,Z" "$image" > /dev/null
sleep 1
"$engine" run --rm --network "$name" -v "$work/etc:/etc/nginx:ro,Z" --entrypoint nginx "$image" -t -q
"$engine" run -d --name "$name-edge" --network "$name" --network-alias edge -v "$work/etc:/etc/nginx:ro,Z" "$image" > /dev/null
sleep 1

"$engine" exec "$name-edge" sh -c '
  head -c 900000 /dev/zero | tr "\0" a > /tmp/pad
  { printf "{\"pad\":\""; cat /tmp/pad; printf "\",\"methodCalls\":[[\"Mailbox/get\",{},\"0\"]]}"; } > /tmp/big-user
  sed "s#Mailbox/get#x:Account/get#" /tmp/big-user > /tmp/big-admin
  head -c 1100000 /dev/zero | tr "\0" a > /tmp/huge
  head -c 5000000 /dev/zero > /tmp/5mb
  head -c 53000000 /dev/zero > /tmp/53mb'

failures=0
check() {
  local code=$1 upstream=$2 desc=$3
  shift 3
  local got out
  got=$("$engine" exec "$name-edge" curl -sk -o /tmp/out -w '%{http_code}' "$@" https://localhost/ || true)
  out=$("$engine" exec "$name-edge" cat /tmp/out)
  if [[ $got == "$code" && ( -z $upstream || $out == "UPSTREAM $upstream upgrade=[]" ) ]]; then
    printf 'ok    %-45s %s\n' "$desc" "$got"
  else
    printf 'FAIL  %-45s expected %s %s, got %s %s\n' "$desc" "$code" "$upstream" "$got" "${out:0:120}"
    failures=$((failures + 1))
  fi
}

json=(-H 'Content-Type: application/json' -H 'Authorization: Bearer test')
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
check 403 '' 'escaped x: method name' "${json[@]}" --request-target /jmap -d '{"methodCalls":[["x:Account/get",{},"0"]]}'
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
for path in /admin /api/account /account /metrics/prometheus /.jmap-api /.jmap-ws; do
  check 404 '' "GET $path" --request-target "$path"
done

"$engine" run --rm --network "$name" -v "$work/py:/py:ro,Z" "$python" python -u /py/wsclient.py || failures=$((failures + 1))
received=$("$engine" logs "$name-wsecho" 2>&1 | grep '^RECEIVED' || true)
if grep -q 'x:' <<< "$received"; then
  printf 'FAIL  %-45s %s\n' 'ws upstream received an x: method' "$(grep 'x:' <<< "$received" | head -3 | tr '\n' ' ')"
  failures=$((failures + 1))
else
  printf 'ok    %-45s %s\n' 'ws upstream never received an x: method' "$(wc -l <<< "$received") messages"
fi

echo "$failures failure(s)"
[[ $failures -eq 0 ]]
