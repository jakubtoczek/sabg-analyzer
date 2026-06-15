@echo off
rem Double-click launcher for the SABG Analyzer GUI.
rem Runs the sabg_gui package from the repo root (so sabg_gui + sabg_analyzer import).
rem Uses pythonw (no console window) when available, else falls back to python.
cd /d "%~dp0"
where pythonw >nul 2>nul && (
    start "" pythonw -m sabg_gui
) || (
    python -m sabg_gui
)
