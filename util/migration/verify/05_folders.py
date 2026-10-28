#!/usr/bin/env python3
"""05: folder consistency on the target. Standard folders exist, and each folder's totalEmails/unreadEmails agree
with the messages and $seen flags actually in it."""
import collections
from concurrent.futures import ThreadPoolExecutor
from common import principals, enc, jmap, ids_of, get_paged, Report, short

r = Report("05 folders")
src_acc, _ = principals()
ROLES = ("inbox", "sent", "drafts", "trash", "junk")


def account(lid):
    who, aid = src_acc[lid]["name"], enc(lid)
    boxes = jmap([["Mailbox/get", {"accountId": aid, "properties": ["role", "totalEmails", "unreadEmails"]}, "0"]], who)[0][1]["list"]
    per, unread = collections.Counter(), collections.Counter()
    for e in get_paged(who, aid, "Email", ids_of(who, aid, "Email"), ["mailboxIds", "keywords"]):
        for mb in e.get("mailboxIds", {}):
            per[mb] += 1
            unread[mb] += not (e.get("keywords") or {}).get("$seen")
    roles = {b.get("role") for b in boxes}
    return (short(who), [x for x in ROLES if x not in roles],
            sum(b["totalEmails"] != per.get(b["id"], 0) for b in boxes),
            sum(b["unreadEmails"] != unread.get(b["id"], 0) for b in boxes))


with ThreadPoolExecutor(4) as ex:
    res = list(ex.map(account, sorted(src_acc)))
missing = [(n, m) for n, m, _, _ in res if m]
r.check(not missing, f"accounts missing a standard folder: {len(missing)} {missing[:5]}")
r.check(sum(x[2] for x in res) == 0, f"folders whose totalEmails disagrees with contents: {sum(x[2] for x in res)}")
r.check(sum(x[3] for x in res) == 0, f"folders whose unreadEmails disagrees with $seen flags: {sum(x[3] for x in res)}")
r.done()
