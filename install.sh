#!/usr/bin/env bash
# Uninstaller installer (installs for the current user only)
#   ./install.sh              install or update
#   ./install.sh --uninstall  remove
# https://github.com/cyb3rl00m/uninstaller
set -euo pipefail

APP_ID="io.github.cyb3rl00m.Uninstaller"
SRC="$(cd "$(dirname "$0")" && pwd)"
BIN_DIR="$HOME/.local/bin"
APPS_DIR="$HOME/.local/share/applications"
ICON_DIR="$HOME/.local/share/icons/hicolor/scalable/apps"

if [[ "${1:-}" == "--uninstall" ]]; then
    rm -f "$BIN_DIR/uninstaller" "$APPS_DIR/$APP_ID.desktop" "$ICON_DIR/$APP_ID.svg"
    update-desktop-database -q "$APPS_DIR" 2>/dev/null || true
    echo "Uninstaller removed."
    exit 0
fi

if [[ $EUID -eq 0 ]]; then
    echo "Run this as your normal user (not with sudo). It asks for your password only if packages are missing."
    exit 1
fi

if command -v apt-get >/dev/null; then
    deps=(python3 python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 pkexec)
    missing=()
    for p in "${deps[@]}"; do
        dpkg -s "$p" &>/dev/null || missing+=("$p")
    done
    if ((${#missing[@]})); then
        echo "Installing required packages: ${missing[*]}"
        sudo apt-get update -qq
        sudo apt-get install -y "${missing[@]}"
    fi
else
    echo "Uninstaller is made for Ubuntu/Debian (it removes APT packages). Snap and Flatpak lists may still work."
fi

install -Dm755 "$SRC/uninstaller.py" "$BIN_DIR/uninstaller"
install -Dm644 "$SRC/$APP_ID.svg" "$ICON_DIR/$APP_ID.svg"
mkdir -p "$APPS_DIR"
cat > "$APPS_DIR/$APP_ID.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Uninstaller
GenericName=Remove Software
Comment=Completely remove apps and packages (APT, Snap, Flatpak)
Exec=$BIN_DIR/uninstaller
Icon=$APP_ID
Terminal=false
Categories=System;Settings;PackageManager;GTK;
Keywords=uninstall;remove;delete;package;apt;snap;flatpak;purge;cleanup;
StartupNotify=true
EOF

gtk-update-icon-cache -q -t "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
update-desktop-database -q "$APPS_DIR" 2>/dev/null || true

echo
echo "$("$BIN_DIR/uninstaller" --version) installed."
echo "Open 'Uninstaller' from the app grid."
