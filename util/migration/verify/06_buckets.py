#!/usr/bin/env python3
"""06: blob buckets. Lists both buckets (read-only) and compares keys and sizes. Every blob the target references
(message bodies from 03) must exist in the target bucket. Objects only on the source are expected when 0.16 has
purged unreferenced blobs (they show up as delete markers on a versioned bucket)."""
import json, os, subprocess
from common import need, OUT, Report

r = Report("06 blob buckets")


def listing(bucket, profile):
    """Every key and size. The CLI pages list-objects-v2 itself (--no-paginate is not set)."""
    out = subprocess.run(["aws", "s3api", "list-objects-v2", "--bucket", bucket, "--profile", profile, "--region", need("AWS_REGION"),
                          "--query", "Contents[].[Key,Size]", "--output", "json"], capture_output=True, text=True, check=True).stdout
    return {k: s for k, s in (json.loads(out) or [])}


def delete_markers(bucket, profile):
    out = subprocess.run(["aws", "s3api", "list-object-versions", "--bucket", bucket, "--profile", profile, "--region", need("AWS_REGION"),
                          "--query", "DeleteMarkers[?IsLatest].Key", "--output", "json"], capture_output=True, text=True, check=True).stdout
    return set(json.loads(out or "null") or [])


src = listing(need("SOURCE_BUCKET"), need("SOURCE_AWS_PROFILE"))
tgt = listing(need("TARGET_BUCKET"), need("TARGET_AWS_PROFILE"))
r.ok(f"objects: source {len(src)}, target {len(tgt)}")
diff = [k for k in set(src) & set(tgt) if src[k] != tgt[k]]
r.check(not diff, f"same key, different size: {len(diff)} {diff[:3]}")

path = f"{OUT}/target-message-blobs.json"
if not os.path.exists(path):
    r.fail("no target-message-blobs.json: run 03 first, otherwise nothing proves the referenced blobs exist")
else:
    refs = json.load(open(path))
    if refs.get("bodies_checked") == "all":
        r.ok(f"message blobs referenced by the target: {len(refs['blobs'])}; 03 downloaded every one through Stalwart")
    else:
        r.warn(f"message blobs referenced by the target: {len(refs['blobs'])}; 03 downloaded only a sample, "
               "so blob presence rests on the key and size comparison below")

only_src = set(src) - set(tgt)
dm = delete_markers(need("TARGET_BUCKET"), need("TARGET_AWS_PROFILE"))
purged = only_src & dm
new = only_src - dm
r.check(not new, f"only on source: {len(only_src)} ({len(purged)} purged by the target, recoverable from versions; "
        f"{len(new)} missing from the target with no delete marker) {sorted(new)[:3]}")
r.ok(f"only on target: {len(set(tgt) - set(src))} (written by the target since the copy)")
r.done()
