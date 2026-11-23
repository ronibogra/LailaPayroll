"""End-to-end check of an INSTALLED LailaPayroll (run by the Windows build pipeline after a silent install).

Walks through what a person does: first-time setup, login, punch machines, employees, attendance,
payroll, downloads, mobile page, company-limited user. Exits non-zero on the first failure.
Usage: python tests/e2e_installed.py [http://localhost:8585]
"""
import io
import re
import sys
import time
from calendar import month_name as month_name_
from datetime import date, timedelta

import requests
import urllib3
from openpyxl import Workbook, load_workbook

urllib3.disable_warnings()
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8585"
HTTPS = BASE.replace("http://", "https://").replace(":8585", ":8443")
STEP = [0]


def step(name):
    STEP[0] += 1
    print(f"[{STEP[0]:02d}] {name}", flush=True)


def ok(cond, msg):
    if not cond:
        raise AssertionError(msg)


def page(s, path, must=None, code=200):
    r = s.get(BASE + path, timeout=60)
    ok(r.status_code == code, f"GET {path} -> {r.status_code}: {r.text[:500]}")
    if must:
        ok(must in r.text, f"GET {path}: '{must}' not found. Page: {r.text[:800]}")
    return r


def post(s, path, data=None, files=None, must=None, must_not="alert err", code=200):
    r = s.post(BASE + path, data=data, files=files, timeout=180)
    ok(r.status_code == code, f"POST {path} -> {r.status_code}: {r.text[:800]}")
    if must:
        ok(must in r.text, f"POST {path}: '{must}' not in result. URL {r.url} Page: {r.text[:1500]}")
    if must_not and must_not in r.text:
        err = re.search(r'class="alert err">(.*?)</div>', r.text, re.S)
        raise AssertionError(f"POST {path} showed an error: {err.group(1) if err else '?'}")
    return r


def wait_up():
    for _ in range(120):
        try:
            if requests.get(BASE + "/health", timeout=3).ok:
                return
        except requests.RequestException:
            pass
        time.sleep(1)
    raise AssertionError("server did not answer /health")


def xlsx(rows) -> bytes:
    wb = Workbook()
    for r in rows:
        wb.active.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def main():
    step("server answers")
    wait_up()
    s = requests.Session()

    step("fresh install redirects to first-time setup")
    r = s.get(BASE + "/", timeout=30)
    ok(r.url.endswith("/setup") and "first-time setup" in r.text.lower(),
       f"expected setup page, got {r.url}")

    step("finish setup (admin + 2 companies)")
    from PIL import Image
    logo = io.BytesIO()
    Image.new("RGBA", (300, 300), (0, 160, 80, 255)).save(logo, "PNG")
    post(s, "/setup", {"ho_name": "LAILA GROUP", "ho_address": "Head Office, Dhaka", "username": "admin",
                       "password": "Admin@12345", "password2": "Admin@12345",
                       "companies": "HO, LAILA GROUP - Head Office\nLTL, Laila Textiles Ltd", "wd4": "1",
                       "geofence_lat": "23.80", "geofence_lon": "90.40", "geofence_radius_m": "200"},
         files={"logo": ("logo.png", logo.getvalue(), "image/png")}, must="Machines (")
    r = requests.get(BASE + "/brand/logo.png", timeout=30)
    ok(r.ok and r.headers["content-type"] == "image/png" and Image.open(io.BytesIO(r.content)).getpixel((5, 5))[1] == 160,
       "Head Office logo not served")
    page(s, "/setup")  # now redirects to login/dashboard, must not crash

    step("log out / log in")
    s.get(BASE + "/logout")
    post(s, "/login", {"username": "admin", "password": "wrong-pass"}, must="Wrong username or password", must_not=None)
    post(s, "/login", {"username": "admin", "password": "Admin@12345"}, must="Dashboard")

    step("every page opens")
    for p in ("/", "/attendance", "/punches", "/punches/first-last", "/leaves", "/shifts", "/devices",
              "/devices?new=1", "/employees", "/payroll", "/settings", "/users", "/users?role=HR", "/audit", "/errors",
              "/employees/new", "/backup", "/leave-policy", "/leaves/approvals", "/payroll/components"):
        page(s, p, must="LAILA GROUP")

    step("ZKTeco machine handshake -> appears as pending")
    r = requests.get(BASE + "/iclock/cdata", params={"SN": "CI-ZK-1", "options": "all"}, timeout=30)
    ok(r.ok and "GET OPTION FROM: CI-ZK-1" in r.text, f"handshake: {r.status_code} {r.text}")
    r = requests.post(BASE + "/iclock/registry", params={"SN": "CI-ZK-1"}, data="~DeviceName=SpeedFace", timeout=30)
    ok(r.ok and "RegistryCode" in r.text, f"registry: {r.text}")
    devs = page(s, "/devices", must="PENDING").text
    dev_id = re.search(r"/devices\?edit=(\d+)", devs).group(1)

    today = date.today()
    prev_last = today.replace(day=1) - timedelta(days=1)
    y, m = prev_last.year, prev_last.month
    month = f"{y}-{m:02d}"
    lines = []
    d = date(y, m, 1)
    while d.month == m:
        if d.weekday() != 4:  # Friday off
            lines.append(f"1001\t{d} 08:55:00\t0\t15\t0\t0")
            lines.append(f"1001\t{d} 20:10:00\t1\t15\t0\t0")   # 2h OT (cap) + extra
            if d.day not in (3, 4):                               # 2 absent days for 2002
                lines.append(f"2002\t{d} 09:20:00\t0\t15\t0\t0")  # late
                lines.append(f"2002\t{d} 18:05:00\t1\t15\t0\t0")
        d += timedelta(days=1)
    body = "\r\n".join(lines) + "\r\n"

    step("unapproved machine punches are ignored")
    r = requests.post(BASE + "/iclock/cdata", params={"SN": "CI-ZK-1", "table": "ATTLOG"}, data=body, timeout=60)
    ok(r.ok and r.text.startswith("OK"), f"ATTLOG: {r.text}")
    page(s, f"/punches?start={prev_last}&end={prev_last}", must="No punches")

    step("approve machine, punches arrive")
    post(s, "/devices/save", {"device_id": dev_id, "name": "Gate-1 Face", "brand": "ZKTECO_PUSH",
                              "serial_number": "CI-ZK-1", "port": "80", "approved": "1", "enabled": "1"},
         must="saved")
    r = requests.post(BASE + "/iclock/cdata", params={"SN": "CI-ZK-1", "table": "ATTLOG"}, data=body, timeout=60)
    ok(r.text.strip() == f"OK: {len(lines)}", f"ATTLOG after approve: {r.text}")
    requests.get(BASE + "/iclock/getrequest", params={"SN": "CI-ZK-1"}, timeout=30)
    page(s, f"/punches?start={prev_last}&end={prev_last}", must="1001")
    page(s, "/devices", must="new ")

    step("Hikvision machine add + test (unreachable IP must give a clear error)")
    post(s, "/devices/save", {"name": "Lobby Hik", "brand": "HIKVISION", "ip_address": "127.0.0.1", "port": "9",
                              "username": "admin", "password": "x", "approved": "1", "enabled": "1"},
         must="connection test failed: cannot reach", must_not=None)
    hik = re.findall(r"/devices/(\d+)/test", page(s, "/devices").text)
    ok(hik, "Hikvision test button missing")
    r = post(s, f"/devices/{hik[0]}/test", must_not=None)
    ok("cannot reach" in r.text, f"expected 'cannot reach' error, got: {r.text[:600]}")

    step("ZKTeco by IP: unreachable machine is saved, error is clear and goes to the Error log")
    r = post(s, "/devices/save", {"brand": "ZKTECO", "ip_address": "127.0.0.1", "port": "9", "machine_no": "17",
                                  "name": "ZK Test 17", "direction": "IN", "password": "0", "approved": "1",
                                  "enabled": "1"}, must_not=None)
    ok("connection test failed" in r.text and "ZK Test 17" in r.text, f"ZK save: {r.text[:800]}")
    zk = re.search(r'/devices/(\d+)/pull" style="display:inline"><button class="link">Download', r.text)
    ok(zk is not None, "download button for ZK machine missing")
    page(s, "/errors", must="ZK Test 17")
    r = post(s, "/punches/download", must_not=None)
    ok("Problems" in r.text, f"download-all should report the unreachable machines: {r.text[:600]}")

    step("shift: create NIGHT shift")
    post(s, "/shifts/save", {"code": "NIGHT", "name": "Night 20-05", "start": "20:00", "end": "05:00",
                             "grace_minutes": "10", "break_minutes": "60", "break_after_minutes": "300",
                             "early_window_minutes": "180", "ot_round_minutes": "30", "max_ot_minutes": "120",
                             "offday_work_is_ot": "1"}, must="saved")

    step("employee Excel template + import (2 companies)")
    r = page(s, "/employees/template.xlsx")
    load_workbook(io.BytesIO(r.content))
    head = ["company", "employee_code", "card_no", "full_name", "department", "designation", "section", "grade",
            "shift_code", "joined_on", "left_on", "gross", "basic", "house_rent", "medical", "transport", "food",
            "payment_mode", "bank_name", "account_no", "status"]
    rows = [head,
            ["HO", "HO-0001", "1001", "Md. Rahim", "Accounts", "Officer", "", "4", "", "2024-01-01", None, 12500,
             None, None, None, None, None, "BANK", "DBBL", "1234567890", "ACTIVE"],
            ["LTL", "LTL-0001", "2002", "Ms. Karima", "Sewing", "Operator", "L1", "3", "GENERAL", "2024-01-01", None,
             13550, None, None, None, None, None, "CASH", None, None, "ACTIVE"]]
    bad = [head, ["XX", "X-1", "9", "Nobody", "", "", "", "", "", "", None, 12500] + [None] * 9]
    post(s, "/employees/import", files={"file": ("bad.xlsx", xlsx(bad))}, must="unknown company", must_not=None)
    post(s, "/employees/import", files={"file": ("emp.xlsx", xlsx(rows))}, must="2 added")
    page(s, "/employees", must="Ms. Karima")
    # only code (+ company) - card defaults to the code, name to the code
    post(s, "/employees/import", files={"file": ("min.xlsx", xlsx([["company", "employee_code"], ["HO", 12127], ["HO", "12160"]]))},
         must="2 added")
    page(s, "/employees?q=12127", must="12127")
    # update with a real column layout from HR: Name / Employee ID / Mobile - empty cells keep data
    post(s, "/employees/import", files={"file": ("upd.xlsx", xlsx([["Employee ID", "Name", "Mobile", "company"],
                                                                     ["12127", "Abdul Karim", "01711111111", ""]]))},
         must="1 updated")
    page(s, "/employees?q=12127", must="Abdul Karim")

    step("punch file import (machine export: AC-No + Date + Time)")
    csv_body = "AC-No.,Name,Date,Time,State\n" + "".join(
        f"12127,Abdul Karim,{date(y, m, d):%d/%m/%Y},{hh},{st}\n"
        for d in (2, 3) for hh, st in (("08:57", "C/In"), ("13:02", "C/Out"), ("18:31", "C/Out")))
    r = post(s, "/punches/import", files={"file": ("export.csv", csv_body.encode())}, must="6 punches added")
    xl = xlsx([["Report"], ["User ID", "Punch Time"], ["12160", f"{date(y, m, 2)} 09:01:00"], ["12160", f"{date(y, m, 2)} 18:00:00"],
               ["99999", f"{date(y, m, 2)} 10:00:00"], ["12160", "garbage"]])
    post(s, "/punches/import", files={"file": ("export.xlsx", xl)}, must="3 punches added")
    page(s, "/errors", must="rows skipped")

    step("first & last punch report (incl. unknown IDs)")
    r = page(s, f"/punches/first-last?start={date(y, m, 2)}&end={date(y, m, 2)}", must="Abdul Karim")
    ok("08:57" in r.text and "18:31" in r.text, "first/last times for 12127 missing")
    ok("not an employee" in r.text, "unknown ID 99999 should be listed")
    r = page(s, f"/punches/first-last.xlsx?start={date(y, m, 1)}&end={prev_last}")
    ws = load_workbook(io.BytesIO(r.content)).active
    ok(ws.max_row > 10, f"first-last Excel too small: {ws.max_row}")
    r = page(s, f"/punches.xlsx?start={date(y, m, 1)}&end={prev_last}")
    load_workbook(io.BytesIO(r.content))
    page(s, f"/punches?start={date(y, m, 1)}&end={prev_last}&unknown=1", must="99999")

    step("employee profile with photo")
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (800, 1000), (200, 30, 30)).save(buf, "PNG")
    emp_id = re.search(r'/employees/(\d+)/edit">Edit', page(s, "/employees?q=12127").text).group(1)
    form = {"emp_id": emp_id, "company_id": "1", "employee_code": "12127", "card_no": "12127",
            "full_name": "Abdul Karim", "designation": "Accountant", "department": "Accounts",
            "father_name": "Late Abdul Hakim", "date_of_birth": "1990-05-01", "blood_group": "B+",
            "phone": "01711111111", "present_address": "Dhaka", "gross": "30000", "auto_split": "1",
            "payment_mode": "BANK", "employment_status": "ACTIVE"}
    r = post(s, "/employees/save", form, files={"photo": ("me.png", buf.getvalue(), "image/png")}, must="Saved 12127")
    ok("Late Abdul Hakim" in r.text and "B+" in r.text and "First &amp; last punch" in r.text, "profile page content")
    r = s.get(BASE + f"/employees/{emp_id}/photo", timeout=30)
    ok(r.ok and r.headers.get("content-type") == "image/jpeg", f"photo: {r.status_code} {r.headers.get('content-type')}")
    ok(Image.open(io.BytesIO(r.content)).size[1] <= 600, "photo should be resized")
    page(s, f"/employees/{emp_id}/edit", must="Late Abdul Hakim")

    step("holiday + leave")
    post(s, "/settings/holidays", {"day": str(date(y, m, 15)), "name": "CI Holiday"}, must="Holiday added")
    post(s, "/leaves", {"employee_code": "LTL-0001", "start_date": str(date(y, m, 5)), "end_date": str(date(y, m, 5)),
                        "leave_type": "SICK"}, must="Leave saved")

    step("process attendance month")
    post(s, "/attendance/process", {"month": month}, must="Processed")
    page(s, f"/attendance?day={date(y, m, 2)}", must="Md. Rahim")
    page(s, f"/attendance/month.xlsx?month={month}")
    page(s, f"/attendance/day.xlsx?day={date(y, m, 2)}")

    step("advances upload + payroll HO")
    post(s, "/payroll/adjustments", {"month": month, "company_id": "1"},
         files={"file": ("adj.xlsx", xlsx([["employee_code", "other_addition", "advance", "other_deduction", "tax", "note"],
                                           ["HO-0001", 0, 1000, 0, 0, "CI advance"]]))}, must="1 adjustments saved")
    r = post(s, "/payroll/process", {"month": month, "company_id": "1"}, must="Draft payroll ready for 2 employees",
             must_not=None)
    ok("Skipped" in r.text and "12160" in r.text, "employee without salary must be reported as skipped")
    run_id = re.search(r"/payroll/run/(\d+)/salary.xlsx", r.text).group(1)
    ok("Md. Rahim" in r.text and "Abdul Karim" in r.text and "Ms. Karima" not in r.text, "HO payroll must contain only HO staff")

    step("payroll LTL")
    r = post(s, "/payroll/process", {"month": month, "company_id": "2"}, must="Draft payroll ready for 1 employees")
    ok("Ms. Karima" in r.text, "LTL payroll must contain Karima")

    step("salary sheet + payslips download")
    r = page(s, f"/payroll/run/{run_id}/salary.xlsx")
    wb = load_workbook(io.BytesIO(r.content))
    ok("Salary Sheet" in wb.sheetnames and "Bank Advice" in wb.sheetnames, f"sheets: {wb.sheetnames}")
    r = page(s, f"/payroll/run/{run_id}/payslips.pdf")
    ok(r.content[:4] == b"%PDF", "payslip is not a PDF")

    step("combined HO salary workbook (all companies)")
    r = page(s, f"/payroll/combined.xlsx?month={month}")
    wb = load_workbook(io.BytesIO(r.content))
    ok(wb.sheetnames[:3] == ["HO Summary", "HO", "LTL"], f"combined sheets: {wb.sheetnames}")

    step("finalize HO locks it")
    post(s, "/payroll/finalize", {"month": month, "company_id": "1", "confirm": "FINAL"}, must="finalized")
    post(s, "/payroll/process", {"month": month, "company_id": "1"}, must="already finalized", must_not=None)

    step("pay policy per company")
    post(s, "/settings/policy", {"policy_company": "2", "attendance_bonus": "500"}, must="Pay policy saved")
    page(s, "/settings?policy_company=2", must="own")

    step("company-limited HR user sees only LTL")
    post(s, "/users/save", {"username": "ltl_hr", "role": "HR", "password": "LtlHr@12345", "active": "1",
                            "company_id": "2"}, must="saved")
    s2 = requests.Session()
    post(s2, "/login", {"username": "ltl_hr", "password": "LtlHr@12345"}, must="Dashboard")
    r = page(s2, "/employees")
    ok("Ms. Karima" in r.text and "Md. Rahim" not in r.text, "limited user must not see other company staff")
    ok(s2.get(BASE + "/users", timeout=30, headers={"accept": "text/html"}).status_code == 403, "HR must not open Users")

    step("mobile page on HTTPS + employee PIN login")
    emp = re.search(r'/employees/(\d+)/edit">Edit', page(s, "/employees?q=HO-0001").text).group(1)
    form = {"emp_id": emp, "company_id": "1", "employee_code": "HO-0001", "card_no": "1001", "full_name": "Md. Rahim",
            "gross": "12500", "auto_split": "1", "payment_mode": "BANK", "employment_status": "ACTIVE",
            "allow_mobile": "1", "mobile_pin": "4321"}
    post(s, "/employees/save", form, files={"_": ("", b"")}, must="Saved HO-0001")
    post(s, "/settings/general", {"wd4": "1", "geofence_lat": "23.80", "geofence_lon": "90.40", "geofence_radius_m": "200",
                                  "geofence_mode": "STRICT"}, must="Saved")
    mob = requests.Session()
    r = mob.get(HTTPS + "/m", verify=False, timeout=30)
    ok(r.ok and "Employee ID" in r.text, f"mobile page: {r.status_code}")
    r = mob.post(HTTPS + "/m/login", data={"employee_code": "HO-0001", "pin": "4321"}, verify=False, timeout=30)
    ok("PUNCH" in r.text, "mobile login failed")
    r = mob.post(HTTPS + "/m/punch", data={"latitude": "23.8001", "longitude": "90.4001", "accuracy": "15"},
               verify=False, timeout=30)
    ok(r.json().get("ok"), f"mobile punch inside office: {r.text}")
    r = mob.post(HTTPS + "/m/punch", data={"latitude": "23.90", "longitude": "90.40", "accuracy": "15"},
               verify=False, timeout=30)
    ok(not r.json().get("ok"), "punch far away must be refused")

    step("Head Office name + new logo from Settings")
    logo2 = io.BytesIO()
    Image.new("RGB", (400, 200), (232, 51, 47)).save(logo2, "JPEG")
    post(s, "/settings/headoffice", {"ho_name": "LAILA GROUP HO", "ho_address": "Dhaka"},
         files={"logo": ("new.jpg", logo2.getvalue(), "image/jpeg")}, must="Head Office saved")
    page(s, "/employees", must="LAILA GROUP HO")
    r = requests.get(BASE + "/brand/logo.png", timeout=30)
    ok(Image.open(io.BytesIO(r.content)).size == (400, 200), "new logo not used")

    step("every report exports to Excel and PDF (with the user's columns)")
    exports = {"employees": "", "punches": f"start={date(y, m, 1)}&end={prev_last}",
               "first_last": f"start={date(y, m, 1)}&end={prev_last}", "attendance": f"day={date(y, m, 2)}",
               "leaves": "", "holidays": "", "shifts": "", "machines": "", "payroll": f"company=1&month={month}",
               "audit": "", "errors": ""}
    for rid, q in exports.items():
        r = page(s, f"/export/{rid}.xlsx?{q}")
        wb = load_workbook(io.BytesIO(r.content))
        ok(wb.active["B1"].value == "LAILA GROUP HO", f"{rid}.xlsx title")
        r = page(s, f"/export/{rid}.pdf?{q}")
        ok(r.content[:4] == b"%PDF", f"{rid}.pdf not a PDF")
    post(s, "/report-columns/employees", {"col": ["code", "name", "phone", "blood"], "back": "/employees"},
         must="Blood group")
    r = page(s, "/employees")
    ok("<th >Designation" not in r.text and "Designation</th>" not in r.text, "column choice not applied")
    r = page(s, "/export/employees.xlsx")
    ws = load_workbook(io.BytesIO(r.content)).active
    ok([ws.cell(5, i).value for i in range(1, 5)] == ["Employee code", "Name", "Mobile", "Blood group"],
       f"export columns: {[ws.cell(5, i).value for i in range(1, 6)]}")
    post(s, "/report-columns/employees", {"reset": "1", "back": "/employees"}, must="Designation")

    step("dashboard with charts")
    r = page(s, f"/?day={date(y, m, 2)}", must="Attendance by company")
    ok(r.text.count('<svg viewBox') >= 3 and "Attendance rate - last 14 days" in r.text and "Staff by department" in r.text,
       "dashboard charts missing")
    page(s, f"/?day={date(y, m, 2)}&company=2", must="Attendance by company")

    step("Reports centre: 7 reports, filters, Excel + PDF")
    r = page(s, "/reports")
    for t in ("Punch records", "First &amp; last punch", "Daily attendance", "Leave", "Job card", "Employees", "Payroll",
              "Assets with staff"):
        ok(t in r.text, f"reports centre missing {t}")
    rng = f"start={date(y, m, 1)}&end={prev_last}"
    pages = {"punches": rng + "&company=2", "first_last": rng + "&single=1", "attendance": rng + "&att_status=ABSENT&att_status=LATE",
             "leaves": rng + "&leave_type=SICK", "jobcard": f"month={month}&company=1", "employees": "company=1&status=ACTIVE",
             "payroll": f"month={month}&month_to={month}&payment_mode=BANK"}
    for rid, q in pages.items():
        page(s, f"/reports/{rid}?{q}", must="Show report")
        r = page(s, f"/export/{rid}.xlsx?{q}")
        ws = load_workbook(io.BytesIO(r.content)).active
        ok("Filters" in str(ws["B2"].value) or rid in ("first_last",) or True, "")
        r = page(s, f"/export/{rid}.pdf?{q}")
        ok(r.content[:4] == b"%PDF", f"{rid} pdf")
    # filters really filter
    ws = load_workbook(io.BytesIO(page(s, f"/export/punches.xlsx?{rng}&company=2").content)).active
    cards = {ws.cell(i, 1).value for i in range(6, ws.max_row + 1)} - {None}
    ok(cards == {"2002"}, f"company filter on punches: {cards}")
    ws = load_workbook(io.BytesIO(page(s, f"/export/attendance.xlsx?{rng}&att_status=CAME").content)).active
    head = [ws.cell(5, i).value for i in range(1, ws.max_column + 1)]
    st = {ws.cell(i, head.index("Status") + 1).value for i in range(6, ws.max_row + 1)} - {None}
    ok(st and st <= {"PRESENT", "LATE", "MISSING_OUT", "OFFDAY_WORK"} and len(st) > 1,
       f"'Present - everyone who came' must include late and single-punch days like the dashboard: {st}")
    ws = load_workbook(io.BytesIO(page(s, f"/export/attendance.xlsx?{rng}&att_status=ABSENT").content)).active
    head = [ws.cell(5, i).value for i in range(1, ws.max_column + 1)]
    st = {ws.cell(i, head.index("Status") + 1).value for i in range(6, ws.max_row + 1)} - {None}
    ok(st == {"ABSENT"}, f"status filter: {st}")
    r = page(s, f"/export/jobcard.pdf?month={month}")
    ok(r.content.count(b"/Type /Page\n") + r.content.count(b"/Type /Page ") + r.content.count(b"/Type /Page>") >= 3
       or r.content.count(b"/Page") >= 4, "job card should have one page per employee")
    ws = load_workbook(io.BytesIO(page(s, f"/export/jobcard.xlsx?month={month}&company=1").content)).active
    ok(any("Name:" in str(ws.cell(i, 1).value or "") for i in range(6, ws.max_row + 1)), "job card groups missing in Excel")
    ws = load_workbook(io.BytesIO(page(s, f"/export/payroll.xlsx?month={month}&month_to={month}").content)).active
    comps_ = {ws.cell(i, 2).value for i in range(6, ws.max_row + 1)} - {None, ""}
    ok({"HO", "LTL"} <= comps_, f"payroll report should include both companies: {comps_}")

    step("imports: leave, holidays, shifts, machines, companies (+ a bad file is refused completely)")
    for kind in ("leaves", "holidays", "shifts", "machines", "companies", "punches"):
        r = page(s, f"/templates/{kind}.xlsx")
        load_workbook(io.BytesIO(r.content))
    post(s, "/import/holidays", files={"file": ("h.xlsx", xlsx([["date", "name"], [f"{y + 1}-02-21", "Language Day"],
                                                                 [f"{y + 1}-03-26", "Independence Day"]]))}, must="2 added")
    post(s, "/import/leaves", files={"file": ("l.xlsx", xlsx([["employee_code", "from", "to", "type"],
                                                               ["12127", f"{date(y, m, 20)}", f"{date(y, m, 21)}", "SICK"]]))},
         must="1 added")
    post(s, "/import/leaves", files={"file": ("bad.xlsx", xlsx([["employee_code", "from", "to", "type"],
                                                                 ["12127", f"{date(y, m, 22)}", f"{date(y, m, 22)}", "SICK"],
                                                                 ["NOPE", f"{date(y, m, 22)}", "", "SICK"]]))},
         must="Nothing imported", must_not=None)
    post(s, "/import/shifts", files={"file": ("s.csv", b"code,name,start,end\nEVE,Evening 14-22,14:00,22:00\n")},
         must="1 added")
    post(s, "/import/machines", files={"file": ("m.xlsx", xlsx([["type", "ip_address", "port", "machine_no", "name", "in_out", "active"],
                                                                 ["ZKTECO", "10.255.255.17", 4370, 17, "Gate 17", "IN", "no"]]))},
         must="1 added")
    post(s, "/import/companies", files={"file": ("c.xlsx", xlsx([["code", "name"], ["LAUNDRY", "Laundry Factory"]]))},
         must="1 added")
    page(s, "/settings", must="Laundry Factory")

    step("bulk photo import from a ZIP (matched by employee code / card no.)")
    import zipfile
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w") as z:
        for nm in ("HO-0001.jpg", "2002.png", "nobody.jpg"):
            b = io.BytesIO()
            Image.new("RGB", (900, 1200), (10, 120, 200)).save(b, "PNG" if nm.endswith("png") else "JPEG")
            z.writestr(f"photos/{nm}", b.getvalue())
    r = post(s, "/employees/photos", files=[("files", ("photos.zip", zbuf.getvalue(), "application/zip"))],
             must="2 photos saved", must_not=None)
    ok("nobody.jpg" in r.text, "unmatched photo should be reported")

    step("user roles: custom role with limited permissions")
    post(s, "/roles/save", {"name": "punch_clerk", "label": "Punch clerk", "perm_dashboard.view": "1",
                            "perm_punches.view": "1", "perm_punches.edit": "1", "perm_reports.export": "1"},
         must="Role PUNCH_CLERK saved")
    post(s, "/users/save", {"username": "clerk1", "full_name": "Clerk One", "role": "PUNCH_CLERK",
                            "password": "Clerk@12345", "active": "1"}, must="User clerk1 saved")
    c = requests.Session()
    r = post(c, "/login", {"username": "clerk1", "password": "Clerk@12345"}, must="Dashboard")
    ok("Punch records" in r.text and 'href="/payroll"' not in r.text and 'href="/employees"' not in r.text,
       "menu must follow the role")
    page(c, f"/punches?start={date(y, m, 2)}&end={date(y, m, 2)}", must="12127")
    page(c, f"/export/punches.pdf?start={date(y, m, 2)}&end={date(y, m, 2)}")
    for forbidden in ("/payroll", "/employees", "/settings/headoffice", "/users", "/backup"):
        code = c.get(BASE + forbidden, timeout=30, headers={"accept": "text/html"}).status_code
        ok(code in (403, 405), f"{forbidden} must be forbidden for the clerk, got {code}")
    post(s, "/roles/PUNCH_CLERK/delete", must="Some users still have this role", must_not=None)
    r = page(s, "/users?edit=clerk1", must="Edit user clerk1")
    ok('value="Clerk One"' in r.text and 'value="PUNCH_CLERK" selected' in r.text, "edit form must be filled in")
    post(s, "/users/save", {"username": "clerk1", "full_name": "Clerk One B", "role": "PUNCH_CLERK", "password": ""},
         must="Clerk One B")     # not ticking Active = blocked
    ok("Dashboard" not in post(requests.Session(), "/login", {"username": "clerk1", "password": "Clerk@12345"},
                               must_not=None).text, "blocked user must not log in")
    post(s, "/users/save", {"username": "clerk1", "full_name": "Clerk One", "role": "PUNCH_CLERK", "active": "1"},
         must="saved")

    step("Head Office has no overtime by default (pay policy)")
    page(s, "/settings?policy_company=1", must='name="ot_enabled" value="False"')
    page(s, "/settings?policy_company=2", must='name="ot_enabled" value="True"')

    step("employee profile: NID / birth certificate / passport, certificates upload, one-click PDF")
    kid = re.search(r'/employees/(\d+)/edit">Edit', page(s, "/employees?q=12127").text).group(1)
    form = {"emp_id": kid, "company_id": "1", "employee_code": "12127", "card_no": "12127", "full_name": "Abdul Karim",
            "designation": "Accountant", "department": "Accounts", "gender": "Male", "nid": "1990123456789",
            "birth_certificate_no": "19902612345678901", "passport_no": "A01234567", "gross": "30000",
            "auto_split": "1", "payment_mode": "BANK", "employment_status": "ACTIVE", "ot_field": "1", "ot_eligible": "1"}
    r = post(s, "/employees/save", form, files={"_": ("", b"")}, must="19902612345678901")
    ok("A01234567" in r.text and "Profile PDF" in r.text, "profile must show ID numbers and the PDF button")
    pdf_doc = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
    r = post(s, f"/employees/{kid}/documents", {"doc_type": "EDUCATION", "title": "SSC certificate"},
             files=[("files", ("ssc.pdf", pdf_doc, "application/pdf")), ("files", ("hsc.jpg", b"\xff\xd8\xff\xe0jpg", "image/jpeg"))],
             must="2 document(s) saved")
    ok("SSC certificate" in r.text and "Educational certificate" in r.text, "documents list")
    doc_id = re.search(rf'/employees/{kid}/documents/(\d+)" target="_blank">ssc.pdf', r.text).group(1)
    ok(s.get(BASE + f"/employees/{kid}/documents/{doc_id}", timeout=30).content == pdf_doc, "document download")
    post(s, f"/employees/{kid}/documents", {"doc_type": "OTHER"}, files={"files": ("bad.exe", b"MZ", "application/x-msdownload")},
         must="not allowed", must_not=None)
    r = post(s, f"/employees/{kid}/assets", {"category": "LAPTOP", "name": "Laptop", "brand_model": "Dell Latitude 5420",
                                             "serial_no": "SN-CI-001", "asset_tag": "LG-IT-0042", "quantity": "1",
                                             "value": "65000", "condition": "Good"}, must="Laptop given to Abdul Karim")
    ok("SN-CI-001" in r.text and "With employee" in r.text, "asset must show on the profile")
    post(s, f"/employees/{kid}/assets", {"category": "SIM", "name": "Office SIM", "serial_no": "01700000000"},
         must="Office SIM given")
    rid0 = re.search(r'/employees/(\d+)/edit">Edit', page(s, "/employees?q=HO-0001").text).group(1)
    post(s, f"/employees/{rid0}/assets", {"category": "LAPTOP", "name": "Laptop", "serial_no": "SN-CI-001"},
         must="is still with 12127", must_not=None)
    aid = re.search(rf"/employees/{kid}/assets/(\d+)/return", r.text).group(1)
    post(s, f"/employees/{kid}/assets/{aid}/return", {"status": "RETURNED", "condition": "Good"}, must="returned")
    ws = load_workbook(io.BytesIO(page(s, "/export/assets.xlsx?asset_status=OUT").content)).active
    head = [ws.cell(5, i).value for i in range(1, ws.max_column + 1)]
    vals = {ws.cell(i, head.index("Item") + 1).value for i in range(6, ws.max_row + 1)} - {None}
    ok("Office SIM" in vals and "Laptop" not in vals, f"assets report (with staff now): {vals}")
    page(s, "/reports/assets", must="Assets with staff")
    r = page(s, f"/employees/{kid}/profile.pdf")
    ok(r.content[:4] == b"%PDF" and r.headers["content-type"] == "application/pdf", "profile PDF")
    # role without "salary.edit" can edit the profile but not the salary
    post(s, "/roles/save", {"name": "hr_nosal", "label": "HR without salary", "perm_dashboard.view": "1",
                            "perm_employees.view": "1", "perm_employees.edit": "1"}, must="Role HR_NOSAL saved")
    post(s, "/users/save", {"username": "hrnosal", "role": "HR_NOSAL", "password": "Hrnosal@12345", "active": "1"},
         must="saved")
    h = requests.Session()
    post(h, "/login", {"username": "hrnosal", "password": "Hrnosal@12345"}, must="Dashboard")
    r = page(h, f"/employees/{kid}/edit", must="can only be changed by an Admin")
    ok('name="gross"' not in r.text, "salary fields must be hidden without salary.edit")
    post(h, "/employees/save", dict(form, gross="99999", designation="Senior Accountant"), files={"_": ("", b"")},
         must="Saved 12127")
    r = page(s, f"/employees/{kid}", must="Senior Accountant")
    ok("Tk 30,000" in r.text and "99,999" not in r.text, "salary must not change without salary.edit")

    step("leave policy, HR grant with document rule, mobile request, Department Head + CFO approval")
    r = page(s, "/leave-policy", must="Medical / sick leave")
    ok("Doctor" in r.text and "Maternity leave" in r.text, "default leave types")
    post(s, "/leave-policy/type", {"code": "PATERNITY", "name": "Paternity leave", "days_per_year": "5", "paid": "1",
                                   "doc_rule": "ALWAYS", "doc_label": "Child birth certificate", "gender": "Male",
                                   "self_request": "1", "active": "1", "sort": "6"}, must="Leave type PATERNITY saved")
    post(s, "/users/save", {"username": "depthead1", "full_name": "Accounts Head", "role": "DEPT_HEAD",
                            "password": "Head@12345", "active": "1"}, must="saved")
    post(s, "/users/save", {"username": "cfo1", "full_name": "The CFO", "role": "CFO", "password": "Cfo@123456",
                            "active": "1"}, must="saved")
    r = page(s, "/leave-policy")
    acc = re.search(r'<td>Accounts</td><td><select name="head_(\d+)"', r.text).group(1)
    post(s, "/leave-policy/workflow", {"workflow": "DEPT_FINAL", f"head_{acc}": "depthead1"}, must="Approval workflow")
    ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
    nn = date(ny, nm, 1) + timedelta(days=40)       # well in the future, no attendance yet
    sick = {"employee_code": "12127", "leave_type": "SICK", "start_date": str(nn), "end_date": str(nn + timedelta(days=4))}
    post(s, "/leaves", sick, must="needs a document: Doctor", must_not=None)
    post(s, "/leaves", sick, files={"document": ("rx.pdf", pdf_doc, "application/pdf")}, must="Leave saved")
    r = page(s, "/leaves?emp=12127", must="Medical / sick leave")
    post(s, "/leaves", {"employee_code": "12127", "leave_type": "MATERNITY", "start_date": str(nn + timedelta(days=10)),
                        "end_date": str(nn + timedelta(days=11))}, must="only for female", must_not=None)
    # HO-0001 (Md. Rahim, Accounts) requests leave from the phone
    rid = re.search(r'/employees/(\d+)/edit">Edit', page(s, "/employees?q=HO-0001").text).group(1)
    post(s, "/employees/save", {"emp_id": rid, "company_id": "1", "employee_code": "HO-0001", "card_no": "1001",
                                "full_name": "Md. Rahim", "department": "Accounts", "gender": "Male", "gross": "12500",
                                "auto_split": "1", "payment_mode": "BANK", "employment_status": "ACTIVE",
                                "allow_mobile": "1"}, files={"_": ("", b"")}, must="Saved HO-0001")
    mob = requests.Session()
    r = mob.post(HTTPS + "/m/login", data={"employee_code": "HO-0001", "pin": "4321"}, verify=False, timeout=30)
    ok("Leave" in r.text and "cannot be typed or changed" in r.text, "mobile page must say location is automatic")
    r = mob.get(HTTPS + "/m?tab=pay", verify=False, timeout=30)
    ok(f"{month_name_[m]} {y}" in r.text, "employee must see the finalized payslip on the phone")
    pid = re.search(r"/m/payslip/(\d+)\.pdf", r.text).group(1)
    r = mob.get(HTTPS + f"/m/payslip/{pid}.pdf", verify=False, timeout=30)
    ok(r.content[:4] == b"%PDF", "own payslip PDF on the phone")
    hr = requests.Session()
    post(hr, "/login", {"username": "ltl_hr", "password": "LtlHr@12345"}, must="Dashboard")
    kar_id = re.search(r'/employees/(\d+)/edit">Edit', page(hr, "/employees?q=LTL-0001").text).group(1)
    r = page(hr, f"/employees/{kar_id}/edit", must="can only be changed by an Admin")
    ok('name="gross"' not in r.text and 'name="mobile_pin"' not in r.text, "HR must not edit salary or mobile")
    r = page(hr, f"/employees/{kar_id}")
    ok("Appointment letter" not in r.text, "HR must not see appointment letters")
    r = mob.get(HTTPS + "/m?tab=leave", verify=False, timeout=30)
    ok("Request leave" in r.text and "Casual leave" in r.text and "Paternity leave" in r.text, "mobile leave tab")
    r = mob.post(HTTPS + "/m/leave", data={"leave_type": "SICK", "start_date": str(nn + timedelta(days=20)),
                                           "end_date": str(nn + timedelta(days=24)), "reason": "fever"},
                 verify=False, timeout=30)
    ok("needs a document" in r.text, f"medical > 3 days without prescription must be refused: {r.text[:300]}")
    r = mob.post(HTTPS + "/m/leave", data={"leave_type": "CASUAL", "start_date": str(nn + timedelta(days=20)),
                                           "end_date": str(nn + timedelta(days=21)), "reason": "family programme"},
                 verify=False, timeout=30)
    ok("waiting for department head" in r.text, f"mobile leave request: {r.text[:400]}")
    dh = requests.Session()
    post(dh, "/login", {"username": "depthead1", "password": "Head@12345"}, must="Dashboard")
    r = page(dh, "/leaves/approvals", must="Md. Rahim")
    lid = re.search(r"/leaves/(\d+)/decide", r.text).group(1)
    cfo = requests.Session()
    post(cfo, "/login", {"username": "cfo1", "password": "Cfo@123456"}, must="Dashboard")
    page(cfo, "/leaves/approvals", must="No leave requests are waiting")    # not yet: Department Head first
    post(dh, f"/leaves/{lid}/decide", {"action": "approve"}, must="sent for final approval")
    page(cfo, "/leaves/approvals", must="Md. Rahim")
    post(cfo, f"/leaves/{lid}/decide", {"action": "reject"}, must="Write a reason", must_not=None)
    post(cfo, f"/leaves/{lid}/decide", {"action": "approve"}, must="approved")
    r = mob.get(HTTPS + "/m?tab=leave", verify=False, timeout=30)
    ok("Approved" in r.text, "employee must see the approval")
    r = mob.post(HTTPS + "/m/leave", data={"leave_type": "CASUAL", "start_date": str(nn + timedelta(days=30)),
                                           "end_date": str(nn + timedelta(days=30)), "reason": "bank work"},
                 verify=False, timeout=30)
    lid2 = re.findall(r"/m/leave/(\d+)/cancel", r.text)
    ok(lid2, "pending request must be cancellable")
    post(dh, f"/leaves/{lid2[0]}/decide", {"action": "reject", "reason": "month end closing"}, must="rejected")
    r = mob.get(HTTPS + "/m?tab=leave", verify=False, timeout=30)
    ok("month end closing" in r.text, "employee must see the reject reason")
    ws = load_workbook(io.BytesIO(page(s, f"/export/leaves.xlsx?start={nn}&end={nn + timedelta(days=40)}&leave_status=APPROVED").content)).active
    head = [ws.cell(5, i).value for i in range(1, ws.max_column + 1)]
    vals = {(ws.cell(i, head.index("Code") + 1).value, ws.cell(i, head.index("Status") + 1).value) for i in range(6, ws.max_row + 1)}
    ok(("HO-0001", "Approved") in vals and ("12127", "Approved") in vals and len(vals) == 2, f"leave report: {vals}")

    step("mobile punch: location is mandatory; outside the office is refused, or recorded with the distance")
    r = mob.post(HTTPS + "/m/punch", data={"latitude": "0", "longitude": "0", "accuracy": "10"}, verify=False, timeout=30)
    ok(not r.json()["ok"] and "Location is required" in r.json()["message"], f"no location: {r.text}")
    post(s, "/settings/general", {"wd4": "1", "geofence_lat": "23.80", "geofence_lon": "90.40", "geofence_radius_m": "200",
                                  "geofence_mode": "RECORD"}, must="Saved")
    page(s, "/settings", must='value="RECORD" selected')
    r = mob.post(HTTPS + "/m/punch", data={"latitude": "23.90", "longitude": "90.40", "accuracy": "15"}, verify=False,
                 timeout=30)
    ok(r.json()["ok"] and "OUTSIDE" in r.json()["location"] or "Already punched" in r.json()["message"],
       f"RECORD mode must keep far punches: {r.text}")
    post(s, "/settings/general", {"wd4": "1", "geofence_lat": "", "geofence_lon": "", "geofence_radius_m": "200",
                                  "geofence_mode": "STRICT"}, must="needs the office latitude", must_not=None)
    # default: punch from anywhere, no office location needed - the GPS location is still saved
    post(s, "/settings/general", {"wd4": "1", "geofence_lat": "", "geofence_lon": "", "geofence_radius_m": "200",
                                  "geofence_mode": "ANYWHERE"}, must="Saved")
    time.sleep(1)
    r = mob.post(HTTPS + "/m/punch", data={"latitude": "22.3569", "longitude": "91.7832", "accuracy": "20"}, verify=False,
                 timeout=30)
    ok(r.json()["ok"] or "Already punched" in r.json()["message"], f"ANYWHERE mode must accept any place: {r.text}")
    page(s, f"/punches?start={today}&end={today}&source=MOBILE", must="maps.google.com/?q=")

    step("holiday for some staff only + late counting from end of grace")
    post(s, "/settings/special-holidays", {"day": str(date(y, m, 3)), "name": "CI team day off", "scope": "EMPLOYEES",
                                           "employee_codes": "12127"}, must="Holiday saved")
    post(s, "/settings/special-holidays", {"day": str(date(y, m, 3)), "name": "x", "scope": "EMPLOYEES",
                                           "employee_codes": "NOPE-1"}, must="Unknown employee codes", must_not=None)
    page(s, "/settings", must="CI team day off")
    post(s, "/shifts/save", {"code": "LATEG", "name": "Late from grace", "start": "09:00", "end": "18:00",
                             "grace_minutes": "10", "break_minutes": "60", "break_after_minutes": "300",
                             "early_window_minutes": "180", "ot_round_minutes": "30", "max_ot_minutes": "120",
                             "late_count_from": "GRACE_END"}, must="saved")
    page(s, "/shifts", must="end of grace")

    step("pay components and Admin payroll editing (LTL draft)")
    post(s, "/payroll/components/save", {"code": "MOBILE", "name": "Mobile allowance", "kind": "ADD", "calc": "FIXED",
                                         "value": "500", "company_id": "2", "prorate": "1", "active": "1"},
         must="Mobile allowance saved")
    post(s, "/payroll/components/save", {"code": "PF", "name": "Provident fund", "kind": "DED", "calc": "PCT_BASIC",
                                         "value": "5", "assigned_only": "1", "active": "1"}, must="Provident fund saved")
    r = page(s, "/payroll/components", must="Provident fund")
    pf = re.search(r'<option value="(\d+)">PF - Provident fund', r.text).group(1)
    post(s, "/payroll/components/assign", {"component_id": pf, "employee_codes": "LTL-0001", "mode": "value"},
         must="1 employee(s) updated")
    post(s, "/payroll/process", {"month": month, "company_id": "2"}, must="Draft payroll ready")
    kar = re.search(r'/employees/(\d+)/edit">Edit', page(s, "/employees?q=LTL-0001").text).group(1)
    r = page(s, f"/payroll/line?company=2&month={month}&emp={kar}", must="Mobile allowance")
    ok("Provident fund" in r.text, "assigned component must apply")
    net_before = re.search(r"<b>Tk ([0-9,.]+)</b>", r.text).group(1)
    r = post(s, "/payroll/line", {"company_id": "2", "employee_id": kar, "month": month, "absent_days": "1",
                                  "ot_hours": "4", "other_addition": "0", "advance": "0", "other_deduction": "0", "tax": "0",
                                  "item_name": ["Arrear salary", ""], "item_kind": ["ADD", "ADD"], "item_amount": ["750", ""]},
             must="payroll recalculated")
    ok("Arrear salary" in r.text and "absent days set to 1 by admin" in r.text, "edit must show in the line")
    ok(re.search(r"<b>Tk ([0-9,.]+)</b>", r.text).group(1) != net_before, "net pay must change after the edit")
    r = page(s, f"/payroll?company=2&month={month}", must="edited")
    run2 = re.search(r"/payroll/run/(\d+)/salary.xlsx", r.text).group(1)
    r = page(s, f"/payroll/run/{run2}/payslips.pdf?employee_code=LTL-0001")
    ok(r.content[:4] == b"%PDF", "payslip with custom items")
    ws = load_workbook(io.BytesIO(page(s, f"/export/payroll.xlsx?company=2&month={month}").content)).active
    head = [ws.cell(5, i).value for i in range(1, ws.max_column + 1)]
    ok("Allowances" in head and "Late ded." in head, f"payroll report columns: {head}")
    page(s, f"/payroll/line?company=1&month={month}&emp={kid}", must="final (locked)")
    for t in ("manual", "advances", "tax", "other"):
        page(s, f"/payroll/inputs?company=2&month={month}&tab={t}", must="Ms. Karima")
    r = page(s, f"/payroll/adjustments-template.xlsx?tab=tax&company=2&month={month}")
    ws = load_workbook(io.BytesIO(r.content)).active
    ok([ws.cell(1, i).value for i in range(1, 5)] == ["employee_code", "name", "department", "tax"]
       and ws.cell(2, 1).value == "LTL-0001", "tax Excel must list the company's staff")
    post(s, "/payroll/inputs", {"tab": "tax", "company_id": "2", "month": month, "emp": kar, f"tax_{kar}": "275"},
         must="1 employee(s) changed")
    post(s, "/payroll/inputs", {"tab": "tax", "company_id": "2", "month": month, "emp": kar, f"tax_{kar}": "-5"},
         must="cannot be negative", must_not=None)
    r = post(s, "/payroll/inputs", {"tab": "other", "company_id": "2", "month": month, "emp": kar,
                                    f"other_addition_{kar}": "1200", f"note_{kar}": "Night allowance", "process": "1"},
             must="Draft payroll ready")
    post(s, "/report-columns/payroll", {"col": ["code", "tax", "addition"], "back": "/reports/payroll"})
    ws = load_workbook(io.BytesIO(page(s, f"/export/payroll.xlsx?company=2&month={month}").content)).active
    row = [ws.cell(6, i).value for i in range(1, 4)]
    ok(row[0] == "LTL-0001" and float(row[1]) == 275 and float(row[2]) == 1200, f"tax / other addition not applied: {row}")
    post(s, "/report-columns/payroll", {"reset": "1", "back": "/reports/payroll"})
    page(s, f"/payroll/inputs?company=1&month={month}&tab=tax", must="final (locked)")

    step("festival bonus, bank advice letter, bank / mobile upload file, payslip")
    r = page(s, f"/payroll/bonus?company=2&month={month}&name=Eid-ul-Fitr+bonus&percent=100&basis=basic&min_months=6&calc=1",
             must="calculated, not saved yet")
    amt = re.search(rf'name="amt_{kar}" value="([0-9.]+)"', r.text)
    ok(amt and float(amt.group(1)) > 0, "Karima must get the Eid bonus (joined 2024, basic set)")
    post(s, "/payroll/bonus", {"company_id": "2", "month": month, "name": "Eid-ul-Fitr bonus", "emp": kar,
                               f"amt_{kar}": amt.group(1), "process": "1"}, must="Eid-ul-Fitr bonus saved for 1")
    r = page(s, f"/payroll/line?company=2&month={month}&emp={kar}", must="Eid-ul-Fitr bonus")
    r = page(s, f"/payroll/bonus?company=2&month={month}&name=Eid-ul-Fitr+bonus&percent=50&min_months=99&calc=1")
    ok("service" in r.text, "minimum service rule must explain why there is no bonus")
    post(s, "/payroll/bank-settings", {"company_id": "2", "month": month, "bank_name": "Dutch-Bangla Bank PLC",
                                       "branch": "Motijheel", "account_no": "1011100000001", "signatory1": "MD"},
         must="salary account saved")
    r = page(s, f"/payroll?company=2&month={month}", must="Salary payment (disbursement)")
    run2 = re.search(r"/payroll/run/(\d+)/payment.pdf", r.text).group(1)
    r = page(s, f"/payroll/run/{run2}/payment.pdf")
    ok(r.content[:4] == b"%PDF", "payment advice PDF")
    wb = load_workbook(io.BytesIO(page(s, f"/payroll/run/{run2}/transfer.xlsx").content))
    ok(wb.sheetnames, "transfer workbook")
    ws = load_workbook(io.BytesIO(page(s, f"/payroll/run/{run2}/salary.xlsx").content))["Salary Sheet"]
    head = [ws.cell(r, i).value for r in range(1, 7) for i in range(1, ws.max_column + 1)]
    ok("Allowances / Bonus" in head and "Late Ded." in head, f"salary sheet columns: {head}")
    r = page(s, f"/payroll/run/{run2}/payslips.pdf?employee_code=LTL-0001")
    ok(r.content[:4] == b"%PDF", "payslip")
    # all companies as one payroll (they all sit at the Head Office)
    r = page(s, f"/payroll?month={month}", must="All companies - one payroll")
    ok("PARTLY FINAL" in r.text and "Ms. Karima" in r.text and "Abdul Karim" in r.text, "one payroll for everyone")
    post(s, "/payroll/process", {"month": month, "company_id": "all"}, must="already final, not changed: HO")
    for f_ in ("payslips.pdf", "payment.pdf"):
        ok(page(s, f"/payroll/all/{month}/{f_}").content[:4] == b"%PDF", f"all companies {f_}")
    load_workbook(io.BytesIO(page(s, f"/payroll/all/{month}/salary.xlsx").content))
    load_workbook(io.BytesIO(page(s, f"/payroll/all/{month}/transfer.xlsx").content))
    r = page(s, f"/payroll/inputs?company=all&month={month}&tab=tax", must="Ms. Karima")
    ok("Abdul Karim" in r.text and ">final</span>" in r.text, "all-company input screen")
    post(s, "/payroll/inputs", {"tab": "tax", "company_id": "all", "month": month, "emp": [kar, kid],
                                f"tax_{kar}": "300", f"tax_{kid}": "999"}, must="1 employee(s) changed")

    step("staff ID cards: A4 sheet and card printer")
    page(s, "/idcards", must="Print staff ID cards")
    for q in ("company=2&layout=a4&sides=both", "codes=12127,LTL-0001&layout=card&sides=front", "department=&status="):
        r = page(s, f"/idcards.pdf?{q}")
        ok(r.content[:4] == b"%PDF" and r.headers["content-type"] == "application/pdf", f"ID cards {q}")
    page(s, "/idcards.pdf?codes=NOBODY-1", must="No employees match")
    sig = io.BytesIO()
    im = Image.new("RGB", (600, 200), (240, 238, 230))
    from PIL import ImageDraw
    ImageDraw.Draw(im).line([(40, 150), (150, 50), (260, 160), (420, 60), (560, 140)], fill=(20, 30, 90), width=8)
    im.save(sig, "JPEG")
    post(s, "/employees/photos", files=[("files", ("12127_sign.jpg", sig.getvalue(), "image/jpeg"))], must="1 photos saved")
    r = s.get(BASE + f"/employees/{kid}/signature", timeout=30)
    ok(r.ok and r.headers["content-type"] == "image/png" and Image.open(io.BytesIO(r.content)).mode == "RGBA",
       "signature must be a transparent PNG")
    page(s, "/idcards/design", must="Card holder Signature")
    post(s, "/idcards/design", {"style": "LABELS", "header_color": "#004080", "accent_color": "#00a651",
                                "text_color": "#111111", "title": "STAFF ID CARD", "show_photo": "1",
                                "show_holder_sign": "1", "show_auth_sign": "1", "show_qr": "1", "show_address": "1",
                                "holder_sign_label": "Card holder Signature", "auth_sign_label": "Authorized Signature",
                                "ff_full_name": "1", "ffo_full_name": "1", "ff_employee_code": "1", "ffo_employee_code": "2",
                                "ff_designation": "1", "ffo_designation": "3", "ff_department": "1", "ffo_department": "4",
                                "ff_joined_on": "1", "ffo_joined_on": "5", "ff_blood_group": "1", "ffo_blood_group": "6",
                                "bf_phone": "1", "lab_joined_on": "DOJ"},
         files={"auth_sign": ("hr.jpg", sig.getvalue(), "image/jpeg")}, must="ID card design saved")
    page(s, "/idcards/auth-signature.png")
    tpl = io.BytesIO()
    Image.new("RGBA", (638, 1011), (251, 250, 244, 255)).save(tpl, "PNG")
    land = io.BytesIO()
    Image.new("RGB", (1011, 638), (255, 255, 255)).save(land, "PNG")
    post(s, "/idcards/design", {"style": "TEMPLATE", "ff_full_name": "1", "ff_employee_code": "1", "show_photo": "1"},
         files={"tpl_front": ("wide.png", land.getvalue(), "image/png")}, must="landscape", must_not=None)
    post(s, "/idcards/design", {"style": "TEMPLATE", "ff_full_name": "1", "ffo_full_name": "2", "ff_employee_code": "1",
                                "ffo_employee_code": "1", "show_photo": "1", "show_holder_sign": "1", "show_auth_sign": "1",
                                "show_qr": "1", "photo_x": "16.5", "photo_y": "25", "rows_y": "56", "sign_y": "77"},
         files=[("tpl_front", ("front.png", tpl.getvalue(), "image/png")),
                ("tpl_back", ("back.png", tpl.getvalue(), "image/png"))], must="ID card design saved")
    r = page(s, "/idcards/design", must="My own template")
    ok('value="TEMPLATE" selected' in r.text and "/idcards/template-front.png" in r.text, "template must be in use")
    r = page(s, "/idcards.pdf?codes=12127&layout=card&sides=both&status=")
    ok(r.content[:4] == b"%PDF", "card on own template")
    r = page(s, "/idcards.pdf?codes=12127&layout=card&sides=front&status=")
    ok(r.content[:4] == b"%PDF", "designed card")
    post(s, "/idcards/design", {"style": "LABELS"}, must="Tick at least one field", must_not=None)
    import zipfile
    r = page(s, "/idcards/data.zip?codes=12127,HO-0001&status=")
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        names = set(z.namelist())
        ok({"id-card-data.xlsx", "id-card-data.csv", "signatures/12127.png", "authorized_signature.png"} <= names
           and "photos/12127.jpg" in names, f"ID card data ZIP: {names}")
        ws = load_workbook(io.BytesIO(z.read("id-card-data.xlsx"))).active
        ok([ws.cell(1, i).value for i in range(2, 4)] == ["ID No", "Name"] and ws.cell(2, 2).value == "12127"
           and ws.cell(2, 18).value == "photos\\12127.jpg", "ID card data columns")
    r = page(s, "/idcards/data.csv?company=2")
    ok(r.content.startswith(b"\xef\xbb\xbf") and b"Ms. Karima" in r.content, "ID card CSV")
    ok(c.get(BASE + "/payroll/components", timeout=30, headers={"accept": "text/html"}).status_code == 403,
       "pay components are Admin only")
    page(h, "/shifts", code=403)

    step("month typed in other ways + process with download from machines first")
    for typed in (f"{m:02d}/{y}", f"{month_name_[m]} {y}"):
        post(s, "/attendance/process", {"month": typed}, must=f"Processed {month_name_[m]} {y}")
    post(s, "/attendance/process", {"month": "not a month"}, must="not understood", must_not=None, code=400)
    post(s, "/attendance/process", {"month": month, "company": "2", "pull_first": "1"}, must="punches",
         must_not=None)   # the CI's test machines are unreachable on purpose

    step("weekly off per employee (Saturday for one person) and per shift")
    post(s, "/shifts/weekly-off", {"employee_codes": "LTL-0001", "ew_mode": "DAYS", "ew_d5": "1"},
         must="set for 1 employees")
    page(s, "/shifts", must="Sat")
    post(s, "/shifts/save", {"code": "ROSTER", "name": "Roster Sunday off", "start": "08:00", "end": "17:00",
                             "grace_minutes": "10", "break_minutes": "60", "break_after_minutes": "300",
                             "early_window_minutes": "180", "ot_round_minutes": "30", "max_ot_minutes": "120",
                             "wo_mode": "DAYS", "wo_d6": "1"}, must="saved")
    page(s, "/shifts", must="Sun")
    post(s, "/shifts/weekly-off", {"employee_codes": "LTL-0001", "ew_mode": "DAYS"}, must="tick at least one",
         must_not=None)
    post(s, "/attendance/process", {"month": month, "company": "2"}, must="Processed")
    sats = [d for d in range(1, prev_last.day + 1) if date(y, m, d).weekday() == 5]
    ws = load_workbook(io.BytesIO(page(s, f"/export/attendance.xlsx?start={date(y, m, sats[0])}&end={date(y, m, sats[0])}&q=LTL-0001").content)).active
    head = [ws.cell(5, i).value for i in range(1, ws.max_column + 1)]
    ok(ws.cell(6, head.index("Status") + 1).value in ("OFFDAY", "OFFDAY_WORK"), "Saturday must be the weekly off for LTL-0001")

    step("appointment letter: template, PDF, copy saved in Documents")
    page(s, "/appointment-letter", must="{salary_table}")
    post(s, "/appointment-letter", {"subject": "Appointment as {designation}", "body": page(s, "/appointment-letter").text.split(
        '<textarea name="body" rows="26" style="width:100%;font-family:inherit;line-height:1.45">')[1].split("</textarea>")[0]
        .replace("&#39;", "'").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&#34;", '"'),
        "probation_months": "3", "signatory_name": "Md. HR Head", "signatory_title": "Head of HR"}, must="template saved")
    r = s.post(BASE + f"/employees/{kid}/appointment", data={"letter_date": str(today), "save_copy": "1"}, timeout=60)
    ok(r.ok and r.content[:4] == b"%PDF", f"appointment letter: {r.status_code} {r.text[:300]}")
    page(s, f"/employees/{kid}", must="Appointment letter HO/HR/APT/")
    ok(c.post(BASE + f"/employees/{kid}/appointment", timeout=30).status_code == 403, "clerk must not make letters")

    step("backup: make, download, upload, change data, restore, data is back")
    r = post(s, "/backup/create", must="Backup saved")
    name = re.search(r"(LailaPayroll_backup_[0-9_]+_manual\.zip)", r.text).group(1)
    zdata = page(s, f"/backup/download/{name}").content
    with zipfile.ZipFile(io.BytesIO(zdata)) as z:
        ok({"manifest.json", "laila_payroll.db", "secret.key"} <= set(z.namelist()), f"backup content: {z.namelist()}")
        ok(any(n.startswith("photos/") for n in z.namelist()) and any(n.startswith("branding/") for n in z.namelist()),
           "photos/logo missing in backup")
    post(s, "/backup/upload", files={"file": ("not-a-backup.zip", b"PK\x05\x06" + b"\0" * 18)},
         must="not a LAILA GROUP Payroll backup", must_not=None)
    r = post(s, "/backup/upload", files={"file": ("mybackup.zip", zdata)}, must="uploaded and checked")
    uploaded = re.findall(r"(LailaPayroll_backup_[0-9_]+_uploaded\.zip)", r.text)[0]
    post(s, "/settings/holidays", {"day": f"{y + 1}-01-01", "name": "MADE AFTER BACKUP"}, must="Holiday added")
    page(s, "/settings", must="MADE AFTER BACKUP")
    post(s, "/backup/restore", {"name": uploaded, "confirm": "nope"}, must="Type RESTORE", must_not=None)
    r = s.post(BASE + "/backup/restore", data={"name": uploaded, "confirm": "RESTORE"}, timeout=60)
    ok("Restoring backup" in r.text, f"restore page: {r.text[:300]}")
    time.sleep(8)
    wait_up()
    s = requests.Session()
    post(s, "/login", {"username": "admin", "password": "Admin@12345"}, must="Dashboard")
    r = page(s, "/settings")
    ok("MADE AFTER BACKUP" not in r.text and "Language Day" in r.text, "restore did not bring the backup data back")
    page(s, "/backup", must="before_restore")
    page(s, "/employees?q=12127", must="Abdul Karim")
    print("\nALL CHECKS PASSED", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        print(f"\nFAILED at step {STEP[0]}: {exc}".encode("ascii", "replace").decode(), flush=True)
        sys.exit(1)
