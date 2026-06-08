@echo off
rem Double-click launcher for the SABG Analyzer GUI.
rem Uses pythonw (no console window) when available, else falls back to python.
cd /d "%~dp0"
where pythonw >nul 2>nul && (
    start "" pythonw "%~dp0sabg_gui.py"
) || (
    python "%~dp0sabg_gui.py"
)
