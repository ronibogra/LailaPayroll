"""User roles: each role is a set of permissions. ADMIN always has everything.
Roles are edited on the Users page; users get one role (+ optional single-company limit)."""

PERMS: dict[str, str] = {
    "dashboard.view": "Dashboard",
    "employees.view": "See employees & profiles",
    "employees.edit": "Add / edit / import employees, photos and documents",
    "salary.view": "See salaries and salary details",
    "salary.edit": "Change salaries, pay mode and OT eligibility of employees (Admin)",
    "mobile.manage": "Allow mobile punch and set employees' mobile PINs (Admin)",
    "appointment.letter": "Appointment, confirmation, release letters and experience certificates (Admin)",
    "assets.manage": "Edit asset records and transfer assets to another employee (Admin)",
    "punches.view": "See punch records and first & last punch",
    "punches.edit": "Import punch files, manual punches, download from machines",
    "machines.manage": "Add / edit / remove machines",
    "attendance.view": "See daily attendance, leave and shifts",
    "attendance.process": "Process attendance month",
    "leave.manage": "Grant / remove / import leave for employees (HR)",
    "leave.approve_dept": "Approve leave requests as Department Head (own departments)",
    "leave.approve_final": "Final approval of leave requests (e.g. CFO / HR head)",
    "leave.policy": "Leave types, yearly days and document rules (Admin)",
    "shifts.manage": "Create, edit, import and assign shifts (Admin)",
    "holidays.manage": "Add / remove / import holidays",
    "payroll.view": "See payroll, salary sheets and payslips",
    "payroll.process": "Process payroll, upload advances & deductions",
    "payroll.finalize": "Finalize (lock) payroll",
    "payroll.edit": "Edit payroll lines and custom pay components (Admin)",
    "policy.edit": "Change the pay policy and late / OT rules",
    "company.manage": "Head Office, companies, logo, weekly off day, office location",
    "users.manage": "Users and roles",
    "backup.manage": "Backup and restore",
    "logs.view": "Error log and activity log",
    "reports.export": "Export reports to Excel / PDF",
    "self.service": "My page: own profile, attendance, leave requests, payslips and assets (Staff)",
}

ADMIN_ROLES = ("SUPER_ADMIN", "ADMIN")       # these roles always have every permission (not editable)


def super_like(name: str) -> bool:
    """A role someone made by hand for the super admin: SUPER_ADMIN, SUPPER_ADMIN, SUPERADMIN, Super Admin ..."""
    import re
    return bool(re.fullmatch(r"SU+P+E*R[\s_-]*ADMIN(ISTRATOR)?", (name or "").strip().upper()))


# not a role permission: only the SUPER_ADMIN role gets it
SUPER_PERMS = {"data.delete": "Delete data - Delete centre and bulk delete (Super Admin)"}

GROUPS = [
    ("General", ["dashboard.view", "reports.export", "self.service"]),
    ("Employees", ["employees.view", "employees.edit", "salary.view", "salary.edit", "mobile.manage",
                   "appointment.letter", "assets.manage"]),
    ("Punch & machines", ["punches.view", "punches.edit", "machines.manage"]),
    ("Attendance", ["attendance.view", "attendance.process", "shifts.manage", "holidays.manage"]),
    ("Leave", ["leave.manage", "leave.approve_dept", "leave.approve_final", "leave.policy"]),
    ("Payroll", ["payroll.view", "payroll.process", "payroll.finalize", "payroll.edit", "policy.edit"]),
    ("Administration", ["company.manage", "users.manage", "backup.manage", "logs.view"]),
]

BUILTIN_ROLES: dict[str, tuple[str, list[str]]] = {
    "SUPER_ADMIN": ("Super Admin - everything, including deleting data", list(PERMS)),
    "ADMIN": ("Administrator - everything except deleting data", list(PERMS)),
    "HR": ("HR", ["dashboard.view", "reports.export", "employees.view", "employees.edit", "salary.view",
                  "punches.view", "punches.edit", "machines.manage", "attendance.view", "attendance.process",
                  "leave.manage", "holidays.manage", "payroll.view", "payroll.process", "logs.view"]),
    "ACCOUNTS": ("Accounts", ["dashboard.view", "reports.export", "employees.view", "salary.view", "punches.view",
                              "attendance.view", "attendance.process", "payroll.view", "payroll.process",
                              "payroll.finalize", "policy.edit"]),
    "VIEWER": ("Viewer - attendance only, no salaries", ["dashboard.view", "reports.export", "employees.view",
                                                          "punches.view", "attendance.view"]),
    "DEPT_HEAD": ("Department Head - approves leave of own departments", [
        "dashboard.view", "reports.export", "employees.view", "punches.view", "attendance.view", "leave.approve_dept"]),
    "STAFF": ("Staff - employee's own page only", ["self.service"]),
    "CFO": ("CFO - final leave approval, payroll view & finalize", [
        "dashboard.view", "reports.export", "employees.view", "salary.view", "punches.view", "attendance.view",
        "payroll.view", "payroll.finalize", "leave.approve_final"]),
}

# changes to built-in roles of existing installations, applied once each (see main.startup)
ROLE_MIGRATIONS = [
    (2, "HR", ["salary.edit"], ["shifts.manage"]),
    # salary & payment, mobile attendance and appointment letters: Admin only
    (3, "HR", [], ["salary.edit"]),
]


def parse(perms: str | None) -> set[str]:
    return {p for p in (perms or "").split(",") if p in PERMS}
