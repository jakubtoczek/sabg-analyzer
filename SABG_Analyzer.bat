@echo off
rem ===========================================================================
rem  Double-click launcher for the SABG Analyzer GUI.
rem  - Prefers a Python that ALREADY has the packages; otherwise prefers a
rem    wheel-friendly version (3.12/3.11/3.10) over a too-new one (3.13/3.14...),
rem    because pylibCZIrw only ships prebuilt wheels for ~3.9-3.12. On a newer
rem    Python there is no wheel (and pip would try to COMPILE it -> needs CMake).
rem  - If only a too-new Python exists, it offers to install 3.12 via the Python
rem    manager (py install 3.12), else guides a manual 3.12 install.
rem  - Installs wheels only (never compiles); check/install/launch use ONE Python.
rem  - Works from a network / OneDrive Desktop (UNC): everything is keyed off this
rem    script's own folder (%~dp0), never the current directory.
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
set "PY="
for %%C in (%CANDS%) do if not defined PY ( %%~C -c "import sys" >nul 2>nul && set "PY=%%~C" )
if not defined PY goto :no_python

rem 2b) Reject a too-new (or too-old) Python BEFORE the doomed install: pylibCZIrw
rem     wheels exist only for CPython 3.9 - 3.12. Gate on Python's EXIT CODE, not a
rem     captured stdout number: on some setups (e.g. the new PyManager 'python') the
rem     old `for /f` capture came back EMPTY, so the guard fell through and a 3.13/3.14
rem     went on to a doomed pylibCZIrw install. An exit code can't be lost this way.
%PY% -c "import sys; v=sys.version_info[0]*100+sys.version_info[1]; sys.exit(0 if 309<=v<=312 else 1)"
if errorlevel 1 goto :wrong_python

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

:wrong_python
echo.
echo ---------------------------------------------------------------------------
echo  The Python found is too new for the CZI reader. pylibCZIrw has prebuilt
echo  packages only for Python 3.9 - 3.12; you need Python 3.12.
echo ---------------------------------------------------------------------------
echo.
%PY% -c "import sys;print('  (found Python',sys.version.split()[0]+')')" 2>nul
echo.
set /p "ANS=Install Python 3.12 now with the Python manager (py install 3.12)? [Y/n] "
if /I "%ANS%"=="n" goto :wrong_python_manual
echo.
py install 3.12
if errorlevel 1 goto :wrong_python_manual
echo.
echo Python 3.12 installed - continuing...
set "PY="
for %%C in (%CANDS%) do if not defined PY ( %%~C -c "import sys" >nul 2>nul && set "PY=%%~C" )
if defined PY goto :install_deps

:wrong_python_manual
echo.
echo Install Python 3.12 manually, then double-click SABG_Analyzer.bat again:
echo   python.org -^> Downloads -^> Stable Releases -^> Python 3.12.x -^> Windows
echo   installer (64-bit), and TICK "Add python.exe to PATH".
start "" https://www.python.org/downloads/windows/
echo.
pause
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
echo If it mentions pylibCZIrw / "no matching distribution" / building a wheel,
echo your Python is too new: install Python 3.12 (py install 3.12, or python.org
echo -^> 3.12.x 64-bit, "Add to PATH"), then run SABG_Analyzer.bat again.
echo Otherwise check internet / proxy.
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
