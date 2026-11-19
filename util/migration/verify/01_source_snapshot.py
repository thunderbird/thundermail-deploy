#!/usr/bin/env python3
"""01: snapshot the source (v0.15) DB: live documents per account and collection, live email bytes. Read-only.
Writes $VERIFY_OUT/source.json, which the later checks compare against.
VERIFY_ACCOUNT_RANGE=lo-hi snapshots only those accounts (one batch of a piece)."""
import json
import os
from common import sql, ACC, DOC, LIVE, COLLECTIONS, OUT, Report, account_range

r = Report("01 source snapshot")
rng = account_range()
if rng:
    upper = f" and k < '\\x{rng[1] + 1:08x}'::bytea" if rng[1] < 0xFFFFFFFF else ""
    LIVE = f"{LIVE} and k >= '\\x{rng[0]:08x}'::bytea{upper}"
    IDX = f" where accid between {rng[0]} and {rng[1]}"
    S_EMAIL = f"(select * from s_email{IDX})"
    UID_OK = f"e.uid is not null and e.accid between {rng[0]} and {rng[1]}"
else:
    IDX = ""
    UID_OK = "e.uid is not null"
    S_EMAIL = "s_email"
pending = int(sql("select count(*) from f")[0][0])
if pending == 0 or os.environ.get("TARGET_ENV") != "tb-prod":
    r.check(pending == 0, f"v0.15 task queue (table f) is empty: {pending} pending tasks "
            "(index updates and other work must drain before the copy, or the snapshot is incomplete)")
else:
    r.ok(f"v0.15 task queue (table f): {pending} pending tasks, not a failure: documents are "
         "counted in p; messages the index has not caught up with are compared by id only")
acc = {}
for a, coll, n in sql(f"select {ACC}, get_byte(k,4), count(*) from p where {LIVE} group by 1,2"):
    name = COLLECTIONS.get(int(coll))
    if name:
        acc.setdefault(a, {})[name] = int(n)
# Every live email document, whether or not the search index (s_email) has caught up with it.
# Size and received time come from the index; -1 means "not indexed", and 03 then matches on id only.
msgs = {}
unindexed = 0
for a, d, size, rcvd in sql(f"""with live as (select {ACC} acc, {DOC} doc from p where {LIVE} and get_byte(k,4)=0)
        select l.acc, l.doc, coalesce(e.size, -1), coalesce(e.rcvd, -1)
        from live l left join {S_EMAIL} e on l.acc=e.accid and l.doc=e.docid"""):
    msgs[f"{a}:{d}"] = [int(size), int(rcvd)]
    if int(size) < 0:
        unindexed += 1
    else:
        acc.setdefault(a, {})["email_bytes"] = acc.get(a, {}).get("email_bytes", 0) + int(size)
live_by_coll = lambda c: f"select {ACC}, {DOC} from p where {LIVE} and get_byte(k,4)={c}"
cal = {f"{a}:{u}": t for a, u, t in sql(f"""with live as ({live_by_coll(9)}) select e.accid, e.uid, e.titl from s_cal e
        join live l(acc, doc) on l.acc=e.accid and l.doc=e.docid where {UID_OK}""")}
card = {f"{a}:{u}": e for a, u, e in sql(f"""with live as ({live_by_coll(11)}) select e.accid, e.uid, e.eml from s_card e
        join live l(acc, doc) on l.acc=e.accid and l.doc=e.docid where {UID_OK}""")}
newest = sql("select to_char(to_timestamp(max(rcvd)) at time zone 'UTC','YYYY-MM-DD HH24:MI:SS') "
             f"from s_email{IDX}")[0][0]

json.dump({"accounts": acc, "messages": msgs, "cal_index": cal, "card_index": card, "newest_message": newest},
          open(f"{OUT}/source.json", "w"))
tot = {}
for v in acc.values():
    for k, n in v.items():
        tot[k] = tot.get(k, 0) + n
r.check(tot.get("emails", 0) == len(msgs), f"live email documents {tot.get('emails', 0)} == messages in snapshot {len(msgs)}")
r.check(unindexed == 0, f"live messages missing from the search index: {unindexed} (compared by id only)", warn_only=True)
r.ok(f"{len(acc)} account ids with data; totals {tot}")
r.ok(f"{len(msgs)} live messages; newest received {newest} UTC")
r.done()
