"""Launch host applications without the frozen application's library paths."""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
from pathlib import Path


def host_environment():
    env = os.environ.copy()
    original = env.pop("LD_LIBRARY_PATH_ORIG", "")
    env.pop("LD_LIBRARY_PATH", None)
    if original:
        env["LD_LIBRARY_PATH"] = original
    for key in ("APPIMAGE", "APPDIR", "TCL_LIBRARY", "TK_LIBRARY", "PYTHONHOME", "PYTHONPATH"):
        env.pop(key, None)
    for key in list(env):
        if key.startswith("_PYI"):
            env.pop(key, None)
    return env


def commands_for(target, env):
    native = shutil.which("steam", path=env.get("PATH"))
    flatpak = shutil.which("flatpak", path=env.get("PATH"))
    commands = []
    if target.startswith("steam://"):
        args = ["-applaunch", target.rsplit("/", 1)[-1]] if target.startswith("steam://rungameid/") else [target]
        if native:
            commands.append([native, *args])
        if flatpak:
            commands.append([flatpak, "run", "com.valvesoftware.Steam", *args])
    opener = shutil.which("xdg-open", path=env.get("PATH"))
    if opener:
        commands.append([opener, target])
    # A Workshop page can still be viewed when Steam's URI handler is missing.
    if opener and target.startswith("steam://openurl/https://"):
        commands.append([opener, target.removeprefix("steam://openurl/")])
    return commands


def launch(target: str, log_path: Path):
    env = host_environment()
    commands = commands_for(target, env)
    if not commands:
        raise RuntimeError("Steam and xdg-open were not found. Install Steam or configure the desktop URL handler.")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    failures = []
    for command in commands:
        with log_path.open("a", encoding="utf-8") as log:
            log.write(f"\nLaunching: {command!r}\n")
            log.flush()
            try:
                process = subprocess.Popen(command, env=env, stdout=log, stderr=log, start_new_session=True)
                try:
                    code = process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    threading.Thread(target=process.wait, daemon=True).start()
                    return f"Launch request sent via {Path(command[0]).name}"
                if code == 0:
                    return f"Launch request sent via {Path(command[0]).name}"
                failures.append(f"{Path(command[0]).name}: exit {code}")
            except OSError as exc:
                failures.append(str(exc))
    raise RuntimeError("Could not open Steam or the requested target.\n" + "\n".join(failures)
                       + f"\nDetails: {log_path}\nTry opening Steam normally and retrying.")
