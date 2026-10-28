#!/usr/bin/env python3
"""03: messages. Per-message match on (account, document id), size and receivedAt; message bodies downloaded and
checked for size and RFC 5322 header shape. Needs $VERIFY_OUT/source.json from 01.
VERIFY_SKIP_BODIES=1 downloads a random sample (VERIFY_BODY_SAMPLE, default 1% and at least 200) instead of every body."""
import datetime, json, os, random, re
from concurrent.futures import ThreadPoolExecutor
from common import principals, enc, dec, ids_of, get_paged, download, OUT, Report, short

r = Report("03 messages")
src_acc, _ = principals()
src = {tuple(map(int, k.split(":"))): v for k, v in json.load(open(f"{OUT}/source.json"))["messages"].items()}


def account(lid):
    who, aid = src_acc[lid]["name"], enc(lid)
    try:
        ids = ids_of(who, aid, "Email")
        return lid, get_paged(who, aid, "Email", ids, ["id", "blobId", "size", "receivedAt", "mailboxIds", "threadId"]), None
    except Exception as ex:
        return lid, [], str(ex)[:120]


tgt, failed = {}, []
with ThreadPoolExecutor(4) as ex:
    for lid, rows, err in ex.map(account, sorted(src_acc)):
        if err:
            failed.append((short(src_acc[lid]["name"]), err))
        for e in rows:
            tgt[(lid, dec(e["id"]) & 0xFFFFFFFF)] = e
r.check(not failed, f"accounts that could not be read: {len(failed)} {failed[:3]}")


def ts(s):
    return int(datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


both = set(src) & set(tgt)
missing, extra = sorted(set(src) - set(tgt)), sorted(set(tgt) - set(src))
r.check(not missing, f"messages: source {len(src)}, target {len(tgt)}, matched {len(both)}; missing on target {len(missing)} {missing[:5]}")
r.check(not extra, f"extra on target: {len(extra)} {extra[:5]}", warn_only=True)
indexed = [k for k in both if src[k][0] >= 0]
sz = [k for k in indexed if src[k][0] != tgt[k]["size"]]
r.check(not sz, f"size mismatches: {len(sz)} of {len(indexed)} indexed {sz[:3]}")
tm = [k for k in indexed if abs(src[k][1] - ts(tgt[k]["receivedAt"])) > 1]
r.check(not tm, f"receivedAt mismatches: {len(tm)} of {len(indexed)} indexed {tm[:3]}")
nf = [k for k, e in tgt.items() if not e.get("mailboxIds")]
r.check(not nf, f"messages in no folder: {len(nf)}")
nt = [k for k, e in tgt.items() if not e.get("threadId")]
r.check(not nt, f"messages without a thread: {len(nt)}")

HEADER = re.compile(rb"^[!-9;-~]+:", re.M)
items = list(tgt.items())
sampled = os.environ.get("VERIFY_SKIP_BODIES") == "1"
if sampled:
    n = int(os.environ.get("VERIFY_BODY_SAMPLE", "0")) or max(200, len(items) // 100)
    items = random.sample(items, min(n, len(items)))


def body(item):
    (lid, _), e = item
    try:
        b = download(src_acc[lid]["name"], enc(lid), e["blobId"])
    except Exception as ex:
        return ("error", str(ex)[:60])
    head = b[:16384].split(b"\r\n\r\n", 1)[0].split(b"\n\n", 1)[0]
    shaped = len(HEADER.findall(head)) >= 2
    return ("ok" if len(b) == e["size"] and shaped else "bad", len(b))


with ThreadPoolExecutor(4) as ex:
    res = list(ex.map(body, items))
errs = sum(1 for s, _ in res if s == "error")
bad = sum(1 for s, _ in res if s == "bad")
r.check(errs == 0 and bad == 0, f"bodies downloaded{' (random sample)' if sampled else ''}: "
        f"{sum(1 for s, _ in res if s == 'ok')}/{len(res)} of {len(tgt)} "
        f"(errors {errs}, wrong size or not an email {bad}), {sum(int(n) for s, n in res if s == 'ok')} bytes")
json.dump({"bodies_checked": "sample" if sampled else "all",
           "blobs": {f"{a}:{d}": e["blobId"] for (a, d), e in tgt.items()}},
          open(f"{OUT}/target-message-blobs.json", "w"))
r.done()
