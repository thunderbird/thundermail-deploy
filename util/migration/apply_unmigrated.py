#!/usr/bin/env python3
"""Re-apply v0.15 settings that migrate_v016.py convert leaves out.

Reads the v0.15 settings dump (settings.json from `migrate_v016.py dump`) and writes the
matching v0.16 objects over the JMAP admin API. With --principals it also restores catch-all
addresses (an `@domain` address on a v0.15 account) as each Domain's catchAllAddress.
Dry run by default; pass --apply to write.
Safe to re-run: existing IPs and webhooks are skipped, singletons are only updated if they differ.

Env: STALWART_URL (e.g. http://localhost:18081), STALWART_USER, STALWART_PASSWORD.
--expect-hostname is required: the script reads the target's defaultHostname and stops on a mismatch.
Exit code is 1 if any write failed.
"""
import argparse, base64, json, os, re, sys, urllib.request

EVENT_RENAMES = {"queue.queue-message-authenticated": "queue.authenticated-message-queued"}
UNITS = {"ms": 1, "s": 1000, "m": 60_000, "h": 3_600_000, "d": 86_400_000}


def duration_ms(v):
    m = re.fullmatch(r"(\d+)\s*(ms|s|m|h|d)", str(v).strip())
    if not m:
        raise ValueError(f"unparsed duration {v!r}")
    return int(m[1]) * UNITS[m[2]]


def rate(v):
    count, per = str(v).split("/", 1)
    return {"count": int(count), "period": duration_ms(per if per[0].isdigit() else "1" + per)}


def truthy(v):
    return str(v).lower() == "true"


class Jmap:
    def __init__(self):
        base = os.environ["STALWART_URL"].rstrip("/")
        cred = f'{os.environ["STALWART_USER"]}:{os.environ["STALWART_PASSWORD"]}'
        self.hdr = {"Authorization": "Basic " + base64.b64encode(cred.encode()).decode(), "Content-Type": "application/json"}
        self.url = base + "/jmap"
        s = json.load(urllib.request.urlopen(urllib.request.Request(self.url + "/session", headers=self.hdr), timeout=60))
        self.acct = s["primaryAccounts"].get("urn:stalwart:jmap") or next(iter(s["accounts"]))

    def call(self, method, args):
        body = {"using": ["urn:ietf:params:jmap:core", "urn:stalwart:jmap"], "methodCalls": [[method, {"accountId": self.acct, **args}, "0"]]}
        r = json.load(urllib.request.urlopen(urllib.request.Request(self.url, data=json.dumps(body).encode(), headers=self.hdr), timeout=120))
        name, res, _ = r["methodResponses"][0]
        if name == "error":
            raise RuntimeError(f"{method}: {res}")
        return res

    def all(self, obj):
        """Every object of a type. 0.16 caps each query at query_max_results (default 5000) without an error,
        so page with `position` and check the count against the server's total."""
        ids, total = [], None
        while True:
            r = self.call(f"x:{obj}/query", {"position": len(ids), "limit": 1000, "calculateTotal": True})
            ids += r["ids"]
            total = r.get("total", total)
            if not r["ids"] or (total is not None and len(ids) >= total):
                break
        if total is not None and len(ids) != total:
            raise RuntimeError(f"x:{obj}/query: got {len(ids)} ids, server reports {total}")
        out = []
        for i in range(0, len(ids), 200):
            out += self.call(f"x:{obj}/get", {"ids": ids[i:i + 200]})["list"]
        return out

    def singleton(self, obj):
        return self.call(f"x:{obj}/get", {"ids": ["singleton"]})["list"][0]


def plan(s):
    """Build (object, kind, payload, label) tuples from the v0.15 settings dict."""
    out = []

    hooks = sorted({k.split(".")[1] for k in s if k.startswith("webhook.")})
    for h in hooks:
        p = f"webhook.{h}."
        events = [EVENT_RENAMES.get(v, v) for k, v in sorted(s.items()) if k.startswith(p + "events.")]
        hook = {"url": s[p + "url"], "enable": truthy(s.get(p + "enable", "true")),
                "events": {e: True for e in events}, "eventsPolicy": "include",
                "lossy": truthy(s.get(p + "lossy", "false")), "allowInvalidCerts": truthy(s.get(p + "allow-invalid-certs", "false"))}
        for key, field in (("throttle", "throttle"), ("timeout", "timeout"), ("discard-after", "discardAfter")):
            if p + key in s:
                hook[field] = duration_ms(s[p + key])
        if s.get(p + "signature-key"):
            hook["signatureKey"] = {"@type": "Value", "secret": s[p + "signature-key"]}
        out.append(("WebHook", "create", hook, f"webhook {h} ({len(events)} events)"))

    for kind, prefix in (("BlockedIp", "server.blocked-ip."), ("AllowedIp", "server.allowed-ip.")):
        addrs = sorted(k[len(prefix):] for k in s if k.startswith(prefix))
        for a in addrs:
            item = {"address": a, "reason": "manual" if kind == "BlockedIp" else "migrated from v0.15"}
            out.append((kind, "create", item, a))

    if "session.mta-sts.mx" in s or "session.mta-sts.mode" in s:
        mx = [v for k, v in sorted(s.items()) if k == "session.mta-sts.mx" or k.startswith("session.mta-sts.mx.")]
        upd = {"mxHosts": {m: True for m in mx}}
        if "session.mta-sts.mode" in s:
            upd["mode"] = s["session.mta-sts.mode"]
        if "session.mta-sts.max-age" in s:
            upd["maxAge"] = duration_ms(s["session.mta-sts.max-age"])
        out.append(("MtaSts", "update", upd, "MTA-STS"))

    quotas = sorted({k.split(".")[2] for k in s if k.startswith("queue.quota.")})
    scoped = sorted(q for q in quotas if {k.split(".", 3)[3] for k in s if k.startswith(f"queue.quota.{q}.")} - {"enable", "messages", "size"})
    if scoped or len(quotas) > 1:
        why = f"scoped ({', '.join(scoped)})" if scoped else f"{len(quotas)} quotas share one global rule"
        out.append(("MtaQueueQuota", "refuse", {}, f"queue quotas {', '.join(quotas)}: {why}; recreate them by hand"))
        quotas = []
    for q in quotas:
        p = f"queue.quota.{q}."
        upd = {"enable": truthy(s.get(p + "enable", "true"))}
        if p + "messages" in s:
            upd["messages"] = int(s[p + "messages"])
        if p + "size" in s:
            upd["size"] = int(s[p + "size"])
        out.append(("MtaQueueQuota", "update-global", upd, f"queue quota {q}"))

    sec = {}
    for key, field in (("auth", "auth"), ("abuse", "abuse"), ("loiter", "loiter"), ("scan", "scan")):
        if f"server.auto-ban.{key}.rate" in s:
            sec[f"{field}BanRate"] = rate(s[f"server.auto-ban.{key}.rate"])
        if f"server.auto-ban.{key}.period" in s:
            sec[f"{field}BanPeriod"] = duration_ms(s[f"server.auto-ban.{key}.period"])
    if sec:
        out.append(("Security", "update", sec, "auto-ban rates"))

    for obj, prefix in (("Calendar", "calendar.default."), ("AddressBook", "contacts.default.")):
        upd = {}
        if prefix + "display-name" in s:
            upd["defaultDisplayName"] = s[prefix + "display-name"]
        if prefix + "href-name" in s:
            upd["defaultHrefName"] = s[prefix + "href-name"]
        if upd:
            out.append((obj, "update", upd, f"{obj} default names"))

    http = {}
    if "http.permissive-cors" in s:
        http["usePermissiveCors"] = truthy(s["http.permissive-cors"])
    if "http.hsts" in s:
        http["enableHsts"] = truthy(s["http.hsts"])
    if http:
        out.append(("Http", "update", http, "HTTP CORS/HSTS"))
    return out


def catch_alls(principals_path):
    """v0.15 catch-alls: an account address of the form `@domain`. Returns {domain: owner's primary address}.
    `convert` drops them; in 0.16 a catch-all is the Domain's catchAllAddress."""
    p = json.load(open(principals_path))
    items = p if isinstance(p, list) else p.get("items", p)
    out = {}
    for x in items:
        if x.get("type") != "individual":
            continue
        emails = x.get("emails") or []
        emails = emails if isinstance(emails, list) else [emails]
        primary = next((e for e in emails if not e.startswith("@")), x["name"])
        for e in emails:
            if e.startswith("@"):
                d = e[1:].lower()
                if d in out and out[d] != primary:
                    sys.exit(f"catch-all @{d} is claimed by both {out[d]} and {primary}; fix it in v0.15 first")
                out[d] = primary
    return out


KINDS = ("WebHook", "BlockedIp", "AllowedIp", "MtaSts", "MtaQueueQuota", "Security", "Calendar", "AddressBook", "Http", "Domain")


def parse_only(values):
    only = {k for v in values for k in v.split(",") if k}
    unknown = sorted(only - set(KINDS))
    if unknown:
        sys.exit(f"--only: unknown object type(s) {', '.join(unknown)} (one of {', '.join(KINDS)})")
    return only


def wanted(kind, only, skip):
    return kind not in skip and (not only or kind in only)


def redact(d):
    return {k: ("<secret>" if k == "signatureKey" else v) for k, v in d.items()}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--settings", default="settings.json", help="v0.15 settings dump (default: settings.json)")
    ap.add_argument("--principals", help="v0.15 principals dump; when given, catch-all addresses are re-applied too")
    ap.add_argument("--webhook-url", action="append", default=[], metavar="NAME=URL", help="override a webhook URL, e.g. posthog=https://...")
    ap.add_argument("--webhook-secret-env", action="append", default=[], metavar="NAME=ENVVAR",
                    help="take a webhook's signing key from an env var instead of the dump, e.g. posthog=WEBHOOK_SECRET")
    ap.add_argument("--skip", action="append", default=[], help="object type to skip, e.g. --skip BlockedIp")
    ap.add_argument("--only", action="append", default=[], metavar="KIND[,KIND]",
                    help="apply only these object types (inverse of --skip), e.g. --only BlockedIp,Domain")
    ap.add_argument("--expect-hostname", required=True,
                    help="the target's defaultHostname; the script stops if STALWART_URL points at a different server")
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    a = ap.parse_args()
    only = parse_only(a.only)
    if os.environ.get("OLD_STALWART_URL") and os.environ["OLD_STALWART_URL"].rstrip("/") == os.environ["STALWART_URL"].rstrip("/"):
        sys.exit("STALWART_URL is the same as OLD_STALWART_URL: refusing to write to the source")

    s = json.load(open(a.settings))
    for o in a.webhook_url:
        name, url = o.split("=", 1)
        s[f"webhook.{name}.url"] = url
    for o in a.webhook_secret_env:
        name, var = o.split("=", 1)
        if not os.environ.get(var):
            sys.exit(f"{var} is empty")
        s[f"webhook.{name}.signature-key"] = os.environ[var]
    steps = [st for st in plan(s) if wanted(st[0], only, a.skip)]
    j = Jmap()
    host = j.singleton("SystemSettings").get("defaultHostname")
    bucket = j.singleton("BlobStore").get("bucket")
    print(f"target: {os.environ['STALWART_URL']} hostname={host} blob bucket={bucket}")
    if host != a.expect_hostname:
        sys.exit(f"target hostname is {host!r}, expected {a.expect_hostname!r}: wrong server, nothing written")
    failures = 0
    refused = [label for obj, kind, payload, label in steps if kind == "refuse"]
    if refused:
        sys.exit("cannot carry over safely, nothing written: " + "; ".join(refused) + " (or pass --skip MtaQueueQuota)")

    existing = {o: {x["address"] for x in j.all(o)} for o in ("BlockedIp", "AllowedIp")}
    hooks = {x["url"] for x in j.all("WebHook")}
    summary = {}
    creates = {}
    for obj, kind, payload, label in steps:
        if kind == "create":
            if obj in existing and payload["address"] in existing[obj]:
                summary.setdefault(obj, {"skip": 0, "create": 0})["skip"] += 1
                continue
            if obj == "WebHook" and payload["url"] in hooks:
                print(f"  skip   {label}: a webhook with that URL exists")
                continue
            creates.setdefault(obj, []).append(payload)
            summary.setdefault(obj, {"skip": 0, "create": 0})["create"] += 1
            if obj == "WebHook":
                print(f"  create {label}: {json.dumps(redact(payload))}")
            continue
        if kind == "update-global":
            rules = [q for q in j.all(obj) if not q.get("key") and not (q.get("match") or {}).get("match")]
            if not rules:
                creates.setdefault(obj, []).append({"description": "Global queue quota", **payload})
                print(f"  create {label}: {payload}")
                continue
            cur, target = rules[0], rules[0]["id"]
        else:
            cur, target = j.singleton(obj), "singleton"
        diff = {k: v for k, v in payload.items() if cur.get(k) != v}
        if not diff:
            print(f"  ok     {label}: already set")
            continue
        print(f"  update {label}: " + ", ".join(f"{k} {cur.get(k)!r} -> {v!r}" for k, v in redact(diff).items()))
        if a.apply:
            r = j.call(f"x:{obj}/set", {"update": {target: diff}})
            if r.get("notUpdated") or target not in (r.get("updated") or {}):
                failures += 1
                print(f"    FAILED: {r.get('notUpdated') or 'not in updated'}")
    for obj, c in summary.items():
        if obj in ("BlockedIp", "AllowedIp"):
            print(f"  {obj}: {c['create']} to create, {c['skip']} already present")

    if a.principals and wanted("Domain", only, a.skip):
        domains = {d["name"].lower(): d for d in j.all("Domain")}
        for dom, addr in sorted(catch_alls(a.principals).items()):
            d = domains.get(dom)
            if not d:
                failures += 1
                print(f"  FAILED catch-all @{dom}: domain does not exist on the target")
                continue
            if d.get("catchAllAddress") == addr:
                print(f"  ok     catch-all @{dom}: already {addr}")
                continue
            print(f"  update catch-all @{dom}: {d.get('catchAllAddress')!r} -> {addr!r}")
            if a.apply:
                r = j.call("x:Domain/set", {"update": {d["id"]: {"catchAllAddress": addr}}})
                if r.get("notUpdated") or d["id"] not in (r.get("updated") or {}):
                    failures += 1
                    print(f"    FAILED: {r.get('notUpdated') or 'not in updated'}")

    if a.apply:
        for obj, items in creates.items():
            ok = bad = 0
            for i in range(0, len(items), 100):
                batch = {f"c{i + n}": it for n, it in enumerate(items[i:i + 100])}
                r = j.call(f"x:{obj}/set", {"create": batch})
                ok += len(r.get("created") or {})
                for cid, err in (r.get("notCreated") or {}).items():
                    bad += 1
                    if bad <= 5:
                        print(f"    {obj} {cid} not created: {err}")
            print(f"  {obj}: created {ok}, failed {bad}")
            failures += bad
        print("then run the ReloadSettings and ReloadBlockedIps actions, or the changes are stored but not live")
    else:
        print("dry run: nothing written (pass --apply)")
    if failures:
        print(f"{failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
