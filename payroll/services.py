"""Glue between the database and the pure calculation engine."""
import smtplib
import time
import calendar
from datetime import date
from types import SimpleNamespace
from decimal import ROUND_HALF_UP, Decimal

from django.core.mail import EmailMultiAlternatives, get_connection
from django.db import transaction
from django.template.loader import render_to_string
from django.utils import timezone

from . import access, integrations
from .engine import DEDUCTION, EARNING, Element, FormulaError, Rules, compute_payslip
from .models import PERCENT_RULES, AdjustmentApplication, Employee, EmployeeAdjustment, Payslip, PayslipLine, PayrollRun, SalaryElement, StatutoryRule, TaxBand

RULE_FIELDS = {
    "PENSION_EMPLOYEE": "pension_employee_rate", "PENSION_EMPLOYER": "pension_employer_rate",
    "RENT_RELIEF_RATE": "rent_relief_rate", "RENT_RELIEF_CAP": "rent_relief_cap",
    "ABSENCE_DIVISOR": "absence_divisor", "LEAVE_ALLOWANCE": "leave_allowance_rate",
}


def to_engine(e: SalaryElement) -> Element:
    return Element(e.code, e.name, e.kind, e.formula, e.is_taxable, e.is_pensionable,
                   e.in_nhf_base, e.monthly_cap, leave_base=e.in_leave_base)


def load_rules(company, on: date) -> Rules:
    bands = TaxBand.objects.filter(company=company, effective_from__lte=on)
    if not bands.exists():
        raise ValueError(f"No PAYE bands are set up for {on:%B %Y}. Add them under PAYE bands.")
    latest = bands.order_by("-effective_from").first().effective_from
    rules = Rules(bands=[(b.lower_limit, b.upper_limit, b.rate / 100)
                         for b in bands.filter(effective_from=latest).order_by("lower_limit")])
    values = {}
    for r in StatutoryRule.objects.filter(company=company, effective_from__lte=on).order_by("effective_from"):
        values[r.code] = r.value          # the latest effective rule wins
    for code, attr in RULE_FIELDS.items():
        if code in values:
            setattr(rules, attr, Decimal(values[code]) / 100 if code in PERCENT_RULES else Decimal(values[code]))
    return rules


def _money(x) -> Decimal:
    return Decimal(x).quantize(Decimal("0.01"), ROUND_HALF_UP)


def adjustment_elements(emp, run, settle_all=False):
    """Turn this employee's open loans, IOUs, additions and deductions into extra elements for the month.

    Returns (extra_elements, applied [(adjustment id, amount)], absences [(adjustment, days)]).
    With settle_all (an employee's final month) every loan and IOU is recovered in full, not by instalment.
    """
    extras, applied, absences = [], [], []
    for a in emp.adjustments.filter(is_active=True, is_treated=False):
        idx = (run.year - a.start_year) * 12 + (run.month - a.start_month)
        if idx < 0:
            continue
        if a.kind == "ABSENCE":
            if idx == 0:
                absences.append((a, a.days))
            continue
        months = max(a.months, 1)
        if a.kind in ("LOAN", "IOU"):
            # equal installments; the last one (or any catch-up) takes whatever is left
            balance = a.amount - a.paid_before(run)
            amount = balance if (idx >= months - 1 or settle_all) else min(_money(a.amount / months), balance)
            kind, taxable = DEDUCTION, False
        elif idx < months:
            amount = _money(a.amount)
            kind, taxable = (EARNING, a.is_taxable) if a.kind == "ADDITION" else (DEDUCTION, False)
        else:
            continue
        if amount > 0:
            code = {"LOAN": "LOAN", "IOU": "IOU", "ADDITION": "ADD", "DEDUCTION": "DED"}[a.kind] + str(a.pk)
            label = f"{a.get_kind_display()}: {a.description}"[:100]
            extras.append(Element(code, label, kind, str(_money(amount)), taxable=taxable))
            applied.append((a.pk, _money(amount)))
    return extras, applied, absences


def leave_items(emp, run, rules, elements, overrides, gross=None):
    """Leave allowance for approved leave that earns it, and the days of approved unpaid leave this month.

    Returns (extra elements, [(leave request id, amount)], unpaid leave days).
    """
    extras, applied = [], []
    unpaid_days = integrations.unpaid_leave_days(emp, run.year, run.month)
    due = integrations.allowance_due(emp, run) if rules.leave_allowance_rate > 0 else None
    if due:
        flagged = {e.code.upper() for e in elements if e.leave_base}
        if not flagged:
            raise ValueError("Leave allowance is due but no salary element counts toward it. Tick "
                             "'Leave allowance base' on at least one salary element, for example Basic.")
        try:
            first = compute_payslip(emp.gross_monthly if gross is None else gross, elements, rules,
                                    overrides, emp.annual_rent,
                                    emp.pension_applies, emp.nhf_applies)
        except FormulaError as exc:
            raise ValueError(f"{emp.employee_no} {emp.first_name} {emp.last_name}: {exc}")
        base = sum((l.amount for l in first.lines if l.kind == EARNING and l.code.upper() in flagged),
                   Decimal("0")) * 12
        amount = _money(base * rules.leave_allowance_rate)
        if amount > 0:
            extras.append(Element("LEAVEALLOW", "Leave allowance", EARNING, str(amount), taxable=True, one_off=True))
            applied.append((due.pk, amount))
    return extras, applied, unpaid_days


def compute_employee(emp, run, rules, elements):
    """One employee's pay for a run, worked out but not saved. None when they are not paid this month."""
    terms = integrations.exit_terms(emp, run)
    if terms is not None and terms.skip:
        return None                                        # they left in an earlier month
    starter = integrations.start_terms(emp, run)
    if starter is not None and starter.skip:
        return None                                        # they have not started yet
    leaving = terms is not None and terms.final
    overrides = {o.element.code: o.formula for o in emp.overrides.select_related("element")}
    extras, applied, absences = adjustment_elements(emp, run, settle_all=leaving)
    base_gross = integrations.monthly_gross(emp, run)
    leave_extras, leave_applied, unpaid_days = leave_items(emp, run, rules, elements, overrides, base_gross)
    extras += leave_extras
    arrears, arrears_applied = integrations.salary_arrears(emp, run)
    if arrears > 0:             # back pay is a taxable lump sum, taxed once rather than as 12 equal months
        extras.append(Element("ARREARS", "Salary arrears", EARNING, str(_money(arrears)), taxable=True, one_off=True))
    if leaving:                 # settlement lines; taxable earnings are lump sums, taxed once
        for code, name, kind, amount, taxable in terms.extras:
            extras.append(Element(code, name, kind, str(_money(amount)), taxable=taxable,
                                  one_off=taxable and kind == EARNING))
    unpaid_days += integrations.absent_days(emp, run)    # days marked absent in the attendance register
    overtime_pay, overtime_hours, overtime_applied = integrations.overtime(emp, run)
    if overtime_pay > 0:        # overtime varies month to month, so it is taxed once on top like other lump sums
        extras.append(Element("OVERTIME", f"Overtime ({overtime_hours.normalize():f} hrs)", EARNING,
                              str(_money(overtime_pay)), taxable=True, one_off=True))
    if unpaid_days > 0:
        absences.append((None, unpaid_days))              # unpaid leave and absence reduce pay the same way
    gross, reduction, prorata_cut = base_gross, Decimal("0"), Decimal("0")
    days_in_month = calendar.monthrange(run.year, run.month)[1]
    first_day = starter.first_day if starter is not None else 1          # the day they started, in their first month
    last_day = terms.days_worked if leaving else days_in_month           # the last working day, in their final month
    if first_day > last_day:
        return None
    worked_days = last_day - first_day + 1
    if worked_days < days_in_month:                       # a part month: pay only for the days employed
        prorata_cut = base_gross - _money(base_gross * worked_days / days_in_month)
        gross -= prorata_cut
    if absences:
        if rules.absence_divisor <= 0:
            raise ValueError("The absence day rule (ABSENCE_DIVISOR) must be greater than zero.")
        per_day = base_gross / rules.absence_divisor
        amounts = [(a, _money(per_day * days)) for a, days in absences]
        reduction = min(gross, sum(amount for _, amount in amounts))
        gross -= reduction
        applied += [(a.pk, amount) for a, amount in amounts if a is not None and amount > 0]
    try:
        res = compute_payslip(gross, elements + extras, rules, overrides, emp.annual_rent,
                              emp.pension_applies, emp.nhf_applies)
    except FormulaError as exc:
        raise ValueError(f"{emp.employee_no} {emp.first_name} {emp.last_name}: {exc}")
    return SimpleNamespace(res=res, applied=applied, leave_applied=leave_applied, arrears_applied=arrears_applied,
                           base_gross=base_gross, reduction=reduction, absence_days=sum(d for _, d in absences),
                           prorata_cut=prorata_cut, terms=terms, overtime_applied=overtime_applied,
                           worked_days=worked_days, days_in_month=days_in_month)


@transaction.atomic
def generate(run: PayrollRun):
    if run.status != PayrollRun.Status.DRAFT:
        raise ValueError("Only a draft run can be generated again.")
    rules = load_rules(run.company, date(run.year, run.month, 1))
    elements = [to_engine(e) for e in SalaryElement.objects.filter(company=run.company, is_active=True)]
    if not elements:
        raise ValueError("Add at least one salary element first.")
    integrations.refresh_salaries(run.company)
    employees = list(Employee.objects.filter(company=run.company, is_active=True))
    if not employees:
        raise ValueError(
            f"There are no active employees in {run.company.name}. Check the company selector at "
            "the top, and that your employees belong to this company and are marked active.")
    run.payslips.all().delete()
    created = 0
    for emp in employees:
        pay = compute_employee(emp, run, rules, elements)
        if pay is None:
            continue
        res = pay.res
        slip = Payslip.objects.create(
            run=run, employee=emp, gross=res.gross, total_earnings=res.total_earnings,
            total_deductions=res.total_deductions, paye=res.paye, net_pay=res.net_pay,
            employer_cost=res.employer_cost)
        lines = [PayslipLine(payslip=slip, code=l.code, name=l.name, kind=l.kind, amount=l.amount)
                 for l in res.lines]
        if pay.prorata_cut:
            lines.append(PayslipLine(payslip=slip, code="PRORATA", kind="INFO", amount=pay.prorata_cut,
                                     name=f"Part month: paid for {pay.worked_days} of "
                                          f"{pay.days_in_month} days, pay reduced by"))
        if pay.reduction:
            days = pay.absence_days
            lines.append(PayslipLine(payslip=slip, code="ABSENCE", kind="INFO", amount=pay.reduction,
                                     name=f"Absence and unpaid leave: {days.normalize():f} day(s), pay reduced by"))
        if res.rent_relief > 0:
            lines.append(PayslipLine(payslip=slip, code="RENT_RELIEF", kind="INFO", amount=_money(res.rent_relief / 12),
                                     name="Rent relief (1/12 of annual relief, reduces taxable income)"))
        PayslipLine.objects.bulk_create(lines)
        AdjustmentApplication.objects.bulk_create(
            [AdjustmentApplication(adjustment_id=i, payslip=slip, amount=amount) for i, amount in pay.applied])
        integrations.record_allowances(slip, pay.leave_applied)
        integrations.record_salary(slip, pay.base_gross, pay.arrears_applied)
        integrations.record_overtime(slip, pay.overtime_applied)
        created += 1
    return created


def refresh_treated(run: PayrollRun):
    """After approval, mark loans, IOUs and the rest as treated once they are fully applied."""
    for adj in EmployeeAdjustment.objects.filter(applications__payslip__run=run, is_treated=False).distinct():
        if adj.is_complete():
            adj.is_treated = True
            adj.save(update_fields=["is_treated"])


def _explain(exc):
    """Turn a mail error into something the user can act on."""
    if "too many" in str(exc).lower() or getattr(exc, "smtp_code", None) == 454:
        return ("The mail server says there have been too many login attempts. Wait about an hour, then try "
                f"again. Details: {exc}")
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return ("The mail server rejected the username or password. For Gmail, use a 16-character "
                "App Password (2-Step Verification must be on), not your normal password.")
    if isinstance(exc, smtplib.SMTPServerDisconnected):
        return ("The mail server closed the connection. Check the server and port (Gmail: smtp.gmail.com, "
                "port 587 with Use TLS ticked, or port 465), and that antivirus or a firewall is not "
                f"blocking outgoing mail. Gmail can also hide a rejected password behind this message, so check the "
                f"App Password too. Details: {exc}")
    if isinstance(exc, TimeoutError):
        return ("The mail server did not answer in time. This is usually a slow or unstable internet connection, "
                "or a firewall or antivirus holding the connection. Try again in a minute, try another network "
                f"such as a phone hotspot, or try port 465 with Use TLS unticked. Details: {exc}")
    if isinstance(exc, OSError):
        return f"Could not reach the mail server. Check your internet connection, the server name and the port. Details: {exc}"
    return f"The mail server refused the message: {exc}"


def _build_connection(company, port):
    use_ssl = port == 465
    password = company.smtp_password
    if "gmail" in company.smtp_host.lower():
        password = "".join(password.split())          # Google shows App Passwords with spaces, even hidden ones
    return get_connection(
        host=company.smtp_host, port=port,
        username=company.smtp_username or company.email, password=password,
        use_tls=company.smtp_use_tls and not use_ssl, use_ssl=use_ssl, timeout=30)


def _open_connection(company):
    if not company.email or not company.smtp_host:
        raise ValueError("Set up the company email first (Company settings, in the top bar).")
    ports = [company.smtp_port]
    if company.smtp_port == 587:
        ports.append(465)       # many networks block port 587 but allow 465, so fall back to it
    last = None
    for port in ports:
        connection = _build_connection(company, port)
        try:
            for attempt in range(2):          # slow networks often succeed on a second try
                try:
                    connection.open()
                    return connection
                except (TimeoutError, smtplib.SMTPServerDisconnected):
                    if attempt == 1:
                        raise
                    time.sleep(2)
        except (smtplib.SMTPAuthenticationError, smtplib.SMTPException, OSError) as exc:
            if isinstance(exc, smtplib.SMTPAuthenticationError):
                raise ValueError(_explain(exc))     # a wrong password will not be fixed by another port
            last = exc
        except Exception as exc:               # show anything unexpected instead of a crash page
            raise ValueError(f"Unexpected mail error ({type(exc).__name__}): {exc}")
    raise ValueError(_explain(last))


open_mail_connection = _open_connection      # also used by the leave module


explain_mail_error = _explain


def send_test_email(company):
    """Send a short message to the company's own address to check the mail settings."""
    connection = _open_connection(company)
    try:
        EmailMultiAlternatives(
            subject=f"Test email from {company.name} payroll",
            body="Your mail settings work. Payslips will be sent from this address.",
            from_email=f"{company.name} <{company.email}>", to=[company.email],
            connection=connection).send()
    except (smtplib.SMTPException, OSError) as exc:
        raise ValueError(_explain(exc))
    finally:
        connection.close()


GROUP_LABELS = (("EARNING", "Earnings"), ("DEDUCTION", "Deductions"),
                ("EMPLOYER", "Employer contributions"), ("INFO", "For information"))


def payslip_groups(slip):
    lines = list(slip.lines.all())
    groups = [(label, [l for l in lines if l.kind == kind]) for kind, label in GROUP_LABELS]
    return [g for g in groups if g[1]]


def send_payslips(run: PayrollRun, resend=False, only_ids=None):
    """Email each employee's payslip. Returns {"sent": n, "skipped": [names], "failed": [messages]}."""
    company = run.company
    if run.status not in ("APPROVED", "PAID"):
        raise ValueError("Approve the run before emailing payslips.")
    if not company.email or not company.smtp_host:
        raise ValueError("Set up the company email first (Company settings, in the top bar).")
    slips = list(run.payslips.select_related("employee").order_by("employee__employee_no"))
    if only_ids is not None:
        slips = [s for s in slips if s.pk in only_ids]
    elif not resend:
        slips = [s for s in slips if not s.emailed_at]
    result = {"sent": 0, "skipped": [], "failed": []}
    todo = []
    for s in slips:
        (todo if s.employee.email else result["skipped"]).append(
            s if s.employee.email else f"{s.employee.first_name} {s.employee.last_name}")
    if not todo:
        return result
    connection = _open_connection(company)
    period = f"{run.year}-{run.month:02d}"
    try:
        for s in todo:
            emp = s.employee
            who = f"{emp.first_name} {emp.last_name}"
            try:
                groups = payslip_groups(s)
                text = "\n".join(
                    [f"{company.name}", f"Payslip for {period}", f"{emp.employee_no} {who}", ""]
                    + [f"{l.name}: {l.amount:,.2f}" for _, lines in groups for l in lines]
                    + ["", f"Net pay: {s.net_pay:,.2f}"])
                html = render_to_string("payroll/payslip_email.html",
                                        {"company": company, "slip": s, "emp": emp, "period": period, "groups": groups})
                msg = EmailMultiAlternatives(subject=f"Payslip for {period}", body=text,
                                             from_email=f"{company.name} <{company.email}>",
                                             to=[emp.email], connection=connection)
                msg.attach_alternative(html, "text/html")
                msg.send()
            except (smtplib.SMTPException, OSError) as exc:
                result["failed"].append(f"{who}: {_explain(exc)}")
            except Exception as exc:       # show anything unexpected instead of a crash page
                result["failed"].append(f"{who}: unexpected error ({type(exc).__name__}): {exc}")
            else:
                s.emailed_at = timezone.now()
                s.save(update_fields=["emailed_at"])
                result["sent"] += 1
    finally:
        connection.close()
    return result


TRANSITIONS = {"submit": ("DRAFT", "PENDING"), "approve": ("PENDING", "APPROVED"),
               "pay": ("APPROVED", "PAID"), "reopen": ("PENDING", "DRAFT")}


def perform(run: PayrollRun, action: str, user):
    if action == "generate":
        return generate(run)
    if action == "delete":
        if run.status != "DRAFT":
            raise ValueError("Only a draft run can be deleted.")
        return run.delete()
    if action not in TRANSITIONS:
        raise ValueError("Unknown action.")
    source, target = TRANSITIONS[action]
    if run.status != source:
        raise ValueError(f"This run is {run.get_status_display().lower()}, so it cannot be {action}ed.")
    if action == "submit" and not run.payslips.exists():
        raise ValueError("Generate payslips before submitting.")
    if action == "approve":
        access.check_can_approve(user, run.company, "PAYROLL", "payroll runs")
        if run.created_by_id == user.id and not user.is_superuser:
            raise ValueError("A different user must approve this run.")
        run.approved_by, run.approved_at = user, timezone.now()
    run.status = target
    run.save()
    if action == "approve":
        refresh_treated(run)
