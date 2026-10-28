#!/usr/bin/env python3
"""07: target configuration. Right server, stores point at the target's own bucket, settings re-applied, no internal
(proxy/load balancer) address auto-banned, webhooks present. Required: EXPECT_HOSTNAME, EXPECT_BUCKET,
INTERNAL_CIDRS (comma-separated, e.g. 10.0.0.0/16)."""
import ipaddress
from common import need, x_all, x_singleton, Report

expect_host, expect_bucket = need("EXPECT_HOSTNAME"), need("EXPECT_BUCKET")
nets = [ipaddress.ip_network(c.strip()) for c in need("INTERNAL_CIDRS").split(",") if c.strip()]

r = Report("07 target configuration")
host = x_singleton("SystemSettings").get("defaultHostname")
r.check(host == expect_host, f"default hostname is {host} (expected {expect_host})")
bs = x_singleton("BlobStore")
r.check(bs.get("bucket") == expect_bucket, f"blob store {bs.get('@type')} bucket is {bs.get('bucket')} (expected {expect_bucket})")
r.ok(f"in-memory store {x_singleton('InMemoryStore').get('@type')}, search store {x_singleton('SearchStore').get('@type')}, "
     f"data store {x_singleton('DataStore').get('@type')}")


def inside(addr, net):
    a = ipaddress.ip_network(addr, strict=False)
    return a.version == net.version and a.subnet_of(net)


blocked = x_all("BlockedIp")
banned = [b for b in blocked if b.get("reason") != "manual"]
internal = [b for b in banned if any(inside(b["address"], n) for n in nets)]
allowed = x_all("AllowedIp")
covered = [b["address"] for b in internal if any(inside(b["address"], ipaddress.ip_network(a["address"], strict=False)) for a in allowed)]
r.check(not internal, f"auto-banned internal addresses: {[(b['address'], b['reason'], b['createdAt']) for b in internal]}"
        + (f" (also in the allow list, so not enforced: {covered})" if covered else ""))
r.ok(f"blocked IPs {len(blocked)} ({len(banned)} automatic), allowed IPs {len(allowed)}")
hooks = x_all("WebHook")
r.check(bool(hooks), f"webhooks: {[h.get('url', '').split('/')[2] if '://' in h.get('url', '') else h.get('url') for h in hooks]}", warn_only=True)
r.ok(f"queue quotas: {[(x.get('messages'), x.get('size')) for x in x_all('MtaQueueQuota')]}")
sec = x_singleton("Security")
r.ok(f"auth ban rate {sec.get('authBanRate')}, abuse ban rate {sec.get('abuseBanRate')}")
mx = x_singleton("MtaSts").get("mxHosts") or {}
r.check(bool(mx), f"MTA-STS MX hosts: {sorted(mx)}", warn_only=True)
r.done()
