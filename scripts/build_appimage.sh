#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="$PROJECT_DIR/build"
DIST_DIR="$PROJECT_DIR/dist"
APPDIR="$PROJECT_DIR/AppDir"
RELEASE_DIR="$PROJECT_DIR/release"
TOOL_DIR="$PROJECT_DIR/.tools"
OUTPUT="$RELEASE_DIR/NWN2-Workshop-Manager-x86_64.AppImage"

if command -v pyinstaller >/dev/null 2>&1; then
    PYINSTALLER="$(command -v pyinstaller)"
elif [[ -x "$PROJECT_DIR/.venv/bin/pyinstaller" ]]; then
    PYINSTALLER="$PROJECT_DIR/.venv/bin/pyinstaller"
else
    echo "PyInstaller was not found. Install it with: python3 -m pip install pyinstaller" >&2
    exit 1
fi

PYINSTALLER_BIN_DIR="$(dirname "$PYINSTALLER")"
PYTHON_FOR_BUILD="$PYINSTALLER_BIN_DIR/python"
if [[ ! -x "$PYTHON_FOR_BUILD" ]]; then
    PYTHON_FOR_BUILD="$(command -v python3)"
fi

# PyInstaller normally discovers these automatically. Some self-contained
# Python distributions keep Tcl/Tk beside libpython without registering that
# directory with ldconfig, so include those libraries explicitly when present.
EXTRA_BINARY_ARGS=()
while IFS= read -r library; do
    [[ -n "$library" ]] && EXTRA_BINARY_ARGS+=(--add-binary "$library:.")
done < <(
    "$PYTHON_FOR_BUILD" - <<'PY'
import sys
from pathlib import Path

roots = [Path(sys.base_prefix) / "lib", Path(sys.prefix) / "lib"]
patterns = ("libtcl*.so", "libtcl*.so.*", "libtk*.so", "libtk*.so.*")
seen = set()
for root in roots:
    for pattern in patterns:
        for path in root.glob(pattern):
            resolved = str(path.resolve())
            if resolved not in seen:
                seen.add(resolved)
                print(resolved)
PY
)

rm -rf "$BUILD_DIR" "$DIST_DIR" "$APPDIR"
mkdir -p "$APPDIR/usr/bin" "$APPDIR/usr/share/applications" \
    "$APPDIR/usr/share/icons/hicolor/scalable/apps" "$RELEASE_DIR" "$TOOL_DIR"

"$PYINSTALLER" \
    --noconfirm \
    --clean \
    --windowed \
    --onedir \
    --name nwn2-workshop-manager \
    --paths "$PROJECT_DIR/src" \
    --add-data "$PROJECT_DIR/assets:assets" \
    --collect-data certifi \
    "${EXTRA_BINARY_ARGS[@]}" \
    --distpath "$DIST_DIR" \
    --workpath "$BUILD_DIR/pyinstaller" \
    --specpath "$BUILD_DIR" \
    "$PROJECT_DIR/app.py"

cp -a "$DIST_DIR/nwn2-workshop-manager" "$APPDIR/usr/bin/"
install -Dm755 "$PROJECT_DIR/packaging/AppRun" "$APPDIR/AppRun"
install -Dm644 "$PROJECT_DIR/packaging/nwn2-workshop-manager.desktop" \
    "$APPDIR/nwn2-workshop-manager.desktop"
install -Dm644 "$PROJECT_DIR/packaging/nwn2-workshop-manager.desktop" \
    "$APPDIR/usr/share/applications/nwn2-workshop-manager.desktop"
install -Dm644 "$PROJECT_DIR/assets/nwn2-workshop-manager.svg" \
    "$APPDIR/nwn2-workshop-manager.svg"
install -Dm644 "$PROJECT_DIR/assets/nwn2-workshop-manager.svg" \
    "$APPDIR/usr/share/icons/hicolor/scalable/apps/nwn2-workshop-manager.svg"
install -Dm644 "$PROJECT_DIR/packaging/nwn2-workshop-manager.metainfo.xml" \
    "$APPDIR/usr/share/metainfo/nwn2-workshop-manager.appdata.xml"
ln -sfn nwn2-workshop-manager.svg "$APPDIR/.DirIcon"

if [[ -n "${APPIMAGETOOL:-}" && -x "$APPIMAGETOOL" ]]; then
    TOOL="$APPIMAGETOOL"
elif command -v appimagetool >/dev/null 2>&1; then
    TOOL="$(command -v appimagetool)"
else
    TOOL="$TOOL_DIR/appimagetool-x86_64.AppImage"
    if [[ ! -x "$TOOL" ]]; then
        echo "Downloading the official AppImageKit appimagetool…"
        curl --fail --location --retry 3 \
            --output "$TOOL" \
            "https://github.com/AppImage/AppImageKit/releases/download/continuous/appimagetool-x86_64.AppImage"
        chmod +x "$TOOL"
    fi
fi

rm -f "$OUTPUT" "$OUTPUT.sha256"
ARCH=x86_64 APPIMAGE_EXTRACT_AND_RUN=1 "$TOOL" "$APPDIR" "$OUTPUT"
chmod +x "$OUTPUT"
(
    cd "$RELEASE_DIR"
    sha256sum "$(basename "$OUTPUT")" > "$(basename "$OUTPUT").sha256"
)

echo "Built: $OUTPUT"
