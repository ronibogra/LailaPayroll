"""User roles: each role is a set of permissions. ADMIN always has everything.
Roles are edited on the Users page; users get one role (+ optional single-company limit)."""

PERMS: dict[str, str] = {
    "dashboard.view": "Dashboard",
    "employees.view": "See employees & profiles",
    "employees.edit": "Add / edit / import employees, photos and documents",
    "salary.view": "See salaries and salary details",
    "salary.edit": "Change salaries, pay mode and OT eligibility of employees (Admin)",
    "mobile.manage": "Allow mobile punch and set employees' mobile PINs (Admin)",
    "appointment.letter": "Make and change appointment letters (Admin)",
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
}

GROUPS = [
    ("General", ["dashboard.view", "reports.export"]),
    ("Employees", ["employees.view", "employees.edit", "salary.view", "salary.edit", "mobile.manage",
                   "appointment.letter"]),
    ("Punch & machines", ["punches.view", "punches.edit", "machines.manage"]),
    ("Attendance", ["attendance.view", "attendance.process", "shifts.manage", "holidays.manage"]),
    ("Leave", ["leave.manage", "leave.approve_dept", "leave.approve_final", "leave.policy"]),
    ("Payroll", ["payroll.view", "payroll.process", "payroll.finalize", "payroll.edit", "policy.edit"]),
    ("Administration", ["company.manage", "users.manage", "backup.manage", "logs.view"]),
]

BUILTIN_ROLES: dict[str, tuple[str, list[str]]] = {
    "ADMIN": ("Administrator - everything", list(PERMS)),
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
