@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title chess-tilt

rem ---- find Python 3.11 - 3.13 (all needed packages have ready-made installers for these)
set "PY="
py -3.13 -c "import sys" >nul 2>nul && set "PY=py -3.13"
if not defined PY py -3.12 -c "import sys" >nul 2>nul && set "PY=py -3.12"
if not defined PY py -3.11 -c "import sys" >nul 2>nul && set "PY=py -3.11"
if not defined PY python -c "import sys; assert (3, 11) <= sys.version_info[:2] <= (3, 13)" >nul 2>nul && set "PY=python"
rem ---- otherwise try any newer Python (works if its packages are available for it)
if not defined PY py -3 -c "import sys; assert sys.version_info >= (3, 11)" >nul 2>nul && set "PY=py -3" && set "NEWPY=1"
if not defined PY python -c "import sys; assert sys.version_info >= (3, 11)" >nul 2>nul && set "PY=python" && set "NEWPY=1"
if not defined PY goto :nopython
if defined NEWPY echo Note: using a Python newer than 3.13. If setup fails, install Python 3.13 next to it.

rem ---- one-time setup of a private environment in .venv
if exist ".venv\Scripts\python.exe" goto :ready
if exist ".venv" rmdir /s /q ".venv"
echo First run: installing what the program needs into .venv (one time, about a minute) ...
%PY% -m venv .venv || goto :fail
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt || goto :fail

:ready
set "VPY=.venv\Scripts\python.exe"
if "%~1"=="" goto :ask
"%VPY%" tilt.py run %*
goto :end

:ask
echo.
echo  To analyse a file: drag your Lichess .pgn.zst file onto RUN_TILT.bat.
echo  Or type  selftest  to check that everything works, or paste the path of your file.
echo.
set "FILE="
set /p "FILE=> "
if not defined FILE goto :end
set "FILE=%FILE:"=%"
if /i "%FILE%"=="selftest" (
  "%VPY%" tilt.py selftest
  goto :end
)
"%VPY%" tilt.py run "%FILE%"
goto :end

:nopython
echo Python 3.11 or newer was not found.
echo Install Python 3.13 from https://www.python.org/downloads/windows/
echo and tick "Add python.exe to PATH" on the first screen of the installer. Then run this again.
goto :end

:fail
echo.
echo Setup failed. Check your internet connection and try again.
echo If you have a very new Python (3.14 or later), some packages may not support it yet:
echo install Python 3.13 from https://www.python.org/downloads/windows/ (it can sit next to
echo your other Python), delete the .venv folder, and run this again.

:end
echo.
pause
