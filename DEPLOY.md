# Deploying the payroll app

This is a checklist for putting the app on a real server, where employees and administrators sign in over the
internet. It holds salaries and personal data, so please do not skip the sections on HTTPS, backups and the
pre-launch checklist.

**What has been tested, and what has not.** Verified by automated tests and an end-to-end run against gunicorn
(two workers, a fresh database, production settings): the settings and their refusal to start when weak, Django's
own deployment checklist, the health check, security headers, the sign-in lockout, that static and media files are not
served by the app, and backup and restore of a live database. **Not run in the environment where this was built:**
nginx, systemd, TLS certificates and PostgreSQL. The nginx and systemd files below are examples to adapt and test, not
tested configuration.

## 1. What you need

- A Linux server, a domain name pointing at it, and a TLS certificate (Let's Encrypt is free).
- Python 3.10 or newer, Django 4.2 or newer, and a production server such as gunicorn
  (`pip install gunicorn`). For PostgreSQL also `pip install "psycopg[binary]"`.
- A web server in front (nginx is shown). It terminates HTTPS and serves the static files.

SQLite is fine for one server and a small team: it needs nothing extra, and the backup command takes consistent
snapshots of it. Choose PostgreSQL if you want several app servers or a managed database.

## 2. Settings

Everything is configured by environment variables starting with `PAYROLL_`. See `.env.example` for the full list.
Without `PAYROLL_ENV=production` the app runs in development mode, exactly as before.

In production mode the app **refuses to start** unless: `PAYROLL_SECRET_KEY` is long and random (50+ characters),
`PAYROLL_ENCRYPTION_KEY` is a valid key (section 8), `PAYROLL_ALLOWED_HOSTS` is set, and debug is off. It then turns on HTTPS redirection, secure cookies, HSTS (one hour
to begin with), and a 12-hour idle session timeout. Why `PAYROLL_ALLOWED_HOSTS` matters: invitation and password
links in emails are built from the address the administrator used, so a wrong or open host list can send staff to
the wrong place.

Django's own checklist should then come back clean:

    PAYROLL_ENV=production ... python manage.py check --deploy

Two warnings (`security.W005` and `W021`) remain until you choose to enable HSTS for subdomains and preloading. Those
are commitments that are hard to undo, so they are left for you to decide once HTTPS is settled. The check needs the
database folder to exist.

## 3. First deployment

    sudo useradd --system --home /srv/payroll payroll
    sudo mkdir -p /srv/payroll /var/lib/payroll /var/www/payroll /var/backups/payroll /etc/payroll
    # put the code in /srv/payroll/app, then:
    cd /srv/payroll && python3 -m venv venv && ./venv/bin/pip install -r app/requirements.txt gunicorn segno
    # make the two keys the file asks for, and save a copy of the encryption key somewhere safe (see section 8):
    ./venv/bin/python -c "from django.core.management.utils import get_random_secret_key as g; print(g())"
    ./venv/bin/python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    sudo cp app/.env.example /etc/payroll/payroll.env && sudo nano /etc/payroll/payroll.env
    sudo chmod 600 /etc/payroll/payroll.env && sudo chown payroll /etc/payroll/payroll.env
    sudo chown -R payroll /var/lib/payroll /var/backups/payroll && sudo chmod 700 /var/lib/payroll /var/backups/payroll

    # run these as the payroll user, with the environment file loaded
    set -a; . /etc/payroll/payroll.env; set +a
    cd /srv/payroll/app
    ../venv/bin/python manage.py migrate
    ../venv/bin/python manage.py collectstatic --noinput
    ../venv/bin/python manage.py createsuperuser
    ../venv/bin/python manage.py check --deploy

`segno` is optional: it draws the QR code on the two-factor page, which otherwise shows a key to type in.

Do not run the `seed_*` demo commands on a real system: they create sample people, salaries and tax bands.

## 4. Running it (example systemd unit)

`/etc/systemd/system/payroll.service`:

    [Unit]
    Description=Payroll app
    After=network.target

    [Service]
    User=payroll
    WorkingDirectory=/srv/payroll/app
    EnvironmentFile=/etc/payroll/payroll.env
    ExecStart=/srv/payroll/venv/bin/gunicorn config.wsgi:application --bind 127.0.0.1:8000 --workers 3 --timeout 60
    Restart=on-failure

    [Install]
    WantedBy=multi-user.target

## 5. nginx (example)

    server {
        listen 80;
        server_name payroll.example.com;
        return 301 https://$host$request_uri;
    }
    server {
        listen 443 ssl;
        server_name payroll.example.com;
        # ssl_certificate and ssl_certificate_key from certbot go here

        client_max_body_size 12m;                 # uploads are limited to 10 MB in the app

        location /static/ { alias /var/www/payroll/static/; }
        # There is deliberately NO location for the media folder. HR documents and CVs are only ever served by the app,
        # to signed-in people with access to that company. Never point nginx at PAYROLL_MEDIA_ROOT.

        location / {
            proxy_pass http://127.0.0.1:8000;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;          # the app reads this, see PAYROLL_CLIENT_IP_HEADER
            proxy_set_header X-Forwarded-Proto $https;        # use $scheme if you prefer
        }
    }

The app trusts `X-Forwarded-Proto` and `X-Real-IP` only when you set `PAYROLL_BEHIND_PROXY` and
`PAYROLL_CLIENT_IP_HEADER`. Set them only when nginx overwrites those headers on every request, as above, because a
header a visitor can write must never be trusted. Without them the app sees nginx's own address for everyone, which
makes the audit log's addresses and the per-address sign-in limit useless.

`/healthz` answers `{"status": "ok"}` (or 503) without signing in, for uptime monitors and load balancers. It reveals
nothing else and is exempt from the HTTPS redirect.

## 6. Sign-in protection

After 5 wrong passwords for a username within 15 minutes, that username is locked for the rest of the window, even for
the correct password. The message says how long to wait. The same applies to the admin site's sign-in. Separately, 25
wrong passwords from one address lock that address. Failed attempts are stored in the database (so the limit holds
across all workers), kept for a day, and never include the password. A correct sign-in clears the username's count.

Two things to know. First, anyone can deliberately lock a colleague out by typing their username wrongly five times;
the lock ends by itself, and an administrator can clear it by deleting that username's rows from *Login failures* in
the admin site. Second, this slows guessing but is not two-factor authentication. Use long passwords, and give
administrator rights to as few people as possible.

## 7. Two-factor sign-in

Two-factor sign-in asks for a 6-digit code from an authenticator app (Google Authenticator, Microsoft Authenticator, Authy
and similar) after the password, so a stolen password alone is not enough. It is **optional**: anyone can switch it on
from **Your account** (click your name at the top of any page), and employees using the portal can too. To make it
compulsory for administrators, set `PAYROLL_REQUIRE_2FA_FOR_ADMINS=1`: an administrator without it is sent to set it
up before they can use anything else, and cannot switch it off.

How it behaves:

- A password gets someone only as far as the code screen. Nothing else is reachable until the code is accepted, whichever
  way they signed in (the main page, the admin site or the portal). Each code works **once**.
- Wrong codes are counted like wrong passwords: after 5 the code screen locks for 15 minutes, even for the right code.
- Setting it up shows **8 recovery codes, once**. Each lets someone in one time if their phone is lost or broken. They
  should be printed or kept in a password manager, away from the password.
- Switching it on, or resetting it, signs the person out of every other open session, so a session opened earlier by
  someone with only the password cannot carry on.
- The codes depend on the clocks of the server and the phone agreeing to within about 30 seconds. Keep the server's
  clock right (chrony or systemd-timesyncd).

**Lost phone.** With a recovery code the person can sign in, then make a new set from their account page. With none
left, another administrator opens **Your account, Everyone's two-factor status** and presses **Reset**; the person then
signs in with their password and sets it up again. If the person who lost their phone is the **only** administrator, run
this on the server:

    ./venv/bin/python manage.py reset_two_factor USERNAME

Both routes sign the person out everywhere and appear in the audit log, so resets are visible.

## 8. The encryption key

These are encrypted in the database with the key in `PAYROLL_ENCRYPTION_KEY`: **each company's mail password and
everyone's two-factor secrets.** A copy of the database or of a backup is therefore not enough to read them.

**Nothing else is encrypted by the app.** Salaries, bank account numbers, names, addresses, tax and pension numbers are
ordinary database fields, and uploaded HR documents and CVs are ordinary files. Protect them with full-disk encryption on
the server, tight file permissions (the data folders are mode 700 in section 3), and encrypted backups (section 9).

**Keep the key safe, in two places.** One is the environment file on the server. The other is somewhere that is **not**
the server and **not** the backups (a password manager or secrets vault): the backup archives deliberately do not contain
it, since a backup with its key beside it would protect nothing.

**If the key is lost or wrong,** nothing else is harmed and the app keeps running, but: each company's mail password
reads as blank (enter it again on Company settings), and the two-factor secrets cannot be read. A secret that cannot be
read never accepts a code, so those people cannot use their authenticator app until it is reset. Their recovery codes
still work, and an administrator can reset them (section 7). The log says which setting to check.

**Replacing the key** (for example if it may have leaked):

1. Make a new key. In the environment file put it in `PAYROLL_ENCRYPTION_KEY` and move the old one to
   `PAYROLL_ENCRYPTION_OLD_KEYS` (comma-separated if there are several). Restart the app.
2. Run `./venv/bin/python manage.py rotate_encryption_key`. It re-encrypts everything with the new key, and says so if
   any value could not be read with any key you gave it (and leaves that value untouched).
3. Only after it reports success, delete the old key from `PAYROLL_ENCRYPTION_OLD_KEYS`, and update your second copy.

`PAYROLL_SECRET_KEY` is a different secret. Changing it signs everyone out, cancels outstanding password-reset and
invitation links, and **cancels everyone's recovery codes** (they are checked against it), so people must make new ones.
Treat it as a secret you are not going to change often.

## 9. Backups

The backup command writes one archive containing the database and the private media folder (HR documents and CVs).

    ./venv/bin/python manage.py backup --keep 14

The database is snapshotted with SQLite's own backup API, so it is consistent even while people are using the app.
For PostgreSQL it runs `pg_dump` (the client tools must be installed). The archive is re-read and checked
(checksums, database integrity) as soon as it is written. It is created readable by its owner only.

**Schedule it** (example, as the payroll user, daily at 02:00):

    0 2 * * *  set -a; . /etc/payroll/payroll.env; set +a; cd /srv/payroll/app && /srv/payroll/venv/bin/python manage.py backup --keep 14

**The archive does not contain `PAYROLL_ENCRYPTION_KEY`**, so keep that safely as well (section 8); without it a restored
system cannot read the mail passwords or two-factor secrets.

**A backup on the same disk is not a backup.** Copy the archives to another machine or storage account every day.
They contain salaries and personal data and are **not encrypted by the app**: encrypt them before they leave the server,
for example `gpg --symmetric --cipher-algo AES256 file.tar.gz`, and keep the passphrase somewhere other than the
server.

**Practise restoring.** Do it on a spare machine before you need it, then again every few months.

SQLite restore (stop the app first):

    sudo systemctl stop payroll
    ./venv/bin/python manage.py restore /var/backups/payroll/payroll-backup-YYYYMMDD-HHMMSS-NNNNNN.tar.gz --yes
    ./venv/bin/python manage.py migrate      # only if the backup came from an older version of the app
    sudo systemctl start payroll

The restore checks the archive first and changes nothing if it is damaged. The data it replaces is renamed with
`.before-restore-<time>` rather than deleted; delete those once you are satisfied. Add `--skip-media` to restore only
the database.

PostgreSQL restore: extract the archive (`tar xzf`), run `pg_restore --clean --no-owner -d payroll database.dump`, and copy
the `media/` folder back to `PAYROLL_MEDIA_ROOT`.

## 10. Upgrading

    ./venv/bin/python manage.py backup           # always first
    # replace the code, keeping the environment file and the data folders
    ./venv/bin/python manage.py migrate
    ./venv/bin/python manage.py collectstatic --noinput
    ./venv/bin/python manage.py check --deploy
    sudo systemctl restart payroll

## 11. Before real payroll data goes in

- [ ] `check --deploy` shows nothing you have not decided on, and the site works over **https**.
- [ ] A new `PAYROLL_SECRET_KEY` and `PAYROLL_ENCRYPTION_KEY`; the environment file is mode 600 and not in the code folder.
- [ ] The encryption key is saved in a second safe place that is not the server and not the backups.
- [ ] Two-factor sign-in is on for every administrator (or compulsory with `PAYROLL_REQUIRE_2FA_FOR_ADMINS=1`), and each has saved their recovery codes.
- [ ] The server's clock is kept right, because two-factor codes depend on it.
- [ ] The disk the server uses is encrypted, since salaries and personal data are not encrypted by the app.
- [ ] `PAYROLL_ALLOWED_HOSTS` and `PAYROLL_CSRF_TRUSTED_ORIGINS` name only your domain.
- [ ] Opening `https://your-domain/media/anything` and `.../static/hr_documents/x` gives "not found".
- [ ] A backup has been made, copied off the server, encrypted, **and restored on another machine**.
- [ ] No demo data: employees, tax bands and statutory rules are real, and checked against current rules.
- [ ] Administrators are few. The Users, Approvers and Portal access pages have been reviewed.
- [ ] Each company's email settings work (Company settings, test email), so invitations and payslips send.
- [ ] Someone is named to read the Audit log, and to watch for repeated failed sign-ins.

## 12. Known limits

- Only the mail passwords and two-factor secrets are encrypted by the app (section 8). Salaries, bank details, personal
  data and uploaded files are not: use disk encryption and encrypted backups.
- Two-factor uses authenticator-app codes only. There is no text-message code and no hardware security key.
- A person with the right password who cannot pass the code screen is held there, not locked out for good: recovery codes
  and an administrator reset are the way back. Make sure at least two people can reset it.
- Uploads are limited by type, size and a content check, but are not scanned for malware.
- SQLite suits one server. Several app servers need PostgreSQL.
- The sign-in lockout can be used to annoy a user (section 6). Behind a proxy it needs the client-address settings above.
