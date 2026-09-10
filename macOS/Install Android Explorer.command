#!/bin/bash
set -euo pipefail

APP_NAME="Android Explorer"
APP_DIR="/Applications/Android Explorer.app"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RES_DIR="$APP_DIR/Contents/Resources"
MACOS_DIR="$APP_DIR/Contents/MacOS"

confirm=$(/usr/bin/osascript <<'APPLESCRIPT'
button returned of (display dialog "Install Android Explorer to /Applications?

The installer will install/download Python 3.13, ADB, scrcpy and PySide6 if required." with title "Android Explorer Setup" buttons {"Cancel", "Install"} default button "Install" cancel button "Cancel" with icon note)
APPLESCRIPT
) || exit 0

if [[ "$confirm" != "Install" ]]; then
    exit 0
fi

# Ask for administrator rights once and keep them alive during installation.
sudo -v
while true; do sudo -n true; sleep 45; kill -0 "$$" || exit; done 2>/dev/null &
SUDO_KEEPALIVE=$!
trap 'kill "$SUDO_KEEPALIVE" 2>/dev/null || true' EXIT

echo
echo "=========================================="
echo " Android Explorer macOS Installer"
echo "=========================================="
echo

# Homebrew is used because it provides native builds for both Apple Silicon
# and Intel and handles the macOS runtime dependencies for Python, adb/scrcpy.
if ! command -v brew >/dev/null 2>&1; then
    echo "[1/7] Installing Homebrew..."
    NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

    if [[ -x /opt/homebrew/bin/brew ]]; then
        eval "$(/opt/homebrew/bin/brew shellenv)"
    elif [[ -x /usr/local/bin/brew ]]; then
        eval "$(/usr/local/bin/brew shellenv)"
    fi
else
    echo "[1/7] Homebrew already installed."
fi

if ! command -v brew >/dev/null 2>&1; then
    /usr/bin/osascript -e 'display alert "Android Explorer Setup" message "Homebrew could not be installed." as critical'
    exit 1
fi

echo "[2/7] Installing Python 3.13..."
brew list python@3.13 >/dev/null 2>&1 || brew install python@3.13

echo "[3/7] Installing Android Platform Tools (ADB)..."
brew list --cask android-platform-tools >/dev/null 2>&1 || brew install --cask android-platform-tools

echo "[4/7] Installing scrcpy..."
brew list scrcpy >/dev/null 2>&1 || brew install scrcpy

PYTHON="$(brew --prefix python@3.13)/bin/python3.13"
if [[ ! -x "$PYTHON" ]]; then
    PYTHON="$(command -v python3)"
fi

if [[ ! -x "$PYTHON" ]]; then
    /usr/bin/osascript -e 'display alert "Android Explorer Setup" message "Python could not be located after installation." as critical'
    exit 1
fi

echo "[5/7] Creating Android Explorer.app..."
sudo rm -rf "$APP_DIR"
sudo mkdir -p "$MACOS_DIR" "$RES_DIR"

sudo cp "$SCRIPT_DIR/android_explorer.py" "$RES_DIR/android_explorer.py"
sudo cp "$SCRIPT_DIR/android_explorer.png" "$RES_DIR/android_explorer.png"
sudo cp "$SCRIPT_DIR/android_explorer.icns" "$RES_DIR/android_explorer.icns"

# Create an isolated app-local Python environment.
sudo "$PYTHON" -m venv --copies "$RES_DIR/python3"
sudo "$RES_DIR/python3/bin/python" -m pip install \
    --upgrade \
    --only-binary=:all: \
    --disable-pip-version-check \
    PySide6-Essentials

echo "[6/7] Creating native macOS launcher..."

cat > /tmp/android_explorer_mac_launcher <<'EOF'
#!/bin/bash
APP_DIR="/Applications/Android Explorer.app"
RES="$APP_DIR/Contents/Resources"

# Homebrew path differs between Apple Silicon and Intel.
if [[ -d /opt/homebrew/bin ]]; then
    export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:$PATH"
fi
if [[ -d /usr/local/bin ]]; then
    export PATH="/usr/local/bin:$PATH"
fi

cd "$RES"
exec "$RES/python3/bin/python" "$RES/android_explorer.py"
EOF

sudo mv /tmp/android_explorer_mac_launcher "$MACOS_DIR/Android Explorer"
sudo chmod 755 "$MACOS_DIR/Android Explorer"

cat > /tmp/android_explorer_Info.plist <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>Android Explorer</string>
    <key>CFBundleDisplayName</key>
    <string>Android Explorer</string>
    <key>CFBundleIdentifier</key>
    <string>com.rdsoft.androidexplorer</string>
    <key>CFBundleVersion</key>
    <string>1.0</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0</string>
    <key>CFBundleExecutable</key>
    <string>Android Explorer</string>
    <key>CFBundleIconFile</key>
    <string>android_explorer.icns</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>NSHighResolutionCapable</key>
    <true/>
</dict>
</plist>
EOF

sudo mv /tmp/android_explorer_Info.plist "$APP_DIR/Contents/Info.plist"
sudo chmod 644 "$APP_DIR/Contents/Info.plist"

# The venv and bundle should be readable/executable by all local users.
sudo chmod -R a+rX "$APP_DIR"

echo "[7/7] Verifying..."
"$RES_DIR/python3/bin/python" -c "from PySide6.QtCore import Qt; from PySide6.QtWidgets import QApplication,QMainWindow; print('PySide6 OK')"
command -v adb >/dev/null
command -v scrcpy >/dev/null

# Refresh LaunchServices/Finder metadata.
touch "$APP_DIR"

echo
echo "Installation complete."
echo "Android Explorer is available in /Applications."
echo

/usr/bin/osascript <<'APPLESCRIPT'
set answer to display dialog "Android Explorer was installed successfully in /Applications.

Launch it now?" with title "Android Explorer Setup" buttons {"Later", "Launch"} default button "Launch" with icon note
if button returned of answer is "Launch" then
    do shell script "open -a 'Android Explorer'"
end if
APPLESCRIPT
