
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import hashlib
from pathlib import Path
from datetime import datetime
import threading
from pathlib import PurePosixPath

from PySide6.QtCore import Qt, QSize, QThread, Signal, QFileInfo, QTimer, QMimeData, QUrl, QSettings, QDir
from PySide6.QtGui import QAction, QIcon, QColor, QPalette, QPixmap, QPainter, QPen, QBrush, QFontMetrics, QDrag
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QFileDialog, QFileIconProvider,
    QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPushButton,
    QProgressBar, QScrollArea, QSizePolicy, QSplitter, QStyle, QToolBar,
    QToolButton, QVBoxLayout, QWidget, QInputDialog, QTreeWidget,
    QAbstractItemView,
    QTreeWidgetItem, QStackedWidget
)

APP_TITLE = "Android Explorer"
DEFAULT_PATH = "/"

APP_ICON_CANDIDATES = (
    "android_explorer.ico",
    "android_explorer.png",
)

def find_app_icon_path():
    """
    Locate a bundled app icon beside android_explorer.py.
    """
    script_dir = os.path.dirname(os.path.abspath(sys.argv[0]))
    for name in APP_ICON_CANDIDATES:
        candidate = os.path.join(script_dir, name)
        if os.path.isfile(candidate):
            return candidate
    return None

# Private drag payload used when items are dragged between folders inside
# Android Explorer. Windows Explorer ignores this and consumes file:// URLs.
ANDROID_REMOTE_MIME = "application/x-android-explorer-remote-paths"

LIGHT = {
    "window": "#f3f3f3",
    "sidebar": "#f7f7f7",
    "surface": "#ffffff",
    "toolbar": "#fbfbfb",
    "hover": "#e9e9e9",
    "selected": "#dbeeff",
    "border": "#d7d7d7",
    "text": "#1f1f1f",
    "muted": "#616161",
    "accent": "#0067c0",
}

DARK = {
    # Windows 11 Explorer reference is predominantly RGB(25,25,25).
    "window": "#191919",
    "sidebar": "#191919",
    "surface": "#202020",
    "toolbar": "#202020",
    "hover": "#2b2b2b",
    "selected": "#333333",
    "border": "#2b2b2b",
    "text": "#f2f2f2",
    "muted": "#b5b5b5",
    "accent": "#5b9bd5",
}


def apply_dwm_backdrop(hwnd: int, dark: bool):
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes

        dwm = ctypes.windll.dwmapi
        hwnd = wintypes.HWND(hwnd)

        immersive = ctypes.c_int(1 if dark else 0)
        for attr in (20, 19):
            try:
                dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(immersive), ctypes.sizeof(immersive))
                break
            except Exception:
                pass

        # Rounded Windows 11 corners.
        corner = ctypes.c_int(2)
        try:
            dwm.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(corner), ctypes.sizeof(corner))
        except Exception:
            pass

        # DWMWA_SYSTEMBACKDROP_TYPE = 38
        # 1 = None. Keep native Windows 11 corners/title-bar integration but
        # do not use Mica/Acrylic/translucent window materials.
        backdrop = ctypes.c_int(1)
        try:
            dwm.DwmSetWindowAttribute(
                hwnd, 38,
                ctypes.byref(backdrop),
                ctypes.sizeof(backdrop)
            )
        except Exception:
            pass
    except Exception:
        pass


class ADBError(RuntimeError):
    pass


class ADB:
    def __init__(self):
        self.path = shutil.which("adb") or "adb"
        self.serial = None
        self._process_lock = threading.Lock()
        self._active_processes = set()

    def run(self, args, timeout=60, check=True):
        cmd = [self.path] + list(args)
        proc = None
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )

            with self._process_lock:
                self._active_processes.add(proc)

            try:
                stdout, stderr = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except Exception:
                    pass
                stdout, stderr = proc.communicate()
                raise ADBError("ADB command timed out.")

            class Result:
                pass

            cp = Result()
            cp.returncode = proc.returncode
            cp.stdout = stdout or ""
            cp.stderr = stderr or ""

            if check and cp.returncode != 0:
                raise ADBError((cp.stderr or cp.stdout or "ADB command failed").strip())
            return cp

        except FileNotFoundError:
            raise ADBError("adb was not found. Install Android Platform Tools / adb and ensure it is available on PATH.")
        finally:
            if proc is not None:
                with self._process_lock:
                    self._active_processes.discard(proc)

    def cancel_all(self):
        """
        Terminate any ADB processes still running. Used during application
        shutdown so a long recursive cache scan cannot hold the app open.
        """
        with self._process_lock:
            processes = list(self._active_processes)

        for proc in processes:
            try:
                if proc.poll() is None:
                    proc.terminate()
            except Exception:
                pass

        for proc in processes:
            try:
                if proc.poll() is None:
                    proc.wait(timeout=0.4)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    def dargs(self):
        return ["-s", self.serial] if self.serial else []

    def devices(self):
        cp = self.run(["devices", "-l"], timeout=15)
        result = []
        for line in cp.stdout.splitlines()[1:]:
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            serial = parts[0]
            state = parts[1] if len(parts) > 1 else "unknown"
            info = " ".join(parts[2:])
            model = ""
            m = re.search(r"model:([^\s]+)", info)
            if m:
                model = m.group(1).replace("_", " ")
            result.append((serial, state, model))
        return result

    def connect(self, host):
        host = host.strip()
        if ":" not in host:
            host += ":5555"
        cp = self.run(["connect", host], timeout=20, check=False)
        txt = (cp.stdout + cp.stderr).strip()
        if cp.returncode != 0 or "unable" in txt.lower() or "failed" in txt.lower():
            raise ADBError(txt or "Unable to connect.")
        return txt

    def shell(self, command, timeout=60, check=True):
        return self.run(self.dargs() + ["shell", command], timeout=timeout, check=check).stdout

    def scan_root_only(self):
        """
        Cache only the immediate contents of /, including permissions.

        Root entries are classified with `[ -d ]` so Android symlinked
        directories such as /sdcard are still shown as folders.
        """
        cmd = (
            'for f in /.[!.]* /..?* /*; do '
            '[ -e "$f" ] || [ -L "$f" ] || continue; '
            'n="${f##*/}"; '
            'if [ -d "$f" ]; then t=d; '
            'elif [ -L "$f" ]; then '
            '  if [ -d "$(readlink -f "$f" 2>/dev/null)" ]; then t=d; else t=l; fi; '
            'else t=f; fi; '
            'p=$(stat -c %a "$f" 2>/dev/null || echo ""); '
            'm=$(stat -c %Y "$f" 2>/dev/null || echo 0); '
            'printf "%s\\t%s\\t%s\\t%s\\t%s\\n" "$t" "$p" "$m" "$f" "$n"; '
            'done'
        )

        cp = self.run(
            self.dargs() + ["shell", cmd],
            timeout=45,
            check=False
        )

        rows = []
        seen = set()

        for line in (cp.stdout or "").splitlines():
            parts = line.split("\t", 4)
            if len(parts) != 5:
                continue

            typ, mode, mtime, path, name = parts
            path = path.strip()

            if not path.startswith("/") or path in seen:
                continue

            seen.add(path)
            try:
                mtime_text = datetime.fromtimestamp(int(mtime)).strftime("%d/%m/%Y %I:%M %p") if int(mtime) > 0 else ""
            except Exception:
                mtime_text = ""

            rows.append({
                "path": path,
                "name": name,
                "type": typ,
                "size": 0,
                "mode": mode,
                "mtime_text": mtime_text,
            })

        rows.sort(
            key=lambda r: (
                0 if r["type"] == "d" else 1,
                r["name"].lower()
            )
        )
        return rows

    def scan_recursive_base(self, base):
        """
        Recursively cache one requested base directory, including permissions.

        The staged order is controlled by Explorer.start_next_cache_stage().
        For each entry we cache:
          path, name, file/folder type, chmod-style octal permissions

        Size and modified time remain lazy so the background scan stays
        substantially lighter than a full metadata crawl.
        """
        exists_cp = self.run(
            self.dargs() + [
                "shell",
                f'[ -e {shlex.quote(base)} ] && echo 1 || echo 0'
            ],
            timeout=15,
            check=False
        )
        if not (exists_cp.stdout or "").strip().endswith("1"):
            return []

        qbase = shlex.quote(base)
        cmd = (
            f'find {qbase} -mindepth 1 2>/dev/null | '
            'while IFS= read -r f; do '
            'n="${f##*/}"; '
            'if [ -d "$f" ]; then t=d; '
            'elif [ -L "$f" ]; then '
            '  if [ -d "$(readlink -f "$f" 2>/dev/null)" ]; then t=d; else t=l; fi; '
            'else t=f; fi; '
            'p=$(stat -c %a "$f" 2>/dev/null || echo ""); '
            'm=$(stat -c %Y "$f" 2>/dev/null || echo 0); '
            'printf "%s\\t%s\\t%s\\t%s\\t%s\\n" "$t" "$p" "$m" "$f" "$n"; '
            'done'
        )

        cp = self.run(
            self.dargs() + ["shell", cmd],
            timeout=300,
            check=False
        )

        rows = []
        seen = set()

        for line in (cp.stdout or "").splitlines():
            parts = line.split("\t", 4)
            if len(parts) != 5:
                continue

            typ, mode, mtime, path, name = parts
            path = path.strip()

            if not path.startswith("/") or path in seen:
                continue

            seen.add(path)
            try:
                mtime_text = datetime.fromtimestamp(int(mtime)).strftime("%d/%m/%Y %I:%M %p") if int(mtime) > 0 else ""
            except Exception:
                mtime_text = ""

            rows.append({
                "path": path,
                "name": name,
                "type": typ,
                "size": 0,
                "mode": mode,
                "mtime_text": mtime_text,
            })

        return rows

    def list_dir(self, path):
        q = shlex.quote(path)
        cmd = (
            f'for f in {q}/.[!.]* {q}/..?* {q}/*; do '
            f'[ -e "$f" ] || [ -L "$f" ] || continue; '
            f'n="${{f##*/}}"; '
            f'if [ -d "$f" ]; then t=d; s=0; '
            f'elif [ -L "$f" ]; then '
            f'  if [ -d "$(readlink -f "$f" 2>/dev/null)" ]; then t=d; s=0; '
            f'  else t=l; s=0; fi; '
            f'else t=f; s=$(stat -c %s "$f" 2>/dev/null || echo 0); fi; '
            f'p=$(stat -c %a "$f" 2>/dev/null || echo ""); '
            f'm=$(stat -c %Y "$f" 2>/dev/null || echo 0); '
            f'printf "%s\t%s\t%s\t%s\t%s\n" "$t" "$s" "$p" "$m" "$n"; '
            f'done'
        )
        out = self.shell(cmd, timeout=60)
        rows = []
        for line in out.splitlines():
            parts = line.split("\t", 4)
            if len(parts) != 5:
                continue
            typ, size, mode, mtime, name = parts
            try:
                size = int(size)
            except Exception:
                size = 0
            rows.append({"name": name, "type": typ, "size": size, "mode": mode})
        rows.sort(key=lambda r: (0 if r["type"] == "d" else 1, r["name"].lower()))
        return rows

    def is_dir(self, path):
        """
        Return True when the remote path resolves to a directory.
        Android symlinks to directories are treated as directories too.
        """
        q = shlex.quote(path)
        out = self.shell(
            f'if [ -d {q} ]; then echo 1; else echo 0; fi',
            timeout=20,
            check=False
        )
        return out.strip().endswith("1")

    def exists(self, path):
        q = shlex.quote(path)
        out = self.shell(
            f'if [ -e {q} ] || [ -L {q} ]; then echo 1; else echo 0; fi',
            timeout=20,
            check=False
        )
        return out.strip().endswith("1")

    def copy_to(self, src, dst_path):
        """
        Copy a file/folder to an explicit destination path.
        This is used by Explorer-style paste so duplicate names can become:
          name (1).ext
          name (2).ext
        and folders:
          Folder (1)
          Folder (2)
        """
        s = shlex.quote(src)
        d = shlex.quote(dst_path)
        cmd = (
            f"cp -a {s} {d} 2>/dev/null || "
            f"cp -r {s} {d}"
        )
        self.shell(cmd, timeout=300)

    def copy(self, src, dst):
        self.shell(f"cp -a {shlex.quote(src)} {shlex.quote(dst.rstrip('/') + '/')} 2>/dev/null || cp -r {shlex.quote(src)} {shlex.quote(dst.rstrip('/') + '/')}", timeout=300)

    def delete(self, path):
        self.shell(f"rm -rf {shlex.quote(path)}", timeout=120)

    def rename(self, old, new):
        self.shell(f"mv {shlex.quote(old)} {shlex.quote(new)}", timeout=60)

    def chmod(self, path, mode, recursive=False):
        flag = "-R " if recursive else ""
        self.shell(f"chmod {flag}{mode} {shlex.quote(path)}", timeout=120)

    def get_mode(self, path):
        """Return chmod-style octal permissions for one remote path."""
        out = self.shell(
            f"stat -c %a {shlex.quote(path)} 2>/dev/null",
            timeout=30,
            check=False
        ).strip()
        if re.fullmatch(r"[0-7]{3,4}", out):
            return out
        raise ADBError(f"Could not read permissions for {path}")

    def push_exact(self, local_path, remote_path):
        """Push a local file to one exact remote path."""
        return self.run(
            self.dargs() + ["push", local_path, remote_path],
            timeout=600
        ).stdout.strip()

    def mkdir(self, path):
        self.shell(f"mkdir -p {shlex.quote(path)}")

    def push(self, local, remote_dir):
        return self.run(self.dargs() + ["push", local, remote_dir.rstrip("/") + "/"], timeout=600).stdout.strip()

    def pull(self, remote, local_dir):
        return self.run(self.dargs() + ["pull", remote, local_dir], timeout=600).stdout.strip()

    def install(self, apk):
        cp = self.run(self.dargs() + ["install", "-r", apk], timeout=600, check=False)
        text = (cp.stdout + cp.stderr).strip()
        if cp.returncode != 0 or "failure" in text.lower():
            raise ADBError(text)
        return text

    def remount_rw(self):
        output = []
        cp = self.run(self.dargs() + ["root"], timeout=30, check=False)
        txt = (cp.stdout + cp.stderr).strip()
        if txt:
            output.append(txt)
        try:
            self.run(self.dargs() + ["wait-for-device"], timeout=30)
        except Exception:
            pass

        cp = self.run(self.dargs() + ["remount"], timeout=60, check=False)
        txt = (cp.stdout + cp.stderr).strip()
        if txt:
            output.append(txt)
        if cp.returncode == 0 and "failed" not in txt.lower() and "denied" not in txt.lower():
            return "\n".join(output) or "Remount requested."

        fallback = (
            'su -c "mount -o rw,remount /" 2>/dev/null || '
            'mount -o rw,remount / 2>/dev/null || '
            'su -c "mount -o rw,remount /system" 2>/dev/null || '
            'mount -o rw,remount /system 2>/dev/null'
        )
        cp = self.run(self.dargs() + ["shell", fallback], timeout=60, check=False)
        txt = (cp.stdout + cp.stderr).strip()
        if txt:
            output.append(txt)
        return "\n".join(output) or "R/W remount requested."

    def remount_ro(self):
        cmd = (
            'su -c "mount -o ro,remount /" 2>/dev/null || '
            'mount -o ro,remount / 2>/dev/null || '
            'su -c "mount -o ro,remount /system" 2>/dev/null || '
            'mount -o ro,remount /system 2>/dev/null'
        )
        cp = self.run(self.dargs() + ["shell", cmd], timeout=60, check=False)
        return (cp.stdout + cp.stderr).strip() or "R/O remount requested."


class Worker(QThread):
    ok = Signal(object)
    fail = Signal(str)

    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def run(self):
        try:
            self.ok.emit(self.fn())
        except Exception as e:
            self.fail.emit(str(e))


class WindowsIconProvider:
    """
    Uses Windows' shell icon provider for normal file extensions.
    APK gets a dedicated Android package icon because Windows usually has
    no native .apk association.
    """
    def __init__(self):
        self.provider = QFileIconProvider()
        self.cache = {}
        self.temp_dir = tempfile.mkdtemp(prefix="android_explorer_icons_")
        self.apk_icon = self._make_apk_icon()

    def _make_apk_icon(self):
        px = QPixmap(64, 64)
        px.fill(Qt.transparent)

        painter = QPainter(px)
        painter.setRenderHint(QPainter.Antialiasing, True)

        green = QColor("#3DDC84")
        white = QColor("#ffffff")
        dark = QColor("#1d6f48")

        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(green))
        painter.drawRoundedRect(9, 14, 46, 42, 8, 8)
        painter.drawRoundedRect(13, 8, 38, 22, 12, 12)

        painter.setPen(QPen(dark, 3))
        painter.drawLine(17, 11, 12, 4)
        painter.drawLine(47, 11, 52, 4)

        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(white))
        painter.drawEllipse(21, 16, 4, 4)
        painter.drawEllipse(39, 16, 4, 4)

        painter.setBrush(QBrush(QColor("#ffffff")))
        painter.drawRoundedRect(17, 34, 30, 13, 4, 4)
        painter.setPen(QPen(QColor("#1f1f1f"), 2))
        painter.drawText(20, 44, "APK")
        painter.end()
        return QIcon(px)

    def shell_icon_for_path(self, path, fallback=None):
        """
        Ask the Windows shell for the icon associated with a real local path.
        This gives much closer Windows 11 Explorer icons for known folders
        (Downloads, Documents, Pictures, Music, Videos) and drives.
        """
        try:
            info = QFileInfo(path)
            icon = self.provider.icon(info)
            if not icon.isNull():
                return icon
        except Exception:
            pass

        if fallback is not None:
            return fallback
        return self.provider.icon(QFileIconProvider.File)

    def _make_drive_icon(self, android_badge=False):
        """
        Draw a compact Windows 11-style disk/drive icon that is guaranteed to
        render in the sidebar, even when Qt's native shell provider returns a
        null icon under portable Python/Fusion.
        """
        size = 32
        px = QPixmap(size, size)
        px.fill(Qt.transparent)

        painter = QPainter(px)
        painter.setRenderHint(QPainter.Antialiasing, True)

        # Drive body.
        body = QColor("#9faab2")
        top = QColor("#d1d8dd")
        edge = QColor("#5f6a72")
        slot = QColor("#333a3f")

        painter.setPen(QPen(edge, 1))
        painter.setBrush(QBrush(body))
        painter.drawRoundedRect(4, 11, 24, 14, 3, 3)

        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(top))
        painter.drawRoundedRect(5, 11, 22, 5, 2, 2)

        painter.setBrush(QBrush(slot))
        painter.drawRoundedRect(8, 20, 11, 2, 1, 1)

        # Small activity indicator.
        painter.setBrush(QBrush(QColor("#48b96f")))
        painter.drawEllipse(23, 20, 3, 3)

        if android_badge:
            # Small green Android badge in the lower-left corner.
            green = QColor("#3DDC84")
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(green))
            painter.drawRoundedRect(1, 18, 12, 11, 3, 3)

            painter.setBrush(QBrush(QColor("#ffffff")))
            painter.drawEllipse(4, 21, 2, 2)
            painter.drawEllipse(8, 21, 2, 2)

            painter.setPen(QPen(green, 1))
            painter.drawLine(4, 19, 2, 16)
            painter.drawLine(9, 19, 11, 16)

        painter.end()
        return QIcon(px)

    def _make_this_pc_icon(self):
        """
        Draw a small Windows 'This PC' style monitor icon for Home.
        """
        size = 32
        px = QPixmap(size, size)
        px.fill(Qt.transparent)

        painter = QPainter(px)
        painter.setRenderHint(QPainter.Antialiasing, True)

        border = QColor("#4f5d66")
        screen = QColor("#42a6d9")
        screen2 = QColor("#167fba")
        stand = QColor("#737d84")

        painter.setPen(QPen(border, 1))
        painter.setBrush(QBrush(screen))
        painter.drawRoundedRect(4, 5, 24, 17, 2, 2)

        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(screen2))
        painter.drawRect(5, 15, 22, 6)

        painter.setBrush(QBrush(stand))
        painter.drawRect(14, 22, 4, 4)
        painter.drawRoundedRect(10, 26, 12, 2, 1, 1)

        painter.end()
        return QIcon(px)

    def windows_known_folder_icons(self):
        """
        Sidebar icon set.

        Known user folders use Windows shell icons.
        Home/Internal storage/Android partitions use guaranteed Windows-like
        icons so they always appear in portable PySide6 builds.
        """
        home = str(Path.home())

        candidates = {
            "downloads": os.path.join(home, "Downloads"),
            "documents": os.path.join(home, "Documents"),
            "pictures": os.path.join(home, "Pictures"),
            "music": os.path.join(home, "Music"),
            "videos": os.path.join(home, "Videos"),
        }

        result = {}
        for key, path in candidates.items():
            result[key] = self.shell_icon_for_path(
                path,
                self.provider.icon(QFileIconProvider.Folder)
            )

        # Guaranteed visible icons.
        result["home"] = self._make_this_pc_icon()
        result["sd"] = self._make_drive_icon(android_badge=False)
        result["android_drive"] = self._make_drive_icon(android_badge=True)

        return result

    def _fixed_icon(self, icon, size=64):
        """
        Return a QIcon whose Normal/Active/Selected states all use the same
        pixmap. This prevents Qt/Fusion from tinting icons blue/grey when an
        item is hovered or selected.
        """
        if icon is None or icon.isNull():
            icon = self.provider.icon(QFileIconProvider.File)

        px = icon.pixmap(size, size)
        fixed = QIcon()
        fixed.addPixmap(px, QIcon.Normal, QIcon.Off)
        fixed.addPixmap(px, QIcon.Active, QIcon.Off)
        fixed.addPixmap(px, QIcon.Selected, QIcon.Off)
        fixed.addPixmap(px, QIcon.Disabled, QIcon.Off)
        return fixed

    def generic_file_icon(self):
        """
        One consistent generic file icon for unknown/unmapped file types.
        """
        if "_generic_file_icon" not in self.cache:
            base = self.provider.icon(QFileIconProvider.File)
            self.cache["_generic_file_icon"] = self._fixed_icon(base, 64)
        return self.cache["_generic_file_icon"]

    def known_windows_file_icon(self, ext):
        """
        Return the Windows shell icon for common/known file types.

        This mirrors Windows Explorer much more closely for familiar types
        such as JPG, PNG, ZIP, TXT, PDF, DOCX, MP3, MP4, etc., while still
        keeping obscure Android-specific extensions on the generic file icon.
        """
        if not ext:
            return None

        ext = ext.lower()
        cache_key = f"_known_{ext}"
        if cache_key in self.cache:
            return self.cache[cache_key]

        # Android-specific / misleading Windows associations should NOT be used.
        if ext in {
            ".rc", ".prop", ".conf", ".cfg", ".cnf", ".ini", ".log",
            ".bin", ".img", ".new", ".bak"
        }:
            return None

        known_exts = {
            # images
            ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".ico",
            # archives
            ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz",
            # documents / text
            ".txt", ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
            ".csv", ".rtf", ".md",
            # code / web / data
            ".py", ".js", ".ts", ".json", ".xml", ".html", ".htm", ".css", ".java",
            ".c", ".cpp", ".h", ".hpp", ".cs", ".bat", ".cmd", ".ps1", ".sh",
            # media
            ".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a",
            ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".webm", ".m4v",
        }

        if ext not in known_exts:
            return None

        try:
            fake = os.path.join(self.temp_dir, "sample" + ext)
            if not os.path.exists(fake):
                Path(fake).touch()
            icon = self.provider.icon(QFileInfo(fake))
            fixed = self._fixed_icon(icon, 64)
            self.cache[cache_key] = fixed
            return fixed
        except Exception:
            return None

    def icon_for(self, row):
        if row["type"] == "d":
            return self._fixed_icon(
                self.provider.icon(QFileIconProvider.Folder),
                64
            )

        ext = os.path.splitext(row["name"])[1].lower()

        if ext == ".apk":
            return self._fixed_icon(self.apk_icon, 64)

        # Known/common file types should mirror Windows Explorer icons.
        known = self.known_windows_file_icon(ext)
        if known is not None:
            return known

        # Everything else falls back to one consistent generic file icon.
        return self.generic_file_icon()


class AndroidIconView(QListWidget):
    """
    Icon view with true bidirectional drag/drop:

    PC -> Android:
      - dropping on empty space pushes into the current Android directory
      - dropping directly on a folder pushes into that folder

    Android -> PC:
      - selected Android files/folders are adb-pulled to a staging directory
      - a native file:// QDrag is then exposed to Windows Explorer
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.owner = None

        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setDragEnabled(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self.setDefaultDropAction(Qt.CopyAction)

        self._drag_press_pos = None
        self._drag_press_item = None
        self._android_drag_active = False

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_press_pos = event.position().toPoint()
            self._drag_press_item = self.itemAt(self._drag_press_pos)
        else:
            self._drag_press_pos = None
            self._drag_press_item = None
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if (
            self.owner
            and not self._android_drag_active
            and self._drag_press_pos is not None
            and self._drag_press_item is not None
            and (event.buttons() & Qt.LeftButton)
        ):
            distance = (
                event.position().toPoint() - self._drag_press_pos
            ).manhattanLength()

            if distance >= QApplication.startDragDistance():
                if not self._drag_press_item.isSelected():
                    self.clearSelection()
                    self._drag_press_item.setSelected(True)
                    self.setCurrentItem(self._drag_press_item)

                self._android_drag_active = True
                try:
                    self.owner.start_android_drag(self)
                finally:
                    self._android_drag_active = False
                    self._drag_press_pos = None
                    self._drag_press_item = None
                return

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        self._drag_press_pos = None
        self._drag_press_item = None

    def dragEnterEvent(self, event):
        # Windows Explorer may initially expose only the URI-list MIME type;
        # converting each QUrl to a local path during drag-enter can return
        # nothing until later in the drag. Accept the shell drag as soon as
        # Windows says it contains URLs, then validate the paths on drop.
        mime = event.mimeData()

        # Internal Android -> Android drag. This is a MOVE, matching normal
        # Explorer behaviour when dragging within the same device/filesystem.
        if mime.hasFormat(ANDROID_REMOTE_MIME):
            event.setDropAction(Qt.MoveAction)
            event.accept()
            return

        # External PC -> Android drag.
        if mime.hasUrls() or mime.hasFormat("text/uri-list"):
            event.setDropAction(Qt.CopyAction)
            event.acceptProposedAction()
            return

        event.ignore()

    def dragMoveEvent(self, event):
        mime = event.mimeData()

        if mime.hasFormat(ANDROID_REMOTE_MIME):
            event.setDropAction(Qt.MoveAction)
            event.accept()
            return

        if mime.hasUrls() or mime.hasFormat("text/uri-list"):
            event.setDropAction(Qt.CopyAction)
            event.acceptProposedAction()
            return

        event.ignore()

    def _drop_target_dir(self, pos):
        """
        Windows Explorer behaviour:
        dropping ON a folder copies into that folder;
        dropping on empty space / a file copies into the current directory.
        """
        if not self.owner:
            return "/"

        target_dir = self.owner.current_path
        item = self.itemAt(pos)

        if item is not None:
            row = item.data(Qt.UserRole)
            if row and row.get("type") == "d":
                target_dir = self.owner.full_path(row)

        return target_dir

    def dropEvent(self, event):
        if not self.owner:
            event.ignore()
            return

        mime = event.mimeData()
        target_dir = self._drop_target_dir(event.position().toPoint())

        # Android -> Android: move the remote item directly on the device.
        # Do NOT treat the staged local file:// URLs as a PC upload.
        if mime.hasFormat(ANDROID_REMOTE_MIME):
            try:
                raw = bytes(mime.data(ANDROID_REMOTE_MIME)).decode(
                    "utf-8", errors="ignore"
                )
                remote_paths = [
                    line.strip()
                    for line in raw.splitlines()
                    if line.strip()
                ]
            except Exception:
                remote_paths = []

            if remote_paths:
                self.owner.handle_internal_android_drop(
                    remote_paths,
                    target_dir
                )
                event.setDropAction(Qt.MoveAction)
                event.accept()
                return

            event.ignore()
            return

        if not (mime.hasUrls() or mime.hasFormat("text/uri-list")):
            event.ignore()
            return

        paths = []

        # Normal Qt/Windows Explorer file URLs.
        for url in event.mimeData().urls():
            local = url.toLocalFile()
            if local:
                paths.append(local)

        # Fallback for shells that expose URI-list text but don't populate
        # QMimeData.urls() reliably at drag-enter time.
        if not paths and event.mimeData().hasFormat("text/uri-list"):
            try:
                raw = bytes(event.mimeData().data("text/uri-list")).decode(
                    "utf-8", errors="ignore"
                )
                for line in raw.splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    url = QUrl(line)
                    if url.isLocalFile():
                        local = url.toLocalFile()
                        if local:
                            paths.append(local)
            except Exception:
                pass

        paths = [os.path.normpath(p) for p in paths if p and os.path.exists(p)]

        if not paths:
            event.ignore()
            return

        self.owner.handle_local_drop(paths, target_dir=target_dir)

        event.setDropAction(Qt.CopyAction)
        event.acceptProposedAction()

    def startDrag(self, supportedActions):
        if not self.owner:
            super().startDrag(supportedActions)
            return

        # Force Copy semantics for Android -> desktop. The owner pulls the
        # selected Android items to a local staging folder, then starts a
        # native local-file drag that Windows Explorer understands.
        self.owner.start_android_drag(self)




class BreadcrumbBar(QFrame):
    """
    Windows Explorer-style clickable breadcrumb address bar.
    Shows:
        [This PC icon] > sdcard > Download > ...
    Each segment is clickable and navigates to that Android path.

    Clicking empty space switches to an editable full-path field.
    """

    navigateRequested = Signal(str)

    def __init__(self, owner=None, parent=None):
        super().__init__(parent)
        self.owner = owner
        self.current_path = "/"

        self.setObjectName("BreadcrumbBar")

        self.stack = QStackedWidget(self)
        self.stack.setObjectName("BreadcrumbStack")

        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self.stack)

        # Breadcrumb display page.
        self.crumb_page = QWidget()
        self.crumb_layout = QHBoxLayout(self.crumb_page)
        self.crumb_layout.setContentsMargins(8, 0, 6, 0)
        self.crumb_layout.setSpacing(1)
        self.crumb_layout.addStretch(1)
        self.stack.addWidget(self.crumb_page)

        # Editable path page.
        self.edit = QLineEdit("/")
        self.edit.setObjectName("AddressEdit")
        self.edit.returnPressed.connect(self.commit_edit)
        self.edit.editingFinished.connect(self.cancel_edit_if_needed)
        self.stack.addWidget(self.edit)

        self.stack.setCurrentWidget(self.crumb_page)

    def set_path(self, path):
        path = (path or "/").strip()
        if not path.startswith("/"):
            path = "/" + path
        self.current_path = path.rstrip("/") or "/"
        self.edit.setText(self.current_path)
        self.rebuild()

    def clear_crumbs(self):
        while self.crumb_layout.count():
            item = self.crumb_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()

    def rebuild(self):
        self.clear_crumbs()

        # This PC/root icon segment.
        root_btn = QToolButton()
        root_btn.setObjectName("BreadcrumbButton")
        root_btn.setAutoRaise(True)
        root_btn.setToolButtonStyle(Qt.ToolButtonIconOnly)
        if self.owner is not None:
            root_btn.setIcon(self.owner.icon_provider._make_this_pc_icon())
        root_btn.setIconSize(QSize(18, 18))
        root_btn.setFixedSize(28, 30)
        root_btn.setToolTip("/")
        root_btn.clicked.connect(lambda: self.navigateRequested.emit("/"))
        self.crumb_layout.addWidget(root_btn)

        parts = [p for p in self.current_path.split("/") if p]
        built = ""

        for part in parts:
            chevron = QLabel("›")
            chevron.setObjectName("BreadcrumbChevron")
            chevron.setAlignment(Qt.AlignCenter)
            chevron.setFixedWidth(16)
            self.crumb_layout.addWidget(chevron)

            built += "/" + part
            btn = QToolButton()
            btn.setObjectName("BreadcrumbButton")
            btn.setAutoRaise(True)
            btn.setText(part)
            btn.setToolButtonStyle(Qt.ToolButtonTextOnly)
            btn.setToolTip(built)
            btn.clicked.connect(
                lambda checked=False, p=built: self.navigateRequested.emit(p)
            )
            self.crumb_layout.addWidget(btn)

        self.crumb_layout.addStretch(1)

    def mouseDoubleClickEvent(self, event):
        self.begin_edit()
        super().mouseDoubleClickEvent(event)

    def mousePressEvent(self, event):
        # Clicking empty space at the right end behaves like Windows Explorer:
        # switch to the editable full path.
        child = self.childAt(event.position().toPoint())
        if child in (self, self.crumb_page, self.stack):
            self.begin_edit()
        super().mousePressEvent(event)

    def begin_edit(self):
        self.edit.setText(self.current_path)
        self.stack.setCurrentWidget(self.edit)
        self.edit.setFocus()
        self.edit.selectAll()

    def commit_edit(self):
        path = self.edit.text().strip() or "/"
        self.stack.setCurrentWidget(self.crumb_page)
        self.navigateRequested.emit(path)

    def cancel_edit_if_needed(self):
        if self.stack.currentWidget() is self.edit:
            self.stack.setCurrentWidget(self.crumb_page)
            self.edit.setText(self.current_path)

class AndroidDetailsView(QTreeWidget):
    """
    Details view with the same bidirectional drag/drop behaviour as Icons view.
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.owner = None

        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setDragEnabled(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self.setDefaultDropAction(Qt.CopyAction)

        self._drag_press_pos = None
        self._drag_press_item = None
        self._android_drag_active = False

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_press_pos = event.position().toPoint()
            self._drag_press_item = self.itemAt(self._drag_press_pos)
        else:
            self._drag_press_pos = None
            self._drag_press_item = None
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if (
            self.owner
            and not self._android_drag_active
            and self._drag_press_pos is not None
            and self._drag_press_item is not None
            and (event.buttons() & Qt.LeftButton)
        ):
            distance = (
                event.position().toPoint() - self._drag_press_pos
            ).manhattanLength()

            if distance >= QApplication.startDragDistance():
                if not self._drag_press_item.isSelected():
                    self.clearSelection()
                    self._drag_press_item.setSelected(True)
                    self.setCurrentItem(self._drag_press_item)

                self._android_drag_active = True
                try:
                    self.owner.start_android_drag(self)
                finally:
                    self._android_drag_active = False
                    self._drag_press_pos = None
                    self._drag_press_item = None
                return

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        self._drag_press_pos = None
        self._drag_press_item = None

    def dragEnterEvent(self, event):
        # Windows Explorer may initially expose only the URI-list MIME type;
        # converting each QUrl to a local path during drag-enter can return
        # nothing until later in the drag. Accept the shell drag as soon as
        # Windows says it contains URLs, then validate the paths on drop.
        mime = event.mimeData()

        # Internal Android -> Android drag. This is a MOVE, matching normal
        # Explorer behaviour when dragging within the same device/filesystem.
        if mime.hasFormat(ANDROID_REMOTE_MIME):
            event.setDropAction(Qt.MoveAction)
            event.accept()
            return

        # External PC -> Android drag.
        if mime.hasUrls() or mime.hasFormat("text/uri-list"):
            event.setDropAction(Qt.CopyAction)
            event.acceptProposedAction()
            return

        event.ignore()

    def dragMoveEvent(self, event):
        mime = event.mimeData()

        if mime.hasFormat(ANDROID_REMOTE_MIME):
            event.setDropAction(Qt.MoveAction)
            event.accept()
            return

        if mime.hasUrls() or mime.hasFormat("text/uri-list"):
            event.setDropAction(Qt.CopyAction)
            event.acceptProposedAction()
            return

        event.ignore()

    def _drop_target_dir(self, pos):
        if not self.owner:
            return "/"

        target_dir = self.owner.current_path
        item = self.itemAt(pos)

        if item is not None:
            row = item.data(0, Qt.UserRole)
            if row and row.get("type") == "d":
                target_dir = self.owner.full_path(row)

        return target_dir

    def dropEvent(self, event):
        if not self.owner:
            event.ignore()
            return

        mime = event.mimeData()
        target_dir = self._drop_target_dir(event.position().toPoint())

        # Android -> Android: move the remote item directly on the device.
        # Do NOT treat the staged local file:// URLs as a PC upload.
        if mime.hasFormat(ANDROID_REMOTE_MIME):
            try:
                raw = bytes(mime.data(ANDROID_REMOTE_MIME)).decode(
                    "utf-8", errors="ignore"
                )
                remote_paths = [
                    line.strip()
                    for line in raw.splitlines()
                    if line.strip()
                ]
            except Exception:
                remote_paths = []

            if remote_paths:
                self.owner.handle_internal_android_drop(
                    remote_paths,
                    target_dir
                )
                event.setDropAction(Qt.MoveAction)
                event.accept()
                return

            event.ignore()
            return

        if not (mime.hasUrls() or mime.hasFormat("text/uri-list")):
            event.ignore()
            return

        paths = []

        # Normal Qt/Windows Explorer file URLs.
        for url in event.mimeData().urls():
            local = url.toLocalFile()
            if local:
                paths.append(local)

        # Fallback for shells that expose URI-list text but don't populate
        # QMimeData.urls() reliably at drag-enter time.
        if not paths and event.mimeData().hasFormat("text/uri-list"):
            try:
                raw = bytes(event.mimeData().data("text/uri-list")).decode(
                    "utf-8", errors="ignore"
                )
                for line in raw.splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    url = QUrl(line)
                    if url.isLocalFile():
                        local = url.toLocalFile()
                        if local:
                            paths.append(local)
            except Exception:
                pass

        paths = [os.path.normpath(p) for p in paths if p and os.path.exists(p)]

        if not paths:
            event.ignore()
            return

        self.owner.handle_local_drop(paths, target_dir=target_dir)

        event.setDropAction(Qt.CopyAction)
        event.acceptProposedAction()

    def startDrag(self, supportedActions):
        if not self.owner:
            super().startDrag(supportedActions)
            return

        self.owner.start_android_drag(self)




class Explorer(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.setAcceptDrops(True)

        icon_path = find_app_icon_path()
        if icon_path:
            try:
                icon = QIcon(icon_path)
                if not icon.isNull():
                    self.setWindowIcon(icon)
                    app = QApplication.instance()
                    if app is not None:
                        app.setWindowIcon(icon)
            except Exception:
                pass

        # Windows uses our Explorer-style custom title/tab strip.
        # Linux keeps the native window frame so dragging, resizing and the
        # desktop environment's own snapping/docking controls work correctly.
        if os.name == "nt":
            self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint)
        else:
            self.setWindowFlags(Qt.Window)
        self.resize(1050, 680)

        self.adb = ADB()

        # Persistent UI preferences for this Windows user.
        self.settings = QSettings("RDSoft", "AndroidExplorer")

        self.theme = self.settings.value("theme", "light", type=str)
        if self.theme not in ("light", "dark"):
            self.theme = "light"

        self.current_path = "/"
        self.clipboard = []
        self.worker = None
        self.icon_provider = WindowsIconProvider()

        saved_adb_path = self.settings.value("adb_path", "", type=str)
        if saved_adb_path and os.path.exists(saved_adb_path):
            self.adb.path = saved_adb_path

        # Remote-control backend (scrcpy).
        self.scrcpy_path = self.settings.value("scrcpy_path", "", type=str)
        self.remote_process = None

        # Fast directory navigation cache: {(serial, path): rows}
        self.dir_cache = {}
        self.current_rows = []
        self.tabs = []
        self.active_tab = 0
        self.transparency_enabled = False

        # Background path cache:
        #   / immediate children only
        #   /system, /data, /vendor, /oem, /odm recursively
        self.path_cache = set()
        self.precache_by_parent = {}
        self.precache_complete_for_serial = set()
        self.cache_worker = None
        self.cache_stage_queue = []
        self.cache_stage_index = 0
        self.view_mode = self.settings.value("view_mode", "icons", type=str)
        if self.view_mode not in ("icons", "details"):
            self.view_mode = "icons"
        self._closing = False
        self.drag_temp_dir = tempfile.mkdtemp(prefix="android_explorer_drag_")
        self.open_file_temp_dir = tempfile.mkdtemp(prefix="android_explorer_open_")
        self.open_file_workers = set()

        # Keep cached paths synchronized with device-side additions/deletions.
        self.cache_sync_timer = QTimer(self)
        self.cache_sync_timer.setInterval(120000)  # 2 minutes
        self.cache_sync_timer.timeout.connect(self.sync_device_cache)
        self.cache_sync_timer.start()

        self.build_ui()
        self._install_shortcuts()

        saved_geometry = self.settings.value("window_geometry")
        if saved_geometry is not None:
            try:
                self.restoreGeometry(saved_geometry)
            except Exception:
                pass

        self.heading.setVisible(self.view_mode == "icons")
        self.view_stack.setCurrentIndex(0 if self.view_mode == "icons" else 1)

        self.tabs = [{"path": "/", "title": "Home"}]
        self.active_tab = 0
        self.rebuild_tabs()
        self.apply_theme()
        self.refresh_devices()

        # winId() creates the native HWND. Once it exists, restore the
        # Windows style flags needed for native Snap/docking behaviour.
        self.enable_windows_snap_features()
        apply_dwm_backdrop(int(self.winId()), dark=(self.theme == "dark"))

        if self.view_mode == "details":
            QTimer.singleShot(250, self.refresh_current_metadata_background)

    def _toolbar_icon(self, kind):
        """Draw small Windows-11-style command bar glyphs."""
        size = 22
        px = QPixmap(size, size)
        px.fill(Qt.transparent)

        p = QPainter(px)
        p.setRenderHint(QPainter.Antialiasing, True)
        # Windows 11 command-bar glyph contrast.
        # Light mode intentionally uses a stronger charcoal outline so icons
        # stay crisp on the pale command bar instead of looking washed out.
        fg = QColor("#e6e6e6" if self.theme == "dark" else "#202020")
        accent = QColor("#6cc4f0" if self.theme == "dark" else "#0067c0")
        pen = QPen(fg, 1.65)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)

        if kind == "new":
            p.setPen(QPen(accent, 1.5))
            p.drawEllipse(3, 3, 16, 16)
            p.drawLine(11, 7, 11, 15)
            p.drawLine(7, 11, 15, 11)

        elif kind == "cut":
            p.setPen(QPen(accent, 1.6))
            p.drawEllipse(3, 4, 5, 5)
            p.drawEllipse(3, 13, 5, 5)
            p.drawLine(7, 8, 18, 17)
            p.drawLine(7, 14, 18, 5)

        elif kind == "copy":
            p.setPen(QPen(accent, 1.6))
            p.drawRoundedRect(7, 4, 10, 12, 1.5, 1.5)
            p.drawRoundedRect(4, 7, 10, 12, 1.5, 1.5)

        elif kind == "paste":
            p.setPen(QPen(fg, 1.6))
            p.drawRoundedRect(6, 6, 11, 13, 1.5, 1.5)
            p.drawRoundedRect(8, 3, 7, 5, 1.5, 1.5)

        elif kind == "rename":
            p.setPen(QPen(accent, 1.6))
            p.drawRoundedRect(4, 5, 14, 12, 1.5, 1.5)
            p.drawLine(8, 8, 8, 14)
            p.drawLine(6, 8, 10, 8)
            p.drawLine(6, 14, 10, 14)

        elif kind == "share":
            p.setPen(QPen(accent, 1.6))
            p.drawRoundedRect(4, 9, 12, 9, 1.5, 1.5)
            p.drawLine(10, 12, 18, 4)
            p.drawLine(13, 4, 18, 4)
            p.drawLine(18, 4, 18, 9)

        elif kind == "delete":
            p.setPen(QPen(fg, 1.65))
            p.drawRoundedRect(7, 7, 9, 11, 1.5, 1.5)
            p.drawLine(5, 6, 18, 6)
            p.drawLine(9, 3, 14, 3)
            p.drawLine(10, 9, 10, 16)
            p.drawLine(13, 9, 13, 16)

        elif kind == "sort":
            p.setPen(QPen(accent, 1.6))
            p.drawLine(5, 5, 5, 17)
            p.drawLine(5, 5, 2, 8)
            p.drawLine(5, 5, 8, 8)
            p.drawLine(12, 7, 19, 7)
            p.drawLine(12, 11, 17, 11)
            p.drawLine(12, 15, 15, 15)

        elif kind == "view":
            p.setPen(QPen(fg, 1.6))
            for y in (5, 10, 15):
                p.drawLine(5, y, 18, y)
                p.drawEllipse(2, y - 1, 2, 2)

        elif kind == "remote":
            p.setPen(QPen(accent, 1.6))
            p.drawRoundedRect(3, 4, 16, 11, 1.5, 1.5)
            p.drawLine(8, 18, 14, 18)
            p.drawLine(11, 15, 11, 18)
            p.setPen(Qt.NoPen)
            p.setBrush(accent)
            p.drawEllipse(13, 8, 3, 3)

        elif kind == "more":
            p.setPen(Qt.NoPen)
            p.setBrush(fg)
            for x in (6, 11, 16):
                p.drawEllipse(x - 1.5, 9.5, 3, 3)

        p.end()
        return QIcon(px)

    def _toolbar_separator(self):
        line = QFrame()
        line.setObjectName("CommandSeparator")
        line.setFrameShape(QFrame.VLine)
        line.setFixedHeight(24)
        line.setFixedWidth(9)
        return line

    def show_new_menu(self):
        menu = QMenu(self)
        folder = menu.addAction("New folder")
        install = menu.addAction("Install APK...")
        pos = self.new_button.mapToGlobal(self.new_button.rect().bottomLeft())
        action = menu.exec(pos)
        if action == folder:
            self.new_folder()
        elif action == install:
            self.install_apk()

    def show_sort_menu(self):
        menu = QMenu(self)
        by_name = menu.addAction("Name")
        by_type = menu.addAction("Type")
        by_size = menu.addAction("Size")
        by_date = menu.addAction("Date modified")
        menu.addSeparator()
        ascending = menu.addAction("Ascending")
        descending = menu.addAction("Descending")

        pos = self.sort_button.mapToGlobal(self.sort_button.rect().bottomLeft())
        action = menu.exec(pos)
        if action is None:
            return

        if not hasattr(self, "_sort_key"):
            self._sort_key = "name"
        if not hasattr(self, "_sort_reverse"):
            self._sort_reverse = False

        if action == by_name:
            self._sort_key = "name"
        elif action == by_type:
            self._sort_key = "type"
        elif action == by_size:
            self._sort_key = "size"
        elif action == by_date:
            self._sort_key = "date"
        elif action == ascending:
            self._sort_reverse = False
        elif action == descending:
            self._sort_reverse = True

        self.apply_current_sort()

    def apply_current_sort(self):
        rows = list(getattr(self, "current_rows", []) or [])
        if not rows:
            return

        key = getattr(self, "_sort_key", "name")
        reverse = getattr(self, "_sort_reverse", False)

        def sort_value(row):
            if key == "type":
                return self.row_type_text(row).lower()
            if key == "size":
                return int(row.get("size", 0) or 0)
            if key == "date":
                return row.get("mtime_text", "")
            return row.get("name", "").lower()

        # Keep folders first, like Explorer.
        rows.sort(
            key=lambda r: (
                0 if r.get("type") == "d" else 1,
                sort_value(r)
            ),
            reverse=reverse
        )
        self.render_rows(self.current_path, rows)

    def find_scrcpy(self):
        """
        Locate scrcpy on Windows/Linux/macOS.

        Search order:
        1. Saved Settings path
        2. Beside android_explorer.py
        3. ./scrcpy/scrcpy.exe (or ./scrcpy/scrcpy)
        4. Any nested scrcpy executable inside ./scrcpy
        5. System PATH
        """
        candidates = []

        if self.scrcpy_path:
            candidates.append(self.scrcpy_path)

        script_dir = os.path.dirname(os.path.abspath(sys.argv[0]))
        exe_name = "scrcpy.exe" if os.name == "nt" else "scrcpy"

        candidates.extend([
            os.path.join(script_dir, exe_name),
            os.path.join(script_dir, "scrcpy", exe_name),
        ])

        # Be tolerant of a portable ZIP extracted with an extra version folder,
        # e.g. scrcpy\scrcpy-win64-v4.1\scrcpy.exe.
        scrcpy_root = os.path.join(script_dir, "scrcpy")
        if os.path.isdir(scrcpy_root):
            try:
                for root, dirs, files in os.walk(scrcpy_root):
                    # Keep this bounded; the portable package is shallow.
                    rel = os.path.relpath(root, scrcpy_root)
                    depth = 0 if rel == "." else rel.count(os.sep) + 1
                    if depth > 2:
                        dirs[:] = []
                        continue

                    if exe_name in files:
                        candidates.append(os.path.join(root, exe_name))
            except Exception:
                pass

        path_hit = shutil.which("scrcpy")
        if path_hit:
            candidates.append(path_hit)

        for candidate in candidates:
            if candidate and os.path.isfile(candidate):
                resolved = os.path.abspath(candidate)

                # Remember the working path automatically.
                if resolved != self.scrcpy_path:
                    self.scrcpy_path = resolved
                    self.settings.setValue("scrcpy_path", resolved)
                    self.settings.sync()

                return resolved

        return None

    def choose_scrcpy(self):
        filename, _ = QFileDialog.getOpenFileName(
            self,
            "Select scrcpy executable",
            "",
            "scrcpy executable (scrcpy.exe);;All files (*.*)"
            if os.name == "nt"
            else "scrcpy executable (scrcpy);;All files (*)"
        )
        if not filename:
            return None

        self.scrcpy_path = filename
        self.settings.setValue("scrcpy_path", filename)
        self.settings.sync()
        return filename

    def open_remote_control(self):
        """
        Open a live interactive mirror of the selected Android device.

        scrcpy supplies the low-latency stream and input injection:
        mouse click -> touch, drag -> swipe, wheel -> scroll, keyboard -> device.
        """
        if not self.adb.serial:
            self.info_dialog("Connect to an Android device first.")
            return

        if self.remote_process is not None:
            try:
                if self.remote_process.poll() is None:
                    self.info_dialog("Remote Control is already open.")
                    return
            except Exception:
                pass
            self.remote_process = None

        scrcpy = self.find_scrcpy()
        if not scrcpy:
            result = self.message_dialog(
                "scrcpy.exe was not found.\n\n"
                "Remote Control uses scrcpy to stream the device screen and "
                "send mouse/touch/keyboard input.\n\n"
                "Select scrcpy.exe now?",
                QMessageBox.Information,
                QMessageBox.Yes | QMessageBox.No
            )
            if result != QMessageBox.Yes:
                return

            scrcpy = self.choose_scrcpy()
            if not scrcpy:
                return

        serial = self.adb.serial
        args = [
            scrcpy,
            "--serial", serial,
            "--window-title", f"Android Remote - {serial}",
            "--stay-awake",
        ]

        try:
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            self.remote_process = subprocess.Popen(
                args,
                cwd=os.path.dirname(scrcpy) or None,
                creationflags=creationflags
            )
            self.status.setText(f"Remote Control opened • {serial}")
        except Exception as exc:
            self.remote_process = None
            self.error_dialog(f"Could not start Remote Control.\n\n{exc}")

    def show_more_menu(self):
        menu = QMenu(self)
        push = menu.addAction("Push file/folder...")
        pull = menu.addAction("Pull selected to PC...")
        permissions = menu.addAction("Permissions...")
        menu.addSeparator()
        connect_ip = menu.addAction("Connect by IP...")
        remote_control = menu.addAction("Remote Control")
        menu.addSeparator()
        rw = menu.addAction("Remount R/W")
        ro = menu.addAction("Remount R/O")
        menu.addSeparator()
        settings_action = menu.addAction("Settings")

        pos = self.more_button.mapToGlobal(self.more_button.rect().bottomLeft())
        action = menu.exec(pos)

        if action == push:
            self.push_file()
        elif action == pull:
            self.pull_selected()
        elif action == permissions:
            self.permissions()
        elif action == connect_ip:
            self.connect_ip()
        elif action == remote_control:
            self.open_remote_control()
        elif action == rw:
            self.remount_rw()
        elif action == ro:
            self.remount_ro()
        elif action == settings_action:
            # Re-open the full settings menu from the same toolbar anchor.
            QTimer.singleShot(
                0,
                lambda p=pos: self.show_settings_menu(p)
            )

    def _nav_icon(self, kind):
        """Draw larger Windows 11-style navigation glyphs."""
        size = 24
        px = QPixmap(size, size)
        px.fill(Qt.transparent)

        painter = QPainter(px)
        painter.setRenderHint(QPainter.Antialiasing, True)

        fg = QColor("#f0f0f0" if self.theme == "dark" else "#333333")
        pen = QPen(fg, 1.6)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)

        if kind == "back":
            painter.drawLine(15, 6, 9, 12)
            painter.drawLine(9, 12, 15, 18)
        elif kind == "forward":
            painter.drawLine(9, 6, 15, 12)
            painter.drawLine(15, 12, 9, 18)
        elif kind == "up":
            painter.drawLine(7, 14, 12, 9)
            painter.drawLine(12, 9, 17, 14)
            painter.drawLine(12, 9, 12, 18)
        elif kind == "refresh":
            painter.drawArc(6, 6, 12, 12, 35 * 16, 285 * 16)
            painter.drawLine(17, 6, 17, 11)
            painter.drawLine(17, 6, 12, 6)

        painter.end()
        return QIcon(px)

    def _window_control_icon(self, kind):
        """
        Windows 11-like caption glyphs.

        Drawn as thin 1px geometry at native caption proportions rather than
        using font characters, which vary significantly between systems.
        """
        size = 16
        px = QPixmap(size, size)
        px.fill(Qt.transparent)

        painter = QPainter(px)
        painter.setRenderHint(QPainter.Antialiasing, False)

        fg = QColor("#f5f5f5" if self.theme == "dark" else "#1f1f1f")
        pen = QPen(fg, 1.0)
        pen.setCapStyle(Qt.SquareCap)
        pen.setJoinStyle(Qt.MiterJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)

        if kind == "minimize":
            # Windows caption minimize is a short, thin centered baseline.
            painter.drawLine(4, 10, 12, 10)

        elif kind == "maximize":
            # Thin 8x8 square.
            painter.drawRect(4, 4, 8, 8)

        elif kind == "restore":
            # Two thin overlapping 7x7 squares.
            painter.drawRect(6, 3, 7, 7)
            painter.drawLine(3, 6, 10, 6)
            painter.drawLine(3, 6, 3, 13)
            painter.drawLine(3, 13, 10, 13)
            painter.drawLine(10, 6, 10, 13)

        elif kind == "close":
            # Windows uses a noticeably larger X than our previous version.
            painter.setRenderHint(QPainter.Antialiasing, True)
            pen = QPen(fg, 1.15)
            pen.setCapStyle(Qt.FlatCap)
            painter.setPen(pen)
            painter.drawLine(3, 3, 13, 13)
            painter.drawLine(13, 3, 3, 13)

        painter.end()
        return QIcon(px)

    def refresh_window_control_icons(self):
        if hasattr(self, "min_button"):
            self.min_button.setIcon(self._window_control_icon("minimize"))
            self.min_button.setIconSize(QSize(16, 16))
        if hasattr(self, "max_button"):
            kind = "restore" if self.isMaximized() else "maximize"
            self.max_button.setIcon(self._window_control_icon(kind))
            self.max_button.setIconSize(QSize(16, 16))
        if hasattr(self, "close_button"):
            self.close_button.setIcon(self._window_control_icon("close"))
            self.close_button.setIconSize(QSize(18, 18))

    def refresh_nav_icons(self):
        mappings = [
            ("back_button", "back"),
            ("forward_button", "forward"),
            ("up_button", "up"),
            ("refresh_button", "refresh"),
        ]
        for attr, kind in mappings:
            button = getattr(self, attr, None)
            if button is not None:
                button.setIcon(self._nav_icon(kind))

    def toggle_maximize_restore(self):
        if self.isMaximized():
            self.showNormal()
            self.max_button.setToolTip("Maximize")
            self.max_button.setIcon(self._window_control_icon("maximize"))
        else:
            self.showMaximized()
            self.max_button.setToolTip("Restore Down")
            self.max_button.setIcon(self._window_control_icon("restore"))

    def changeEvent(self, event):
        super().changeEvent(event)
        try:
            if event.type() == event.Type.WindowStateChange:
                self.enable_windows_snap_features()

            if event.type() == event.Type.WindowStateChange and hasattr(self, "max_button"):
                if self.isMaximized():
                    self.max_button.setToolTip("Restore Down")
                    self.max_button.setIcon(self._window_control_icon("restore"))
                else:
                    self.max_button.setToolTip("Maximize")
                    self.max_button.setIcon(self._window_control_icon("maximize"))
        except Exception:
            pass

    def enable_windows_snap_features(self):
        """
        Restore the native Windows window-style capabilities used by Snap,
        docking, Win+Arrow, edge snapping and the Windows 11 Snap Layout menu.

        We intentionally do NOT add WS_CAPTION, so the custom Explorer-style
        tab/title bar remains visible instead of the normal Windows title bar.
        """
        if os.name != "nt":
            return

        try:
            import ctypes

            hwnd = int(self.winId())
            user32 = ctypes.windll.user32

            GWL_STYLE = -16

            WS_CAPTION = 0x00C00000
            WS_THICKFRAME = 0x00040000
            WS_SYSMENU = 0x00080000
            WS_MINIMIZEBOX = 0x00020000
            WS_MAXIMIZEBOX = 0x00010000

            SWP_NOSIZE = 0x0001
            SWP_NOMOVE = 0x0002
            SWP_NOZORDER = 0x0004
            SWP_NOACTIVATE = 0x0010
            SWP_FRAMECHANGED = 0x0020

            get_style = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
            set_style = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)

            style = get_style(hwnd, GWL_STYLE)
            # WS_CAPTION is intentionally restored here. Windows 11 uses
            # the normal overlapped-window capability bits to decide whether
            # the window participates in native Snap / docking UI.
            #
            # We still keep the visible native title bar removed by handling
            # WM_NCCALCSIZE in nativeEvent(), so our custom Explorer-style
            # title/tab strip remains the only title bar the user sees.
            style |= (
                WS_CAPTION
                | WS_THICKFRAME
                | WS_SYSMENU
                | WS_MINIMIZEBOX
                | WS_MAXIMIZEBOX
            )

            set_style(hwnd, GWL_STYLE, style)

            # Tell Windows to recalculate non-client capabilities without
            # restoring the standard caption/title bar.
            user32.SetWindowPos(
                hwnd,
                0,
                0, 0, 0, 0,
                SWP_NOMOVE
                | SWP_NOSIZE
                | SWP_NOZORDER
                | SWP_NOACTIVATE
                | SWP_FRAMECHANGED
            )
        except Exception:
            pass

    def _is_titlebar_interactive_widget(self, widget):
        """
        Tabs, + and window-control buttons must remain clickable.
        Every other empty part of the tab strip is treated as the native
        title-bar drag region.
        """
        interactive = {
            "TabContainer",
            "TabMain",
            "TabClose",
            "AddTabButton",
            "WindowControlButton",
            "WindowCloseButton",
        }
        w = widget
        while w is not None and w is not self:
            try:
                if w.objectName() in interactive:
                    return True
            except Exception:
                pass
            w = w.parentWidget()
        return False

    def nativeEvent(self, eventType, message):
        """
        Windows hit testing for the frameless Explorer shell.

        - window edges/corners remain natively resizable
        - empty tab-strip area behaves as HTCAPTION, so Windows performs
          normal dragging, Aero Snap, double-click maximize, etc.
        - actual tabs/+ /min/max/close remain normal clickable client controls
        """
        if os.name == "nt":
            try:
                import ctypes
                from ctypes import wintypes
                from PySide6.QtCore import QPoint

                WM_NCCALCSIZE = 0x0083
                WM_NCHITTEST = 0x0084
                WM_NCLBUTTONUP = 0x00A2
                WM_NCLBUTTONDBLCLK = 0x00A3

                HTCLIENT = 1
                HTCAPTION = 2
                HTMAXBUTTON = 9
                HTLEFT = 10
                HTRIGHT = 11
                HTTOP = 12
                HTTOPLEFT = 13
                HTTOPRIGHT = 14
                HTBOTTOM = 15
                HTBOTTOMLEFT = 16
                HTBOTTOMRIGHT = 17

                msg = wintypes.MSG.from_address(int(message))

                # Keep the native WS_CAPTION capability bit (required for
                # Windows 11 Snap/docking behaviour), but remove the actual
                # non-client title-bar/frame area so our custom tab strip
                # remains edge-to-edge.
                if msg.message == WM_NCCALCSIZE and msg.wParam:
                    return True, 0

                # HTMAXBUTTON turns our custom maximize region into a native
                # non-client maximize target so Windows 11 can show Snap Layouts.
                # Handle the released click ourselves to keep our custom button
                # and icon state in sync.
                if msg.message == WM_NCLBUTTONUP and msg.wParam == HTMAXBUTTON:
                    QTimer.singleShot(0, self.toggle_maximize_restore)
                    return True, 0

                if msg.message == WM_NCLBUTTONDBLCLK and msg.wParam == HTMAXBUTTON:
                    return True, 0

                if msg.message == WM_NCHITTEST:
                    # Signed 16-bit screen coordinates from LPARAM.
                    sx = ctypes.c_short(msg.lParam & 0xFFFF).value
                    sy = ctypes.c_short((msg.lParam >> 16) & 0xFFFF).value

                    pos = self.mapFromGlobal(QPoint(sx, sy))
                    x, y = pos.x(), pos.y()
                    w, h = self.width(), self.height()

                    # First resolve the custom title/tab strip. This is
                    # deliberately BEFORE the top resize-border test: otherwise
                    # the top few pixels become HTTOP instead of HTCAPTION and
                    # Windows does not treat the movement as a normal title-bar
                    # drag when the window reaches the top of the screen.
                    in_tab_strip = False
                    if hasattr(self, "tab_strip"):
                        strip_pos = self.tab_strip.mapFromGlobal(QPoint(sx, sy))
                        in_tab_strip = self.tab_strip.rect().contains(strip_pos)

                    # Keep native resize borders when not maximized. The top
                    # border is excluded while inside our custom title strip.
                    if not self.isMaximized():
                        border = 7
                        left = x < border
                        right = x >= w - border
                        top = y < border and not in_tab_strip
                        bottom = y >= h - border

                        if top and left:
                            return True, HTTOPLEFT
                        if top and right:
                            return True, HTTOPRIGHT
                        if bottom and left:
                            return True, HTBOTTOMLEFT
                        if bottom and right:
                            return True, HTBOTTOMRIGHT
                        if left:
                            return True, HTLEFT
                        if right:
                            return True, HTRIGHT
                        if top:
                            return True, HTTOP
                        if bottom:
                            return True, HTBOTTOM

                    # Windows 11 Snap Layout support:
                    # tell Windows that our custom maximize control occupies
                    # the native maximize-button hit-test region.
                    if hasattr(self, "max_button"):
                        max_pos = self.max_button.mapFromGlobal(QPoint(sx, sy))
                        if self.max_button.rect().contains(max_pos):
                            return True, HTMAXBUTTON

                    if in_tab_strip:
                        child = self.tab_strip.childAt(strip_pos)

                        # Empty tab-strip background = a genuine native caption
                        # drag. Windows now owns the move loop, which restores:
                        # - top-edge Snap Layout bar
                        # - left/right edge docking previews
                        # - drag-to-top maximize
                        # - Snap Assist after docking
                        if not self._is_titlebar_interactive_widget(child):
                            return True, HTCAPTION

            except Exception:
                pass

        return super().nativeEvent(eventType, message)

    def dragEnterEvent(self, event):
        mime = event.mimeData()
        if mime.hasUrls() or mime.hasFormat("text/uri-list"):
            event.setDropAction(Qt.CopyAction)
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dropEvent(self, event):
        mime = event.mimeData()

        if not (mime.hasUrls() or mime.hasFormat("text/uri-list")):
            super().dropEvent(event)
            return

        paths = []
        for url in mime.urls():
            local = url.toLocalFile()
            if local:
                paths.append(local)

        if not paths and mime.hasFormat("text/uri-list"):
            try:
                raw = bytes(mime.data("text/uri-list")).decode(
                    "utf-8", errors="ignore"
                )
                for line in raw.splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    url = QUrl(line)
                    if url.isLocalFile():
                        local = url.toLocalFile()
                        if local:
                            paths.append(local)
            except Exception:
                pass

        paths = [os.path.normpath(p) for p in paths if p and os.path.exists(p)]

        if paths:
            self.handle_local_drop(paths, target_dir=self.current_path)
            event.setDropAction(Qt.CopyAction)
            event.acceptProposedAction()
            return

        event.ignore()

    def build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Top/title area - intentionally mirrors Explorer.
        top = QWidget()
        top.setObjectName("TopArea")
        top_l = QVBoxLayout(top)
        top_l.setContentsMargins(0, 0, 0, 0)
        top_l.setSpacing(0)

        self.tab_strip = QWidget()
        self.tab_strip.setObjectName("TabStrip")
        self.tab_strip.setFixedHeight(44)

        # Outer strip has NO top/right margin so caption controls touch the
        # physical top/right edges exactly like Windows Explorer.
        tabrow = QHBoxLayout(self.tab_strip)
        tabrow.setContentsMargins(0, 0, 0, 0)
        tabrow.setSpacing(0)

        # Tabs themselves keep a small inset from the top/left.
        self.tab_left_area = QWidget()
        self.tab_left_area.setObjectName("TabLeftArea")
        leftrow = QHBoxLayout(self.tab_left_area)
        leftrow.setContentsMargins(6, 4, 0, 0)
        leftrow.setSpacing(1)

        self.tabs_host = QWidget()
        self.tabs_host.setObjectName("TabsHost")
        self.tabs_host.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)
        self.tabs_layout = QHBoxLayout(self.tabs_host)
        self.tabs_layout.setContentsMargins(0, 0, 0, 0)
        self.tabs_layout.setSpacing(1)
        leftrow.addWidget(self.tabs_host)

        self.plus = QPushButton("+")
        self.plus.setObjectName("AddTabButton")
        self.plus.setToolTip("New tab")
        self.plus.setFixedSize(36, 38)
        self.plus.clicked.connect(self.add_tab)
        leftrow.addWidget(self.plus)

        tabrow.addWidget(self.tab_left_area)
        tabrow.addStretch()

        # Windows-style caption controls are flush with the top/right edges.
        self.min_button = QToolButton()
        self.min_button.setObjectName("WindowControlButton")
        self.min_button.setProperty("controlRole", "minimize")
        self.min_button.setText("")
        self.min_button.setIcon(self._window_control_icon("minimize"))
        self.min_button.setIconSize(QSize(16, 16))
        self.min_button.setToolTip("Minimize")
        self.min_button.setFixedSize(46, 44)
        self.min_button.clicked.connect(self.showMinimized)
        tabrow.addWidget(self.min_button)

        self.max_button = QToolButton()
        self.max_button.setObjectName("WindowControlButton")
        self.max_button.setProperty("controlRole", "maximize")
        self.max_button.setText("")
        self.max_button.setIcon(self._window_control_icon("maximize"))
        self.max_button.setIconSize(QSize(16, 16))
        self.max_button.setToolTip("Maximize")
        self.max_button.setFixedSize(46, 44)
        self.max_button.clicked.connect(self.toggle_maximize_restore)
        tabrow.addWidget(self.max_button)

        self.close_button = QToolButton()
        self.close_button.setObjectName("WindowCloseButton")
        self.close_button.setText("")
        self.close_button.setIcon(self._window_control_icon("close"))
        self.close_button.setIconSize(QSize(18, 18))
        self.close_button.setToolTip("Close")
        self.close_button.setFixedSize(46, 44)
        self.close_button.clicked.connect(self.close)
        tabrow.addWidget(self.close_button)

        top_l.addWidget(self.tab_strip)

        navrow = QHBoxLayout()
        navrow.setContentsMargins(8, 5, 10, 5)
        navrow.setSpacing(2)

        self.back_button = QToolButton()
        self.back_button.setObjectName("NavButton")
        self.back_button.setIcon(self._nav_icon("back"))
        self.back_button.setIconSize(QSize(24, 24))
        self.back_button.setFixedSize(42, 38)
        self.back_button.setToolTip("Back")
        self.back_button.clicked.connect(self.go_up)
        navrow.addWidget(self.back_button)

        self.forward_button = QToolButton()
        self.forward_button.setObjectName("NavButton")
        self.forward_button.setIcon(self._nav_icon("forward"))
        self.forward_button.setIconSize(QSize(24, 24))
        self.forward_button.setFixedSize(42, 38)
        self.forward_button.setToolTip("Forward")
        self.forward_button.setEnabled(False)
        navrow.addWidget(self.forward_button)

        self.up_button = QToolButton()
        self.up_button.setObjectName("NavButton")
        self.up_button.setIcon(self._nav_icon("up"))
        self.up_button.setIconSize(QSize(24, 24))
        self.up_button.setFixedSize(42, 38)
        self.up_button.setToolTip("Up")
        self.up_button.clicked.connect(self.go_up)
        navrow.addWidget(self.up_button)

        self.refresh_button = QToolButton()
        self.refresh_button.setObjectName("NavButton")
        self.refresh_button.setIcon(self._nav_icon("refresh"))
        self.refresh_button.setIconSize(QSize(24, 24))
        self.refresh_button.setFixedSize(42, 38)
        self.refresh_button.setToolTip("Refresh")
        self.refresh_button.clicked.connect(lambda: self.refresh_files(force=True))
        navrow.addWidget(self.refresh_button)

        # Windows Explorer-style breadcrumb address bar.
        self.address_bar = BreadcrumbBar(self)
        self.address_bar.navigateRequested.connect(self.open_path)
        navrow.addWidget(self.address_bar, 1)

        # Keep a compatibility alias for code that still references self.path.
        self.path = self.address_bar.edit

        self.search = QLineEdit()
        self.search.setPlaceholderText("Search")
        self.search.setObjectName("SearchBox")
        self.search.setFixedWidth(240)
        navrow.addWidget(self.search)

        top_l.addLayout(navrow)

        command = QHBoxLayout()
        command.setContentsMargins(10, 3, 10, 5)
        command.setSpacing(1)

        def tool(icon_kind, tooltip, callback, text="", dropdown=False):
            b = QToolButton()
            b.setObjectName("WinCommandButton")
            b.setIcon(self._toolbar_icon(icon_kind))
            b.setIconSize(QSize(20, 20))
            b.setToolTip(tooltip)
            b.setAutoRaise(True)
            if text:
                b.setText(text)
                b.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
            else:
                b.setToolButtonStyle(Qt.ToolButtonIconOnly)
            if dropdown:
                b.setPopupMode(QToolButton.InstantPopup)
            b.clicked.connect(callback)
            return b

        # New + dropdown arrow.
        self.new_button = tool("new", "New", self.show_new_menu, "New  ˅")
        command.addWidget(self.new_button)

        command.addWidget(self._toolbar_separator())

        self.cut_button = tool("cut", "Cut", lambda: None)
        self.cut_button.setEnabled(False)
        command.addWidget(self.cut_button)

        self.copy_button = tool("copy", "Copy", self.copy_selected)
        command.addWidget(self.copy_button)

        self.paste_button = tool("paste", "Paste", self.paste)
        command.addWidget(self.paste_button)

        self.rename_button = tool("rename", "Rename", self.rename_selected)
        command.addWidget(self.rename_button)

        self.share_button = tool("share", "Pull selected to PC", self.pull_selected)
        command.addWidget(self.share_button)

        self.delete_button = tool("delete", "Delete", self.delete_selected)
        command.addWidget(self.delete_button)

        command.addWidget(self._toolbar_separator())

        self.sort_button = tool("sort", "Sort", self.show_sort_menu, "Sort  ˅")
        command.addWidget(self.sort_button)

        self.view_button = tool("view", "View", self.show_view_menu, "View  ˅")
        command.addWidget(self.view_button)

        self.remote_button = tool("remote", "Remote Control", self.open_remote_control, "Remote")
        command.addWidget(self.remote_button)

        self.more_button = tool("more", "More", self.show_more_menu)
        command.addWidget(self.more_button)

        command.addStretch()
        top_l.addLayout(command)
        outer.addWidget(top)

        content_row = QWidget()
        content_layout = QHBoxLayout(content_row)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)
        outer.addWidget(content_row, 1)

        # Left navigation.
        self.sidebar = QListWidget()
        self.sidebar.setObjectName("Sidebar")
        self.sidebar.setFixedWidth(205)
        self.sidebar.setIconSize(QSize(20, 20))
        self.sidebar.setSpacing(1)

        # Use the Windows shell's real icons for the current user's known
        # folders and drives. These match Windows Explorer much more closely
        # than Qt's generic SP_* icons.
        shell_icons = self.icon_provider.windows_known_folder_icons()

        home_icon = shell_icons["home"]
        sd_icon = shell_icons["sd"]
        downloads_icon = shell_icons["downloads"]
        documents_icon = shell_icons["documents"]
        pictures_icon = shell_icons["pictures"]
        music_icon = shell_icons["music"]
        videos_icon = shell_icons["videos"]
        drive_icon = shell_icons["android_drive"]

        def add_nav(name, path, icon):
            # Force identical icon artwork for normal/active/selected states.
            icon = self.icon_provider._fixed_icon(icon, 32)
            it = QListWidgetItem(icon, name)
            it.setData(Qt.UserRole, path)
            self.sidebar.addItem(it)

        def add_divider():
            item = QListWidgetItem()
            item.setFlags(Qt.NoItemFlags)
            item.setSizeHint(QSize(0, 8))
            item.setData(Qt.UserRole, "__divider__")
            self.sidebar.addItem(item)

        add_nav("Home", "/", home_icon)
        add_divider()

        add_nav("Internal storage", "/sdcard", sd_icon)
        add_nav("Downloads", "/sdcard/Download", downloads_icon)
        add_nav("Documents", "/sdcard/Documents", documents_icon)
        add_nav("Pictures", "/sdcard/Pictures", pictures_icon)
        add_nav("Music", "/sdcard/Music", music_icon)
        add_nav("Videos", "/sdcard/Movies", videos_icon)

        add_divider()

        # Root + partition entries use a Windows drive-like icon.
        add_nav("Root", "/", drive_icon)
        add_nav("System", "/system", drive_icon)
        add_nav("Vendor", "/vendor", drive_icon)
        add_nav("Data", "/data", drive_icon)

        self.sidebar.itemClicked.connect(
            lambda it: None
            if it.data(Qt.UserRole) == "__divider__"
            else self.open_path(it.data(Qt.UserRole))
        )
        content_layout.addWidget(self.sidebar)

        # Main file view.
        main = QWidget()
        main_l = QVBoxLayout(main)
        main_l.setContentsMargins(0, 0, 0, 0)
        main_l.setSpacing(0)

        self.heading = QLabel("Home")
        self.heading.setObjectName("Heading")
        main_l.addWidget(self.heading)

        self.view_stack = QStackedWidget()
        self.view_stack.setObjectName("ViewStack")

        # Icon view.
        self.files = AndroidIconView()
        self.files.owner = self
        self.files.setObjectName("Files")
        self.files.setViewMode(QListWidget.IconMode)
        self.files.setResizeMode(QListWidget.Adjust)
        self.files.setMovement(QListWidget.Static)
        self.files.setSelectionMode(QListWidget.ExtendedSelection)
        self.files.setSelectionRectVisible(False)
        self.files.setIconSize(QSize(52, 52))
        self.files.setGridSize(QSize(150, 108))
        self.files.setSpacing(2)
        self.files.setWordWrap(True)
        self.files.setTextElideMode(Qt.ElideNone)
        self.files.itemDoubleClicked.connect(self.open_item)
        self.files.setContextMenuPolicy(Qt.CustomContextMenu)
        self.files.customContextMenuRequested.connect(self.context_menu)
        self.files.setFocusPolicy(Qt.StrongFocus)
        self.view_stack.addWidget(self.files)

        # Details view.
        self.details = AndroidDetailsView()
        self.details.owner = self
        self.details.setObjectName("Details")
        self.details.setColumnCount(5)
        self.details.setHeaderLabels([
            "Name", "Date modified", "Type", "Size", "Permissions"
        ])
        self.details.setRootIsDecorated(False)
        self.details.setItemsExpandable(False)
        self.details.setAlternatingRowColors(False)
        self.details.setSelectionMode(QTreeWidget.ExtendedSelection)
        self.details.setUniformRowHeights(True)
        self.details.setIconSize(QSize(18, 18))
        self.details.itemDoubleClicked.connect(self.open_detail_item)
        self.details.setContextMenuPolicy(Qt.CustomContextMenu)
        self.details.customContextMenuRequested.connect(self.details_context_menu)
        header = self.details.header()
        header.setStretchLastSection(True)
        header.setMinimumSectionSize(70)

        default_widths = [380, 180, 160, 100]
        saved_widths = self.settings.value("details_column_widths")
        restored = False

        if saved_widths is not None:
            try:
                if not isinstance(saved_widths, (list, tuple)):
                    saved_widths = [saved_widths]
                if len(saved_widths) >= 4:
                    for i in range(4):
                        header.resizeSection(i, int(saved_widths[i]))
                    restored = True
            except Exception:
                restored = False

        if not restored:
            for i, width in enumerate(default_widths):
                header.resizeSection(i, width)

        header.sectionResized.connect(self.save_details_column_widths)
        self.view_stack.addWidget(self.details)

        main_l.addWidget(self.view_stack, 1)

        content_layout.addWidget(main, 1)

        # Bottom status.
        status = QWidget()
        status.setObjectName("StatusBarCustom")
        status_l = QHBoxLayout(status)
        status_l.setContentsMargins(10, 4, 10, 4)
        self.status = QLabel("Ready")
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setFixedWidth(110)
        self.progress.setVisible(False)
        status_l.addWidget(self.status, 1)
        status_l.addWidget(self.progress)
        outer.addWidget(status)

    def _style_dialog_window(self, dialog):
        """
        Make child dialogs follow the current Explorer light/dark theme,
        including the native Windows titlebar.
        """
        p = DARK if self.theme == "dark" else LIGHT

        dialog.setStyleSheet(f"""
            QDialog, QMessageBox {{
                background: {p['surface']};
                color: {p['text']};
                font-family: "Segoe UI";
                font-size: 9pt;
            }}
            QLabel {{
                background: transparent;
                color: {p['text']};
            }}
            QLineEdit {{
                background: {p['window']};
                color: {p['text']};
                border: 1px solid {p['border']};
                border-radius: 5px;
                padding: 6px 8px;
                selection-background-color: {p['selected']};
                selection-color: #ffffff;
            }}
            QPushButton {{
                min-width: 72px;
                background: {p['surface']};
                color: {p['text']};
                border: 1px solid {p['border']};
                border-radius: 5px;
                padding: 5px 12px;
            }}
            QPushButton:hover {{
                background: {p['hover']};
            }}
            QPushButton:default {{
                border: 1px solid {p['accent']};
            }}
        """)

        # Wait until the native HWND exists, then force its titlebar theme.
        QTimer.singleShot(
            0,
            lambda d=dialog: apply_dwm_backdrop(
                int(d.winId()),
                dark=(self.theme == "dark")
            )
        )

    def message_dialog(self, text, icon=QMessageBox.Information, buttons=QMessageBox.Ok):
        box = QMessageBox(self)
        box.setWindowTitle(APP_TITLE)
        box.setText(text)
        box.setIcon(icon)
        box.setStandardButtons(buttons)
        self._style_dialog_window(box)
        return box.exec()

    def info_dialog(self, text):
        return self.message_dialog(text, QMessageBox.Information, QMessageBox.Ok)

    def error_dialog(self, text):
        return self.message_dialog(text, QMessageBox.Critical, QMessageBox.Ok)

    def confirm_dialog(self, text):
        result = self.message_dialog(
            text,
            QMessageBox.Question,
            QMessageBox.Yes | QMessageBox.No
        )
        return result == QMessageBox.Yes

    def input_dialog(self, prompt, text=""):
        dlg = QInputDialog(self)
        dlg.setWindowTitle(APP_TITLE)
        dlg.setLabelText(prompt)
        dlg.setInputMode(QInputDialog.TextInput)
        dlg.setTextValue(text)
        self._style_dialog_window(dlg)

        if dlg.exec() == QDialog.Accepted:
            return dlg.textValue(), True
        return "", False

    def _install_shortcuts(self):
        """
        Windows Explorer-style keyboard actions.
        ApplicationShortcut is used so the actions work while the file pane
        has focus, without depending on a particular child widget.
        """
        self.shortcut_copy = QAction(self)
        self.shortcut_copy.setShortcut("Ctrl+C")
        self.shortcut_copy.setShortcutContext(Qt.ApplicationShortcut)
        self.shortcut_copy.triggered.connect(self.copy_selected)
        self.addAction(self.shortcut_copy)

        self.shortcut_paste = QAction(self)
        self.shortcut_paste.setShortcut("Ctrl+V")
        self.shortcut_paste.setShortcutContext(Qt.ApplicationShortcut)
        self.shortcut_paste.triggered.connect(self.paste)
        self.addAction(self.shortcut_paste)

        self.shortcut_delete = QAction(self)
        self.shortcut_delete.setShortcut("Delete")
        self.shortcut_delete.setShortcutContext(Qt.ApplicationShortcut)
        self.shortcut_delete.triggered.connect(self.delete_selected)
        self.addAction(self.shortcut_delete)

    @staticmethod
    def duplicate_name(name, number, is_dir=False):
        """
        Windows Explorer-style duplicate naming:
          file.txt -> file (1).txt
          archive.tar.gz -> archive.tar (1).gz
          Folder -> Folder (1)
        """
        if is_dir:
            return f"{name} ({number})"

        stem, ext = os.path.splitext(name)
        if not stem:
            return f"{name} ({number})"
        return f"{stem} ({number}){ext}"

    def unique_remote_destination(self, src, dst_dir, reserved=None):
        """
        Return a free destination path inside dst_dir.
        Checks both the device and names already reserved by the same paste job.
        """
        if reserved is None:
            reserved = set()

        src_name = PurePosixPath(src).name
        base_dest = str(PurePosixPath(dst_dir) / src_name)

        # Determine whether source is a directory using current cache where possible,
        # falling back to an ADB test only when needed.
        is_dir = False
        src_parent = str(PurePosixPath(src).parent)
        src_row = next(
            (
                r for r in self.dir_cache.get((self.adb.serial, src_parent), [])
                if r.get("name") == src_name
            ),
            None
        )
        if src_row is not None:
            is_dir = src_row.get("type") == "d"
        else:
            q = shlex.quote(src)
            out = self.adb.shell(
                f'if [ -d {q} ]; then echo 1; else echo 0; fi',
                timeout=20,
                check=False
            )
            is_dir = out.strip().endswith("1")

        candidate = base_dest
        n = 1
        while candidate in reserved or self.adb.exists(candidate):
            candidate_name = self.duplicate_name(src_name, n, is_dir=is_dir)
            candidate = str(PurePosixPath(dst_dir) / candidate_name)
            n += 1

        reserved.add(candidate)
        return candidate

    def apply_theme(self):
        p = DARK if self.theme == "dark" else LIGHT
        # Use fully opaque surfaces. Transparency/Mica has intentionally been
        # removed so the window matches normal opaque File Explorer rendering.
        toolbar_bg = p['toolbar']
        sidebar_bg = p['sidebar']
        status_bg = p['surface']
        window_bg = p['window']

        # Windows 11 uses a darker strip behind inactive tabs and the + button.
        if self.theme == "dark":
            tab_strip_bg = "#191919"
            inactive_tab_bg = "#1f1f1f"
            scrollbar_track = "#1b1b1b"
            scrollbar_handle = "#8a8a8a"
            scrollbar_handle_hover = "#a6a6a6"

            # Windows dark mode still gives list items a subtle bluish hover.
            item_hover_bg = "#25374a"
            item_hover_border = "#36506b"

            # Windows-style text selection colours for address/search bars.
            lineedit_selection_bg = "#0a64ad"
            lineedit_selection_fg = "#ffffff"
        else:
            tab_strip_bg = "#e6e6e6"
            inactive_tab_bg = "#dddddd"
            scrollbar_track = "#f3f3f3"
            scrollbar_handle = "#8b8b8b"
            scrollbar_handle_hover = "#6f6f6f"

            # Windows 11 light Explorer hover tint.
            item_hover_bg = "#eaf4ff"
            item_hover_border = "#c7e0f4"

            # Windows Explorer address bar selection look.
            lineedit_selection_bg = "#0078d7"
            lineedit_selection_fg = "#ffffff"

        self.setAttribute(Qt.WA_TranslucentBackground, False)
        pal = self.palette()
        pal.setColor(QPalette.Highlight, QColor(p['selected']))
        pal.setColor(QPalette.HighlightedText, QColor(p['text']))
        self.setPalette(pal)

        self.setStyleSheet(f"""
            QMainWindow, QWidget {{
                background: {window_bg};
                color: {p['text']};
                font-family: "Segoe UI";
                font-size: 9pt;
            }}
            QWidget#TopArea {{
                background: {toolbar_bg};
                border-bottom: 1px solid {p['border']};
            }}
            QWidget#TabStrip {{
                background: {tab_strip_bg};
            }}
            QWidget#TabsHost,
            QWidget#TabLeftArea {{
                background: transparent;
                border: none;
            }}
            QPushButton {{
                background: transparent;
                color: {p['text']};
                border: 1px solid transparent;
                border-radius: 5px;
                padding: 5px 9px;
            }}
            QPushButton:hover {{
                background: {p['hover']};
                border-color: {p['border']};
            }}
            QWidget#TabContainer {{
                background: {inactive_tab_bg};
                border: 1px solid transparent;
                border-top-left-radius: 9px;
                border-top-right-radius: 9px;
                border-bottom-left-radius: 0px;
                border-bottom-right-radius: 0px;
            }}
            QWidget#TabContainer[activeTab="true"] {{
                background: {toolbar_bg};
                border: 1px solid {p['border']};
                border-bottom-color: {toolbar_bg};
            }}
            QWidget#TabContainer[activeTab="false"]:hover {{
                background: {p['hover']};
            }}
            QPushButton#TabMain {{
                background: transparent;
                border: none;
                border-radius: 0px;
                text-align: left;
                padding: 0px 4px;
                color: {p['text']};
                font-size: 9pt;
            }}
            QPushButton#TabMain:hover {{
                background: transparent;
                border: none;
            }}
            QPushButton#TabClose {{
                background: transparent;
                border: none;
                border-radius: 4px;
                padding: 0px;
                margin: 0px;
                color: {p['muted']};
                font-size: 12pt;
            }}
            QPushButton#TabClose:hover {{
                background: {p['hover']};
                color: {p['text']};
            }}
            QPushButton#FlatButton {{
                padding: 0px;
            }}
            QPushButton#AddTabButton {{
                background: {tab_strip_bg};
                color: {p['text']};
                border: none;
                border-radius: 5px;
                padding: 0px;
                font-size: 13pt;
            }}
            QPushButton#AddTabButton:hover {{
                background: {p['hover']};
                border: none;
            }}

            QToolButton#WindowControlButton,
            QToolButton#WindowCloseButton {{
                background: transparent;
                color: {p['text']};
                border: none;
                border-radius: 0px;
                padding: 0px;
                margin: 0px;
                min-width: 46px;
                max-width: 46px;
                min-height: 44px;
                max-height: 44px;
                qproperty-toolButtonStyle: ToolButtonIconOnly;
            }}
            QToolButton#WindowControlButton:hover {{
                background: {p['hover']};
                border: none;
            }}
            QToolButton#WindowControlButton:pressed {{
                background: {p['selected']};
                border: none;
            }}
            QToolButton#WindowCloseButton:hover {{
                background: #c42b1c;
                color: #ffffff;
                border: none;
            }}
            QToolButton#WindowCloseButton:pressed {{
                background: #a1251a;
                color: #ffffff;
                border: none;
            }}
            QPushButton#CommandButton {{
                padding: 5px 9px;
            }}
            QToolButton#WinCommandButton {{
                background: transparent;
                color: {p['text']};
                border: 1px solid transparent;
                border-radius: 4px;
                padding: 4px 7px;
                min-height: 25px;
            }}
            QToolButton#WinCommandButton:hover {{
                background: {p['hover']};
                border-color: {p['border']};
            }}
            QToolButton#WinCommandButton:pressed {{
                background: {p['selected']};
            }}
            QToolButton#WinCommandButton:disabled {{
                color: {p['muted']};
                background: transparent;
            }}
            QFrame#CommandSeparator {{
                color: {p['border']};
                background: transparent;
                border: none;
            }}
            QToolButton#NavButton {{
                background: transparent;
                border: 1px solid transparent;
                border-radius: 5px;
                padding: 6px;
            }}
            QToolButton#NavButton:hover {{
                background: {item_hover_bg};
                border-color: {item_hover_border};
            }}
            QToolButton#NavButton:pressed {{
                background: {p['selected']};
            }}
            QToolButton#NavButton:disabled {{
                background: transparent;
                border-color: transparent;
            }}

            QFrame#BreadcrumbBar {{
                background: {p['surface']};
                border: 1px solid {p['border']};
                border-radius: 6px;
                min-height: 36px;
            }}
            QFrame#BreadcrumbBar:hover {{
                border-color: {item_hover_border};
            }}
            QWidget#BreadcrumbStack, QWidget#BreadcrumbStack QWidget {{
                background: transparent;
            }}
            QToolButton#BreadcrumbButton {{
                background: transparent;
                color: {p['text']};
                border: 1px solid transparent;
                border-radius: 4px;
                padding: 3px 6px;
                min-height: 26px;
            }}
            QToolButton#BreadcrumbButton:hover {{
                background: {item_hover_bg};
                border-color: {item_hover_border};
            }}
            QToolButton#BreadcrumbButton:pressed {{
                background: {p['selected']};
            }}
            QLabel#BreadcrumbChevron {{
                background: transparent;
                color: {p['muted']};
                border: none;
                font-size: 14pt;
            }}
            QLineEdit#AddressEdit {{
                background: {p['surface']};
                color: {p['text']};
                border: none;
                border-radius: 6px;
                padding: 7px 10px;
                selection-background-color: {lineedit_selection_bg};
                selection-color: {lineedit_selection_fg};
            }}
            QLineEdit#SearchBox {{
                background: {p['surface']};
                color: {p['text']};
                border: 1px solid {p['border']};
                border-radius: 6px;
                padding: 7px 10px;
                selection-background-color: {lineedit_selection_bg};
                selection-color: {lineedit_selection_fg};
            }}
            QListWidget#Sidebar {{
                background: {sidebar_bg};
                color: {p['text']};
                border: none;
                border-right: 1px solid {p['border']};
                outline: none;
                padding-top: 7px;
            }}
            QListWidget#Sidebar::item {{
                height: 31px;
                padding-left: 8px;
                border-radius: 4px;
                margin-left: 5px;
                margin-right: 5px;
            }}
            QListWidget#Sidebar::item:disabled {{
                height: 8px;
                margin: 3px 10px;
                padding: 0px;
                border-radius: 0px;
                border-bottom: 1px solid {p['border']};
                background: transparent;
            }}
            QListWidget#Sidebar::item:hover {{
                background: {item_hover_bg};
                border: 1px solid {item_hover_border};
            }}
            QListWidget#Sidebar::item:selected {{
                background: {p['selected']};
                color: {p['text']};
                border: 1px solid {item_hover_border};
            }}
            QListWidget#Sidebar::item:selected:active {{
                color: {p['text']};
            }}
            QListWidget#Sidebar::item:selected:!active {{
                color: {p['text']};
            }}
            QListWidget#Files {{
                background: {p['window']};
                color: {p['text']};
                border: none;
                outline: none;
            }}
            QTreeWidget#Details {{
                background: {p['window']};
                color: {p['text']};
                border: none;
                outline: none;
                gridline-color: {p['border']};
            }}
            QTreeWidget#Details::item {{
                min-height: 24px;
                border-bottom: 1px solid transparent;
            }}
            QTreeWidget#Details::item:hover {{
                background: {item_hover_bg};
            }}
            QTreeWidget#Details::item:selected {{
                background: {p['selected']};
                color: {p['text']};
            }}
            QHeaderView {{
                background: {p['surface']};
                border: none;
            }}
            QHeaderView::section {{
                background: {p['surface']};
                color: {p['text']};
                border: none;
                border-right: 1px solid {p['border']};
                border-bottom: 1px solid {p['border']};
                padding: 6px 8px;
            }}
            QListWidget#Files::item {{
                border: 1px solid transparent;
                border-radius: 5px;
                padding: 7px;
            }}
            QListWidget#Files::item:hover {{
                background: {item_hover_bg};
                border-color: {item_hover_border};
            }}
            QListWidget#Files::item:selected {{
                background: {p['selected']};
                border-color: {p['accent']};
                color: {p['text']};
            }}
            QListWidget#Files::item:selected:active {{
                color: {p['text']};
            }}
            QListWidget#Files::item:selected:!active {{
                color: {p['text']};
            }}
            QLabel#Heading {{
                font-size: 11pt;
                font-weight: 600;
                padding: 8px 12px 7px 12px;
            }}
            QWidget#StatusBarCustom {{
                background: {status_bg};
                border-top: 1px solid {p['border']};
            }}
            QMenu {{
                background: {p['surface']};
                color: {p['text']};
                border: 1px solid {p['border']};
                padding: 5px;
            }}
            QMenu::item {{
                padding: 7px 28px 7px 12px;
                border-radius: 4px;
            }}
            QMenu::item:selected {{
                background: {p['hover']};
            }}
            /* Windows 11-like scrollbars */
            QScrollBar:vertical {{
                background: {scrollbar_track};
                width: 12px;
                margin: 0px;
                border: none;
            }}
            QScrollBar::handle:vertical {{
                background: {scrollbar_handle};
                min-height: 34px;
                border-radius: 5px;
                margin: 2px 3px;
            }}
            QScrollBar::handle:vertical:hover {{
                background: {scrollbar_handle_hover};
            }}
            QScrollBar::add-line:vertical,
            QScrollBar::sub-line:vertical {{
                background: transparent;
                height: 0px;
                border: none;
            }}
            QScrollBar::add-page:vertical,
            QScrollBar::sub-page:vertical {{
                background: transparent;
            }}

            QScrollBar:horizontal {{
                background: {scrollbar_track};
                height: 12px;
                margin: 0px;
                border: none;
            }}
            QScrollBar::handle:horizontal {{
                background: {scrollbar_handle};
                min-width: 34px;
                border-radius: 5px;
                margin: 3px 2px;
            }}
            QScrollBar::handle:horizontal:hover {{
                background: {scrollbar_handle_hover};
            }}
            QScrollBar::add-line:horizontal,
            QScrollBar::sub-line:horizontal {{
                background: transparent;
                width: 0px;
                border: none;
            }}
            QScrollBar::add-page:horizontal,
            QScrollBar::sub-page:horizontal {{
                background: transparent;
            }}
            QProgressBar {{
                border: none;
                background: {p['hover']};
                height: 5px;
            }}
            QProgressBar::chunk {{
                background: {p['accent']};
            }}
        """)
        apply_dwm_backdrop(int(self.winId()), dark=self.theme == "dark")

    def start_device_precache(self, force=False):
        """
        Boot/device-selection cache order:

        1. Cache `/` immediate children only.
        2. Render/open the Explorer using that root cache.
        3. Continue in background, one tree at a time, in this exact order:
           /sdcard
           /system
           /vendor
           /oem
           /odm
           /data
        """
        serial = self.adb.serial
        if not serial or self._closing:
            return

        if self.cache_worker and self.cache_worker.isRunning():
            return

        if force:
            self.clear_device_precache()

        self.status.setText("Caching root directory + permissions + modified dates…")
        # Background caching should not show the bottom-right loading bar.
        self.progress.setVisible(False)
        self.cache_worker = Worker(self.adb.scan_root_only)
        self.cache_worker.ok.connect(
            lambda rows, s=serial: self.finish_root_cache(s, rows)
        )
        self.cache_worker.fail.connect(self.precache_failed)
        self.cache_worker.start()

    def finish_root_cache(self, serial, rows):
        if self._closing or serial != self.adb.serial:
            return

        self._merge_cache_rows(serial, rows)

        # Open/render root immediately from RAM now that root cache is ready.
        self.current_path = "/"
        self.address_bar.set_path("/")
        key = (serial, "/")
        self.render_rows("/", self.dir_cache.get(key, []))
        self.status.setText(
            f"Root ready • {len(self.dir_cache.get(key, []))} items"
        )

        # Start the remaining cache stages in the requested order.
        self.cache_stage_queue = [
            "/sdcard",
            "/system",
            "/vendor",
            "/oem",
            "/odm",
            "/data",
        ]
        self.cache_stage_index = 0
        QTimer.singleShot(50, self.start_next_cache_stage)

    def start_next_cache_stage(self):
        if self._closing or not self.adb.serial:
            return

        if self.cache_worker and self.cache_worker.isRunning():
            return

        if self.cache_stage_index >= len(self.cache_stage_queue):
            self.precache_complete_for_serial.add(self.adb.serial)
            self.progress.setVisible(False)
            self.status.setText(
                f"Background cache complete • {len(self.path_cache):,} paths"
            )
            return

        base = self.cache_stage_queue[self.cache_stage_index]
        serial = self.adb.serial
        self.status.setText(f"Caching {base} + permissions + modified dates…")
        # Keep cache work unobtrusive; status text is enough.
        self.progress.setVisible(False)

        self.cache_worker = Worker(
            lambda b=base: self.adb.scan_recursive_base(b)
        )
        self.cache_worker.ok.connect(
            lambda rows, s=serial, b=base: self.finish_cache_stage(s, b, rows)
        )
        self.cache_worker.fail.connect(
            lambda msg, b=base: self.cache_stage_failed(b, msg)
        )
        self.cache_worker.start()

    def finish_cache_stage(self, serial, base, rows):
        if self._closing or serial != self.adb.serial:
            return

        self._merge_cache_rows(serial, rows)
        self.cache_stage_index += 1
        QTimer.singleShot(10, self.start_next_cache_stage)

    def cache_stage_failed(self, base, message):
        if self._closing:
            return

        # Skip inaccessible/missing partitions and continue the ordered queue.
        self.status.setText(f"Skipped {base} cache")
        self.cache_stage_index += 1
        QTimer.singleShot(10, self.start_next_cache_stage)

    def _merge_cache_rows(self, serial, scanned_rows):
        """
        Merge one staged scan into path_cache and dir_cache without wiping
        already-cached stages.
        """
        grouped_paths = {}
        grouped_rows = {}

        for row in scanned_rows:
            full_path = row["path"]
            self.path_cache.add(full_path)

            parent = str(PurePosixPath(full_path).parent)
            grouped_paths.setdefault(parent, []).append(full_path)
            grouped_rows.setdefault(parent, []).append({
                "name": row["name"],
                "type": row["type"],
                "size": row.get("size", 0),
                "mode": row.get("mode", ""),
                "mtime_text": row.get("mtime_text", ""),
                "_metadata_loaded": bool(row.get("mode")),
            })

        for parent, paths in grouped_paths.items():
            existing = set(self.precache_by_parent.get(parent, []))
            existing.update(paths)
            merged = list(existing)
            merged.sort(key=lambda p: PurePosixPath(p).name.lower())
            self.precache_by_parent[parent] = merged

        for parent, rows in grouped_rows.items():
            normalized = parent.rstrip("/") or "/"
            existing_rows = self.dir_cache.get((serial, normalized), [])
            by_name = {r["name"]: r for r in existing_rows}

            for row in rows:
                by_name[row["name"]] = row

            merged_rows = list(by_name.values())
            merged_rows.sort(
                key=lambda r: (
                    0 if r["type"] == "d" else 1,
                    r["name"].lower()
                )
            )
            self.dir_cache[(serial, normalized)] = merged_rows

    def sync_device_cache(self):
        """
        Refresh the ordered staged caches in the background.
        Starts with root, then the requested recursive areas in order.
        """
        if self._closing or not self.adb.serial:
            return
        if self.cache_worker and self.cache_worker.isRunning():
            return

        self.progress.setVisible(False)
        self.start_device_precache(force=True)

    def precache_failed(self, message):
        if self._closing:
            return
        self.progress.setVisible(False)
        self.status.setText("Root cache failed")

    def clear_device_precache(self):
        serial = self.adb.serial
        self.path_cache.clear()
        self.precache_by_parent.clear()

        if serial:
            # Remove this device's cached directory entries only.
            for key in list(self.dir_cache.keys()):
                if key[0] == serial:
                    self.dir_cache.pop(key, None)
            self.precache_complete_for_serial.discard(serial)

        self.cache_stage_queue = []
        self.cache_stage_index = 0

    def cache_add_path(self, full_path):
        full_path = str(PurePosixPath(full_path))
        self.path_cache.add(full_path)
        parent = str(PurePosixPath(full_path).parent)
        bucket = self.precache_by_parent.setdefault(parent, [])
        if full_path not in bucket:
            bucket.append(full_path)
            bucket.sort(key=lambda p: PurePosixPath(p).name.lower())

    def cache_remove_path(self, full_path):
        full_path = str(PurePosixPath(full_path)).rstrip("/") or "/"
        prefix = full_path + "/"

        doomed = {
            p for p in self.path_cache
            if p == full_path or p.startswith(prefix)
        }
        if not doomed:
            return

        self.path_cache.difference_update(doomed)
        for parent, items in list(self.precache_by_parent.items()):
            filtered = [p for p in items if p not in doomed]
            if filtered:
                self.precache_by_parent[parent] = filtered
            else:
                self.precache_by_parent.pop(parent, None)

    def cache_move_path(self, old_path, new_path):
        self.cache_remove_path(old_path)
        self.cache_add_path(new_path)

    def update_cached_permissions(self, paths, mode, recursive=False):
        """
        Immediately reflect chmod changes in every in-memory directory cache.

        This prevents the Permissions column from continuing to show the old
        value while an authoritative ADB refresh is running in the background.
        For recursive chmod, all currently cached descendants are updated too.
        """
        if not self.adb.serial:
            return

        normalized = [
            str(PurePosixPath(p)).rstrip("/") or "/"
            for p in paths
        ]

        def affected(full_path):
            full_path = str(PurePosixPath(full_path)).rstrip("/") or "/"
            for base in normalized:
                if full_path == base:
                    return True
                if recursive and full_path.startswith(base.rstrip("/") + "/"):
                    return True
            return False

        serial = self.adb.serial

        for (cache_serial, parent), rows in list(self.dir_cache.items()):
            if cache_serial != serial:
                continue

            parent_base = parent.rstrip("/")
            for row in rows:
                full = (
                    f"{parent_base}/{row['name']}"
                    if parent_base
                    else f"/{row['name']}"
                )
                if affected(full):
                    row["mode"] = mode
                    row["_metadata_loaded"] = True

        # Repaint immediately from the corrected RAM cache.
        key = (serial, self.current_path.rstrip("/") or "/")
        if key in self.dir_cache:
            self.render_rows(self.current_path, self.dir_cache[key])

    def rebuild_tabs(self):
        while self.tabs_layout.count():
            item = self.tabs_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

        folder_icon = self.icon_provider.provider.icon(QFileIconProvider.Folder)

        for idx, tab in enumerate(self.tabs):
            tab_widget = QWidget()
            tab_widget.setObjectName("TabContainer")
            tab_widget.setProperty("activeTab", idx == self.active_tab)
            tab_widget.setFixedHeight(40)
            tab_widget.setMinimumWidth(210)
            tab_widget.setMaximumWidth(250)
            tab_widget.setToolTip(tab["path"])

            lay = QHBoxLayout(tab_widget)
            lay.setContentsMargins(10, 2, 6, 1)
            lay.setSpacing(5)

            main = QPushButton(tab["title"])
            main.setObjectName("TabMain")
            main.setIcon(folder_icon)
            main.setIconSize(QSize(16, 16))
            main.setToolTip(tab["path"])
            main.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            main.clicked.connect(lambda checked=False, i=idx: self.switch_tab(i))
            lay.addWidget(main, 1)

            close = QPushButton("×")
            close.setObjectName("TabClose")
            close.setFixedSize(27, 27)
            close.setToolTip("Close tab")
            close.clicked.connect(lambda checked=False, i=idx: self.close_tab(i))
            lay.addWidget(close, 0, Qt.AlignRight | Qt.AlignVCenter)

            self.tabs_layout.addWidget(tab_widget)

        # Re-apply the current theme so dynamic activeTab properties are styled.
        for i in range(self.tabs_layout.count()):
            w = self.tabs_layout.itemAt(i).widget()
            if w:
                w.style().unpolish(w)
                w.style().polish(w)

    def add_tab(self):
        if self.tabs and 0 <= self.active_tab < len(self.tabs):
            self.tabs[self.active_tab]["path"] = self.current_path
            self.tabs[self.active_tab]["title"] = "Home" if self.current_path == "/" else PurePosixPath(self.current_path).name

        self.tabs.append({"path": "/", "title": "Home"})
        self.active_tab = len(self.tabs) - 1
        self.rebuild_tabs()
        self.open_path("/")

    def switch_tab(self, index):
        if index < 0 or index >= len(self.tabs):
            return
        if self.tabs and 0 <= self.active_tab < len(self.tabs):
            self.tabs[self.active_tab]["path"] = self.current_path
            self.tabs[self.active_tab]["title"] = "Home" if self.current_path == "/" else PurePosixPath(self.current_path).name

        self.active_tab = index
        self.rebuild_tabs()
        self.open_path(self.tabs[index]["path"])

    def close_tab(self, index):
        if len(self.tabs) <= 1:
            return
        if index < 0 or index >= len(self.tabs):
            return
        self.tabs.pop(index)
        if self.active_tab >= len(self.tabs):
            self.active_tab = len(self.tabs) - 1
        elif index < self.active_tab:
            self.active_tab -= 1
        self.rebuild_tabs()
        self.open_path(self.tabs[self.active_tab]["path"])

    def show_settings_menu(self, global_pos=None):
        menu = QMenu(self)

        theme_action = menu.addAction(
            "Switch to dark mode" if self.theme == "light" else "Switch to light mode"
        )
        menu.addSeparator()
        clear_cache_action = menu.addAction("Clear all caches")
        rebuild_cache_action = menu.addAction("Rebuild filesystem cache")
        refresh_action = menu.addAction("Refresh current directory")
        menu.addSeparator()
        select_device_action = menu.addAction("Select device...")
        adb_label = "Select adb.exe..." if os.name == "nt" else "Select adb..."
        scrcpy_label = "Select scrcpy.exe..." if os.name == "nt" else "Select scrcpy..."
        adb_action = menu.addAction(adb_label)
        scrcpy_action = menu.addAction(scrcpy_label)

        if global_pos is None:
            if hasattr(self, "more_button"):
                global_pos = self.more_button.mapToGlobal(
                    self.more_button.rect().bottomLeft()
                )
            else:
                global_pos = self.mapToGlobal(self.rect().center())

        action = menu.exec(global_pos)

        if action == theme_action:
            self.toggle_theme()
        elif action == clear_cache_action:
            self.dir_cache.clear()
            self.clear_device_precache()
            self.status.setText("All caches cleared")
        elif action == rebuild_cache_action:
            self.clear_device_precache()
            self.start_device_precache(force=True)
        elif action == refresh_action:
            self.refresh_files(force=True)
        elif action == select_device_action:
            self.refresh_devices()
        elif action == adb_action:
            filename, _ = QFileDialog.getOpenFileName(
                self,
                "Select ADB executable",
                "",
                "ADB executable (adb.exe)" if os.name == "nt" else "ADB executable (adb);;All files (*)"
            )
            if filename:
                self.adb.path = filename
                self.settings.setValue("adb_path", filename)
                self.settings.sync()
                self.status.setText("ADB path updated")
                self.refresh_devices()
        elif action == scrcpy_action:
            filename = self.choose_scrcpy()
            if filename:
                self.status.setText("scrcpy path updated")

    def invalidate_cache(self, *paths):
        serial = self.adb.serial
        if not serial:
            return
        for path in paths:
            key = (serial, path.rstrip("/") or "/")
            self.dir_cache.pop(key, None)

    def refresh_toolbar_icons(self):
        mappings = [
            ("new_button", "new"),
            ("cut_button", "cut"),
            ("copy_button", "copy"),
            ("paste_button", "paste"),
            ("rename_button", "rename"),
            ("share_button", "share"),
            ("delete_button", "delete"),
            ("sort_button", "sort"),
            ("view_button", "view"),
            ("remote_button", "remote"),
            ("more_button", "more"),
        ]
        for attr, kind in mappings:
            button = getattr(self, attr, None)
            if button is not None:
                button.setIcon(self._toolbar_icon(kind))

    def toggle_theme(self):
        self.theme = "dark" if self.theme == "light" else "light"
        self.settings.setValue("theme", self.theme)
        self.settings.sync()

        self.apply_theme()
        self.refresh_toolbar_icons()
        self.refresh_nav_icons()
        self.refresh_window_control_icons()
        if hasattr(self, "address_bar"):
            self.address_bar.rebuild()


    def start_job(self, fn, success=None, label="Working..."):
        if self._closing:
            return
        if self.worker and self.worker.isRunning():
            return
        self.status.setText(label)
        self.progress.setVisible(True)
        self.worker = Worker(fn)
        self.worker.ok.connect(lambda result: self.finish_job(result, success))
        self.worker.fail.connect(self.fail_job)
        self.worker.start()

    def finish_job(self, result, success):
        if self._closing:
            return
        self.progress.setVisible(False)
        self.status.setText("Ready")
        if success:
            success(result)

    def fail_job(self, msg):
        if self._closing:
            return
        self.progress.setVisible(False)
        self.status.setText("Error")
        self.error_dialog(msg)

    def choose_device_dialog(self, devices):
        """
        Prompt the user to choose a connected ADB device.
        Returns the selected serial or None.
        """
        dlg = QDialog(self)
        dlg.setWindowTitle("Select Android Device")
        dlg.resize(420, 260)
        self._style_dialog_window(dlg)

        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        label = QLabel("More than one Android device is connected. Select a device:")
        layout.addWidget(label)

        device_list = QListWidget()
        device_list.setSelectionMode(QListWidget.SingleSelection)
        layout.addWidget(device_list, 1)

        for serial, state, model in devices:
            display = model or serial
            item = QListWidgetItem(f"{display}    [{state}]")
            item.setData(Qt.UserRole, serial)
            device_list.addItem(item)

        if device_list.count():
            device_list.setCurrentRow(0)

        buttons = QHBoxLayout()
        buttons.addStretch()

        cancel_btn = QPushButton("Cancel")
        select_btn = QPushButton("Select")
        select_btn.setDefault(True)

        cancel_btn.clicked.connect(dlg.reject)
        select_btn.clicked.connect(dlg.accept)
        device_list.itemDoubleClicked.connect(lambda _: dlg.accept())

        buttons.addWidget(cancel_btn)
        buttons.addWidget(select_btn)
        layout.addLayout(buttons)

        if dlg.exec() != QDialog.Accepted:
            return None

        item = device_list.currentItem()
        return item.data(Qt.UserRole) if item else None

    def no_device_dialog(self):
        """
        Prompt the user to connect a device when none are detected.
        Offers USB retry or IP connection.
        """
        box = QMessageBox(self)
        box.setWindowTitle(APP_TITLE)
        box.setIcon(QMessageBox.Information)
        box.setText("No Android device is connected.")
        box.setInformativeText(
            "Connect a device by USB with ADB enabled, or connect over the network."
        )

        retry_btn = box.addButton("Retry USB", QMessageBox.AcceptRole)
        ip_btn = box.addButton("Connect by IP", QMessageBox.ActionRole)
        cancel_btn = box.addButton("Cancel", QMessageBox.RejectRole)

        self._style_dialog_window(box)
        box.exec()

        clicked = box.clickedButton()
        if clicked == retry_btn:
            self.refresh_devices()
        elif clicked == ip_btn:
            self.connect_ip()
        else:
            self.status.setText("No device connected")

    def refresh_devices(self):
        def ok(devs):
            usable = [
                d for d in devs
                if d[1] == "device"
            ]

            if len(usable) == 1:
                serial = usable[0][0]
                self.select_device(serial)
                return

            if len(usable) > 1:
                preferred = self.settings.value("last_device_serial", "", type=str)

                # If the previously used device is the only sensible match,
                # still prompt because the user explicitly asked for a choice
                # whenever multiple devices are connected.
                chosen = self.choose_device_dialog(usable)
                if chosen:
                    self.select_device(chosen)
                else:
                    self.status.setText("No device selected")
                return

            # Devices may exist but be unauthorized/offline. Show a more useful message.
            if devs:
                states = ", ".join(f"{serial}: {state}" for serial, state, _ in devs)
                self.status.setText(f"No usable ADB device • {states}")
                self.no_device_dialog()
                return

            self.status.setText("No ADB devices detected")
            self.no_device_dialog()

        self.start_job(self.adb.devices, ok, "Looking for ADB devices...")

    def select_device(self, serial):
        if not serial:
            return

        self.adb.serial = serial
        self.settings.setValue("last_device_serial", serial)
        self.settings.sync()

        self.current_path = "/"
        self.address_bar.set_path("/")

        # Cache root first, render Explorer from that cache, then continue
        # the larger recursive caches in the requested background order.
        QTimer.singleShot(0, self.start_device_precache)

    def connect_ip(self):
        host, ok = self.input_dialog("Device IP or IP:port:")
        if not ok or not host.strip():
            return
        self.start_job(lambda: self.adb.connect(host), lambda _: self.refresh_devices(), f"Connecting to {host}...")

    def handle_internal_android_drop(self, remote_paths, target_dir):
        """
        Move Android files/folders between folders inside Android Explorer.

        This mirrors Windows Explorer's same-drive drag behaviour:
        dragging an item onto another folder moves it there.
        """
        if not self.adb.serial:
            self.error_dialog("Connect to an Android device first.")
            return

        target_dir = str(PurePosixPath(target_dir)).rstrip("/") or "/"

        # Normalize and de-duplicate while keeping selection order.
        seen = set()
        sources = []
        for path in remote_paths:
            p = str(PurePosixPath(path)).rstrip("/") or "/"
            if p not in seen:
                seen.add(p)
                sources.append(p)

        if not sources:
            return

        # Filter no-op moves and block moving a folder into itself/descendant.
        valid = []
        for src in sources:
            src_parent = str(PurePosixPath(src).parent)

            # Dragging within the same current parent is a no-op.
            if src_parent == target_dir:
                continue

            # Never move root.
            if src == "/":
                continue

            # A directory cannot be moved into itself or one of its children.
            if target_dir == src or target_dir.startswith(src.rstrip("/") + "/"):
                self.error_dialog(
                    f"Cannot move '{PurePosixPath(src).name}' into itself."
                )
                return

            valid.append(src)

        if not valid:
            self.status.setText("Nothing to move")
            return

        source_parents = {
            str(PurePosixPath(src).parent)
            for src in valid
        }

        def worker():
            reserved = set()
            moved = []

            for src in valid:
                destination = self.unique_remote_destination(
                    src,
                    target_dir,
                    reserved=reserved
                )

                # mv is a true on-device move/rename; no PC round-trip.
                self.adb.rename(src, destination)
                moved.append((src, destination))

            return moved

        def done(moved):
            # Update path/precache state immediately.
            for old_path, new_path in moved:
                self.cache_move_path(old_path, new_path)

            # The row dictionaries in dir_cache need authoritative refreshes
            # for both source and destination folders.
            for parent in source_parents:
                self.invalidate_cache(parent)
            self.invalidate_cache(target_dir)

            # Refresh the folder currently on screen if it was affected.
            if self.current_path in source_parents or self.current_path == target_dir:
                self.refresh_files(force=True)

            self.status.setText(
                f"Moved {len(moved)} item(s) to {target_dir}"
            )

        self.start_job(
            worker,
            done,
            f"Moving {len(valid)} item(s) to {target_dir}..."
        )

    def handle_local_drop(self, local_paths, target_dir=None):
        """
        Push files/folders dropped from the PC into Android.

        If target_dir is supplied, the items are pushed there. This allows
        dropping directly ON an Android folder just like Windows Explorer.
        Otherwise the current Android directory is used.
        """
        if not self.adb.serial:
            self.error_dialog("Connect to an Android device first.")
            return

        local_paths = [os.path.normpath(p) for p in local_paths if os.path.exists(p)]
        if not local_paths:
            return

        target_dir = target_dir or self.current_path
        target_dir = str(PurePosixPath(target_dir)) or "/"

        def worker():
            results = []
            for local_path in local_paths:
                # adb push handles both files and directories.
                results.append(
                    self.adb.push(local_path, target_dir)
                )
            return results

        def done(_):
            for local_path in local_paths:
                self.cache_add_path(
                    str(PurePosixPath(target_dir) / os.path.basename(local_path.rstrip("\\/")))
                )
            self.invalidate_cache(target_dir)

            if target_dir == self.current_path:
                self.refresh_files(force=True)

            self.status.setText(
                f"Pushed {len(local_paths)} item(s) to {target_dir}"
            )

        self.start_job(
            worker,
            done,
            f"Pushing {len(local_paths)} dropped item(s)..."
        )

    def _clear_drag_temp(self):
        """
        Remove previously staged drag-out files. Best effort only.
        """
        try:
            if os.path.isdir(self.drag_temp_dir):
                for name in os.listdir(self.drag_temp_dir):
                    p = os.path.join(self.drag_temp_dir, name)
                    try:
                        if os.path.isdir(p) and not os.path.islink(p):
                            shutil.rmtree(p, ignore_errors=True)
                        else:
                            os.remove(p)
                    except Exception:
                        pass
        except Exception:
            pass

    def start_android_drag(self, source_view=None):
        """
        Drag selected Android files/folders out to the PC.

        The selected remote items are first adb-pulled to a local staging
        directory. Qt then exposes those staged paths as native local file URLs,
        so Windows Explorer receives a normal file/folder Copy drop.
        """
        rows = self.selected_rows()
        if not rows or not self.adb.serial:
            return

        self._clear_drag_temp()

        # Pull synchronously for the drag gesture. This is necessary because
        # Explorer expects real local paths when the native QDrag starts.
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            self.status.setText(f"Preparing {len(rows)} item(s) for drag...")
            QApplication.processEvents()

            local_urls = []
            for row in rows:
                remote = self.full_path(row)

                # Pull into the staging root. adb creates files/folders there.
                self.adb.pull(remote, self.drag_temp_dir)

                local_path = os.path.join(
                    self.drag_temp_dir,
                    PurePosixPath(remote).name
                )
                if os.path.exists(local_path):
                    local_urls.append(QUrl.fromLocalFile(local_path))

            if not local_urls:
                self.error_dialog("The selected Android item(s) could not be prepared for dragging.")
                return

            mime = QMimeData()
            mime.setUrls(local_urls)

            # Also include original Android paths. When this drag is dropped
            # onto another folder inside Android Explorer, the receiving view
            # uses these paths to perform a direct on-device move instead of
            # re-uploading the staged local copies.
            remote_paths = [
                self.full_path(row)
                for row in rows
            ]
            mime.setData(
                ANDROID_REMOTE_MIME,
                "\n".join(remote_paths).encode("utf-8")
            )

            # Parent the native drag object to the actual originating view.
            # This is more reliable than parenting it to QMainWindow when the
            # cursor leaves the application for Windows Explorer.
            drag_source = source_view if source_view is not None else self
            drag = QDrag(drag_source)
            drag.setMimeData(mime)

            # Internal drop uses MoveAction; Windows/Desktop drop uses
            # CopyAction because the remote item is being pulled to the PC.
            result = drag.exec(
                Qt.CopyAction | Qt.MoveAction,
                Qt.CopyAction
            )

            if result == Qt.CopyAction:
                self.status.setText(
                    f"Pulled {len(local_urls)} item(s) to PC"
                )
            elif result == Qt.MoveAction:
                # The internal drop handler owns the final status text.
                pass
            else:
                self.status.setText("Drag cancelled")
        except Exception as exc:
            self.error_dialog(str(exc))
        finally:
            QApplication.restoreOverrideCursor()

    def save_details_column_widths(self, logical_index=None, old_size=None, new_size=None):
        try:
            header = self.details.header()
            widths = [header.sectionSize(i) for i in range(4)]
            self.settings.setValue("details_column_widths", widths)
            self.settings.sync()
        except Exception:
            pass

    def show_view_menu(self):
        menu = QMenu(self)
        icons_action = menu.addAction("Icons")
        details_action = menu.addAction("Details")
        icons_action.setCheckable(True)
        details_action.setCheckable(True)
        icons_action.setChecked(self.view_mode == "icons")
        details_action.setChecked(self.view_mode == "details")

        # Open directly below the View button, like Windows Explorer.
        if hasattr(self, "view_button"):
            pos = self.view_button.mapToGlobal(self.view_button.rect().bottomLeft())
        else:
            pos = self.mapToGlobal(self.rect().topLeft())

        action = menu.exec(pos)
        if action == icons_action:
            self.set_view_mode("icons")
        elif action == details_action:
            self.set_view_mode("details")

    def set_view_mode(self, mode):
        self.view_mode = mode
        self.settings.setValue("view_mode", mode)
        self.settings.sync()

        is_icons = mode == "icons"
        self.view_stack.setCurrentIndex(0 if is_icons else 1)

        # Details view should begin directly with the column headers.
        self.heading.setVisible(is_icons)

        if mode == "details":
            QTimer.singleShot(0, self.refresh_current_metadata_background)

    @staticmethod
    def row_type_text(row):
        if row["type"] == "d":
            return "File folder"
        ext = os.path.splitext(row["name"])[1].lower()
        if ext == ".apk":
            return "Android package"
        if ext:
            return f"{ext[1:].upper()} File"
        return "File"

    def open_row(self, row):
        """
        Open a row safely.

        Cached metadata can occasionally be stale or a symlink may have been
        classified as a file. Before launching the PC file workflow, verify
        the actual Android path. Directories always navigate in-app.
        """
        if not row:
            return

        remote_path = self.full_path(row)

        # Fast path for entries already known to be directories.
        if row.get("type") == "d":
            self.open_path(remote_path)
            return

        # For anything not marked as a directory, verify against the device.
        # This prevents symlinked/stale folders being pulled/opened as files.
        def checked(is_directory):
            if self._closing:
                return

            if is_directory:
                row["type"] = "d"

                # Repair the current in-memory cache so future opens are instant.
                key = (self.adb.serial, self.current_path.rstrip("/") or "/")
                for cached in self.dir_cache.get(key, []):
                    if cached.get("name") == row.get("name"):
                        cached["type"] = "d"
                        break

                self.open_path(remote_path)
            else:
                self.open_remote_file(row)

        # Use an independent lightweight worker so a slow ADB check does not
        # freeze the UI.
        worker = Worker(lambda: self.adb.is_dir(remote_path))
        self.open_file_workers.add(worker)

        def cleanup():
            self.open_file_workers.discard(worker)
            worker.deleteLater()

        def ok(result):
            cleanup()
            checked(result)

        def fail(_message):
            cleanup()
            if not self._closing:
                # If the type check itself fails, do not risk treating an
                # unknown path as a local-openable file.
                self.error_dialog(
                    f"Could not determine whether this item is a folder:\\n{remote_path}"
                )

        worker.ok.connect(ok)
        worker.fail.connect(fail)
        worker.start()

    def open_detail_item(self, item, column=0):
        row = item.data(0, Qt.UserRole)
        self.open_row(row)

    def details_context_menu(self, pos):
        # QTreeWidget's custom-context-menu position is relative to its
        # viewport. Pass the originating view through so the menu is mapped
        # from the correct coordinate system.
        self.context_menu(pos, source_view=self.details)

    @staticmethod
    def wrap_display_name(name):
        """
        Add invisible wrap opportunities so long Android filenames can wrap
        even when they contain no spaces. The real filename stored in the item
        data is unchanged.
        """
        out = []
        run = 0
        for ch in name:
            out.append(ch)
            run += 1

            # Natural places to wrap filenames.
            if ch in "._-()[]{}":
                out.append("\u200b")
                run = 0
            elif run >= 12:
                out.append("\u200b")
                run = 0

        return "".join(out)

    def icon_item_size(self, display_name):
        """
        Calculate a tile height from the actual wrapped filename so text is
        never clipped. Width stays compact, height grows only when needed.
        """
        tile_width = 132
        text_width = tile_width - 12
        metrics = QFontMetrics(self.files.font())

        rect = metrics.boundingRect(
            0, 0,
            text_width, 1000,
            Qt.TextWordWrap | Qt.TextWrapAnywhere | Qt.AlignHCenter,
            display_name
        )

        # 52px icon + margins + calculated wrapped text height.
        height = 52 + 14 + max(metrics.height(), rect.height()) + 10
        return QSize(tile_width, max(88, height))

    def render_rows(self, path, rows):
        self.current_path = path.rstrip("/") or "/"
        self.address_bar.set_path(self.current_path)
        title = "Home" if self.current_path == "/" else PurePosixPath(self.current_path).name
        self.heading.setText(title)

        if self.tabs and 0 <= self.active_tab < len(self.tabs):
            self.tabs[self.active_tab]["path"] = self.current_path
            self.tabs[self.active_tab]["title"] = title
            self.rebuild_tabs()

        self.files.clear()
        self.details.clear()

        for row in rows:
            icon = self.icon_provider.icon_for(row)

            # Icon view: show the complete name. Invisible wrap points are
            # inserted for long no-space filenames, and each tile grows
            # vertically as needed instead of clipping the text.
            display_name = self.wrap_display_name(row["name"])
            item = QListWidgetItem(icon, display_name)
            item.setData(Qt.UserRole, row)
            item.setTextAlignment(Qt.AlignHCenter | Qt.AlignTop)
            item.setSizeHint(self.icon_item_size(display_name))
            item.setToolTip(
                f"{row['name']}\\n"
                f"{self.row_type_text(row)}\\n"
                f"Permissions: {row.get('mode','')}"
            )
            self.files.addItem(item)

            # Details view.
            modified = row.get("mtime_text", "")
            size_text = "" if row["type"] == "d" else self.human(row["size"])
            details_item = QTreeWidgetItem([
                row["name"],
                modified,
                self.row_type_text(row),
                size_text,
                row.get("mode", "")
            ])
            details_item.setIcon(0, icon)
            details_item.setData(0, Qt.UserRole, row)
            self.details.addTopLevelItem(details_item)

        self.current_rows = rows
        self.status.setText(f"{len(rows)} items")
    def refresh_files(self, force=False):
        if not self.adb.serial or self._closing:
            return

        path = self.current_path or "/"
        if not path.startswith("/"):
            path = "/" + path
        path = path.rstrip("/") or "/"
        key = (self.adb.serial, path)

        # During boot/root-cache stage, don't issue a competing ADB list for /.
        if (
            path == "/"
            and key not in self.dir_cache
            and self.cache_worker
            and self.cache_worker.isRunning()
            and not force
        ):
            return

        # If cached, render immediately from RAM. No ADB command.
        if not force and key in self.dir_cache:
            rows = self.dir_cache[key]
            self.render_rows(path, rows)
            self.status.setText(f"{len(rows)} items • cached")
            return

        def ok(rows):
            for row in rows:
                row["_metadata_loaded"] = True
            self.dir_cache[key] = rows

            full_paths = []
            for row in rows:
                full = str(PurePosixPath(path) / row["name"])
                full_paths.append(full)
                self.path_cache.add(full)

            self.precache_by_parent[path] = full_paths

            # Only repaint if this is still the folder the user is viewing.
            # The cache is updated regardless.
            if self.current_path == path:
                self.render_rows(path, rows)

        self.start_job(
            lambda: self.adb.list_dir(path),
            ok,
            f"Opening {path}..."
        )

    def refresh_current_metadata_background(self):
        """
        Fetch size/date/permissions for only the folder currently on screen.
        Used by Details view; icon navigation remains instant.
        """
        if not self.adb.serial or self._closing:
            return

        path = self.current_path
        key = (self.adb.serial, path)
        current = self.dir_cache.get(key, [])

        if current and all(r.get("_metadata_loaded") for r in current):
            return

        # Use the regular worker only when it is free. Never delay navigation.
        if self.worker and self.worker.isRunning():
            return

        def ok(rows):
            for row in rows:
                row["_metadata_loaded"] = True
            self.dir_cache[key] = rows

            # Only repaint if user is still in the same folder.
            if self.current_path == path and self.view_mode == "details":
                self.render_rows(path, rows)

        self.start_job(
            lambda: self.adb.list_dir(path),
            ok,
            "Loading details..."
        )

    @staticmethod
    def human(n):
        units = ["B", "KB", "MB", "GB", "TB"]
        f = float(n)
        for u in units:
            if f < 1024 or u == "TB":
                return f"{f:.0f} {u}" if u == "B" else f"{f:.1f} {u}"
            f /= 1024

    @staticmethod
    def _sha256_file(path):
        h = hashlib.sha256()
        with open(path, "rb") as f:
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()

    def _shell_open_and_wait(self, local_path):
        """
        Open a local file with the Windows-associated application and wait for
        that application process to close.

        ShellExecuteEx is used instead of os.startfile so we can obtain a
        process handle and know when it is safe to compare the working copy.
        """
        if os.name != "nt":
            raise RuntimeError("Automatic open/edit/sync currently requires Windows.")

        import ctypes
        from ctypes import wintypes

        SEE_MASK_NOCLOSEPROCESS = 0x00000040
        SW_SHOWNORMAL = 1
        WAIT_OBJECT_0 = 0x00000000
        WAIT_TIMEOUT = 0x00000102

        class SHELLEXECUTEINFOW(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("fMask", wintypes.ULONG),
                ("hwnd", wintypes.HWND),
                ("lpVerb", wintypes.LPCWSTR),
                ("lpFile", wintypes.LPCWSTR),
                ("lpParameters", wintypes.LPCWSTR),
                ("lpDirectory", wintypes.LPCWSTR),
                ("nShow", ctypes.c_int),
                ("hInstApp", wintypes.HINSTANCE),
                ("lpIDList", ctypes.c_void_p),
                ("lpClass", wintypes.LPCWSTR),
                ("hkeyClass", wintypes.HKEY),
                ("dwHotKey", wintypes.DWORD),
                ("hIcon", wintypes.HANDLE),
                ("hProcess", wintypes.HANDLE),
            ]

        sei = SHELLEXECUTEINFOW()
        sei.cbSize = ctypes.sizeof(SHELLEXECUTEINFOW)
        sei.fMask = SEE_MASK_NOCLOSEPROCESS
        sei.hwnd = int(self.winId())
        sei.lpVerb = "open"
        sei.lpFile = str(local_path)
        sei.lpParameters = None
        sei.lpDirectory = str(Path(local_path).parent)
        sei.nShow = SW_SHOWNORMAL

        shell32 = ctypes.windll.shell32
        kernel32 = ctypes.windll.kernel32

        if not shell32.ShellExecuteExW(ctypes.byref(sei)):
            raise ctypes.WinError()

        # Some shell handlers do not return a process handle. Fall back to a
        # short settle delay in that unusual case rather than failing to open.
        if not sei.hProcess:
            import time
            time.sleep(2.0)
            return

        try:
            while not self._closing:
                result = kernel32.WaitForSingleObject(sei.hProcess, 250)
                if result == WAIT_OBJECT_0:
                    break
                if result != WAIT_TIMEOUT:
                    break
        finally:
            kernel32.CloseHandle(sei.hProcess)

    def open_remote_file(self, row):
        """
        Pull one Android file to a temporary working copy, open it with the
        Windows-associated application, then:

        - unchanged: delete the temporary copy
        - changed: push it back to the exact original path, restore the exact
          original chmod permissions, then delete the temporary copy

        This runs on its own worker so the Explorer remains usable while the
        external editor/viewer is open.
        """
        if self._closing or not self.adb.serial:
            return

        remote_path = self.full_path(row)
        original_name = row["name"]

        # Give every open operation its own directory so same-name files from
        # different Android folders can never collide.
        job_dir = tempfile.mkdtemp(prefix="edit_", dir=self.open_file_temp_dir)
        local_path = os.path.join(job_dir, original_name)

        def worker_fn():
            # Always query permissions directly before editing so cached/stale
            # values cannot cause us to restore the wrong mode.
            original_mode = self.adb.get_mode(remote_path)

            self.adb.pull(remote_path, job_dir)

            if not os.path.isfile(local_path):
                raise RuntimeError(f"Could not pull file to PC: {remote_path}")

            before_hash = self._sha256_file(local_path)

            self._shell_open_and_wait(local_path)

            if self._closing:
                # App is shutting down. Leave a modified working copy alone
                # rather than risking deletion before we know its state.
                return {
                    "status": "closing",
                    "local_path": local_path,
                    "remote_path": remote_path,
                    "mode": original_mode,
                }

            after_hash = self._sha256_file(local_path)

            if after_hash == before_hash:
                try:
                    os.remove(local_path)
                    os.rmdir(job_dir)
                except Exception:
                    pass
                return {
                    "status": "unchanged",
                    "remote_path": remote_path,
                }

            # Changed: send back to the exact original Android path.
            try:
                self.adb.push_exact(local_path, remote_path)
                self.adb.chmod(remote_path, original_mode, recursive=False)
            except Exception:
                # Keep the local modified copy if sync fails, so no edits are lost.
                raise RuntimeError(
                    f"Could not sync the modified file back to Android.\\n\\n"
                    f"Your edited copy has been kept here:\\n{local_path}"
                )

            # Sync succeeded; temporary working copy is no longer needed.
            try:
                os.remove(local_path)
                os.rmdir(job_dir)
            except Exception:
                pass

            return {
                "status": "synced",
                "remote_path": remote_path,
                "mode": original_mode,
            }

        worker = Worker(worker_fn)
        self.open_file_workers.add(worker)

        def cleanup_worker():
            self.open_file_workers.discard(worker)
            worker.deleteLater()

        def finished(result):
            cleanup_worker()
            if self._closing:
                return

            status = result.get("status")
            if status == "unchanged":
                self.status.setText(f"Closed {original_name} • no changes")
            elif status == "synced":
                self.status.setText(
                    f"Saved {original_name} back to device • permissions {result['mode']}"
                )
                self.invalidate_cache(self.current_path)
                self.refresh_files(force=True)
            elif status == "closing":
                self.status.setText("Closing")

        def failed(message):
            cleanup_worker()
            if not self._closing:
                self.error_dialog(message)

        worker.ok.connect(finished)
        worker.fail.connect(failed)
        self.status.setText(f"Opening {original_name} from device…")
        worker.start()

    def open_path(self, path):
        """
        Navigate to an Android directory.

        The breadcrumb conversion changed refresh_files() to read from
        self.current_path instead of the hidden editable QLineEdit. The old
        implementation only changed self.path text, so refresh_files() kept
        reopening the previous directory.

        Update the actual navigation state first, then render from cache/ADB.
        """
        if not path:
            path = "/"

        path = str(path).strip()
        if not path.startswith("/"):
            path = "/" + path

        path = path.rstrip("/") or "/"

        self.current_path = path

        if hasattr(self, "address_bar"):
            self.address_bar.set_path(path)
        else:
            self.path.setText(path)

        self.refresh_files()

    def go_up(self):
        self.open_path(str(PurePosixPath(self.current_path).parent) or "/")

    def selected_rows(self):
        if self.view_mode == "details":
            return [
                i.data(0, Qt.UserRole)
                for i in self.details.selectedItems()
                if i.data(0, Qt.UserRole) is not None
            ]
        return [i.data(Qt.UserRole) for i in self.files.selectedItems()]

    def full_path(self, row):
        base = self.current_path.rstrip("/")
        return f"{base}/{row['name']}" if base else f"/{row['name']}"

    def open_item(self, item):
        row = item.data(Qt.UserRole)
        self.open_row(row)

    def context_menu(self, pos, source_view=None):
        menu = QMenu(self)
        open_act = menu.addAction("Open")
        menu.addSeparator()
        copy_act = menu.addAction("Copy\tCtrl+C")
        paste_act = menu.addAction("Paste\tCtrl+V")
        pull_act = menu.addAction("Pull to PC...")
        menu.addSeparator()
        rename_act = menu.addAction("Rename")
        delete_act = menu.addAction("Delete\tDel")
        perm_act = menu.addAction("Set permissions...")
        menu.addSeparator()
        rw_act = menu.addAction("Remount partitions R/W")
        ro_act = menu.addAction("Remount partitions R/O")
        menu.addSeparator()
        refresh_act = menu.addAction("Refresh")

        rows = self.selected_rows()
        open_act.setEnabled(len(rows) == 1)
        rename_act.setEnabled(len(rows) == 1)
        copy_act.setEnabled(bool(rows))
        pull_act.setEnabled(bool(rows))
        delete_act.setEnabled(bool(rows))
        perm_act.setEnabled(bool(rows))
        paste_act.setEnabled(bool(self.clipboard))

        # customContextMenuRequested gives viewport-local coordinates for
        # QListWidget/QTreeWidget. Mapping through the widget itself adds the
        # viewport/header offset and causes the popup to appear far away from
        # the cursor (especially in Details view).
        source_view = source_view or self.files
        global_pos = source_view.viewport().mapToGlobal(pos)
        act = menu.exec(global_pos)
        if act == open_act and rows:
            self.open_row(rows[0])
        elif act == copy_act:
            self.copy_selected()
        elif act == paste_act:
            self.paste()
        elif act == pull_act:
            self.pull_selected()
        elif act == rename_act:
            self.rename_selected()
        elif act == delete_act:
            self.delete_selected()
        elif act == perm_act:
            self.permissions()
        elif act == rw_act:
            self.remount_rw()
        elif act == ro_act:
            self.remount_ro()
        elif act == refresh_act:
            self.refresh_files(force=True)

    def copy_selected(self):
        rows = self.selected_rows()
        if rows:
            self.clipboard = [self.full_path(r) for r in rows]
            self.status.setText(f"Copied {len(rows)} item(s)")

    def paste(self):
        if not self.clipboard:
            return

        srcs = list(self.clipboard)
        dst = self.current_path

        def worker():
            reserved = set()
            created = []

            for src in srcs:
                destination = self.unique_remote_destination(
                    src,
                    dst,
                    reserved=reserved
                )
                self.adb.copy_to(src, destination)
                created.append(destination)

            return created

        def done(created):
            # Add newly-created paths to the cache immediately, then force one
            # authoritative refresh to capture correct type/size/permissions.
            for new_path in created:
                self.cache_add_path(new_path)

            self.invalidate_cache(dst)
            self.refresh_files(force=True)

        self.start_job(
            worker,
            done,
            f"Pasting {len(srcs)} item(s)..."
        )

    def delete_selected(self):
        rows = self.selected_rows()
        if not rows:
            return
        if not self.confirm_dialog(f"Permanently delete {len(rows)} selected item(s)?\n\nThis cannot be undone."):
            return
        paths = [self.full_path(r) for r in rows]
        def done(_):
            for p in paths:
                self.cache_remove_path(p)
            self.invalidate_cache(self.current_path)
            self.refresh_files(force=True)
        self.start_job(lambda: [self.adb.delete(p) for p in paths], done, "Deleting...")

    def rename_selected(self):
        rows = self.selected_rows()
        if len(rows) != 1:
            return
        row = rows[0]
        new, ok = self.input_dialog("New name:", row["name"])
        if not ok or not new.strip() or "/" in new:
            return
        old = self.full_path(row)
        parent = self.current_path.rstrip("/")
        newp = f"{parent}/{new}" if parent else f"/{new}"
        def done(_):
            self.cache_move_path(old, newp)
            self.invalidate_cache(self.current_path)
            self.refresh_files(force=True)
        self.start_job(lambda: self.adb.rename(old, newp), done, "Renaming...")

    def new_folder(self):
        name, ok = self.input_dialog("Folder name:")
        if not ok or not name.strip() or "/" in name:
            return
        parent = self.current_path.rstrip("/")
        path = f"{parent}/{name}" if parent else f"/{name}"
        def done(_):
            self.cache_add_path(path)
            self.invalidate_cache(self.current_path)
            self.refresh_files(force=True)
        self.start_job(lambda: self.adb.mkdir(path), done, "Creating folder...")

    def permissions(self):
        rows = self.selected_rows()
        if not rows:
            return
        mode, ok = self.input_dialog("Permission mode (e.g. 644, 755, 777):")
        if not ok or not re.fullmatch(r"[0-7]{3,4}", mode):
            return
        recursive = any(r["type"] == "d" for r in rows) and self.confirm_dialog(
            "Apply recursively inside selected folders?"
        )
        paths = [self.full_path(r) for r in rows]

        def done(_):
            # Reflect the successful chmod immediately in all cached rows,
            # including descendants when recursive mode was chosen.
            self.update_cached_permissions(paths, mode, recursive)

            # Then verify against the device with a fresh directory listing.
            # Do not clear the corrected cache first; keeping it populated
            # avoids flashing the old value while the ADB refresh completes.
            self.refresh_files(force=True)
            self.status.setText(
                f"Permissions updated to {mode}"
                + (" recursively" if recursive else "")
            )

        self.start_job(
            lambda: [self.adb.chmod(p, mode, recursive) for p in paths],
            done,
            f"Applying chmod {mode}..."
        )

    def push_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Push file to Android")
        if not path:
            return
        def done(_):
            self.cache_add_path(str(PurePosixPath(self.current_path) / os.path.basename(path)))
            self.invalidate_cache(self.current_path)
            self.refresh_files(force=True)
        self.start_job(lambda: self.adb.push(path, self.current_path), done, f"Pushing {os.path.basename(path)}...")

    def pull_selected(self):
        rows = self.selected_rows()
        if not rows:
            return
        dest = QFileDialog.getExistingDirectory(self, "Save to...")
        if not dest:
            return
        paths = [self.full_path(r) for r in rows]
        self.start_job(lambda: [self.adb.pull(p, dest) for p in paths], lambda _: self.status.setText("Pull complete"), "Pulling...")

    def install_apk(self):
        apk, _ = QFileDialog.getOpenFileName(self, "Install APK", filter="Android APK (*.apk)")
        if apk:
            self.start_job(lambda: self.adb.install(apk), lambda msg: self.info_dialog(msg or "Installed"), "Installing APK...")

    def remount_rw(self):
        if self.confirm_dialog("Attempt to remount Android partitions read/write?"):
            self.start_job(self.adb.remount_rw, lambda msg: self.info_dialog(msg), "Remounting R/W...")

    def remount_ro(self):
        self.start_job(self.adb.remount_ro, lambda msg: self.info_dialog(msg), "Remounting R/O...")


    def closeEvent(self, event):
        """
        Fast, clean shutdown.

        Long recursive ADB scans are explicitly terminated first, then Qt
        threads are given a short window to exit. This avoids both hanging on
        close and the 'QThread destroyed while still running' warning.
        """
        try:
            self.settings.setValue("theme", self.theme)
            self.settings.setValue("view_mode", self.view_mode)
            self.settings.setValue("window_geometry", self.saveGeometry())
            self.save_details_column_widths()
            self.settings.sync()
        except Exception:
            pass

        self._closing = True

        try:
            if hasattr(self, "cache_sync_timer") and self.cache_sync_timer:
                self.cache_sync_timer.stop()
        except Exception:
            pass

        try:
            self.progress.setVisible(False)
        except Exception:
            pass

        # Kill underlying adb subprocesses so worker functions return promptly.
        try:
            self.adb.cancel_all()
        except Exception:
            pass

        threads = []
        for name in ("worker", "cache_worker"):
            thread = getattr(self, name, None)
            try:
                if thread and thread.isRunning():
                    threads.append(thread)
            except Exception:
                pass

        # File-open workers poll self._closing while waiting for external apps,
        # so include them in shutdown handling too.
        for thread in list(getattr(self, "open_file_workers", set())):
            try:
                if thread and thread.isRunning():
                    threads.append(thread)
            except Exception:
                pass

        for thread in threads:
            try:
                thread.requestInterruption()
                thread.quit()
            except Exception:
                pass

        # Brief wait only.
        for thread in threads:
            try:
                if not thread.wait(1200):
                    thread.terminate()
                    thread.wait(300)
            except Exception:
                pass

        try:
            if hasattr(self, "drag_temp_dir") and os.path.isdir(self.drag_temp_dir):
                shutil.rmtree(self.drag_temp_dir, ignore_errors=True)
        except Exception:
            pass

        # Only remove the file-open temp root if no retained working copies
        # remain. Modified files are intentionally preserved on sync failure.
        try:
            if (
                hasattr(self, "open_file_temp_dir")
                and os.path.isdir(self.open_file_temp_dir)
                and not os.listdir(self.open_file_temp_dir)
            ):
                os.rmdir(self.open_file_temp_dir)
        except Exception:
            pass

        event.accept()



if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    icon_path = find_app_icon_path()
    if icon_path:
        try:
            app_icon = QIcon(icon_path)
            if not app_icon.isNull():
                app.setWindowIcon(app_icon)
        except Exception:
            pass

    win = Explorer()
    win.show()
    sys.exit(app.exec())
