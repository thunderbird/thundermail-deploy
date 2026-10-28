#!/usr/bin/env python3
"""List every account with TOTP two-factor login in the v0.15 principals.json dump.

`convert` carries the otpauth secret over, but on 0.16 an account with TOTP cannot use IMAP password
login, so these users need an app password after the window (#1552 notice).

Input:  principals.json from `migrate_v016.py dump` (holds secrets; keep it mode 600).
Output: CSV with account, primary address and display name. The otpauth secret is never read past its prefix.

Usage:
  python totp_users.py --principals principals.json --out totp-users.csv
"""
import argparse, csv, json, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from shapes import norm_addr, strings, unwrap_list, unwrap_str


def has_totp(principal):
    return any(isinstance(s, str) and s.startswith("otpauth://") for s in unwrap_list(principal.get("secrets")))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--principals", required=True)
    ap.add_argument("--out", default="totp-users.csv")
    a = ap.parse_args()

    p = json.load(open(a.principals))
    items = p if isinstance(p, list) else p.get("items", p)
    rows = []
    for x in items:
        if unwrap_str(x.get("type")) != "individual" or not has_totp(x):
            continue
        name = unwrap_str(x.get("name"))
        emails = [norm_addr(e) for e in strings(x.get("emails"))]
        rows.append({
            "account": name,
            "primary_address": next((e for e in emails if not e.startswith("@")), name),
            "display_name": x.get("description") or "",
        })

    rows.sort(key=lambda r: r["account"])
    old = os.umask(0o077)
    with open(a.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["account", "primary_address", "display_name"])
        w.writeheader()
        w.writerows(rows)
    os.umask(old)

    accounts = sum(1 for x in items if unwrap_str(x.get("type")) == "individual")
    print(f"{len(rows)} of {accounts} accounts use TOTP")
    print(f"wrote {a.out}")


if __name__ == "__main__":
    sys.exit(main())
