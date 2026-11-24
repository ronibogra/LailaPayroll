"""Confirmation letter, release letter and experience certificate - editable texts (Admin), filled from the
employee's data, printed on the same letterhead as the appointment letter. A line 'Remarks: {remarks}' is left
out when no remarks are given."""
from __future__ import annotations

import json
from datetime import date

KINDS = {
    "confirmation": ("Confirmation letter", "Confirmation of Service", """Dear {name},

We are pleased to inform you that, on successful completion of your probation period, your service as {designation} in the {department} department of {company} is confirmed with effect from {confirmation_date}.

Remarks: {remarks}

All other terms and conditions of your appointment letter remain unchanged. We appreciate your contribution and look forward to your continued dedication.

We wish you every success."""),
    "release": ("Release letter", "Release Letter", """Dear {name},

This is to inform you that your {separation_type} has been accepted by the management. You are released from the service of {company} with effect from the close of business on {last_working_day}.

You worked with us as {designation} in the {department} department from {joining_date} to {last_working_day} ({service_length}). Your final settlement has been made as per company policy and the company property in your custody has been returned.

Remarks: {remarks}

We thank you for your service and wish you success in your future endeavours."""),
    "experience": ("Experience certificate", "Experience Certificate", """TO WHOM IT MAY CONCERN

This is to certify that {name}, son / daughter of {father_name}, was employed with {company} as {designation} in the {department} department from {joining_date} to {last_working_day} ({service_length}).

Remarks: {remarks}

To the best of our knowledge, {name} bears a good moral character. We wish {name} every success in the future."""),
}
KINDS["promotion"] = ("Promotion letter", "Promotion", """Dear {name},

We are pleased to inform you that, in recognition of your performance and contribution, the management has promoted you from {old_designation} to {new_designation} with effect from {effective_date}.

Your monthly gross salary is revised from Tk {old_gross} to Tk {new_gross} ({new_gross_words}), an increase of Tk {increment_amount} ({increment_percent}%), as follows:
{salary_table}

Remarks: {remarks}

All other terms and conditions of your employment remain unchanged. Congratulations, and we wish you continued success.""")
KINDS["increment"] = ("Increment letter", "Salary Increment", """Dear {name},

We are pleased to inform you that the management has approved an increment in your salary with effect from {effective_date}. Your monthly gross salary is revised from Tk {old_gross} to Tk {new_gross} ({new_gross_words}), an increase of Tk {increment_amount} ({increment_percent}%), as follows:
{salary_table}

Remarks: {remarks}

All other terms and conditions of your employment remain unchanged. We appreciate your contribution and hope you will continue your good work.""")
CHANGE_FIELDS = {"old_designation": "Designation before", "new_designation": "New designation",
                 "old_grade": "Grade before", "new_grade": "New grade", "new_department": "New department",
                 "effective_date": "Effective date", "old_gross": "Gross before", "new_gross": "New gross",
                 "new_gross_words": "New gross in words", "increment_amount": "Increase (Tk)",
                 "increment_percent": "Increase (%)"}


def drop_salary_part(body: str) -> str:
    """Promotion without a pay rise: leave out the salary sentence and table."""
    keep = []
    for ln in body.split("\n"):
        if "{old_gross}" in ln or "{new_gross}" in ln or ln.strip() == "{salary_table}":
            continue
        keep.append(ln)
    return "\n".join(keep)


SEP_TYPES = {"RESIGNATION": "resignation", "TERMINATION": "termination", "DISMISSAL": "dismissal",
             "RETIREMENT": "retirement", "CONTRACT_END": "end of contract", "DISCHARGE": "discharge",
             "DEATH": "death", "ABSCONDING": "absence without notice"}
CHECKLIST = {"id_card": "ID card / access card returned", "handover": "Work and files handed over",
             "it_accounts": "Email / system accounts closed", "advance": "Advance / loan settled",
             "final_salary": "Final salary and dues paid", "noc": "No-objection from department head"}


def template(db, kind: str) -> dict:
    from services.policy import get_setting
    _, subj, body = KINDS[kind]
    return {"subject": get_setting(db, f"letter.{kind}.subject", default="") or subj,
            "body": get_setting(db, f"letter.{kind}.body", default="") or body}


def service_length(start: date | None, end: date | None) -> str:
    if not start or not end or end < start:
        return "-"
    months = (end.year - start.year) * 12 + (end.month - start.month) - (1 if end.day < start.day else 0)
    y, m = divmod(max(months, 0), 12)
    parts = ([f"{y} year{'s' if y != 1 else ''}"] if y else []) + ([f"{m} month{'s' if m != 1 else ''}"] if m else [])
    return " ".join(parts) or f"{(end - start).days + 1} days"


def drop_empty_remarks(body: str, remarks: str) -> str:
    if remarks.strip():
        return body
    return "\n".join(ln for ln in body.split("\n") if ln.strip() not in ("Remarks: {remarks}", "Remarks:{remarks}"))


def checklist(sep) -> dict:
    try:
        return json.loads(sep.checklist_json or "{}") if sep else {}
    except ValueError:
        return {}
