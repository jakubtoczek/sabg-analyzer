"""Windows taskbar identity and Start menu entry.

The same file ships in each of these apps; change it in all of them or none.
No-op off Windows, and any failure only costs the taskbar pin.
"""

from __future__ import annotations

import ctypes
import os
import sys
import threading
from pathlib import Path


class _GUID(ctypes.Structure):
    _fields_ = [("d1", ctypes.c_ulong), ("d2", ctypes.c_ushort), ("d3", ctypes.c_ushort),
                ("d4", ctypes.c_ubyte * 8)]


class _PROPKEY(ctypes.Structure):
    _fields_ = [("fmtid", _GUID), ("pid", ctypes.c_ulong)]


class _PROPVARIANT(ctypes.Structure):  # only the VT_LPWSTR case is used
    _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort), ("r2", ctypes.c_ushort),
                ("r3", ctypes.c_ushort), ("ptr", ctypes.c_void_p), ("pad", ctypes.c_void_p)]


def _guid(text: str) -> _GUID:
    g = _GUID()
    ctypes.oledll.ole32.CLSIDFromString(text, ctypes.byref(g))
    return g


def _com(obj: ctypes.c_void_p, slot: int, *args) -> None:
    """Call method *slot* of a COM object's vtable; a failed HRESULT raises OSError."""
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    types = [ctypes.c_wchar_p if isinstance(a, str) else ctypes.c_int if isinstance(a, int)
             else ctypes.c_void_p for a in args]
    ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *types)(vtable[slot])(obj, *args)


def taskbar_identity(app_id: str, name: str, launcher: Path, icon: Path) -> None:
    """Give the app its own taskbar button and icon (*app_id*; otherwise Windows files the
    window under pythonw.exe) and keep a Start menu entry *name* with that same ID which
    runs *launcher*. Call before the first window.

    Windows only honours a taskbar pin of the running window when such an entry exists;
    without it, the pin is the bare pythonw.exe: Python logo, and clicking it starts
    nothing. The entry is rewritten at each start, so it follows a moved folder, in a
    thread: the first COM call costs ~0.1 s, not worth holding the window for."""
    if sys.platform != "win32":
        return
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    if launcher.exists():
        threading.Thread(target=_start_menu_entry, args=(app_id, name, launcher, icon),
                         daemon=True).start()


def _start_menu_entry(app_id: str, name: str, launcher: Path, icon: Path) -> None:
    lnk = Path(os.environ["APPDATA"], r"Microsoft\Windows\Start Menu\Programs", f"{name}.lnk")
    link, store, persist = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    try:
        ctypes.windll.ole32.CoInitialize(None)
        ctypes.oledll.ole32.CoCreateInstance(  # ShellLink, IShellLinkW
            ctypes.byref(_guid("{00021401-0000-0000-C000-000000000046}")), None, 1,
            ctypes.byref(_guid("{000214F9-0000-0000-C000-000000000046}")), ctypes.byref(link))
        # through cmd.exe: Windows won't pin a shortcut to a .bat itself
        _com(link, 20, os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe"))  # SetPath
        _com(link, 11, f'/c ""{launcher}""')                                      # SetArguments
        _com(link, 9, str(launcher.parent))                                  # SetWorkingDirectory
        _com(link, 17, str(icon), 0)                                         # SetIconLocation
        _com(link, 15, 7)                    # SetShowCmd: minimised, like the Desktop shortcut
        _com(link, 0, ctypes.byref(_guid("{886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99}")),
             ctypes.byref(store))                                # QueryInterface IPropertyStore
        buf = ctypes.create_unicode_buffer(app_id)
        _com(store, 6, ctypes.byref(_PROPKEY(_guid("{9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3}"), 5)),
             ctypes.byref(_PROPVARIANT(vt=31, ptr=ctypes.addressof(buf))))  # AppUserModel.ID
        _com(store, 7)                                                       # Commit
        _com(link, 0, ctypes.byref(_guid("{0000010B-0000-0000-C000-000000000046}")),
             ctypes.byref(persist))                              # QueryInterface IPersistFile
        _com(persist, 6, str(lnk), 1)                                        # Save
    except Exception:  # noqa: BLE001 - only the taskbar pin is affected
        pass
    # ponytail: the three COM objects are not released; this runs once per process
