#!/usr/bin/env python3
"""04: collections and content. Per-account counts of folders, calendars, events, address books, contacts, files, sieve
scripts and emails vs the source; file bodies downloaded; event titles and contact emails matched by UID against
the source search index. Needs $VERIFY_OUT/source.json from 01."""
import json, re
from concurrent.futures import ThreadPoolExecutor
from common import principals, enc, jmap, ids_of, get_paged, download, OUT, Report, short

r = Report("04 collections and content")
src_acc, _ = principals()
S = json.load(open(f"{OUT}/source.json"))
src = {int(k): v for k, v in S["accounts"].items()}

# Identity/get is left out on purpose: on 0.16 it creates missing identities and deletes ones whose address no
# longer resolves to the account, so reading it changes the target.
GET = {"mailboxes": "Mailbox", "calendars": "Calendar", "addressbooks": "AddressBook"}
QUERY = {"emails": "Email", "events": "CalendarEvent", "contacts": "ContactCard", "files": "FileNode", "sieve": "SieveScript"}


def account(lid):
    who, aid = src_acc[lid]["name"], enc(lid)
    out = {}
    for k, obj in GET.items():
        x = jmap([[f"{obj}/get", {"accountId": aid, "properties": ["id"]}, "0"]], who)[0]
        if x[0] == "error":
            raise RuntimeError(f"{who} {obj}/get: {x[1]}")
        out[k] = len(x[1]["list"])
    for k, obj in QUERY.items():
        out[k] = len(ids_of(who, aid, obj))
    files = get_paged(who, aid, "FileNode", ids_of(who, aid, "FileNode"), ["blobId", "size"]) if out["files"] else []
    out["file_bad"] = 0
    for f in files:
        if f.get("blobId") and len(download(who, aid, f["blobId"])) != f.get("size"):
            out["file_bad"] += 1
    out["file_bodies"] = sum(1 for f in files if f.get("blobId"))
    ev = get_paged(who, aid, "CalendarEvent", ids_of(who, aid, "CalendarEvent"), ["uid", "title"]) if out["events"] else []
    cc = get_paged(who, aid, "ContactCard", ids_of(who, aid, "ContactCard"), ["uid", "emails"]) if out["contacts"] else []
    out["events_by_uid"] = {e.get("uid"): e.get("title") or "" for e in ev}
    out["cards_by_uid"] = {c.get("uid"): sorted(x.get("address", "").lower() for x in (c.get("emails") or {}).values()) for c in cc}
    return lid, out


with ThreadPoolExecutor(4) as ex:
    tgt = dict(ex.map(account, sorted(src_acc)))

# Exact: data users created. Defaults: 0.16 creates a calendar and address book on first use.
for k in ("emails", "mailboxes", "events", "contacts", "files", "sieve"):
    s = sum(src.get(a, {}).get(k, 0) for a in src_acc)
    t = sum(v[k] or 0 for v in tgt.values())
    diff = [(short(src_acc[a]["name"]), src.get(a, {}).get(k, 0), v[k]) for a, v in tgt.items() if v[k] != src.get(a, {}).get(k, 0)]
    if k == "mailboxes":
        only_defaults = all(s_ == 0 and t_ in (5, 6) for _, s_, t_ in diff)
        msg = f"{k}: source {s}, target {t}; differing accounts {len(diff)}"
        if not diff:
            r.ok(msg)
        elif only_defaults:
            r.warn(msg + " (all had none and got the default set)")
        else:
            r.fail(msg + f" {diff[:4]}")
    else:
        r.check(not diff, f"{k}: source {s}, target {t}; differing accounts {len(diff)} {diff[:4]}")
for k in ("calendars", "addressbooks"):
    lower = [(short(src_acc[a]["name"]), src.get(a, {}).get(k, 0), v[k]) for a, v in tgt.items() if (v[k] or 0) < src.get(a, {}).get(k, 0)]
    added = sum((v[k] or 0) - src.get(a, {}).get(k, 0) for a, v in tgt.items())
    r.check(not lower, f"{k}: none lost ({added} defaults added by 0.16); accounts with fewer: {lower[:4]}")

fb = sum(v["file_bad"] for v in tgt.values())
r.check(fb == 0, f"file bodies: {sum(v['file_bodies'] for v in tgt.values())} downloaded, {fb} wrong size")


def toks(tsv):
    return set(re.findall(r"'([^']+)'", tsv or ""))


ev_miss = ev_bad = 0
for key, tsv in S["cal_index"].items():
    a, uid = key.split(":", 1)
    t = tgt.get(int(a), {}).get("events_by_uid", {})
    if uid not in t:
        ev_miss += 1
    elif not all(w in t[uid].lower() or w[:4] in t[uid].lower() for w in toks(tsv)):
        ev_bad += 1
r.check(ev_miss == 0 and ev_bad == 0, f"events by UID: {len(S['cal_index'])} in source index, missing {ev_miss}, title differs {ev_bad}")
cd_miss = cd_bad = 0
for key, tsv in S["card_index"].items():
    a, uid = key.split(":", 1)
    t = tgt.get(int(a), {}).get("cards_by_uid", {})
    if uid not in t:
        cd_miss += 1
    elif {w for w in toks(tsv) if "@" in w} - set(t[uid]):
        cd_bad += 1
r.check(cd_miss == 0 and cd_bad == 0, f"contacts by UID: {len(S['card_index'])} in source index, missing {cd_miss}, emails differ {cd_bad}")
r.done()
