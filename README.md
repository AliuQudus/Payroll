# Payroll, Leave, HR records, Recruitment, Salary history, Exits, Attendance, Remittances, Employee portal, Audit log and Security (local)

Separate modules in one project:

- `payroll/` - employees, salary elements, PAYE, adjustments, payroll runs and payslips.
- `leave/` - leave types, requests and approval, balances, entitlements and public holidays.
- `hr/` - employee records: job title, hire date, manager, contract and probation dates, statutory IDs
  (TIN, RSA PIN), emergency contacts, uploaded documents with expiry dates, and an alerts page.
- `recruitment/` - vacancies, applicants through screening, interview, offer and hire (with CV storage and interview
  notes), and an onboarding checklist for each new employee. Hiring creates the employee and, when the HR module
  is installed, their HR record.
- `salary/` - pay grades (salary bands) and the dated history of each employee's monthly gross. Payroll takes the
  gross for each month from this history, and a change dated in a month that was already approved adds back pay
  (taxed as a one-off lump sum) to the next run you generate.
- `exits/` - an employee leaving: clearance checklist, settlement lines (unused leave, pay in lieu of notice,
  gratuity, deductions), a preview of the final payslip, and completing the exit. The final month is paid pro rata
  through the normal payroll run, loans are recovered in full, and later runs skip the employee.
- `attendance/` - a daily attendance register (with CSV import and a monthly summary) and overtime requests with
  approval. Days marked absent reduce pay at the usual daily rate; approved overtime is paid in the next payroll run.
- `remittances/` - PAYE and pension schedules per approved payroll run (CSV download), deadlines, and a record of
  the payments made to the tax authority and the pension administrators.
- `portal/` - employee self-service: a login for each employee, linked to that employee only, to see their own
  approved payslips, check leave balances, request or withdraw leave, see their own attendance and overtime (and
  request overtime for approval), and update their phone, address and emergency contacts. Administrators give access under Portal access; the employee chooses their password from an emailed link.
- `security/` - sign-in lockout after repeated wrong passwords, optional two-factor sign-in with an authenticator
  app (compulsory for administrators if you choose), encryption of stored mail passwords and two-factor secrets, a
  `backup` and `restore` command, a `/healthz` check and proxy-aware client addresses. See DEPLOY.md for putting
  the app on a real server.
- `audit/` - a record of who changed what and when across payroll, HR, salary, leave and remittances, plus sign-ins,
  failed sign-ins and downloads of sensitive files. Administrators only, under Audit log.

Payroll works on its own. When the leave module is installed, approved unpaid leave reduces pay and approved
leave that earns the leave allowance puts it on the payslip. The only link is `payroll/integrations.py`.
To run payroll without leave, remove `"leave"` from INSTALLED_APPS in `config/settings.py` and the `leave/`
line in `config/urls.py`. The HR module is removed the same way (`"hr"` and the `hr/` URL line); nothing
else depends on it. Recruitment is optional too (`"recruitment"` and its URL line); nothing else depends on it. Salary history is optional too (`"salary"` and its URL line): payroll then uses the salary on each employee
record as before. Exits is optional too (`"exits"` and its URL line): payroll then pays everyone normally. The employee portal is optional too (`"portal"` and its URL line); its leave and personal-details pages appear only
when the leave and HR modules are installed. Attendance is optional too (`"attendance"` and its URL line): payroll then ignores absence records and overtime.
Remittances is optional too (`"remittances"` and its URL line). It reads TIN, RSA PIN and
pension administrator from the HR module when that is installed, and still works without them.

HR documents are saved in the `media/` folder, which is not served publicly: files can only be downloaded
through the app by a signed-in user with access to that company. Back this folder up with the database.

    python -m venv venv
    venv\Scripts\activate        (Windows)   |   source venv/bin/activate   (Mac/Linux)
    pip install -r requirements.txt
    python manage.py migrate
    python manage.py createsuperuser
    python manage.py seed_demo   (optional: demo company, elements, bands, employees)
    python manage.py seed_leave  (optional: usual leave types and sample holidays for the first company)
    python manage.py seed_hr     (optional: sample HR records for the demo employees)
    python manage.py runserver

Open http://127.0.0.1:8000 and log in. Run the tests with `python manage.py test`.
`python check_mail.py you@gmail.com` tests your mail settings.
The PAYE bands, leave days, holidays and remittance deadlines (10th of the following month) are samples:
check them before real use. Change the deadlines under Remittances, Deadlines (administrators only).

Audit log: bank account numbers, mail passwords and user passwords are recorded as "changed" but their values are
never stored. Entries cannot be edited or deleted from the app or the admin site, but anyone with direct database
access still can, so restrict that access. The IP address is the one the server sees, which is the proxy's address if
you run behind one. Removing the audit log means removing `"audit"` from INSTALLED_APPS, the
`audit.middleware.AuditMiddleware` line from MIDDLEWARE, and the `audit/` URL line. The app refuses to start
(a system check) if it is installed without its middleware, because entries would then not say who acted.

Final pay (exits) rests on assumptions you should confirm: the final month is paid for the calendar days up to and
including the last working day; unused annual leave (leave that earns the leave allowance) accrues evenly through
the year and is paid at the monthly gross divided by the absence-day rule; PAYE on the final month is worked out
like any other month, not as a year-to-date true-up; and whether an exit payment is taxable is chosen on each
settlement line.

Employee portal: an employee login has no company access, so every payroll, HR, salary and audit screen refuses it,
and the portal only ever queries records belonging to the linked employee. Payslips appear only after the run is
approved. Bank details cannot be changed from the portal, to keep pay safe from account takeover. Switching a login
off, or marking the employee inactive, ends access immediately, including open sessions. The portal is meant to be
opened by every employee, so before offering it follow DEPLOY.md: production mode (a real secret key, debug off,
your domain in the allowed hosts, which invitation links are built from, HTTPS with secure cookies) and the sign-in
lockout, which the security module provides.

Attendance and overtime rest on settings you should confirm under Attendance, Overtime rules: the working hours in a
month (173.33, a 40-hour week), the share of monthly gross used as the overtime base (100%, so set it lower if your
policy pays overtime on basic pay), and the rates for weekdays (1.5), weekends (2) and public holidays (2). An
overtime amount is fixed when the request is approved. Overtime is taxed as a one-off lump sum on top of the month's
pay, and is not pensionable. Absence from the register is deducted at gross divided by the absence-day rule (30), the
same as other absence, so do not also enter a manual absence adjustment for the same days.

Start dates: when the HR module is installed, payroll pays a new employee only from their HR hire date. A month
before it they get no payslip, and in the month they start they are paid for the days from the start date (for
example 11 of 30 days when starting on the 20th), the same calculation used for the last month of a leaver. Check that
hire dates on existing HR records are right: one entered in the future means no pay until that month. Applicants'
CVs are stored in the same private media folder as HR documents, never served publicly, and only people with access to
the company can download them; they are personal data, so delete CVs you no longer need.

Deploying for real: read DEPLOY.md. Production is switched on with the environment variable PAYROLL_ENV=production
(see .env.example); development needs no configuration. The sign-in protection can be removed by taking `"security"`
out of INSTALLED_APPS, deleting the AUTHENTICATION_BACKENDS setting, removing the `security.middleware.TwoFactorMiddleware`
line from MIDDLEWARE, and removing the `accounts/login/` and security lines from `config/urls.py`; the two-factor and
backup features go with it. Encryption of mail passwords and the appearance choice live in payroll itself and stay. The app has a `config/wsgi.py` entry point for gunicorn.

Navigation and appearance: the sections are listed down the left (the pages of the current section open beneath it),
and become a slide-out menu on a phone. Each person chooses Light, Dark or Match my device, from the button at the top
of any page or from Your account (click your name); it is saved on their account and follows them to any device.
Until someone chooses, the site follows their device as it always did. Paper (printed payslips) is always light.

Requirements: Django 4.2 or newer and `cryptography` (see requirements.txt). `segno` is optional and only draws the QR
code on the two-factor page. Production also needs `PAYROLL_ENCRYPTION_KEY` (see .env.example and DEPLOY.md).

