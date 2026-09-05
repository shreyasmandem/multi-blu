"""Launch Multi-Blu.

Double-click this file, or use Multi-Blu.bat / the desktop shortcut. Anything
missing is installed on first run so the app works straight out of the folder.
"""

import importlib.util
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

REQUIREMENTS = {
    "numpy": "numpy",
    "comtypes": "comtypes",
    "pycaw": "pycaw",
    "pyaudiowpatch": "PyAudioWPatch",
}


def _install_missing() -> str:
    if getattr(sys, "frozen", False):
        return ""                       # the .exe already carries everything
    missing = [package for module, package in REQUIREMENTS.items()
               if importlib.util.find_spec(module) is None]
    if not missing:
        return ""
    try:
        subprocess.run([sys.executable, "-m", "pip", "install", "--user", *missing],
                       check=True, capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as exc:
        return "Multi-Blu needs these packages:\n\n    %s\n\nInstall them with:\n\n" \
               "    pip install %s\n\n(%s)" % ("\n    ".join(missing),
                                               " ".join(missing), exc)
    return ""


def _complain(message: str) -> None:
    try:
        import tkinter
        from tkinter import messagebox
        root = tkinter.Tk()
        root.withdraw()
        messagebox.showerror("Multi-Blu", message)
        root.destroy()
    except Exception:
        print(message)


if __name__ == "__main__":
    problem = _install_missing()
    if problem:
        _complain(problem)
        sys.exit(1)
    from multiblu.app import main
    main()
