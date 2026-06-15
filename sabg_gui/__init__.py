"""SABG Analyzer GUI package (Tkinter front-end).

Run with ``python -m sabg_gui`` (or double-click ``SABG_Analyzer.bat``). Modules:
``__main__`` (the main window / CLI front-end), ``widgets`` (shared widgets: layers
panel, detection sections, sliders, canvas nav), ``preview_gui`` (interactive
Preview/ROI tuner), ``info_config`` (Info + Config windows). The GUI shells out to
``python -m sabg_analyzer`` for the heavy pipeline; it does not import it directly
(beyond the cheap ``__version__``).
"""
