#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-python3}"
OUTPUT_DIR="${OUTPUT_DIR:-dist_pyinstaller}"
VERSION="${VERSION:-$($PYTHON -c "exec(open('version.py').read()); print(__version__)")}"

echo "=== Building Zapret Hub $VERSION for Linux ==="

# 1. Build web UI
WEB_UI_ROOT="$ROOT/web_ui"
if [ ! -d "$WEB_UI_ROOT/node_modules" ]; then
    echo "--- Installing web UI dependencies ---"
    npm --prefix "$WEB_UI_ROOT" ci
fi
echo "--- Building web UI ---"
npm --prefix "$WEB_UI_ROOT" run build

# 2. Stage runtime
STAGING_ROOT="$ROOT/.build_staging"
RUNTIME_STAGE="$STAGING_ROOT/runtime"
WEB_UI_DIST_STAGE="$STAGING_ROOT/web_ui_dist"

rm -rf "$STAGING_ROOT"
mkdir -p "$RUNTIME_STAGE" "$WEB_UI_DIST_STAGE"

# Copy runtime (exclude .git, __pycache__, *.pyc)
rsync -a --exclude='.git' --exclude='__pycache__' --exclude='*.pyc' --exclude='*.pyo' \
    "$ROOT/runtime/" "$RUNTIME_STAGE/"

# Remove stale generated runtime configs
rm -rf "$RUNTIME_STAGE/v2rayN/goshkow-vpn" 2>/dev/null || true
rm -f  "$RUNTIME_STAGE/v2rayN/goshkow-vpn-subscription.txt" 2>/dev/null || true

# Freeze web_ui/dist
cp -r "$WEB_UI_ROOT/dist/." "$WEB_UI_DIST_STAGE/"

# 3. Build with PyInstaller
echo "--- Building with PyInstaller ---"
PYTHONPATH="$ROOT/src" $PYTHON -m PyInstaller \
    --noconfirm \
    --clean \
    --distpath "$OUTPUT_DIR" \
    --workpath "$ROOT/build" \
    "$ROOT/packaging/zapret_hub_linux.spec"

# 4. Copy staged runtime into PyInstaller output
DIST_DIR=$(find "$OUTPUT_DIR" -mindepth 1 -maxdepth 1 -type d | head -1)
if [ -z "$DIST_DIR" ]; then
    echo "ERROR: PyInstaller output directory not found in $OUTPUT_DIR"
    exit 1
fi

echo "--- Copying runtime into $DIST_DIR ---"
rm -rf "$DIST_DIR/runtime"
cp -r "$RUNTIME_STAGE" "$DIST_DIR/runtime"

# Copy web_ui/dist
rm -rf "$DIST_DIR/web_ui"
mkdir -p "$DIST_DIR/web_ui"
cp -r "$WEB_UI_DIST_STAGE" "$DIST_DIR/web_ui/dist"

# Copy top-level assets expected by bootstrap on Linux
cp -r "$ROOT/ui_assets" "$DIST_DIR/ui_assets"
cp -r "$ROOT/sample_data" "$DIST_DIR/sample_data"
cp "$ROOT/version.py" "$DIST_DIR/version.py"

# 5. Cleanup staging
rm -rf "$STAGING_ROOT"

# 6. Rename to match PKGBUILD naming and package as tar.gz
FINAL_DIR="$OUTPUT_DIR/zapret_hub_${VERSION}_linux_x64"
mv "$DIST_DIR" "$FINAL_DIR"
ARCHIVE_NAME="zapret_hub_${VERSION}_linux_x64.tar.gz"
echo "--- Creating $ARCHIVE_NAME ---"
tar -czf "$ROOT/$ARCHIVE_NAME" -C "$OUTPUT_DIR" "zapret_hub_${VERSION}_linux_x64"

ARCHIVE_PATH="$ROOT/$ARCHIVE_NAME"
ARCHIVE_SIZE=$(du -h "$ARCHIVE_PATH" | cut -f1)
echo "=== Build complete ==="
echo "Archive: $ARCHIVE_PATH ($ARCHIVE_SIZE)"
echo "Contents: $(tar -tzf "$ARCHIVE_PATH" | wc -l) files"
