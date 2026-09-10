Android Explorer - Windows + Linux + macOS Installers
=====================================================

Choose the folder matching your operating system.

WINDOWS
-------
Open:
    Windows\Install_Android_Explorer.bat

Installs to:
    C:\Program Files\Android_Explorer

Creates optional Desktop and Start Menu shortcuts and downloads the required
portable Python runtime, PySide6-Essentials, scrcpy and adb.

LINUX
-----
Run:
    chmod +x "Linux/Install_Android_Explorer_Linux.sh"
    ./Linux/Install_Android_Explorer_Linux.sh

Installs to:
    /opt/Android_Explorer

Creates an Applications-menu launcher and installs Python/PySide6, ADB and
the official scrcpy Linux x86_64 build.

MACOS
-----
Double-click:
    macOS/Install Android Explorer.command

Installs to:
    /Applications/Android Explorer.app

Installs Python 3.13, ADB and scrcpy through Homebrew and creates an isolated
PySide6 environment inside the app bundle. Supports Apple Silicon and Intel.

All three installers require internet access during installation.
