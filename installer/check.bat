@echo off
title LAILA GROUP Payroll - check
if /i not "%~1"=="run" (
  cmd /k ""%~f0" run"
  exit /b
)
set LOGS=%ProgramData%\LailaPayroll\logs
echo ===== Start-up task =====
schtasks /query /tn LailaPayroll /v /fo list | findstr /i /c:"Status" /c:"Last Run Time" /c:"Last Result" /c:"Task To Run"
echo.
echo ===== Is the program running? =====
tasklist /fi "imagename eq LailaPayroll.exe" | findstr /i LailaPayroll || echo NOT RUNNING
echo.
echo ===== Is port 8585 open? =====
netstat -ano | findstr /c:":8585 " | findstr LISTENING || echo NOTHING LISTENING ON 8585
echo.
echo ===== startup-error.txt =====
if exist "%LOGS%\startup-error.txt" (type "%LOGS%\startup-error.txt") else (echo none)
echo.
echo ===== install.log =====
if exist "%LOGS%\install.log" (powershell -NoProfile -Command "Get-Content '%LOGS%\install.log' -Tail 15") else (echo none)
echo.
echo ===== last 25 lines of server.log =====
if exist "%LOGS%\server.log" (powershell -NoProfile -Command "Get-Content '%LOGS%\server.log' -Tail 25") else (echo no server.log yet)
echo.
echo Take a screenshot of this window and send it to IT.
echo To (re)start the program now: schtasks /run /tn LailaPayroll
