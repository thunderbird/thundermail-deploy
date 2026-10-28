#!/usr/bin/env python3
"""02: directory. Accounts, ids, names, addresses, aliases, catch-alls and quota limits: source dump vs target."""
from common import principals, x_all, dec, Report, short

r = Report("02 directory")
src, items = principals()
doms = {d["id"]: d for d in x_all("Domain")}
dname = {i: d["name"] for i, d in doms.items()}
tgt = {dec(a["id"]): a for a in x_all("Account")}

r.check(set(src) == set(tgt), f"account ids: source {len(src)}, target {len(tgt)}, "
        f"missing {sorted(set(src) - set(tgt))[:5]}, extra {sorted(set(tgt) - set(src))[:5]}")


def addresses(a):
    s = {a["emailAddress"]} if a.get("emailAddress") else set()
    for v in (a.get("aliases") or {}).values():
        if isinstance(v, dict):
            s.add(f"{v.get('name')}@{dname.get(v.get('domainId'))}")
    return s


catch_all = {d["name"]: d.get("catchAllAddress") for d in doms.values()}
names = addrs = quotas = 0
missing_catchall = []
for lid, l in src.items():
    a = tgt.get(lid)
    if not a:
        continue
    if f"{a['name']}@{dname.get(a['domainId'])}" != l["name"]:
        names += 1
    emails = l["emails"] if isinstance(l["emails"], list) else [l["emails"]]
    want = set(emails)
    primary = next((e for e in emails if not e.startswith("@")), l["name"])
    for miss in want - addresses(a):
        if miss.startswith("@"):
            if catch_all.get(miss[1:]) != primary:
                missing_catchall.append(f"{short(l['name'])} {miss} -> {catch_all.get(miss[1:])!r}")
        else:
            addrs += 1
    if (l.get("quota") or 0) != ((a.get("quotas") or {}).get("maxDiskQuota") or 0):
        quotas += 1
r.check(names == 0, f"account names (local@domain) mismatched: {names}")
r.check(addrs == 0, f"primary/alias addresses missing: {addrs}")
r.check(not missing_catchall, f"catch-all addresses missing or owned by another account (set Domain.catchAllAddress): {missing_catchall}")
r.check(quotas == 0, f"quota limits mismatched: {quotas}")

src_domains = {x["name"] for x in items if x.get("type") == "domain"}
src_domains |= {e.split("@", 1)[1] for l in src.values() for e in (l["emails"] if isinstance(l["emails"], list) else [l["emails"]])}
r.check(src_domains <= set(dname.values()), f"domains used on source present on target: missing {sorted(src_domains - set(dname.values()))}")
dk = x_all("DkimSignature")
no_dkim = sorted(set(dname.values()) - {dname.get(d.get("domainId")) for d in dk})
r.check(not no_dkim, f"every domain has a DKIM key ({len(dk)} keys); without: {no_dkim}", warn_only=True)
r.done()
