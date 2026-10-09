@echo off
setlocal
title SABG Analyzer launcher

rem --- where the Python runtime lives (no admin, no PATH or registry changes) ---
rem     shared:  one Python + one package cache for every tool set up this way
rem     private: a self-contained folder only SABG Analyzer uses
rem     The interpreter is fetched and pinned here, which is why this script no
rem     longer hunts for a system Python: the CZI reader has no wheel past 3.13,
rem     and that constraint now lives in pyproject.toml instead of in a version
rem     gate the user has to satisfy by hand.
set "SHARED_ROOT=C:\ProgramData\PyApps"
set "PRIVATE_ROOT=C:\Users\Public\SABG_Analyzer"
set "PROJECT=%~dp0"
set "PYTHONPATH=%~dp0"
set "ICON=%~dp0sabg_gui\assets\sabg_analyzer.ico"

rem --- pick a root. No question is asked: shared unless you ask for private. ---
rem       SABG_Analyzer.bat private   (or 2)  -> a folder only this tool uses
rem       set SABG_ANALYZER_RUNTIME=...    -> some other location of your own
rem     After the first run the folder on disk is the memory, so a plain
rem     double-click (and the Desktop shortcut) re-use whatever is there.
set "ROOT="
if defined SABG_ANALYZER_RUNTIME set "ROOT=%SABG_ANALYZER_RUNTIME%"
if not defined ROOT if /I "%~1"=="private" set "ROOT=%PRIVATE_ROOT%"
if not defined ROOT if "%~1"=="2" set "ROOT=%PRIVATE_ROOT%"
if not defined ROOT if exist "%PRIVATE_ROOT%\uv.exe" set "ROOT=%PRIVATE_ROOT%"
if not defined ROOT if exist "%SHARED_ROOT%\uv.exe"  set "ROOT=%SHARED_ROOT%"
if not defined ROOT set "ROOT=%SHARED_ROOT%"

set "UV=%ROOT%\uv.exe"
set "UV_PYTHON_INSTALL_DIR=%ROOT%\python"
set "UV_CACHE_DIR=%ROOT%\cache"
set "UV_PROJECT_ENVIRONMENT=%ROOT%\envs\sabg_analyzer"
rem the cache must sit on the same drive as the venv, or uv copies instead of
rem hardlinking and the sharing buys nothing
set "UV_NO_MODIFY_PATH=1"

rem --- one-time: fetch the uv binary ---
if not exist "%UV%" (
    echo First run: downloading uv into %ROOT% ...
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
        "New-Item -ItemType Directory -Force -Path '%ROOT%' | Out-Null;" ^
        "Invoke-WebRequest -Uri 'https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip' -OutFile '%ROOT%\uv.zip';" ^
        "Expand-Archive -Force '%ROOT%\uv.zip' '%ROOT%';" ^
        "Remove-Item '%ROOT%\uv.zip'"
    if not exist "%UV%" (
        echo Failed to download uv into %ROOT%.
        echo Check your internet connection, or whether that folder is writable.
        pause
        exit /b 1
    )
)

rem --- install Python + dependencies ---
rem     skipped while uv.lock is the one the environment was built from: even a no-op
rem     sync costs seconds on a cold start (antivirus scanning uv and the cache)
fc /b "%PROJECT%uv.lock" "%UV_PROJECT_ENVIRONMENT%\.sabg_analyzer.lock" >nul 2>&1
if errorlevel 1 (
    echo Preparing environment in %ROOT% ...
    "%UV%" sync --project "%PROJECT%." --python 3.13
    if errorlevel 1 (
        echo Environment setup failed.
        pause
        exit /b 1
    )
    copy /y "%PROJECT%uv.lock" "%UV_PROJECT_ENVIRONMENT%\.sabg_analyzer.lock" >nul
)

rem --- one-time: put a SABG Analyzer shortcut (with the app icon) on the Desktop ---
rem     resolve Desktop via .NET so a OneDrive-redirected / localized folder works;
rem     best-effort - a shortcut failure must never block launch.
rem     The stamp lives in the venv, not the root, because two apps can share
rem     one root and each still needs its own shortcut.
rem     The stamp is what keeps this off the fast path: starting PowerShell only to be
rem     told the shortcut already exists cost a third of a second of every launch.
rem     Stamp .shortcut-icon and no "already there" test: shortcuts made before the app
rem     had an icon (2026.10.9) are rewritten once to get it.
if not exist "%UV_PROJECT_ENVIRONMENT%\.shortcut-icon" (
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
        "try {" ^
            "$lnk = Join-Path ([Environment]::GetFolderPath('Desktop')) 'SABG Analyzer.lnk';" ^
                "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($lnk);" ^
                "$s.TargetPath='%~f0'; $s.WorkingDirectory='%~dp0';" ^
                "$s.IconLocation='%ICON%'; $s.WindowStyle=7;" ^
                "$s.Description='SABG Analyzer - senescence quantification from Zeiss CZI scans'; $s.Save()" ^
        "} catch {}"
    echo done> "%UV_PROJECT_ENVIRONMENT%\.shortcut-icon"
)

rem --- launch the GUI with pythonw (no console window) and exit ---
start "" "%UV_PROJECT_ENVIRONMENT%\Scripts\pythonw.exe" -m sabg_gui
exit /b 0
