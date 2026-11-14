@echo off
cd /d "%~dp0"
set "PY="
py -3 --version >nul 2>&1 && set "PY=py -3"
if not defined PY python --version >nul 2>&1 && set "PY=python"
if not defined PY for /d %%D in ("%LocalAppData%\Python\pythoncore-*") do if exist "%%D\python.exe" set PY="%%D\python.exe"
if not defined PY for /d %%D in ("%LocalAppData%\Programs\Python\Python3*") do if exist "%%D\python.exe" set PY="%%D\python.exe"
if not defined PY for /d %%D in ("%ProgramFiles%\Python3*") do if exist "%%D\python.exe" set PY="%%D\python.exe"
if not defined PY (
  echo Python was not found on this PC.
  echo Install Python 3.10 or newer from python.org ^(tick "Add python.exe to PATH"^), then run this file again.
  pause
  exit /b 1
)
echo Using Python: %PY%
echo.
echo Updating the data from the Excel files in 02_UPLOAD_NEW_DATA ...
%PY% -m pip install -q --disable-pip-version-check -r requirements.txt
%PY% 01_DASHBOARD_APP\run_pipeline.py --no-wait
echo.
echo Result: see 00_START_HERE\Last_Update_Status.txt
echo Refresh the dashboard page (or press "Refresh data") to see the new data.
echo.
pause
