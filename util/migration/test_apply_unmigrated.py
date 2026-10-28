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


if __name__ == "__main__":
    unittest.main()
