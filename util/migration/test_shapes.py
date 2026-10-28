import csv, importlib.util, json, os, pathlib, subprocess, sys, tempfile, textwrap, unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import shapes


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


au = load("apply_unmigrated", HERE / "apply_unmigrated.py")

PRINCIPALS = [
    {"id": 1, "type": "individual", "name": {"string": "alice@Example.COM"},
     "emails": {"stringList": ["alice@Example.COM", "@Example.COM"]},
     "secrets": {"stringList": ["$app$Phone$h", "otpauth://totp/a?secret=X"]}, "quota": {"integer": 1000}},
    {"id": {"integer": 2}, "type": {"string": "individual"}, "name": ["bob"],
     "emails": {"string": "bob@example.com"}, "secrets": "$app$Laptop$h"},
    {"id": 7, "type": "group", "name": "team@example.com", "emails": ["team@example.com"]},
    {"id": 9, "type": "domain", "name": "Example.COM"},
]


class Shapes(unittest.TestCase):
    def test_unwrap_list(self):
        self.assertEqual(shapes.unwrap_list({"stringList": ["a", "b"]}), ["a", "b"])
        self.assertEqual(shapes.unwrap_list({"string": "a"}), ["a"])
        self.assertEqual(shapes.unwrap_list({"integerList": [1]}), [1])
        self.assertEqual(shapes.unwrap_list("a"), ["a"])
        self.assertEqual(shapes.unwrap_list(None), [])
        self.assertEqual(shapes.unwrap_list({"other": 1}), [])

    def test_unwrap_str_and_int(self):
        self.assertEqual(shapes.unwrap_str({"string": "alice"}), "alice")
        self.assertEqual(shapes.unwrap_str({"stringList": ["alice", "x"]}), "alice")
        self.assertEqual(shapes.unwrap_str(["bob"]), "bob")
        self.assertEqual(shapes.unwrap_str(None), "")
        self.assertEqual(shapes.unwrap_int({"integer": 2}), 2)
        self.assertEqual(shapes.unwrap_int("3"), 3)
        self.assertIsNone(shapes.unwrap_int("x"))

    def test_duration_ms(self):
        self.assertEqual(shapes.duration_ms("1w"), 604_800_000)
        self.assertEqual(shapes.duration_ms("500"), 500)
        self.assertEqual(shapes.duration_ms("30s"), 30_000)
        self.assertEqual(shapes.duration_ms("5 M"), 300_000)
        with self.assertRaises(ValueError):
            shapes.duration_ms("1y")

    def test_norm_addr(self):
        self.assertEqual(shapes.norm_addr("Alice@Example.COM "), "Alice@example.com")
        self.assertEqual(shapes.norm_addr("@Example.COM"), "@example.com")
        self.assertEqual(shapes.norm_addr("bob"), "bob")


class ApplyUnmigrated(unittest.TestCase):
    def test_catch_alls_read_wrapped_and_mixed_case_emails(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(PRINCIPALS, f)
        self.assertEqual(au.catch_alls(f.name), {"example.com": "alice@example.com"})

    def test_indexed_mx_and_converter_durations(self):
        steps = au.plan({"session.mta-sts.mx.0": "mail.example.com", "session.mta-sts.max-age": "1w",
                         "webhook.h.url": "https://example.invalid", "webhook.h.timeout": "1500"})
        mts = [p for o, k, p, _ in steps if o == "MtaSts"]
        self.assertEqual(mts, [{"mxHosts": {"mail.example.com": True}, "maxAge": 604_800_000}])
        self.assertEqual([p["timeout"] for o, k, p, _ in steps if o == "WebHook"], [1500])


class NoticeScripts(unittest.TestCase):
    def run_script(self, name):
        d = tempfile.mkdtemp()
        src, out = os.path.join(d, "p.json"), os.path.join(d, "out.csv")
        pathlib.Path(src).write_text(json.dumps(PRINCIPALS))
        subprocess.run([sys.executable, "-I", str(HERE / name), "--principals", src, "--out", out], check=True, capture_output=True)
        return list(csv.DictReader(pathlib.Path(out).read_text().splitlines()))

    def test_app_password_users_reads_wrapped_names(self):
        rows = self.run_script("app_password_users.py")
        self.assertEqual([(r["account"], r["primary_address"]) for r in rows],
                         [("alice@Example.COM", "alice@example.com"), ("bob", "bob@example.com")])

    def test_totp_users_reads_wrapped_names(self):
        rows = self.run_script("totp_users.py")
        self.assertEqual([(r["account"], r["primary_address"]) for r in rows], [("alice@Example.COM", "alice@example.com")])


class Directory(unittest.TestCase):
    def test_02_normalises_domains_and_reads_wrapped_values(self):
        d = tempfile.mkdtemp()
        real = HERE / "verify" / "common.py"
        (pathlib.Path(d) / "common.py").write_text(textwrap.dedent(f"""
            import importlib.util, json, os
            _s = importlib.util.spec_from_file_location("real_common", {str(real)!r})
            _m = importlib.util.module_from_spec(_s)
            _s.loader.exec_module(_m)
            globals().update({{k: getattr(_m, k) for k in dir(_m) if not k.startswith("__")}})
            with open(os.environ["FAKE_TARGET"]) as _f:
                _t = json.load(_f)
            def x_all(o):
                return _t[o]
        """))
        (pathlib.Path(d) / "02_directory.py").write_text((HERE / "verify" / "02_directory.py").read_text())
        common = load("real_common_for_ids", real)
        enc = common.enc
        target = {
            "Domain": [{"id": "d", "name": "example.com", "catchAllAddress": "alice@example.com"}],
            "Account": [
                {"@type": "User", "id": enc(1), "name": "alice", "domainId": "d", "emailAddress": "alice@example.com",
                 "quotas": {"maxDiskQuota": 1000}},
                {"@type": "User", "id": enc(2), "name": "bob", "domainId": "d", "emailAddress": "bob@example.com"},
                {"@type": "Group", "id": enc(7), "name": "team", "domainId": "d", "emailAddress": "team@example.com"},
            ],
            "DkimSignature": [{"domainId": "d"}],
        }
        pathlib.Path(d, "p.json").write_text(json.dumps(PRINCIPALS))
        pathlib.Path(d, "t.json").write_text(json.dumps(target))
        env = dict(os.environ, PRINCIPALS=os.path.join(d, "p.json"), FAKE_TARGET=os.path.join(d, "t.json"), VERIFY_OUT=d)
        r = subprocess.run([sys.executable, "02_directory.py"], cwd=d, env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("FAIL", r.stdout)


if __name__ == "__main__":
    unittest.main()
