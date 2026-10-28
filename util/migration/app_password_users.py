#!/usr/bin/env python3
"""List every account whose v0.15 app passwords will stop working after the 0.16 migration.

0.16 only accepts server-generated app passwords, so every v0.15 app password is dropped by `convert`
and cannot be carried over. Run this against the v0.15 `principals.json` dump BEFORE the migration,
send the notice, and keep the CSV to answer support questions after cutover.

Input:  principals.json from `migrate_v016.py dump` (holds hashes; keep it mode 600).
Output: CSV with account, primary address, display name, which notice to send, the user-created app
        password names, and whether Appointment's CalDAV connection needs redoing.
        No hashes are written. The app password NAME is what the user typed when creating it
        (e.g. "Thunderbird laptop"), which tells them which client to reconfigure.

Optional: --contacts FILE joins a contact address per account (CSV with columns
`account,contact_email,language`), e.g. the Accounts recovery email exported with
`python manage.py shell` (see README). Without it, the notice goes to the mailbox itself, which still works
over webmail and JMAP but not in the client whose app password just broke.

Usage:
  python app_password_users.py --principals principals.json --out app-password-users.csv
  python app_password_users.py --principals principals.json --contacts contacts.csv --out app-password-users.csv
"""
import argparse, csv, json, os, sys


def str_list(v):
    if v is None:
        return []
    if isinstance(v, list):
        return v
    if isinstance(v, dict):
        if isinstance(v.get("stringList"), list):
            return v["stringList"]
        return [v["string"]] if isinstance(v.get("string"), str) else []
    return [v]


def app_passwords(principal):
    """Names of the v0.15 app passwords. Stored as `$app$<name>$<hash>`; the hash is never read."""
    names = []
    for s in str_list(principal.get("secrets")):
        if isinstance(s, str) and s.startswith("$app$"):
            name, _, _ = s[len("$app$"):].partition("$")
            names.append(name or "(unnamed)")
    return names


# Accounts labels the app password it creates for Appointment's CalDAV setup with this prefix
# (settings.APPOINTMENT_APP_PASSWORD_PREFIX). Those are not the user's own; Appointment needs reconnecting.
APPOINTMENT_PREFIX = "appointment-caldav-setup-"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--principals", required=True)
    ap.add_argument("--contacts", help="optional CSV: account,contact_email,language")
    ap.add_argument("--out", default="app-password-users.csv")
    a = ap.parse_args()

    p = json.load(open(a.principals))
    items = p if isinstance(p, list) else p.get("items", p)
    contacts = {}
    if a.contacts:
        lines = [l for l in open(a.contacts) if l.strip()]
        start = next((i for i, l in enumerate(lines) if l.lower().startswith("account,")), None)
        if start is None:
            sys.exit(f"{a.contacts}: no 'account,contact_email,language' header line")
        for row in csv.DictReader(lines[start:]):
            contacts[row["account"].strip().lower()] = row

    rows = []
    for x in items:
        if x.get("type") != "individual":
            continue
        names = app_passwords(x)
        if not names:
            continue
        emails = [e for e in str_list(x.get("emails")) if isinstance(e, str)]
        primary = next((e for e in emails if not e.startswith("@")), x["name"])
        c = contacts.get(x["name"].lower()) or contacts.get(primary.lower()) or {}
        mine = [n for n in names if not n.startswith(APPOINTMENT_PREFIX)]
        appt = len(names) - len(mine)
        rows.append({
            "account": x["name"],
            "primary_address": primary,
            "display_name": x.get("description") or "",
            "notice": "both" if mine and appt else ("app_passwords" if mine else "appointment"),
            "app_password_count": len(mine),
            "app_password_names": "; ".join(mine),
            "appointment_caldav": "yes" if appt else "",
            "contact_email": c.get("contact_email") or "",
            "language": c.get("language") or "",
        })

    rows.sort(key=lambda r: r["account"])
    old = os.umask(0o077)
    with open(a.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["account"])
        w.writeheader()
        w.writerows(rows)
    os.umask(old)

    total = sum(r["app_password_count"] for r in rows)
    accounts = sum(1 for x in items if x.get("type") == "individual")
    print(f"{len(rows)} of {accounts} accounts lose app passwords in the migration:")
    for kind in ("app_passwords", "appointment", "both"):
        print(f"  notice={kind}: {sum(1 for r in rows if r['notice'] == kind)} accounts")
    print(f"  user-created app passwords: {total}; Appointment CalDAV connections: {sum(1 for r in rows if r['appointment_caldav'])}")
    if a.contacts:
        missing = sum(1 for r in rows if not r["contact_email"])
        print(f"contact address found for {len(rows) - missing}, missing for {missing} (those get the notice in their mailbox)")
    print(f"wrote {a.out}")


if __name__ == "__main__":
    sys.exit(main())
