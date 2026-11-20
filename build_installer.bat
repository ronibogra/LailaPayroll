@echo off
REM Re-open in a window that stays open, so any error message can be read.
if /i not "%~1"=="run" (
  cmd /k ""%~f0" run"
  exit /b
)
title LAILA GROUP Payroll - building LailaPayroll_Setup.exe
REM Double-click on any Windows 10/11 PC with internet. Installs Python 3.12 and Inno Setup 6 if missing
REM (using winget), then builds installer\Output\LailaPayroll_Setup.exe. Developed by: SPHere - 01617144755
setlocal EnableDelayedExpansion
cd /d "%~dp0"
if not exist "%~dp0requirements-dev.txt" (
  echo This file must be run from the UNZIPPED LailaPayroll folder.
  echo Right-click the zip ^> Extract All, open the LailaPayroll folder, then double-click build_installer.bat.
  goto :err
)
echo Folder: %CD%
ver

echo === 1/5 Checking Python 3.12 ===
set PY=
for %%P in ("%LocalAppData%\Programs\Python\Python312\python.exe" "%ProgramFiles%\Python312\python.exe") do if exist %%P set PY=%%P
if not defined PY (
  py -3.12 -c "import sys" >nul 2>&1 && set PY=py -3.12
)
if not defined PY (
  echo Python 3.12 not found - installing it with winget...
  winget install -e --id Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements
  for %%P in ("%LocalAppData%\Programs\Python\Python312\python.exe" "%ProgramFiles%\Python312\python.exe") do if exist %%P set PY=%%P
)
if not defined PY (
  echo.
  echo Could not install Python automatically. Install Python 3.12 from https://www.python.org/downloads/
  echo ^(tick "Add python.exe to PATH"^) and double-click this file again.
  goto :err
)

echo === 2/5 Checking Inno Setup 6 ===
set ISCC=
if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set ISCC="%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set ISCC="%ProgramFiles%\Inno Setup 6\ISCC.exe"
if exist "%LocalAppData%\Programs\Inno Setup 6\ISCC.exe" set ISCC="%LocalAppData%\Programs\Inno Setup 6\ISCC.exe"
if not defined ISCC (
  echo Inno Setup not found - installing it with winget...
  winget install -e --id JRSoftware.InnoSetup --silent --accept-package-agreements --accept-source-agreements
  if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set ISCC="%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
  if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set ISCC="%ProgramFiles%\Inno Setup 6\ISCC.exe"
  if exist "%LocalAppData%\Programs\Inno Setup 6\ISCC.exe" set ISCC="%LocalAppData%\Programs\Inno Setup 6\ISCC.exe"
)
if not defined ISCC (
  echo.
  echo Could not install Inno Setup automatically. Install it from https://jrsoftware.org/isdl.php
  echo and double-click this file again.
  goto :err
)

echo === 3/5 Installing libraries (first time takes a few minutes) ===
if not exist .venv-build\Scripts\python.exe %PY% -m venv .venv-build || goto :err
set VPY=.venv-build\Scripts\python.exe
%VPY% -m pip install --upgrade pip >nul
%VPY% -m pip install -r requirements-dev.txt || goto :err

echo === 4/5 Testing and building LailaPayroll.exe ===
%VPY% -m pytest -q || (echo Tests failed - not building & goto :err)
%VPY% -m PyInstaller --noconfirm --clean --onedir --noconsole --name LailaPayroll ^
  --icon static\img\lg-logo.ico ^
  --add-data "templates;templates" --add-data "static;static" ^
  --collect-submodules uvicorn --collect-submodules sqlalchemy.dialects.sqlite ^
  --hidden-import python_multipart --hidden-import multipart --hidden-import pyodbc ^
  --collect-data tzdata --collect-submodules reportlab --collect-submodules cryptography ^
  run.py || goto :err

echo === 5/5 Packing the installer ===
%ISCC% /Q installer\LailaPayroll.iss || goto :err

echo.
echo ============================================================
echo  DONE:  %~dp0installer\Output\LailaPayroll_Setup.exe
echo  Copy it to the head-office server and run it as administrator.
echo ============================================================
explorer "%~dp0installer\Output"
pause
exit /b 0

:err
echo.
echo ************  BUILD FAILED  ************
echo Send a photo/screenshot of the lines above.
pause
exit /b 1
