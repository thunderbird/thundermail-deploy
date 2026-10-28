# Read-only: print account,contact_email,language for every Accounts user with a mailbox.
# contact_email = the recovery email when set and not bounced, otherwise blank.
# Run with:  python manage.py shell < export_contacts.py > contacts.csv
import csv, sys
from thunderbird_accounts.mail.models import Account

w = csv.writer(sys.stdout)
w.writerow(["account", "contact_email", "language"])
for a in Account.objects.select_related("user").order_by("name"):
    u = a.user
    if not u:
        continue
    ok = u.recovery_email and not u.recovery_email_rejected_at
    w.writerow([a.name, u.recovery_email if ok else "", u.language or ""])
