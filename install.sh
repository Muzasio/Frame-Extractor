#!/usr/bin/env bash
set -e

INSTALL_DIR="$HOME/.local/bin"
DESKTOP_DIR="$HOME/.local/share/applications"

mkdir -p "$INSTALL_DIR" "$DESKTOP_DIR"

cp frameextract.py "$INSTALL_DIR/frameextract.py"
chmod +x "$INSTALL_DIR/frameextract.py"

sed "s|__HOME__/.local/bin/frameextract.py|$INSTALL_DIR/frameextract.py|" \
    frameextract.desktop > "$DESKTOP_DIR/frameextract.desktop"
chmod +x "$DESKTOP_DIR/frameextract.desktop"

update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true

echo "installed: $INSTALL_DIR/frameextract.py"
echo "launcher: $DESKTOP_DIR/frameextract.desktop"
echo "drag a video onto the FrameExtract entry in your app launcher/taskbar to test"
