#!/usr/bin/env bash
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
    if command -v pkexec >/dev/null 2>&1; then
        exec pkexec bash "$0"
    else
        exec sudo bash "$0"
    fi
fi

rm -f /usr/local/bin/android-explorer
rm -f /usr/share/applications/android-explorer.desktop
rm -f /usr/share/icons/hicolor/256x256/apps/android-explorer.png
rm -rf /opt/Android_Explorer

echo "Android Explorer has been removed."
