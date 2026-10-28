# App password notices

Every v0.15 app password stops working when the server moves to Stalwart 0.16. 0.16 only accepts app passwords it generated itself, so the old ones can't be carried over. `app_password_users.py` finds the affected accounts; these are the notices to send them.

## Finding who to notify

Run this before the migration, against the `principals.json` from `migrate_v016.py dump`:

```bash
# optional: contact addresses from Accounts (read-only Django shell), so the notice
# reaches users whose mail client has just stopped working
python manage.py shell < export_contacts.py > contacts.csv

python app_password_users.py --principals principals.json --contacts contacts.csv --out app-password-users.csv
```

The CSV contains no password hashes, only the names users gave their app passwords. Keep it mode 600 anyway, because it lists users and their contact addresses. Use the `notice` column to choose the message:

| `notice` | Who | Send |
|---|---|---|
| `app_passwords` | Created their own app passwords (mail clients, scripts) | Notice A |
| `appointment` | Connected Appointment to their calendar; Accounts created that password for them | Notice B |
| `both` | Both of the above | Notice A, then the two paragraphs of Notice B |

Send to `contact_email` when it is set. Otherwise send to `primary_address`: the user can still read it in webmail, but not in the client that just lost its password.

When to send: once before the maintenance window ("On {date} at {time} {tz} we will upgrade..."), and again right after cutover.

Prerequisite: Accounts must have `STALWART_ADMIN_API_USE_JMAP=true` before you send anything. Without it, creating an app password fails on 0.16. Test it first by creating, then deleting, an app password for a test account.

Rehearse both notices on stage before sending them to real users. The Notice B `/setup` path is not tested yet.

On the tb-dev rehearsal (stage data): 116 of 160 accounts were affected. 109 got Notice A, 6 got Notice B and 1 got both (117 passwords: 110 user-created, 7 Appointment). 106 had a contact address; the 10 without were load-test accounts.

## Notice A: app passwords

> **Subject:** Action needed after the Thundermail upgrade on {date}: recreate your app passwords
>
> Hi {display_name},
>
> We upgraded the Thundermail server on {date} at {time} {tz}. App passwords created before the upgrade no longer work, so any mail app that signs in with one will stop syncing until you give it a new one.
>
> To fix it:
> 1. Sign in at {accounts_url}. Under **Account settings**, find **App Password** and click **Create app password**.
> 2. Click **Save**. The new password is shown once. Click **Copy app password** and keep it until you have finished, because it won't be shown again.
> 3. In **each** app that stopped working, replace the old password with this same new password.
>
> You get one app password, and all your apps use it. If you create another one later, the previous one stops working everywhere.
>
> Your mail, contacts and calendars are unchanged. Signing in to the web dashboard with your normal password works as before.
>
> Questions? Contact us at {accounts_url}/contact.
>
> The Thundermail team

## Notice B: Appointment calendar connection

> **Subject:** Reconnect your calendar in Appointment after the {date} upgrade
>
> Hi {display_name},
>
> After the Thundermail upgrade on {date} at {time} {tz}, Appointment can no longer reach your Thundermail calendar. Your existing bookings are safe. Until you reconnect, new bookings won't appear on your calendar, and Appointment can't see your existing meetings, so people may book over them.
>
> To reconnect:
> 1. Sign in to Appointment at {appointment_url}/setup.
> 2. Leave **Connect Thundermail Calendar** selected and click **Continue**.
> 3. When the next step ("Create booking page") appears, the connection is fixed. Go back to {appointment_url}/dashboard. Your booking page stays as it was.
>
> If that doesn't work: open **Settings → Connected Applications**, find the account named {primary_address}, click **⋯ → Reconnect**, and sign in with your Thundermail address and an app password from {accounts_url}.
>
> Questions? Contact us at {accounts_url}/contact.
>
> The Thundermail team
