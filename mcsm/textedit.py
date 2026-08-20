"""Handing a file to whatever text editor the user already has.
"""

import os
import subprocess
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Opening a text file in whatever editor the user already has
#
# Windows will happily "open" an unassociated extension by popping the
# "How do you want to open this file?" picker rather than failing, so
# os.startfile() inside a try/except is not a usable test for whether an
# association exists -- by the time it returns, the user is already looking at
# a dialog we didn't want. Ask the association database first instead, and
# only launch once we know a real handler is registered.
# ---------------------------------------------------------------------------

ASSOCF_VERIFY = 0x00000040   # make the shell confirm the handler still exists
ASSOCSTR_EXECUTABLE = 2      # "the executable this extension opens with"

# What AssocQueryStringW hands back when nothing real is registered. These are
# the shell's own "ask the user" shims, not editors -- launching one is the
# picker dialog we're trying to avoid.
NON_HANDLER_EXECUTABLES = {"openwith.exe", "rundll32.exe", "shell32.dll"}


def windows_associated_executable(ext: str) -> Optional[str]:
    """Path to the executable registered to open `ext` (e.g. ".properties"),
    or None if there is no usable association.

    Uses shlwapi's AssocQueryStringW rather than shelling out to assoc/ftype:
    no console window flash, and no parsing of locale-dependent output."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        shlwapi = ctypes.WinDLL("shlwapi", use_last_error=True)
        query = shlwapi.AssocQueryStringW
        query.restype = ctypes.c_long
        query.argtypes = [
            ctypes.c_uint32, ctypes.c_uint32, wintypes.LPCWSTR,
            wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.POINTER(ctypes.c_uint32),
        ]
        size = ctypes.c_uint32(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        hr = query(ASSOCF_VERIFY, ASSOCSTR_EXECUTABLE, ext, None, buf,
                   ctypes.byref(size))
    except (ImportError, OSError, AttributeError, ValueError):
        # No ctypes, no shlwapi, or an unexpected calling convention. Callers
        # have a fallback chain; this just means "we couldn't ask".
        return None
    if hr != 0:
        return None
    exe = buf.value.strip()
    if not exe or Path(exe).name.lower() in NON_HANDLER_EXECUTABLES:
        return None
    return exe


def open_in_text_editor(path: Path) -> str:
    """Open `path` in whatever text editor the user already has, and return a
    short human-readable name for whichever route worked (for the status bar).

    Three steps, most-preferred first:
      1. a real handler for the file's own extension -- if someone has mapped
         .properties to VS Code or IntelliJ, that wins;
      2. the .txt handler, launched with the file as an argument. Not
         os.startfile, which would re-resolve the original extension and land
         back at step 1;
      3. notepad.exe, which is always present on Windows.

    Raises OSError if every route fails."""
    path = Path(path)
    errors = []

    if os.name == "nt":
        own_ext = path.suffix or ".txt"
        if windows_associated_executable(own_ext):
            try:
                os.startfile(str(path))
                return "the default handler"
            except OSError as e:
                # A stale association pointing at an uninstalled app: the
                # query succeeded but the launch didn't. Fall through.
                errors.append(f"{own_ext} handler: {e}")

        txt_exe = windows_associated_executable(".txt")
        if txt_exe:
            try:
                subprocess.Popen([txt_exe, str(path)])
                return Path(txt_exe).stem
            except OSError as e:
                errors.append(f".txt handler: {e}")

        try:
            subprocess.Popen(["notepad.exe", str(path)])
            return "Notepad"
        except OSError as e:
            errors.append(f"notepad.exe: {e}")
    else:
        # The rest of the app is Windows-bound (explorer, Segoe UI), so this
        # branch is a courtesy rather than a supported path -- it is here so
        # the function degrades to a clear error instead of an AttributeError
        # on os.startfile. Unverified.
        for opener in ("xdg-open", "open"):
            try:
                subprocess.Popen([opener, str(path)])
                return opener
            except OSError as e:
                errors.append(f"{opener}: {e}")

    raise OSError("; ".join(errors) or "no way to open the file was found")
