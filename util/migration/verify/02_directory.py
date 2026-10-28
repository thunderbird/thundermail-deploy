#!/usr/bin/env python3
"""02: directory. Accounts, ids, names, addresses, aliases, catch-alls and quota limits: source dump vs target."""
from common import principals, x_all, dec, Report, short, norm_addr, norm_domain, strings, unwrap_int, unwrap_str

r = Report("02 directory")
src, items = principals()
doms = {d["id"]: d for d in x_all("Domain")}
dname = {i: norm_domain(d["name"]) for i, d in doms.items()}
accounts = x_all("Account")
tgt = {dec(a["id"]): a for a in accounts if a.get("@type", "User") == "User"}

r.check(set(src) == set(tgt), f"account ids: source {len(src)}, target {len(tgt)}, "
        f"missing {sorted(set(src) - set(tgt))[:5]}, extra {sorted(set(tgt) - set(src))[:5]}")
src_groups = {unwrap_int(x.get("id")) for x in items if unwrap_str(x.get("type")) == "group"}
tgt_groups = {dec(a["id"]) for a in accounts if a.get("@type") == "Group"}
r.check(src_groups == tgt_groups, f"group ids: source {len(src_groups)}, target {len(tgt_groups)}, "
        f"missing {sorted(src_groups - tgt_groups)[:5]}, extra {sorted(tgt_groups - src_groups)[:5]}")


def addresses(a):
    s = {norm_addr(a["emailAddress"])} if a.get("emailAddress") else set()
    for v in (a.get("aliases") or {}).values():
        if isinstance(v, dict):
            s.add(f"{v.get('name')}@{dname.get(v.get('domainId'))}")
    return s


catch_all = {norm_domain(d["name"]): norm_addr(d.get("catchAllAddress") or "") or None for d in doms.values()}
names = addrs = quotas = 0
missing_catchall = []
for lid, l in src.items():
    a = tgt.get(lid)
    if not a:
        continue
    lname = unwrap_str(l.get("name"))
    local, _, dom = lname.rpartition("@") if "@" in lname else (lname, "", "")
    if a["name"] != local or (dom and dname.get(a["domainId"]) != norm_domain(dom)):
        names += 1
    emails = [norm_addr(e) for e in strings(l.get("emails"))]
    want = set(emails)
    primary = next((e for e in emails if not e.startswith("@")), lname)
    for miss in want - addresses(a):
        if miss.startswith("@"):
            if catch_all.get(miss[1:]) != primary:
                missing_catchall.append(f"{short(lname)} {miss} -> {catch_all.get(miss[1:])!r}")
        else:
            addrs += 1
    if (unwrap_int(l.get("quota")) or 0) != ((a.get("quotas") or {}).get("maxDiskQuota") or 0):
        quotas += 1
r.check(names == 0, f"account names (local part, and domain when the source name has one) mismatched: {names}")
r.check(addrs == 0, f"primary/alias addresses missing: {addrs}")
r.check(not missing_catchall, f"catch-all addresses missing or owned by another account (set Domain.catchAllAddress): {missing_catchall}")
r.check(quotas == 0, f"quota limits mismatched: {quotas}")

src_domains = {norm_domain(unwrap_str(x.get("name"))) for x in items if unwrap_str(x.get("type")) == "domain"}
src_domains |= {norm_addr(e).rpartition("@")[2] for l in src.values() for e in strings(l.get("emails")) if "@" in e}
r.check(src_domains <= set(dname.values()), f"domains used on source present on target: missing {sorted(src_domains - set(dname.values()))}")
dk = x_all("DkimSignature")
no_dkim = sorted(set(dname.values()) - {dname.get(d.get("domainId")) for d in dk})
r.check(not no_dkim, f"every domain has a DKIM key ({len(dk)} keys); without: {no_dkim}", warn_only=True)
r.done()
