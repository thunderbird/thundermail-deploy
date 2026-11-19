import http.server
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import importlib.util, pathlib, unittest

spec = importlib.util.spec_from_file_location("apply_unmigrated", pathlib.Path(__file__).with_name("apply_unmigrated.py"))
au = importlib.util.module_from_spec(spec)
spec.loader.exec_module(au)

SETTINGS = {
    "server.blocked-ip.192.0.2.1": "",
    "server.allowed-ip.198.51.100.7": "",
    "webhook.posthog.url": "https://example.invalid/hook",
    "http.hsts": "true",
}


def kinds(only, skip):
    return sorted({st[0] for st in au.plan(SETTINGS) if au.wanted(st[0], only, skip)})


class Only(unittest.TestCase):
    def test_default_keeps_everything(self):
        self.assertEqual(kinds(set(), []), ["AllowedIp", "BlockedIp", "Http", "WebHook"])

    def test_only_is_the_inverse_of_skip(self):
        only = au.parse_only(["BlockedIp"])
        self.assertEqual(kinds(only, []), ["BlockedIp"])
        self.assertEqual(kinds(only, []), kinds(set(), ["AllowedIp", "Http", "WebHook"]))

    def test_comma_list_and_repeats(self):
        self.assertEqual(au.parse_only(["BlockedIp,Domain", "Http"]), {"BlockedIp", "Domain", "Http"})

    def test_skip_wins_over_only(self):
        self.assertEqual(kinds(au.parse_only(["BlockedIp,Http"]), ["Http"]), ["BlockedIp"])

    def test_domain_catch_alls_follow_only(self):
        self.assertTrue(au.wanted("Domain", au.parse_only(["BlockedIp,Domain"]), []))
        self.assertFalse(au.wanted("Domain", au.parse_only(["BlockedIp"]), []))
        self.assertTrue(au.wanted("Domain", set(), []))

    def test_unknown_kind_is_rejected(self):
        with self.assertRaises(SystemExit):
            au.parse_only(["BlockedIps"])


class Quotas(unittest.TestCase):
    def test_one_unscoped_quota_updates_the_global_rule(self):
        steps = au.plan({"queue.quota.all.messages": "100", "queue.quota.all.size": "1000"})
        self.assertEqual([(o, k) for o, k, _, _ in steps], [("MtaQueueQuota", "update-global")])

    def test_scoped_quota_is_refused(self):
        steps = au.plan({"queue.quota.sender.key.0": "sender", "queue.quota.sender.messages": "10"})
        self.assertEqual([(o, k) for o, k, _, _ in steps], [("MtaQueueQuota", "refuse")])

    def test_several_quotas_are_refused(self):
        steps = au.plan({"queue.quota.a.messages": "1", "queue.quota.b.messages": "2"})
        self.assertEqual([(o, k) for o, k, _, _ in steps], [("MtaQueueQuota", "refuse")])

    def test_skip_drops_the_refusal(self):
        steps = au.plan({"queue.quota.sender.match": "x", "server.blocked-ip.192.0.2.1": ""})
        self.assertEqual(sorted({st[0] for st in steps if au.wanted(st[0], set(), ["MtaQueueQuota"])}), ["BlockedIp"])


HERE = pathlib.Path(__file__).resolve().parent


class _Fake(http.server.BaseHTTPRequestHandler):
    domains = []

    def log_message(self, *a):
        pass

    def send(self, body):
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self.send({"primaryAccounts": {"urn:stalwart:jmap": "a"}, "accounts": {"a": {}}})

    def do_POST(self):
        name, args, tag = json.loads(self.rfile.read(int(self.headers["Content-Length"])))["methodCalls"][0]
        if name == "x:SystemSettings/get":
            res = {"list": [{"defaultHostname": "mail.example.test"}]}
        elif name == "x:BlobStore/get":
            res = {"list": [{"bucket": "b"}]}
        elif name == "x:Domain/query":
            res = {"ids": [d["id"] for d in self.domains][args.get("position", 0):], "total": len(self.domains)}
        elif name.endswith("/query"):
            res = {"ids": [], "total": 0}
        else:
            res = {"list": [d for d in self.domains if d["id"] in args["ids"]]}
        self.send({"methodResponses": [[name, res, tag]]})


class CatchAllsOnDroppedOrUnicodeDomains(unittest.TestCase):
    def test_a_dropped_domain_is_skipped_and_a_unicode_name_found_in_punycode(self):
        t = pathlib.Path(tempfile.mkdtemp())
        _Fake.domains = [
            {"id": "d1", "name": "a.example.com", "catchAllAddress": None},
            {"id": "d2", "name": "xn--mnchen-3ya.example.com", "catchAllAddress": None},
        ]
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Fake)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        (t / "settings.json").write_text("{}")
        (t / "principals.json").write_text(json.dumps([
            {"id": 1, "type": "individual", "name": "u", "emails": ["u@a.example.com", "@a.example.com"]},
            {"id": 2, "type": "individual", "name": "v", "emails": ["v@a.example.com", "@mail.example.notatld"]},
            {"id": 3, "type": "individual", "name": "w", "emails": ["w@a.example.com", "@m\u00fcnchen.example.com"]},
            {"id": 4, "type": "individual", "name": "x", "emails": ["x@a.example.com", "@b\u00fccher.example.com"]},
            {"id": 5, "type": "individual", "name": "y", "emails": ["y@a.example.com", "@xn--bcher-kva.example.com"]},
        ], ensure_ascii=False))
        (t / "dropped.json").write_text(json.dumps(
            {"domains": ["mail.example.notatld", "b\u00fccher.example.com", "xn--bcher-kva.example.com"], "dkim_dropped": 0, "dkim_kept": 0},
            ensure_ascii=False,
        ))
        env = dict(os.environ, STALWART_URL="http://127.0.0.1:%d" % srv.server_port, STALWART_USER="a", STALWART_PASSWORD="p")
        env.pop("OLD_STALWART_URL", None)
        p = subprocess.run(
            [sys.executable, str(HERE / "apply_unmigrated.py"), "--settings", str(t / "settings.json"),
             "--principals", str(t / "principals.json"), "--dropped-domains", str(t / "dropped.json"),
             "--expect-hostname", "mail.example.test", "--only", "Domain"],
            capture_output=True, text=True, env=env, timeout=60,
        )
        self.assertNotIn("FAILED", p.stdout, p.stdout + p.stderr)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("skip   catch-alls on Domains dropped before the apply (domain_fix.py): 3", p.stdout)
        self.assertIn("update catch-all @xn--mnchen-3ya.example.com", p.stdout)
        self.assertIn("update catch-all @a.example.com", p.stdout)


if __name__ == "__main__":
    unittest.main()
