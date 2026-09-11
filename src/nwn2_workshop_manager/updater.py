"""GitHub release updates: verified download, explicit restart, old binary retained."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import urllib.request
from pathlib import Path

REPO = "HUHman416/Neverwinter-Nights-2-Workshop-Manager"
ASSET = "NWN2-Workshop-Manager-x86_64.AppImage"


def version_tuple(value):
    if not re.fullmatch(r"v?\d+\.\d+\.\d+", value):
        raise ValueError("Unsupported release version")
    return tuple(map(int, value.lstrip("v").split(".")))


def request(url):
    return urllib.request.urlopen(urllib.request.Request(url, headers={
        "User-Agent": "NWN2-Workshop-Manager", "Accept": "application/vnd.github+json"
    }), timeout=30)


def check_release(current):
    with request(f"https://api.github.com/repos/{REPO}/releases/latest") as response:
        release = json.load(response)
    if release.get("prerelease") or release.get("draft"):
        return None
    if version_tuple(release["tag_name"]) <= version_tuple(current):
        return None
    assets = {item["name"]: item["browser_download_url"] for item in release["assets"]}
    for name in (ASSET, ASSET + ".sha256"):
        url = assets[name]
        if not url.startswith(f"https://github.com/{REPO}/releases/download/"):
            raise ValueError("Unexpected update download location")
    return {"version": release["tag_name"], "binary": assets[ASSET], "checksum": assets[ASSET + ".sha256"]}


def download_release(release, directory: Path):
    version_tuple(release["version"])
    directory.mkdir(parents=True, exist_ok=True)
    with request(release["checksum"]) as response:
        expected = response.read(4096).decode("ascii").split()[0]
    if not re.fullmatch(r"[a-fA-F0-9]{64}", expected):
        raise ValueError("Invalid release checksum")
    fd, name = tempfile.mkstemp(prefix=".nwn2-update-", dir=directory)
    temporary = Path(name)
    try:
        digest = hashlib.sha256()
        size = 0
        with os.fdopen(fd, "wb") as output, request(release["binary"]) as response:
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > 512 * 1024 * 1024:
                    raise ValueError("Update exceeds size limit")
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if digest.hexdigest().lower() != expected.lower():
            raise ValueError("Update checksum mismatch; download discarded")
        with temporary.open("rb") as handle:
            if handle.read(4) != b"\x7fELF":
                raise ValueError("Download is not an AppImage executable")
        temporary.chmod(0o755)
        target = directory / f"NWN2-Workshop-Manager-{release['version']}-x86_64.AppImage"
        if target.exists():
            raise FileExistsError(f"Update already exists: {target}")
        os.replace(temporary, target)
        return target
    finally:
        temporary.unlink(missing_ok=True)
