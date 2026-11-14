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
echo Installing / checking the required packages (first start only takes a minute)...
%PY% -m pip install -q --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
  echo.
  echo The packages could not be installed. Check the internet connection, then run this file again.
  pause
  exit /b 1
)
echo.
echo Starting the Digitalization dashboard. Close this window to stop it.
cd /d "%~dp0\01_DASHBOARD_APP"
%PY% -m streamlit run app.py
pause
