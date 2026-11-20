# LAILA GROUP Attendance & Payroll (Head Office)

Attendance and payroll for the head office, which also handles staff of other Laila Group companies.
Punches come **directly from the ZKTeco and Hikvision face machines**. There is no link to GTRHRIS
or any other old system, and no old data is carried over.

Developed by: SPHere – 01617144755

## Install: one click

1. On the head-office server (Windows 10/11 or Server 2016+, 64-bit), double-click **`LailaPayroll_Setup.exe`**
   and accept the Windows admin prompt.
2. The installer does everything else by itself:
   * stops and removes any earlier installation of this software
   * **permanently deletes all old data** (it asks once, only if old data is found)
   * installs the program and opens firewall ports 8585 (office PCs + ZKTeco machines)
     and 8443 (mobile punch)
   * sets it to start automatically with Windows and restart itself if it stops
   * opens the **first-time setup** page in the browser
3. On the setup page, create the admin account, list the companies (one per line, `CODE, Name`),
   tick the weekly off day and optionally enter the office location. Click **Finish setup**.

Nothing else is needed: no Python, no database server, no settings file. The data lives in
`C:\ProgramData\LailaPayroll` (database, backups, logs, HTTPS certificate). A backup is made every day.

Office PCs open `http://<server-ip>:8585`, or use the **LAILA GROUP Payroll** desktop icon on the server.

## After setup

1. **Punch machines** page:
   * **ZKTeco**: on each machine go to *Menu › COMM › Cloud Server Setting* and enter
     server = server IP, port = 8585, HTTPS off. The machine appears as PENDING. Click Edit, name it,
     and tick *Approved*. Its stored punches upload by themselves and new punches arrive in real time.
   * **Hikvision**: enable ISAPI on the machine, then add it on the Devices page (IP, port 80, admin user and password).
     Click *Test*, then *Pull 31 days*. After that the server reads it every minute.
   * Machines that aren't approved are ignored.
2. **Settings**:
   * Companies (with the address shown on payslips) and holidays.
   * The pay policy. It has a group default and each company can override any value.
3. **Shifts**: create the shifts and assign them by employee, department or whole company.
4. **Employees**: download the Excel template, fill one row per person, then upload.
   * `company` = company code.
   * `card_no` = the user ID enrolled on the machines. It must be the same on every machine and unique
     across all companies, because the head-office machines are shared.
5. **Users**: give each company's HR or Accounts person a login limited to their own company.
   ADMIN users and users with "All companies" access see everyone.

## Month end (per company)

1. Check holidays and leave. Fix single-punch days with manual punches.
2. On **Daily attendance**, click **Process month**.
3. On **Payroll**: pick the company, upload advances/deductions (Excel), click **Process payroll**. This makes a draft and can be re-run.
4. Download the salary sheet. It includes the bank advice, cash sheet, mobile-banking sheet and department summary. Also download the payslips PDF.
5. Accounts types `FINAL` to lock that company's month.

## Pay rules - CONFIRM WITH HR / ACCOUNTS

Defaults, all editable on the Settings page:

* Gross split: basic = (gross − medical 750 − transport 450 − food 1,250) ÷ 1.5, and house rent = 50% of basic.
  This is the Dec-2023 RMG wage-board structure.
* OT per hour = basic ÷ 208 × 2 (Labour Act s.108).
* OT is counted in 30-minute blocks with a 2-hour daily cap. Anything above the cap shows as "extra OT" and is still paid unless switched off.
* Absent / unpaid leave = basic ÷ 30 per day.
* Attendance bonus and revenue stamp are off until you set them.

The Labour Act was amended in 2025–26. HR should check the rules for each company before the first payroll.

## Mobile attendance

* HR enables it per employee and sets a PIN.
* The employee opens `http://<server-ip>:8585/m` on the office Wi-Fi. They are sent to the secure page on port 8443 automatically.
* The certificate is made by the server itself, so each phone shows a security warning once. Tap *Advanced › Proceed*.
  After that the phone shares GPS with the page.
* A punch is accepted only within the set radius of the office location, and it is saved with its map position.
  GPS can be faked on some phones, so enable mobile punch only for staff who need it.

## Building the installer (IT, on a development PC)

You need Python 3.12 and Inno Setup 6. Run `build_installer.bat`. It runs the tests, builds `LailaPayroll.exe`
with PyInstaller and packs `installer\Output\LailaPayroll_Setup.exe`.

For development: `pip install -r requirements-dev.txt`, `python -m pytest -q`, `python run.py`
(on non-Windows the data goes to `./data`).

Optional overrides (ports, SQL Server instead of the built-in database): see `settings.env.example`.

## Code map

* `attendance/rules.py`: shifts, night shifts, late, OT (tested)
* `payroll/engine.py`: salary calculation (tested)
* `integration/zk_adms.py`: ZKTeco push protocol and USB attlog files (tested)
* `integration/hikvision.py`: Hikvision ISAPI event reading (tested)
* `integration/employee_import.py`, `integration/csv_import.py`: Excel and CSV imports
* `services/`: devices, month processing, payroll per company, settings and policy, mobile, backups and certificate
* `reports/`: Excel sheets and PDF payslips
* `main.py`: pages and routes. `run.py`: starts HTTP + HTTPS
* `installer/LailaPayroll.iss`: the one-click installer
