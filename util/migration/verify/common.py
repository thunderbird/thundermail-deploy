"""Shared helpers for the Stalwart 0.15 -> 0.16 verification scripts.

Source (v0.15): Postgres, read-only. Every session runs with default_transaction_read_only=on.
Target (v0.16): JMAP admin API, read-only calls (get/query/download). Admin impersonation (`user%admin`) is used to
read each account. Note: 0.16 creates a default calendar and address book the first time an account is opened,
so the first run can add those to accounts that had none.

Configuration comes from environment variables (see README.md).
"""
import base64, json, os, ssl, subprocess, sys, time, urllib.error, urllib.request

OUT = os.environ.get("VERIFY_OUT", "out")
os.umask(0o077)
os.makedirs(OUT, exist_ok=True)


def need(var):
    v = os.environ.get(var)
    if not v:
        sys.exit(f"missing env var {var} (see README.md)")
    return v


# ---------- JMAP ids ----------
_A = b"abcdefghijklmnopqrstuvwxyz792013"


def enc(n):
    if n == 0:
        return "a"
    M = (1 << 64) - 1
    buf = ""
    i = n >> 60
    n = ((n << 4) | 1) & M
    if i == 0:
        n = (n << ((64 - n.bit_length()) // 5 * 5)) & M
    else:
        buf += chr(_A[i])
    while n != 1 << 60:
        buf += chr(_A[n >> 59])
        n = (n << 5) & M
    return buf


def dec(s):
    n = 0
    for ch in s:
        n = (n << 5) | _A.index(ch.encode())
    return n


# ---------- source: principals dump ----------
def principals():
    """principals.json from `migrate_v016.py dump`. Returns {id: principal} for individual accounts."""
    p = json.load(open(need("PRINCIPALS")))
    items = p if isinstance(p, list) else p.get("items", p)
    return {x["id"]: x for x in items if x.get("type") == "individual"}, items


# ---------- source: Postgres (read-only) ----------
_pg = None


def _pg_conn():
    global _pg
    if _pg is None:
        if os.environ.get("SOURCE_PG_SECRET_ID"):
            out = subprocess.run(["aws", "secretsmanager", "get-secret-value", "--secret-id", os.environ["SOURCE_PG_SECRET_ID"],
                                  "--profile", need("SOURCE_AWS_PROFILE"), "--region", need("AWS_REGION"),
                                  "--query", "SecretString", "--output", "text"], capture_output=True, text=True, check=True).stdout
            s = json.loads(out)
            _pg = {"host": s["host"], "dbname": s["database"], "user": s["user"], "password": s["password"]}
        else:
            _pg = {"host": need("SOURCE_PG_HOST"), "dbname": need("SOURCE_PG_DB"), "user": need("SOURCE_PG_USER"), "password": need("SOURCE_PG_PASSWORD")}
        _pg["host"] = _pg["host"].replace("-pooler", "")
    return _pg


def sql(query):
    """Run one query on the source DB inside a READ ONLY transaction. Returns a list of rows (lists of strings).
    `-X` ignores ~/.psqlrc so nothing can switch read-only off."""
    c = _pg_conn()
    dsn = f"host={c['host']} dbname={c['dbname']} user={c['user']} sslmode=require connect_timeout=10 options='-c default_transaction_read_only=on'"
    psql = ["psql"] if os.environ.get("PSQL_MODE") == "local" else \
        ["podman", "run", "--rm", "--network", "host", "-e", "PGPASSWORD", "docker.io/library/postgres:18-alpine", "psql"]
    args = [dsn, "-X", "-q", "-v", "ON_ERROR_STOP=1", "-AtF", "\t",
            "-c", "BEGIN TRANSACTION READ ONLY", "-c", query, "-c", "COMMIT"]
    r = subprocess.run(psql + args, env=dict(os.environ, PGPASSWORD=c["password"]),
                       capture_output=True, text=True, timeout=1800)
    if r.returncode:
        sys.exit("source query failed: " + r.stderr.strip()[-300:])
    return [l.split("\t") for l in r.stdout.splitlines() if l and l not in ("BEGIN", "COMMIT")]


# v0.15 property table `p`: key = account u32 BE | collection u8 | field u8 | document u32 BE.
# Field 50 is present once on every live document, so it counts documents per collection.
ACC = "get_byte(k,0)::bigint*16777216+get_byte(k,1)*65536+get_byte(k,2)*256+get_byte(k,3)"
DOC = "get_byte(k,6)::bigint*16777216+get_byte(k,7)*65536+get_byte(k,8)*256+get_byte(k,9)"
LIVE = "length(k)=10 and get_byte(k,5)=50"
COLLECTIONS = {0: "emails", 1: "mailboxes", 3: "identities", 5: "sieve", 8: "calendars", 9: "events",
               10: "addressbooks", 11: "contacts", 12: "files"}


# ---------- target: JMAP ----------
_CAPS = ["urn:ietf:params:jmap:core", "urn:ietf:params:jmap:mail", "urn:ietf:params:jmap:submission",
         "urn:ietf:params:jmap:calendars", "urn:ietf:params:jmap:contacts", "urn:ietf:params:jmap:filenode",
         "urn:ietf:params:jmap:sieve", "urn:stalwart:jmap"]
_ctx = ssl.create_default_context()
if os.environ.get("TARGET_INSECURE") == "1":
    _ctx.check_hostname = False
    _ctx.verify_mode = ssl.CERT_NONE


def _auth(who=None):
    u, p = need("TARGET_USER"), need("TARGET_PASSWORD")
    login = f"{who}%{u}" if who else u
    return {"Authorization": "Basic " + base64.b64encode(f"{login}:{p}".encode()).decode()}


def _open(req, timeout=180):
    """urlopen with retries for transient failures. HTTP 400 is retried only when 0.16 reports its
    concurrent-request limit; every other 400 is a real client error and is raised at once."""
    for attempt in range(6):
        try:
            return _opener.open(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            body = e.read()[:2000]
            transient = e.code in (429, 500, 502, 503, 504) or (e.code == 400 and b"limit" in body.lower())
            if transient and attempt < 5:
                wait = e.headers.get("Retry-After")
                time.sleep(float(wait) if wait and wait.isdigit() else 1.5 * (attempt + 1))
                continue
            raise RuntimeError(f"HTTP {e.code} from {req.full_url}: {body[:300]!r}") from None
        except (urllib.error.URLError, ConnectionResetError, TimeoutError) as e:
            if attempt < 5:
                time.sleep(2 * (attempt + 1))
                continue
            raise


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects so the Authorization header is never sent to another host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError(f"refusing redirect {code} from {req.full_url} to {newurl}")


_opener = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=_ctx))


def jmap(calls, who=None):
    """POST method calls to /jmap, optionally as user `who` via admin impersonation. Returns methodResponses."""
    body = json.dumps({"using": _CAPS, "methodCalls": calls}).encode()
    req = urllib.request.Request(need("TARGET_URL").rstrip("/") + "/jmap", data=body,
                                 headers={**_auth(who), "Content-Type": "application/json"})
    return json.load(_open(req))["methodResponses"]


def download(who, account_id, blob_id):
    req = urllib.request.Request(f"{need('TARGET_URL').rstrip('/')}/jmap/download/{account_id}/{blob_id}/b", headers=_auth(who))
    return _open(req, timeout=300).read()


_admin = None


def admin_account():
    global _admin
    if _admin is None:
        req = urllib.request.Request(need("TARGET_URL").rstrip("/") + "/jmap/session", headers=_auth())
        s = json.load(_open(req))
        _admin = s["primaryAccounts"].get("urn:stalwart:jmap") or next(iter(s["accounts"]))
    return _admin


def query_all(method, args, who=None, page=1000):
    """Every id from a JMAP /query. 0.16 caps each query at its query_max_results (default 5000) without
    an error, so page with `position` and compare with the server's total."""
    ids, total = [], None
    while True:
        r = jmap([[method, {**args, "position": len(ids), "limit": page, "calculateTotal": True}, "0"]], who)[0]
        if r[0] == "error":
            raise RuntimeError(f"{method}: {r[1]}")
        batch, total = r[1]["ids"], r[1].get("total", total)
        ids += batch
        if not batch or (total is not None and len(ids) >= total):
            break
    if total is not None and len(ids) != total:
        raise RuntimeError(f"{method}: got {len(ids)} ids, server reports {total}")
    return ids


def x_all(obj):
    """All objects of a Stalwart management type (paged x:Obj/query + paged x:Obj/get)."""
    ids = query_all(f"x:{obj}/query", {"accountId": admin_account()})
    out = []
    for i in range(0, len(ids), 200):
        out += jmap([[f"x:{obj}/get", {"accountId": admin_account(), "ids": ids[i:i + 200]}, "0"]])[0][1]["list"]
    return out


def x_singleton(obj):
    return jmap([[f"x:{obj}/get", {"accountId": admin_account(), "ids": ["singleton"]}, "0"]])[0][1]["list"][0]


def ids_of(who, aid, obj, extra=None):
    return query_all(f"{obj}/query", {"accountId": aid, **(extra or {})}, who)


def get_paged(who, aid, obj, ids, props, page=300):
    out = []
    for i in range(0, len(ids), page):
        r = jmap([[f"{obj}/get", {"accountId": aid, "ids": ids[i:i + page], "properties": props}, "0"]], who)[0]
        if r[0] == "error":
            raise RuntimeError(f"{obj}/get: {r[1]}")
        out += r[1]["list"]
    return out


# ---------- reporting ----------
class Report:
    def __init__(self, name):
        self.name, self.fails, self.warns = name, 0, 0
        print(f"== {name}")

    def ok(self, msg):
        print(f"  PASS  {msg}")

    def warn(self, msg):
        self.warns += 1
        print(f"  WARN  {msg}")

    def fail(self, msg):
        self.fails += 1
        print(f"  FAIL  {msg}")

    def check(self, cond, msg, warn_only=False):
        (self.ok if cond else (self.warn if warn_only else self.fail))(msg)

    def done(self):
        print(f"  -> {self.fails} fail, {self.warns} warn")
        sys.exit(1 if self.fails else 0)


def short(name):
    return name.split("@")[0]
