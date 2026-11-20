from datetime import date, datetime, time
from decimal import Decimal as D

import pytest

from attendance.rules import (ShiftRule, evaluate_day, evaluate_month, WEEKEND,
                              PRESENT, LATE, ABSENT, MISSING_OUT, OFFDAY, OFFDAY_WORK,
                              LEAVE_PAID, LEAVE_UNPAID, NOT_EMPLOYED)
from payroll.engine import PayPolicy, split_gross, compute_pay, Adjustments

DAY = ShiftRule()                                     # 08:00-17:00
NIGHT = ShiftRule(code="NIGHT", start=time(20, 0), end=time(5, 0))
dt = lambda *a: datetime(*a)
FRIDAY = {4}


# ---------- attendance ----------
def test_normal_day():
    r = evaluate_day(date(2026, 11, 2), [dt(2026, 11, 2, 7, 55), dt(2026, 11, 2, 17, 2)], DAY)
    assert (r.status, r.late_minutes, r.ot_minutes, r.worked_minutes) == (PRESENT, 0, 0, 487)


def test_grace_and_late():
    ok = evaluate_day(date(2026, 11, 2), [dt(2026, 11, 2, 8, 10), dt(2026, 11, 2, 17, 0)], DAY)
    late = evaluate_day(date(2026, 11, 2), [dt(2026, 11, 2, 8, 11), dt(2026, 11, 2, 17, 0)], DAY)
    assert ok.status == PRESENT and late.status == LATE and late.late_minutes == 11


def test_ot_rounded_down_and_capped():
    r = evaluate_day(date(2026, 11, 2), [dt(2026, 11, 2, 8, 0), dt(2026, 11, 2, 19, 29)], DAY)
    assert (r.ot_minutes, r.extra_ot_minutes) == (120, 0)          # 2h29 -> 2h00
    r = evaluate_day(date(2026, 11, 2), [dt(2026, 11, 2, 8, 0), dt(2026, 11, 2, 22, 10)], DAY)
    assert (r.ot_minutes, r.extra_ot_minutes) == (120, 180)        # 5h -> 2h + 3h extra


def test_duplicate_punch_is_one_punch():
    r = evaluate_day(date(2026, 11, 2), [dt(2026, 11, 2, 8, 0), dt(2026, 11, 2, 8, 1)], DAY)
    assert r.status == MISSING_OUT and r.punch_count == 1


def test_day_shift_late_ot_after_midnight_stays_same_day():
    g = DAY.work_day_of(dt(2026, 11, 3, 1, 30))
    assert g == date(2026, 11, 2)


def test_night_shift_one_work_day():
    punches = [dt(2026, 11, 2, 19, 50), dt(2026, 11, 3, 5, 5)]
    m = evaluate_month(2026, 11, punches, NIGHT, FRIDAY, set())
    d2 = m.days[1]
    assert d2.work_date == date(2026, 11, 2) and d2.status == PRESENT
    assert m.days[2].status == ABSENT        # Nov 3 not double counted


def test_absent_leave_offday():
    d = date(2026, 11, 2)
    assert evaluate_day(d, [], DAY).status == ABSENT
    assert evaluate_day(d, [], DAY, leave="PAID").status == LEAVE_PAID
    assert evaluate_day(d, [], DAY, leave="UNPAID").status == LEAVE_UNPAID
    assert evaluate_day(d, [], DAY, day_type=WEEKEND).status == OFFDAY


def test_friday_work_is_ot():
    r = evaluate_day(date(2026, 11, 6), [dt(2026, 11, 6, 8, 0), dt(2026, 11, 6, 13, 0)], DAY,
                     day_type=WEEKEND)
    assert r.status == OFFDAY_WORK and r.ot_minutes == 120 and r.extra_ot_minutes == 180


def full_month_punches(year, month, out=time(17, 0), skip=()):
    ps, d = [], date(year, month, 1)
    while d.month == month:
        if d.weekday() not in FRIDAY and d.day not in skip:
            ps += [datetime.combine(d, time(7, 55)), datetime.combine(d, out)]
        d = date.fromordinal(d.toordinal() + 1)
    return ps


def test_month_summary():
    m = evaluate_month(2026, 11, full_month_punches(2026, 11, skip={10, 11}), DAY, FRIDAY,
                       {date(2026, 11, 16)}, {date(2026, 11, 11): "PAID"})
    # Nov 2026: 30 days, 4 Fridays; Nov 16 holiday (worked -> OFFDAY_WORK)
    assert m.days_in_month == 30 and m.offdays == 4
    assert m.absent_days == 1 and m.paid_leave_days == 1
    assert m.payable_days == 29


def test_joiner_mid_month():
    m = evaluate_month(2026, 11, [], DAY, FRIDAY, set(), joined_on=date(2026, 11, 21))
    assert m.employed_days == 10
    assert sum(d.status == NOT_EMPLOYED for d in m.days) == 20


# ---------- payroll ----------
P = PayPolicy()


@pytest.mark.parametrize("gross,basic,house", [(12500, 6700, 3350), (15035, 8390, 4195),
                                               (13550, 7400, 3700)])
def test_split_matches_wage_board(gross, basic, house):
    s = split_gross(gross, P)
    assert (s.basic, s.house_rent) == (D(basic), D(house))


def test_full_month_with_ot():
    m = evaluate_month(2026, 11, full_month_punches(2026, 11, out=time(19, 0)), DAY, FRIDAY, set())
    r = compute_pay(split_gross(12500, P), m, P)
    # 26 workdays x 2h OT = 52h; rate = 6700/208*2 = 64.42
    assert r.ot_hours == D("52.00") and r.ot_rate == D("64.42")
    assert r.ot_amount == D("3349.84")
    assert r.net_pay == D("15850")


def test_absent_deduction_and_bonus():
    pol = PayPolicy(attendance_bonus=D("500"))
    full = evaluate_month(2026, 11, full_month_punches(2026, 11), DAY, FRIDAY, set())
    assert compute_pay(split_gross(12500, pol), full, pol).net_pay == D("13000")
    two_abs = evaluate_month(2026, 11, full_month_punches(2026, 11, skip={2, 3}), DAY, FRIDAY, set())
    r = compute_pay(split_gross(12500, pol), two_abs, pol)
    assert r.absent_deduction == D("446.67")       # 6700/30*2
    assert r.attendance_bonus == 0 and r.net_pay == D("12053")


def test_prorated_joiner_and_adjustments():
    m = evaluate_month(2026, 11, full_month_punches(2026, 11), DAY, FRIDAY, set(),
                       joined_on=date(2026, 11, 16))
    pol = PayPolicy(stamp_charge=D("10"))
    r = compute_pay(split_gross(12500, pol), m, pol, Adjustments(advance=D("1000")))
    assert r.employed_days == 15 and r.earned_gross == D("6250.00")
    assert r.net_pay == D("5240")


def test_net_never_negative():
    m = evaluate_month(2026, 11, [], DAY, FRIDAY, set())
    r = compute_pay(split_gross(12500, P), m, P, Adjustments(advance=D("20000")))
    assert r.net_pay == 0 and r.warnings


# ---------- machine protocols ----------
def test_zk_attlog_push_and_usb():
    from integration.zk_adms import parse_attlog, options_response
    body = "1001\t2026-11-20 08:01:02\t0\t15\t0\t0\t0\r\n1002\t2026-11-20 08:03:10\t0\t15\r\n\r\nbad line\n"
    p, bad = parse_attlog(body)
    assert p == [("1001", datetime(2026, 11, 20, 8, 1, 2)), ("1002", datetime(2026, 11, 20, 8, 3, 10))]
    assert bad == 1
    usb = "     1003  2026-11-20 17:05:44  1  0  1  0\n"
    assert parse_attlog(usb)[0] == [("1003", datetime(2026, 11, 20, 17, 5, 44))]
    assert "GET OPTION FROM: ABC123" in options_response("ABC123") and "TimeZone=6" in options_response("X")


def test_hikvision_events():
    from integration.hikvision import parse_events, build_query, to_local
    payload = {"AcsEvent": {"searchID": "x", "responseStatusStrg": "MORE", "numOfMatches": 3, "totalMatches": 40,
               "InfoList": [
                   {"major": 5, "minor": 75, "time": "2026-11-20T08:01:02+06:00", "employeeNoString": "2001"},
                   {"major": 5, "minor": 21, "time": "2026-11-20T08:02:00+06:00"},          # door open, no person
                   {"major": 5, "minor": 75, "time": "2026-11-20T02:30:00Z", "employeeNoString": "2002"}]}}
    p, more, n = parse_events(payload)
    assert more and n == 3
    assert p == [("2001", datetime(2026, 11, 20, 8, 1, 2)), ("2002", datetime(2026, 11, 20, 8, 30))]
    q = build_query(datetime(2026, 11, 1), datetime(2026, 11, 2), 30)["AcsEventCond"]
    assert q["startTime"] == "2026-11-01T00:00:00+06:00" and q["searchResultPosition"] == 30
    assert to_local("2026-11-20T08:00:00") == datetime(2026, 11, 20, 8)


# ---------- punch file import ----------
def _xlsx(rows):
    import io as _io
    from openpyxl import Workbook
    wb = Workbook()
    for r in rows:
        wb.active.append(r)
    b = _io.BytesIO()
    wb.save(b)
    return b.getvalue()


def test_punch_import_formats():
    from integration.punch_import import read_punch_file, ImportProblem
    # ZKTeco style export: AC-No, Date + Time separate, In/Out text
    rows, err = read_punch_file("x.csv", b"AC-No.,Name,Date,Time,State\n12127,Rahim,20/11/2026,08:01,C/In\n12160,Karim,2026-11-20,6:05 PM,C/Out\n")
    assert not err and rows == [("12127", datetime(2026, 11, 20, 8, 1), None, "IN"),
                                ("12160", datetime(2026, 11, 20, 18, 5), None, "OUT")]
    # Excel with title row, real datetime cell, numeric ID
    data = _xlsx([["Attendance report"], ["Employee ID", "Name", "Punch Time", "Device"],
                  [12127.0, "Rahim", datetime(2026, 11, 20, 8, 1, 30), "Gate-1"]])
    rows, err = read_punch_file("x.xlsx", data)
    assert rows == [("12127", datetime(2026, 11, 20, 8, 1, 30), "Gate-1", None)]
    # Excel with date + time cells
    rows, _ = read_punch_file("x.xlsx", _xlsx([["User ID", "Date", "Time"], ["7", date(2026, 11, 2), time(17, 5)]]))
    assert rows == [("7", datetime(2026, 11, 2, 17, 5), None, None)]
    # USB attlog
    rows, _ = read_punch_file("1_attlog.dat", b"  12127\t2026-11-20 08:01:02\t0\t1\t0\t0\n")
    assert rows == [("12127", datetime(2026, 11, 20, 8, 1, 2), "USB", None)]
    # bad file gives a clear message
    with pytest.raises(ImportProblem):
        read_punch_file("x.csv", b"Name,Salary\nA,1\n")
    rows, err = read_punch_file("x.csv", b"card_no,punch_time\n1,not a date\n2,2026-11-20 08:00\n")
    assert len(rows) == 1 and err and "row 2" in err[0]
