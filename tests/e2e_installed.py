"""End-to-end check of an INSTALLED LailaPayroll (run by the Windows build pipeline after a silent install).

Walks through what a person does: first-time setup, login, punch machines, employees, attendance,
payroll, downloads, mobile page, company-limited user. Exits non-zero on the first failure.
Usage: python tests/e2e_installed.py [http://localhost:8585]
"""
import io
import re
import sys
import time
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


def post(s, path, data=None, files=None, must=None, must_not="alert err"):
    r = s.post(BASE + path, data=data, files=files, timeout=180)
    ok(r.status_code == 200, f"POST {path} -> {r.status_code}: {r.text[:800]}")
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
              "/employees/new", "/backup"):
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
    m = requests.Session()
    r = m.get(HTTPS + "/m", verify=False, timeout=30)
    ok(r.ok and "Employee ID" in r.text, f"mobile page: {r.status_code}")
    r = m.post(HTTPS + "/m/login", data={"employee_code": "HO-0001", "pin": "4321"}, verify=False, timeout=30)
    ok("PUNCH" in r.text, "mobile login failed")
    r = m.post(HTTPS + "/m/punch", data={"latitude": "23.8001", "longitude": "90.4001", "accuracy": "15"},
               verify=False, timeout=30)
    ok(r.json().get("ok"), f"mobile punch inside office: {r.text}")
    r = m.post(HTTPS + "/m/punch", data={"latitude": "23.90", "longitude": "90.40", "accuracy": "15"},
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
        print(f"\nFAILED at step {STEP[0]}: {exc}", flush=True)
        sys.exit(1)
