@echo off
rem ===========================================================================
rem  Double-click launcher for the SABG Analyzer GUI.
rem  - Prefers a Python that ALREADY has the packages; otherwise prefers a
rem    wheel-friendly version (3.12/3.11/3.10) over a too-new default (e.g. 3.13/
rem    3.14), because pylibCZIrw only ships prebuilt wheels for those - on a newer
rem    Python pip would try to COMPILE it from source (needs CMake/C++ -> fails).
rem  - Installs wheels only (never compiles); the check/install/launch use ONE Python.
rem  - On a PC with no Python at all, it guides installing Python 3.12.
rem  - Works even from a network / OneDrive Desktop (UNC): everything is keyed off
rem    this script's own folder (%~dp0), never the current directory.
rem ===========================================================================
set "HERE=%~dp0"
pushd "%HERE%" 2>nul

set "DEPS=import pylibCZIrw,czifile,skimage,numpy,cv2,pandas,matplotlib,yaml"
rem Interpreter preference (wheel-friendly versions first, then PATH python, then py):
set "CANDS="py -3.12" "py -3.11" "py -3.10" "python" "py""

rem 1) A Python that ALREADY imports everything wins.
set "PY="
for %%C in (%CANDS%) do if not defined PY ( %%~C -c "%DEPS%" >nul 2>nul && set "PY=%%~C" )
if defined PY goto :launch

rem 2) None has the packages yet -> pick one to install into (wheel-friendly first).
for %%C in (%CANDS%) do if not defined PY ( %%~C -c "import sys" >nul 2>nul && set "PY=%%~C" )
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
rem --only-binary=pylibCZIrw: use a prebuilt wheel, never compile from source.
%PY% -m pip install --only-binary=pylibCZIrw -r "%HERE%requirements.txt"
if errorlevel 1 goto :pip_failed
echo.
echo Done. Starting SABG Analyzer...

:launch
rem Make the package importable regardless of the current directory (a UNC Desktop
rem can leave cmd's cwd in C:\Windows), then launch windowless when possible.
set "PYTHONPATH=%HERE%;%PYTHONPATH%"
if /I "%PY%"=="python"   ( where pythonw >nul 2>nul && ( start "" pythonw -m sabg_gui & goto :end ) )
if /I "%PY%"=="py"       ( where pyw     >nul 2>nul && ( start "" pyw -m sabg_gui     & goto :end ) )
if /I "%PY:~0,3%"=="py " ( where pyw     >nul 2>nul && ( start "" pyw%PY:~2% -m sabg_gui & goto :end ) )
%PY% -m sabg_gui
goto :end

:no_python
echo ===========================================================================
echo  SABG Analyzer needs Python, which is not installed on this PC yet.
echo ===========================================================================
echo.
echo  A browser will now open the Python download page. IMPORTANT: install
echo  Python 3.12 (NOT the newest 3.13/3.14 - the CZI reader has no prebuilt
echo  package for those yet). Then:
echo.
echo    1. Under "Stable Releases", find a "Python 3.12.x" entry.
echo    2. Download its "Windows installer (64-bit)".
echo    3. Run it. On the FIRST screen, TICK the box at the bottom:
echo          [x] Add python.exe to PATH
echo    4. Click "Install Now", let it finish.
echo    5. Close this window, then double-click SABG_Analyzer.bat again.
echo.
start "" https://www.python.org/downloads/windows/
echo Press any key to close this window...
pause >nul
goto :end

:pip_failed
echo.
echo Package install failed - see the messages above.
echo If it mentions building a wheel / CMake / pylibCZIrw, your Python is too new:
echo install Python 3.12 (python.org -^> Stable Releases -^> 3.12.x, 64-bit, "Add to
echo PATH"), then run SABG_Analyzer.bat again. Otherwise check internet / proxy.
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
