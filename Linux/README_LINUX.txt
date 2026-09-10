Android Explorer Linux Installer
================================

Install destination:
    /opt/Android_Explorer

Run:
    chmod +x Install_Android_Explorer_Linux.sh
    ./Install_Android_Explorer_Linux.sh

On desktops with Zenity installed, the installer uses a GUI confirmation and
progress window. Otherwise it falls back to the terminal.

It installs:
- the Linux-compatible Android Explorer app
- an isolated Python virtual environment in /opt/Android_Explorer/python3
- PySide6-Essentials
- ADB through the Linux distribution package manager
- the official scrcpy 4.1 Linux x86_64 static build
- /usr/local/bin/android-explorer launcher
- an Applications-menu shortcut
- an optional Desktop shortcut when ~/Desktop exists

Supported package managers for prerequisites:
- apt (Debian/Ubuntu/Mint)
- dnf (Fedora)
- pacman (Arch/Manjaro)
- zypper (openSUSE)

Linux-specific app changes:
- uses the native Linux window frame rather than Windows-only frameless
  hit-testing, so normal Linux dragging/resizing/snapping works
- ADB/scrcpy executable labels are platform-correct
- bundled blue-folder application icon is retained

Architecture:
- scrcpy official static package used by this installer is x86_64.
