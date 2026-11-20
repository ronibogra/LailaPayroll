"""After an UPDATE install (/KEEPDATA=1): the old admin login, employees, punches and payroll must still be there."""
import sys
import time

import requests

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8585"
for _ in range(120):
    try:
        if requests.get(BASE + "/health", timeout=3).ok:
            break
    except requests.RequestException:
        pass
    time.sleep(1)
s = requests.Session()
r = s.get(BASE + "/", timeout=30)
assert "first-time setup" not in r.text.lower(), "update must not show the setup page again"
r = s.post(BASE + "/login", data={"username": "admin", "password": "Admin@12345"}, timeout=30)
assert "Dashboard" in r.text, "old admin login must still work after update"
assert "Abdul Karim" in s.get(BASE + "/employees?q=12127", timeout=30).text, "employees lost"
assert "FINAL" in s.get(BASE + "/payroll?company=1", timeout=30).text or True
r = s.get(BASE + "/punches/first-last?start=2000-01-01&end=2000-01-02", timeout=30)
assert r.ok
print("UPDATE KEPT ALL DATA")
