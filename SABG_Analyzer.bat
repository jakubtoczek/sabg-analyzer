@echo off
rem ===========================================================================
rem  Double-click launcher for the SABG Analyzer GUI.
rem  On a fresh PC it (1) guides the one-time Python install, then on the next
rem  run (2) offers to install the Python packages, then (3) launches the GUI.
rem ===========================================================================
cd /d "%~dp0"
title SABG Analyzer

rem --- 1. Find Python (the py launcher, else python on PATH) ---
set "PY="
where py     >nul 2>nul && set "PY=py"
if not defined PY (
    where python >nul 2>nul && set "PY=python"
)
if not defined PY goto :no_python

rem --- 2. First run: make sure the dependencies are importable ---
%PY% -c "import pylibCZIrw, czifile, skimage, numpy, cv2, pandas, matplotlib, yaml" >nul 2>nul
if errorlevel 1 goto :install_deps
goto :launch

:install_deps
echo.
echo SABG Analyzer needs to install a few Python packages the first time it runs
echo (this can take a couple of minutes and needs an internet connection).
echo.
set /p "ANS=Install them now? [Y/n] "
if /I "%ANS%"=="n" goto :abort
echo.
%PY% -m pip install --upgrade pip
%PY% -m pip install -r requirements.txt
if errorlevel 1 goto :pip_failed
echo.
echo Done. Starting SABG Analyzer...

:launch
rem Prefer a windowless launch (no black console) when available.
where pythonw >nul 2>nul && ( start "" pythonw -m sabg_gui & exit /b )
where pyw     >nul 2>nul && ( start "" pyw -m sabg_gui & exit /b )
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
