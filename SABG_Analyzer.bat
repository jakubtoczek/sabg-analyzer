@echo off
rem ===========================================================================
rem  Double-click launcher for the SABG Analyzer GUI.
rem  - Uses an interpreter that ALREADY has the packages (prefers `python` on
rem    PATH, then the `py` launcher) so it never re-installs needlessly.
rem  - Only if none has them does it offer a one-time install, into that same
rem    interpreter; the check, the install, and the launch all use ONE Python.
rem  - On a PC with no Python at all, it guides the install.
rem  - Works even from a network / OneDrive Desktop where cmd cannot `cd` to a
rem    UNC path: everything is keyed off this script's own folder (%~dp0), never
rem    the current directory.
rem ===========================================================================
set "HERE=%~dp0"
pushd "%HERE%" 2>nul

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
if not exist "%HERE%requirements.txt" (
    echo ERROR: requirements.txt was not found next to this launcher:
    echo     "%HERE%requirements.txt"
    echo Double-click SABG_Analyzer.bat from INSIDE the unzipped project folder
    echo ^(the folder that also contains requirements.txt and the sabg_gui folder^).
    pause
    goto :end
)
echo SABG Analyzer needs to install a few Python packages the first time it runs
echo (a couple of minutes, needs an internet connection).
echo.
set /p "ANS=Install them into the Python above now? [Y/n] "
if /I "%ANS%"=="n" goto :abort
%PY% -m pip install --upgrade pip
%PY% -m pip install -r "%HERE%requirements.txt"
if errorlevel 1 goto :pip_failed
echo.
echo Done. Starting SABG Analyzer...

:launch
rem Make the package importable regardless of the current directory (a UNC Desktop
rem can leave cmd's cwd in C:\Windows), then launch windowless when possible.
set "PYTHONPATH=%HERE%;%PYTHONPATH%"
set "PYW="
if /I "%PY%"=="python" set "PYW=pythonw"
if /I "%PY%"=="py"     set "PYW=pyw"
if defined PYW where %PYW% >nul 2>nul && ( start "" %PYW% -m sabg_gui & goto :end )
%PY% -m sabg_gui
goto :end

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
goto :end

:pip_failed
echo.
echo Package install failed - please check the messages above (internet / proxy?).
pause
goto :end

:abort
echo.
echo SABG Analyzer cannot run without those packages. Re-run when ready.
pause
goto :end

:end
popd 2>nul
exit /b
