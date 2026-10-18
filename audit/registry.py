"""What gets recorded. Everything here is a plain list, so adding a model is one line."""
from django.conf import settings

USER = settings.AUTH_USER_MODEL.lower()

# Models whose creation, change and deletion are recorded. Models from apps that are not installed are skipped.
AUDITED = [
    "payroll.company", "payroll.employee", "payroll.salaryelement", "payroll.employeeelementoverride",
    "payroll.taxband", "payroll.statutoryrule", "payroll.payrollrun", "payroll.employeeadjustment",
    "payroll.companyaccess", "payroll.approver", USER,
    "leave.leavetype", "leave.publicholiday", "leave.leaveentitlement", "leave.leaverequest",
    "hr.employeeprofile", "hr.emergencycontact", "hr.employeedocument",
    "salary.paygrade", "salary.salaryrevision",
    "exits.exit", "exits.clearanceitem", "exits.settlementline",
    "portal.employeeaccount", "security.twofactor",
    "recruitment.vacancy", "recruitment.applicant", "recruitment.interview", "recruitment.offer",
    "recruitment.onboarding", "recruitment.onboardingitem",
    "attendance.attendancesetting", "attendance.attendancerecord", "attendance.overtimerequest",
    "remittances.remittancesetting", "remittances.remittancepayment",
]

# A change is recorded, but the values never are: the log shows that it happened, not what the secret was.
MASKED = {
    "payroll.company": {"smtp_password"},
    "payroll.employee": {"account_number"},
    USER: {"password"},
}
MASK_WORDS = ("password", "secret", "token")

# Fields that are noise or are stored under a meaningless name.
EXCLUDED = {
    USER: {"last_login", "date_joined"},
    "hr.employeedocument": {"file"},
    "recruitment.applicant": {"cv"},
    "security.twofactor": {"last_step"},                  # changes with every sign-in: noise
}

# Views that matter even though they change nothing: {view name: (HTTP method, what to write)}.
WATCHED = {
    "run_csv": ("GET", "Bank payment file downloaded"),
    "payslip_email": ("POST", "Payslip emails requested"),
    "hr:document_download": ("GET", "HR document downloaded"),
    "remittances:csv": ("GET", "Remittance schedule downloaded"),
    "audit:csv": ("GET", "Audit log exported"),
}
HIDDEN = "(hidden)"
