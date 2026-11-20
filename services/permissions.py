"""User roles: each role is a set of permissions. ADMIN always has everything.
Roles are edited on the Users page; users get one role (+ optional single-company limit)."""

PERMS: dict[str, str] = {
    "dashboard.view": "Dashboard",
    "employees.view": "See employees & profiles",
    "employees.edit": "Add / edit / import employees and photos",
    "salary.view": "See salaries and salary details",
    "punches.view": "See punch records and first & last punch",
    "punches.edit": "Import punch files, manual punches, download from machines",
    "machines.manage": "Add / edit / remove machines",
    "attendance.view": "See daily attendance, leave and shifts",
    "attendance.process": "Process attendance month",
    "leave.manage": "Add / remove / import leave",
    "shifts.manage": "Create, import and assign shifts",
    "holidays.manage": "Add / remove / import holidays",
    "payroll.view": "See payroll, salary sheets and payslips",
    "payroll.process": "Process payroll, upload advances & deductions",
    "payroll.finalize": "Finalize (lock) payroll",
    "policy.edit": "Change the pay policy",
    "company.manage": "Head Office, companies, logo, weekly off day, office location",
    "users.manage": "Users and roles",
    "backup.manage": "Backup and restore",
    "logs.view": "Error log and activity log",
    "reports.export": "Export reports to Excel / PDF",
}

GROUPS = [
    ("General", ["dashboard.view", "reports.export"]),
    ("Employees", ["employees.view", "employees.edit", "salary.view"]),
    ("Punch & machines", ["punches.view", "punches.edit", "machines.manage"]),
    ("Attendance", ["attendance.view", "attendance.process", "leave.manage", "shifts.manage", "holidays.manage"]),
    ("Payroll", ["payroll.view", "payroll.process", "payroll.finalize", "policy.edit"]),
    ("Administration", ["company.manage", "users.manage", "backup.manage", "logs.view"]),
]

BUILTIN_ROLES: dict[str, tuple[str, list[str]]] = {
    "ADMIN": ("Administrator - everything", list(PERMS)),
    "HR": ("HR", ["dashboard.view", "reports.export", "employees.view", "employees.edit", "salary.view",
                  "punches.view", "punches.edit", "machines.manage", "attendance.view", "attendance.process",
                  "leave.manage", "shifts.manage", "holidays.manage", "payroll.view", "payroll.process", "logs.view"]),
    "ACCOUNTS": ("Accounts", ["dashboard.view", "reports.export", "employees.view", "salary.view", "punches.view",
                              "attendance.view", "attendance.process", "payroll.view", "payroll.process",
                              "payroll.finalize", "policy.edit"]),
    "VIEWER": ("Viewer - attendance only, no salaries", ["dashboard.view", "reports.export", "employees.view",
                                                          "punches.view", "attendance.view"]),
}


def parse(perms: str | None) -> set[str]:
    return {p for p in (perms or "").split(",") if p in PERMS}
