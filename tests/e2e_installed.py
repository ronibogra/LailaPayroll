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
    post(s, "/setup", {"username": "admin", "password": "Admin@12345", "password2": "Admin@12345",
                       "companies": "HO, LAILA GROUP - Head Office\nLTL, Laila Textiles Ltd", "wd4": "1",
                       "geofence_lat": "23.80", "geofence_lon": "90.40", "geofence_radius_m": "200"},
         must="Punch machines")
    page(s, "/setup")  # now redirects to login/dashboard, must not crash

    step("log out / log in")
    s.get(BASE + "/logout")
    post(s, "/login", {"username": "admin", "password": "wrong-pass"}, must="Wrong username or password", must_not=None)
    post(s, "/login", {"username": "admin", "password": "Admin@12345"}, must="Dashboard")

    step("every page opens")
    for p in ("/", "/attendance", "/attendance/punches", "/leaves", "/shifts", "/devices", "/employees",
              "/payroll", "/settings", "/users", "/audit", "/employees/new"):
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
    page(s, f"/attendance/punches?day={prev_last}", must="No punches")

    step("approve machine, punches arrive")
    post(s, "/devices/save", {"device_id": dev_id, "name": "Gate-1 Face", "brand": "ZKTECO",
                              "serial_number": "CI-ZK-1", "port": "80", "approved": "1", "enabled": "1"},
         must="saved")
    r = requests.post(BASE + "/iclock/cdata", params={"SN": "CI-ZK-1", "table": "ATTLOG"}, data=body, timeout=60)
    ok(r.text.strip() == f"OK: {len(lines)}", f"ATTLOG after approve: {r.text}")
    requests.get(BASE + "/iclock/getrequest", params={"SN": "CI-ZK-1"}, timeout=30)
    page(s, f"/attendance/punches?day={prev_last}", must="1001")
    page(s, "/devices", must="new ")

    step("Hikvision machine add + test (unreachable IP must give a clear error)")
    post(s, "/devices/save", {"name": "Lobby Hik", "brand": "HIKVISION", "ip_address": "127.0.0.1", "port": "9",
                              "username": "admin", "password": "x", "approved": "1", "enabled": "1"}, must="saved")
    hik = re.findall(r"/devices/(\d+)/test", page(s, "/devices").text)
    ok(hik, "Hikvision test button missing")
    r = post(s, f"/devices/{hik[0]}/test", must_not=None)
    ok("cannot reach" in r.text, f"expected 'cannot reach' error, got: {r.text[:600]}")

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
    r = post(s, "/payroll/process", {"month": month, "company_id": "1"}, must="Draft payroll ready for 1 employees")
    run_id = re.search(r"/payroll/run/(\d+)/salary.xlsx", r.text).group(1)
    ok("Md. Rahim" in r.text and "Ms. Karima" not in r.text, "HO payroll must contain only HO staff")

    step("payroll LTL")
    r = post(s, "/payroll/process", {"month": month, "company_id": "2"}, must="Draft payroll ready for 1 employees")
    ok("Ms. Karima" in r.text, "LTL payroll must contain Karima")

    step("salary sheet + payslips download")
    r = page(s, f"/payroll/run/{run_id}/salary.xlsx")
    wb = load_workbook(io.BytesIO(r.content))
    ok("Salary Sheet" in wb.sheetnames and "Bank Advice" in wb.sheetnames, f"sheets: {wb.sheetnames}")
    r = page(s, f"/payroll/run/{run_id}/payslips.pdf")
    ok(r.content[:4] == b"%PDF", "payslip is not a PDF")

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
    emp = re.search(r"/employees/(\d+)\">Edit</a>", page(s, "/employees?q=HO-0001").text).group(1)
    form = {"emp_id": emp, "company_id": "1", "employee_code": "HO-0001", "card_no": "1001", "full_name": "Md. Rahim",
            "gross": "12500", "auto_split": "1", "payment_mode": "BANK", "employment_status": "ACTIVE",
            "allow_mobile": "1", "mobile_pin": "4321"}
    post(s, "/employees/save", form, must="Saved HO-0001")
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

    step("backup now")
    post(s, "/settings/backup", must="Backup saved")
    print("\nALL CHECKS PASSED", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        print(f"\nFAILED at step {STEP[0]}: {exc}", flush=True)
        sys.exit(1)
