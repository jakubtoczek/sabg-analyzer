@echo off
rem ===========================================================================
rem  Double-click launcher for the SABG Analyzer GUI.
rem  - Uses an interpreter that ALREADY has the packages (prefers `python` on
rem    PATH, then the `py` launcher) so it never re-installs needlessly.
rem  - Only if none has them does it offer a one-time install, into that same
rem    interpreter; the check, the install, and the launch all use ONE Python.
rem  - On a PC with no Python at all, it guides the install.
rem ===========================================================================
cd /d "%~dp0"
title SABG Analyzer

set "DEPS=import pylibCZIrw,czifile,skimage,numpy,cv2,pandas,matplotlib,yaml"

rem 1) An interpreter that ALREADY imports everything wins (python on PATH, then py).
set "PY="
for %%I in (python py) do if not defined PY ( %%I -c "%DEPS%" >nul 2>nul && set "PY=%%I" )
if defined PY goto :launch

rem 2) None has the packages yet -> pick one to install into (python on PATH, then py).
for %%I in (python py) do if not defined PY ( where %%I >nul 2>nul && set "PY=%%I" )
if not defined PY goto :no_python

:install_deps
echo.
%PY% -c "import sys;print('Using Python',sys.version.split()[0],'at',sys.executable)"
echo.
echo SABG Analyzer needs to install a few Python packages the first time it runs
echo (a couple of minutes, needs an internet connection).
echo.
set /p "ANS=Install them into the Python above now? [Y/n] "
if /I "%ANS%"=="n" goto :abort
%PY% -m pip install --upgrade pip
%PY% -m pip install -r requirements.txt
if errorlevel 1 goto :pip_failed
echo.
echo Done. Starting SABG Analyzer...

:launch
rem Launch windowless (no black console) with the SAME interpreter's GUI variant.
set "PYW="
if /I "%PY%"=="python" set "PYW=pythonw"
if /I "%PY%"=="py"     set "PYW=pyw"
if defined PYW where %PYW% >nul 2>nul && ( start "" %PYW% -m sabg_gui & exit /b )
%PY% -m sabg_gui
exit /b

:no_python
echo ===========================================================================
echo  SABG Analyzer needs Python, which is not installed on this PC yet.
echo ===========================================================================
echo.
echo  A browser will now open the official Python download page. Then:
echo.
echo    1. Download the latest "Windows installer (64-bit)".
echo    2. Run it. On the FIRST screen, TICK the box at the bottom:
echo          [x] Add python.exe to PATH
echo       (this lets Windows find Python - it is easy to miss).
echo    3. Click "Install Now" and wait for it to finish.
echo    4. Close this window, then double-click SABG_Analyzer.bat again.
echo.
start "" https://www.python.org/downloads/windows/
echo Press any key to close this window...
pause >nul
exit /b 0

:pip_failed
echo.
echo Package install failed - please check the messages above (internet / proxy?).
pause
exit /b 1

:abort
echo.
echo SABG Analyzer cannot run without those packages. Re-run when ready.
pause
exit /b 1
