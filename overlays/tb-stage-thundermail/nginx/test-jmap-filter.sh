#!/usr/bin/env bash
set -euo pipefail

overlay=$(cd "$(dirname "$0")/.." && pwd)
engine=$(command -v podman || command -v docker)
image=${NGINX_IMAGE:-docker.io/library/nginx:1.31}
name=jmap-filter-$$
work=$(mktemp -d)
trap '"$engine" rm -f "$name-edge" "$name-mock" >/dev/null 2>&1 || true; "$engine" network rm "$name" >/dev/null 2>&1 || true; rm -rf "$work"' EXIT

kubectl kustomize "$overlay" > "$work/rendered.yaml"
mkdir -p "$work/etc" "$work/ssl"
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
    location / {
      return 200 "UPSTREAM $request_method $request_uri upgrade=[$http_upgrade]";
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

"$engine" network create "$name" > /dev/null
"$engine" run -d --name "$name-mock" --network "$name" --network-alias stalwart-https --network-alias stalwart-mail \
  -v "$work/mock.conf:/etc/nginx/nginx.conf:ro,Z" -v "$work/ssl:/etc/nginx/ssl:ro,Z" "$image" > /dev/null
sleep 1
"$engine" run --rm --network "$name" -v "$work/etc:/etc/nginx:ro,Z" --entrypoint nginx "$image" -t -q
"$engine" run -d --name "$name-edge" --network "$name" -v "$work/etc:/etc/nginx:ro,Z" "$image" > /dev/null
sleep 1

"$engine" exec "$name-edge" sh -c '
  head -c 900000 /dev/zero | tr "\0" a > /tmp/pad
  { printf "{\"pad\":\""; cat /tmp/pad; printf "\",\"methodCalls\":[[\"Mailbox/get\",{},\"0\"]]}"; } > /tmp/big-user
  sed "s#Mailbox/get#x:Account/get#" /tmp/big-user > /tmp/big-admin
  head -c 1100000 /dev/zero | tr "\0" a > /tmp/huge'

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
check 200 'POST /jmap' 'user 900KB body' "${json[@]}" --request-target /jmap --data-binary @/tmp/big-user
check 403 '' 'mixed user/admin batch' "${json[@]}" --request-target /jmap -d "$mixed"
check 403 '' 'admin on POST /jmap/' "${json[@]}" --request-target /jmap/ -d '{"methodCalls":[["x:Domain/set",{},"0"]]}'
check 403 '' 'admin on POST /jmap?q=1' "${json[@]}" --request-target '/jmap?q=1' -d "$mixed"
check 403 '' 'admin on POST /jmap#/upload/a' "${json[@]}" --request-target '/jmap#/upload/a' -d "$mixed"
check 403 '' 'admin on POST /jmap//upload/a' "${json[@]}" --request-target /jmap//upload/a -d "$mixed"
check 403 '' 'admin on POST /jmap//../upload/a' "${json[@]}" --request-target /jmap//../upload/a -d "$mixed"
check 403 '' 'escaped x\u003a method name' "${json[@]}" --request-target /jmap -d '{"methodCalls":[["x\u003aAccount/get",{},"0"]]}'
check 403 '' 'duplicate methodCalls, admin last' "${json[@]}" --request-target /jmap \
  -d '{"methodCalls":[["Core/echo",{},"0"]],"methodCalls":[["x:Account/get",{},"1"]]}'
check 403 '' 'admin chunked body' "${json[@]}" -H 'Transfer-Encoding: chunked' --request-target /jmap -d "$mixed"
check 403 '' 'admin 900KB body' "${json[@]}" --request-target /jmap --data-binary @/tmp/big-admin
check 403 '' 'invalid JSON' "${json[@]}" --request-target /jmap -d '{"methodCalls":[["x:Account/get",{},"0"]'
check 403 '' 'methodCalls not an array' "${json[@]}" --request-target /jmap -d '{"methodCalls":{"0":["x:Account/get",{},"0"]}}'
check 403 '' 'empty body' "${json[@]}" --request-target /jmap -X POST
check 413 '' 'body over 1m' "${json[@]}" --request-target /jmap --data-binary @/tmp/huge
check 200 'POST /jmap/upload/a/' 'upload POST /jmap/upload/a/' -H 'Content-Type: application/octet-stream' \
  --request-target /jmap/upload/a/ -d "$mixed"
check 200 'GET /jmap/download/a/b/My%20file.pdf?accept=application/pdf' 'download GET /jmap/download' \
  --request-target '/jmap/download/a/b/My%20file.pdf?accept=application/pdf'
check 200 'GET /jmap/session' 'session GET /jmap/session' --request-target /jmap/session
check 200 'GET /.well-known/jmap' 'GET /.well-known/jmap' --request-target /.well-known/jmap
check 200 'GET /jmap/eventsource/' 'eventsource drops Upgrade header' \
  -H 'Connection: Upgrade' -H 'Upgrade: websocket' --request-target /jmap/eventsource/
check 404 '' 'websocket GET /jmap/ws' -H 'Connection: Upgrade' -H 'Upgrade: websocket' --request-target /jmap/ws
check 200 'PROPFIND /dav/cal/u/' 'PROPFIND /dav/cal/u/' -X PROPFIND --request-target /dav/cal/u/
check 200 'POST /auth/token' 'POST /auth/token' -d grant_type=password --request-target /auth/token
for path in /admin /api/account /account /metrics/prometheus; do
  check 404 '' "GET $path" --request-target "$path"
done

echo "$failures failure(s)"
[[ $failures -eq 0 ]]
