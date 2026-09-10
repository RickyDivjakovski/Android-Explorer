#!/usr/bin/env bash
set -Eeuo pipefail

APP_NAME="Android Explorer"
INSTALL_DIR="/opt/Android_Explorer"
BIN_LINK="/usr/local/bin/android-explorer"
DESKTOP_FILE="/usr/share/applications/android-explorer.desktop"

SCRCPY_VERSION="4.1"
SCRCPY_ARCHIVE="scrcpy-linux-x86_64-v${SCRCPY_VERSION}.tar.gz"
SCRCPY_URL="https://github.com/Genymobile/scrcpy/releases/download/v${SCRCPY_VERSION}/${SCRCPY_ARCHIVE}"
SCRCPY_SHA256="ad56ae8bfeedf41e824945c11dbf55fcb092b3e615b9b486f48a50e30d389635"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ORIGINAL_USER="${AE_INSTALL_USER:-${SUDO_USER:-${USER:-}}}"

show_error() {
    if command -v zenity >/dev/null 2>&1 && [[ -n "${DISPLAY:-}" ]]; then
        zenity --error --title="$APP_NAME Setup" --text="$1" || true
    else
        printf '\nERROR: %s\n' "$1" >&2
    fi
}

show_info() {
    if command -v zenity >/dev/null 2>&1 && [[ -n "${DISPLAY:-}" ]]; then
        zenity --info --title="$APP_NAME Setup" --text="$1" || true
    else
        printf '\n%s\n' "$1"
    fi
}

confirm_install() {
    if command -v zenity >/dev/null 2>&1 && [[ -n "${DISPLAY:-}" ]]; then
        zenity --question \
            --title="$APP_NAME Setup" \
            --width=460 \
            --text="Install Android Explorer to:\n\n<b>${INSTALL_DIR}</b>\n\nThis will download Python dependencies, the official scrcpy Linux build, and install ADB if required.\n\nA launcher will be added to your Applications menu."
    else
        echo
        echo "Android Explorer Linux Installer"
        echo "Install location: $INSTALL_DIR"
        read -r -p "Continue? [Y/n] " answer
        [[ -z "$answer" || "$answer" =~ ^[Yy]$ ]]
    fi
}

install_system_prereqs() {
    if command -v apt-get >/dev/null 2>&1; then
        export DEBIAN_FRONTEND=noninteractive
        apt-get update
        apt-get install -y python3 python3-venv python3-pip adb curl ca-certificates
        return
    fi

    if command -v dnf >/dev/null 2>&1; then
        dnf install -y python3 python3-pip android-tools curl ca-certificates
        return
    fi

    if command -v pacman >/dev/null 2>&1; then
        pacman -Sy --needed --noconfirm python python-pip android-tools curl ca-certificates
        return
    fi

    if command -v zypper >/dev/null 2>&1; then
        zypper --non-interactive install python3 python3-pip android-tools curl ca-certificates
        return
    fi

    echo "No supported package manager found."
    echo "Install python3, python3-venv/pip, adb and curl manually, then re-run this installer."
    exit 20
}

root_install() {
    [[ "$(id -u)" -eq 0 ]] || {
        echo "Root privileges are required."
        exit 10
    }

    echo "[1/8] Installing system prerequisites..."
    install_system_prereqs

    echo "[2/8] Creating installation directory..."
    rm -rf "$INSTALL_DIR"
    mkdir -p "$INSTALL_DIR"

    install -m 0644 "$SCRIPT_DIR/android_explorer.py" "$INSTALL_DIR/android_explorer.py"
    install -m 0644 "$SCRIPT_DIR/android_explorer.png" "$INSTALL_DIR/android_explorer.png"
    if [[ -f "$SCRIPT_DIR/android_explorer.ico" ]]; then
        install -m 0644 "$SCRIPT_DIR/android_explorer.ico" "$INSTALL_DIR/android_explorer.ico"
    fi

    echo "[3/8] Creating isolated Python environment..."
    python3 -m venv --copies "$INSTALL_DIR/python3"

    echo "[4/8] Installing minimum Python GUI dependency..."
    "$INSTALL_DIR/python3/bin/python" -m pip install \
        --upgrade \
        --only-binary=:all: \
        --disable-pip-version-check \
        PySide6-Essentials

    "$INSTALL_DIR/python3/bin/python" - <<'PY'
from PySide6.QtCore import Qt, QSettings
from PySide6.QtGui import QIcon, QDrag
from PySide6.QtWidgets import QApplication, QMainWindow, QListWidget, QTreeWidget
print("PySide6 verification OK")
PY

    echo "[5/8] Downloading official scrcpy ${SCRCPY_VERSION} Linux static build..."
    tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' EXIT

    curl -fL --retry 3 --connect-timeout 20 \
        "$SCRCPY_URL" \
        -o "$tmp/$SCRCPY_ARCHIVE"

    echo "${SCRCPY_SHA256}  $tmp/$SCRCPY_ARCHIVE" | sha256sum -c -

    echo "[6/8] Installing scrcpy..."
    mkdir -p "$INSTALL_DIR/scrcpy"
    tar -xzf "$tmp/$SCRCPY_ARCHIVE" -C "$tmp"

    src="$tmp/scrcpy-linux-x86_64-v${SCRCPY_VERSION}"
    [[ -x "$src/scrcpy" ]] || {
        echo "scrcpy binary was not found after extraction."
        exit 30
    }

    cp -a "$src/." "$INSTALL_DIR/scrcpy/"
    chmod +x "$INSTALL_DIR/scrcpy/scrcpy"

    echo "[7/8] Creating launcher and desktop entry..."
    cat > "$INSTALL_DIR/android-explorer-launcher" <<'EOF'
#!/usr/bin/env bash
set -e
APP_DIR="/opt/Android_Explorer"
export PATH="$APP_DIR/scrcpy:$PATH"
cd "$APP_DIR"
exec "$APP_DIR/python3/bin/python" "$APP_DIR/android_explorer.py"
EOF
    chmod 0755 "$INSTALL_DIR/android-explorer-launcher"
    ln -sf "$INSTALL_DIR/android-explorer-launcher" "$BIN_LINK"

    install -Dm0644 "$INSTALL_DIR/android_explorer.png" \
        "/usr/share/icons/hicolor/256x256/apps/android-explorer.png"

    cat > "$DESKTOP_FILE" <<'EOF'
[Desktop Entry]
Type=Application
Name=Android Explorer
Comment=Browse and manage Android files over ADB
Exec=/usr/local/bin/android-explorer
Icon=android-explorer
Terminal=false
Categories=Utility;FileManager;
StartupNotify=true
EOF
    chmod 0644 "$DESKTOP_FILE"

    # Optional user Desktop shortcut.
    if [[ -n "$ORIGINAL_USER" ]] && id "$ORIGINAL_USER" >/dev/null 2>&1; then
        user_home="$(getent passwd "$ORIGINAL_USER" | cut -d: -f6)"
        desktop_dir="$user_home/Desktop"
        if [[ -d "$desktop_dir" ]]; then
            cp "$DESKTOP_FILE" "$desktop_dir/Android Explorer.desktop"
            chown "$ORIGINAL_USER":"$(id -gn "$ORIGINAL_USER")" "$desktop_dir/Android Explorer.desktop"
            chmod +x "$desktop_dir/Android Explorer.desktop"
        fi
    fi

    if command -v update-desktop-database >/dev/null 2>&1; then
        update-desktop-database /usr/share/applications >/dev/null 2>&1 || true
    fi
    if command -v gtk-update-icon-cache >/dev/null 2>&1; then
        gtk-update-icon-cache -f /usr/share/icons/hicolor >/dev/null 2>&1 || true
    fi

    echo "[8/8] Verifying installation..."
    command -v adb >/dev/null 2>&1
    test -x "$INSTALL_DIR/scrcpy/scrcpy"
    test -x "$INSTALL_DIR/python3/bin/python"
    test -f "$DESKTOP_FILE"

    rm -rf "$tmp"
    trap - EXIT

    echo "INSTALL_OK"
}

gui_run_root() {
    local log status_file
    log="$(mktemp)"
    status_file="$(mktemp)"

    (
        if command -v pkexec >/dev/null 2>&1; then
            pkexec env \
                AE_INSTALL_USER="$ORIGINAL_USER" \
                DISPLAY="${DISPLAY:-}" \
                XAUTHORITY="${XAUTHORITY:-}" \
                bash "$0" --root >"$log" 2>&1
        else
            sudo env AE_INSTALL_USER="$ORIGINAL_USER" bash "$0" --root >"$log" 2>&1
        fi
        echo "$?" >"$status_file"
    ) &
    pid=$!

    (
        while kill -0 "$pid" 2>/dev/null; do
            echo "# Installing Android Explorer..."
            sleep 0.6
        done
    ) | zenity --progress \
            --pulsate \
            --auto-close \
            --no-cancel \
            --width=460 \
            --title="$APP_NAME Setup" \
            --text="Installing Android Explorer..."

    wait "$pid" || true
    rc="$(cat "$status_file" 2>/dev/null || echo 1)"

    if [[ "$rc" == "0" ]] && grep -q "INSTALL_OK" "$log"; then
        rm -f "$log" "$status_file"
        show_info "Android Explorer was installed successfully.\n\nOpen it from your Applications menu or run:\nandroid-explorer"
        return 0
    fi

    details="$(tail -n 20 "$log" 2>/dev/null || true)"
    rm -f "$log" "$status_file"
    show_error "Installation failed.\n\n${details}"
    return 1
}

case "${1:-}" in
    --root)
        root_install
        exit 0
        ;;
esac

confirm_install || exit 0

if command -v zenity >/dev/null 2>&1 && [[ -n "${DISPLAY:-}" ]]; then
    gui_run_root
else
    echo
    echo "Administrator/root permission is required."
    if command -v pkexec >/dev/null 2>&1; then
        pkexec env AE_INSTALL_USER="$ORIGINAL_USER" bash "$0" --root
    else
        sudo env AE_INSTALL_USER="$ORIGINAL_USER" bash "$0" --root
    fi
fi
