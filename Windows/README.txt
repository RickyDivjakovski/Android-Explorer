Android Explorer GUI Installer
==============================

Run:
    Install_Android_Explorer.bat

The installer:
- requests Administrator permission because it installs to Program Files
- installs to:
    C:\Program Files\Android_Explorer
- copies android_explorer.py and its app icons
- downloads the official CPython 3.13.9 embeddable x64 runtime
- installs PySide6-Essentials
- downloads the official scrcpy 4.1 portable x64 package
- installs scrcpy and adb into:
    C:\Program Files\Android_Explorer\scrcpy
- creates a Desktop shortcut (optional)
- creates a Start Menu shortcut (optional)
- launches via pythonw.exe so no console window is shown

Internet access is required during installation.
