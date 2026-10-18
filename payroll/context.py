from django.apps import apps
from django.urls import reverse

from .access import companies_for


def _modules(user):
    section = lambda name: reverse("section_list", args=[name])
    modules = [
        ("Employees", section("employees"), ("/s/employees",), []),
        ("Payroll", reverse("run_list"), ("/runs",), []),
        ("Payslips", reverse("payslip_list"), ("/payslips",), []),
        ("Adjustments", section("loans"), ("/s/loans", "/s/absences", "/s/ious", "/s/additions", "/s/deductions"),
         [("Loans", section("loans")), ("Absence", section("absences")), ("IOU", section("ious")),
          ("Other additions", section("additions")), ("Other deductions", section("deductions"))]),
        ("Setup", section("elements"), ("/s/elements", "/s/bands", "/s/rules", "/company", "/users", "/approvers"),
         [("Salary elements", section("elements")), ("PAYE bands", section("bands")),
          ("Statutory rules", section("rules"))]
         + ([("Company settings", reverse("company_edit")), ("Users", reverse("user_list")),
             ("Approvers", reverse("approver_list"))] if user.is_superuser else [])),
        ("Calculator", reverse("calculator"), ("/calculator",), []),
    ]
    if apps.is_installed("leave"):          # the leave module is optional and has its own menu
        leave = lambda name: reverse("leave:section_list", args=[name])
        modules.insert(3, ("Leave", reverse("leave:requests"), ("/leave",),
                           [("Requests", reverse("leave:requests")), ("Balances", reverse("leave:balances")),
                            ("Leave types", leave("types")), ("Entitlements", leave("entitlements")),
                            ("Public holidays", leave("holidays"))]))
    if apps.is_installed("remittances"):    # PAYE and pension schedules; sits right after Payslips
        at = next(i for i, m in enumerate(modules) if m[0] == "Payslips") + 1
        modules.insert(at, ("Remittances", reverse("remittances:overview"), ("/remittances",),
                            [("Overview", reverse("remittances:overview"))]
                            + ([("Deadlines", reverse("remittances:deadlines"))] if user.is_superuser else [])))
    if apps.is_installed("attendance"):     # attendance register and overtime; sits just before Adjustments
        at = next(i for i, m in enumerate(modules) if m[0] == "Adjustments")
        subs_ = [("Register", reverse("attendance:register")), ("Summary", reverse("attendance:summary")),
                 ("Overtime", reverse("attendance:overtime")), ("Import", reverse("attendance:import"))]
        modules.insert(at, ("Attendance", reverse("attendance:register"), ("/attendance",), subs_))
    if apps.is_installed("exits"):          # leavers and final pay; sits just before Adjustments
        at = next(i for i, m in enumerate(modules) if m[0] == "Adjustments")
        modules.insert(at, ("Exits", reverse("exits:overview"), ("/exits",), []))
    if apps.is_installed("salary"):         # salary history and back pay; sits just before Payroll
        at = next(i for i, m in enumerate(modules) if m[0] == "Payroll")
        modules.insert(at, ("Salary", reverse("salary:overview"), ("/salary",),
                            [("Salaries", reverse("salary:overview")), ("Pay grades", reverse("salary:grades"))]))
    if apps.is_installed("recruitment"):    # vacancies and onboarding; sits just before Payroll
        at = next(i for i, m in enumerate(modules) if m[0] == "Payroll")
        modules.insert(at, ("Recruitment", reverse("recruitment:vacancies"), ("/recruitment",),
                            [("Vacancies", reverse("recruitment:vacancies")),
                             ("Onboarding", reverse("recruitment:onboarding"))]))
    if apps.is_installed("hr"):             # the HR records module is optional too
        modules.insert(1, ("HR", reverse("hr:directory"), ("/hr",),
                           [("Directory", reverse("hr:directory")), ("Alerts", reverse("hr:alerts"))]))
    if apps.is_installed("portal") and user.is_superuser:      # who has an employee login; administrators only
        modules.append(("Portal access", reverse("portal:access"), ("/portal/access",), []))
    if apps.is_installed("audit") and user.is_superuser:      # administrators only
        modules.append(("Audit log", reverse("audit:log"), ("/audit",), []))
    return modules


def _theme(user):
    """The person's saved appearance. Anyone who has never chosen follows their device, as the site always did."""
    preference = getattr(user, "preference", None)
    return preference.theme if preference else "system"


def company(request):
    if not request.user.is_authenticated:
        return {"theme": "system", "theme_next": "light"}
    theme = _theme(request.user)
    path, modules, subs = request.path, [], []
    for label, url, prefixes, items in _modules(request.user):
        active = path.startswith(prefixes)
        modules.append({"label": label, "url": url, "active": active})
        if active and items:
            best = max((u for _, u in items if path.startswith(u)), key=len, default=None)
            subs = [{"label": l, "url": u, "active": u == best} for l, u in items]
    return {"companies": companies_for(request.user).order_by("name"),
            "current_company": getattr(request, "company", None),
            "nav_modules": modules, "nav_subs": subs,
            "audit_installed": apps.is_installed("audit"),
            "theme": theme, "theme_next": "dark" if theme == "light" else "light"}
