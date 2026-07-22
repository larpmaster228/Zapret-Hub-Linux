from __future__ import annotations

import ctypes
import base64
from concurrent.futures import TimeoutError as FuturesTimeout
from datetime import datetime
import hashlib
import json
import locale
import os
import platform
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from installer.embedded_app_icon import APP_PNG_BASE64
from installer.common import (
    INSTALLER_VERSION,
    copy_bundled_uninstaller,
    perform_uninstall,
    terminate_running_instances,
    write_uninstall_registry as _write_uninstall_registry_common,
)
from PySide6.QtCore import QEasingCurve, QEvent, QObject, Property, QPropertyAnimation, QRectF, QSize, QThread, QTimer, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QColor, QIcon, QImage, QMouseEvent, QPainter, QPen, QPixmap, QShowEvent
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QProgressBar,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

if sys.platform.startswith("win"):
    import winreg


def _is_ru() -> bool:
    try:
        lang = (locale.getdefaultlocale()[0] or "").lower()  # type: ignore[call-arg]
    except Exception:
        lang = ""
    return lang.startswith("ru")


RU = _is_ru()
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\ZapretHub"
INSTALLER_LOG_PATH = Path(tempfile.gettempdir()) / "zapret_hub_installer.log"

def tr(ru: str, en: str) -> str:
    return ru if RU else en


def _resource_candidates() -> list[Path]:
    candidates: list[Path] = []
    try:
        file_path = Path(__file__).resolve()
    except Exception:
        file_path = None
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        candidates.append(exe_dir)
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            candidates.append(Path(meipass))
        if file_path is not None:
            candidates.append(file_path.parent)
            for parent in file_path.parents:
                candidates.append(parent)
    else:
        if file_path is not None:
            candidates.append(file_path.parents[1])
            candidates.append(file_path.parent)
            for parent in file_path.parents:
                candidates.append(parent)
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate).lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique


def resource_root() -> Path:
    for candidate in _resource_candidates():
        if (candidate / "ui_assets" / "icons" / "installer_runtime_icon.png").exists():
            return candidate
    for candidate in _resource_candidates():
        if (candidate / "ui_assets" / "icons" / "app.png").exists():
            return candidate
    for candidate in _resource_candidates():
        if (candidate / "ui_assets" / "icons" / "app.ico").exists():
            return candidate
    return _resource_candidates()[0]


def payload_root() -> Path:
    for candidate in _resource_candidates():
        if (candidate / "installer_payload").exists():
            return candidate
        if (candidate / "win_x64.zip").exists() or (candidate / "win_arm64.zip").exists():
            return candidate
    return resource_root()


def _installer_log(event: str, **context: object) -> None:
    try:
        timestamp = datetime.now().isoformat(timespec="seconds")
        details = ", ".join(f"{key}={context[key]!r}" for key in sorted(context))
        line = f"[{timestamp}] {event}"
        if details:
            line += f" | {details}"
        with INSTALLER_LOG_PATH.open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")
    except Exception:
        return


def _is_within_path(path: Path, root: Path) -> bool:
    try:
        resolved_path = path.resolve()
        resolved_root = root.resolve()
        resolved_path.relative_to(resolved_root)
        return True
    except Exception:
        return False


def _top_level_install_name(path: Path, install_dir: Path) -> str:
    try:
        relative = path.resolve().relative_to(install_dir.resolve())
    except Exception:
        return ""
    parts = relative.parts
    return parts[0] if parts else ""


def _is_preserved_user_root(path: Path, install_dir: Path) -> bool:
    return _top_level_install_name(path, install_dir) in {"data", "mods", "configs", "cache"}


def _embedded_app_pixmap() -> QPixmap:
    try:
        raw = base64.b64decode(APP_PNG_BASE64)
    except Exception:
        return QPixmap()
    image = QImage.fromData(raw, "PNG")
    if image.isNull():
        return QPixmap()
    return QPixmap.fromImage(image)


def app_icon() -> QIcon:
    embedded = _embedded_app_pixmap()
    if not embedded.isNull():
        return QIcon(embedded)
    installer_png_path = resource_root() / "ui_assets" / "icons" / "installer_runtime_icon.png"
    if installer_png_path.exists():
        image = QImage(str(installer_png_path))
        if not image.isNull():
            pixmap = QPixmap.fromImage(image)
            if not pixmap.isNull():
                return QIcon(pixmap)
    png_path = resource_root() / "ui_assets" / "icons" / "app.png"
    if png_path.exists():
        image = QImage(str(png_path))
        if not image.isNull():
            pixmap = QPixmap.fromImage(image)
            if not pixmap.isNull():
                return QIcon(pixmap)
    icon_path = resource_root() / "ui_assets" / "icons" / "app.ico"
    if icon_path.exists():
        icon = QIcon(str(icon_path))
        if not icon.isNull():
            return icon
    if getattr(sys, "frozen", False):
        icon = QIcon(str(Path(sys.executable)))
        if not icon.isNull():
            return icon
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setBrush(QColor("#5865f2"))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(QRectF(4, 4, 56, 56), 14, 14)
    painter.setPen(QPen(QColor("#ffffff"), 4.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    painter.drawLine(20, 44, 30, 24)
    painter.drawLine(30, 24, 44, 40)
    painter.end()
    return QIcon(pixmap)


def app_pixmap(size: int) -> QPixmap:
    embedded = _embedded_app_pixmap()
    dpr = 1.0
    app_instance = QApplication.instance()
    try:
        if app_instance is not None and app_instance.primaryScreen() is not None:
            dpr = max(1.0, float(app_instance.primaryScreen().devicePixelRatio()))
    except Exception:
        dpr = 1.0
    target_px = max(size, int(round(size * dpr)))
    if not embedded.isNull():
        scaled = embedded.scaled(target_px, target_px, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        scaled.setDevicePixelRatio(dpr)
        return scaled
    installer_png_path = resource_root() / "ui_assets" / "icons" / "installer_runtime_icon.png"
    if installer_png_path.exists():
        image = QImage(str(installer_png_path))
        pixmap = QPixmap.fromImage(image) if not image.isNull() else QPixmap()
        if not pixmap.isNull():
            scaled = pixmap.scaled(target_px, target_px, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            scaled.setDevicePixelRatio(dpr)
            return scaled
    icon_path = resource_root() / "ui_assets" / "icons" / "app.png"
    if icon_path.exists():
        image = QImage(str(icon_path))
        pixmap = QPixmap.fromImage(image) if not image.isNull() else QPixmap()
        if not pixmap.isNull():
            scaled = pixmap.scaled(target_px, target_px, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            scaled.setDevicePixelRatio(dpr)
            return scaled
    ico_path = resource_root() / "ui_assets" / "icons" / "app.ico"
    if ico_path.exists():
        pixmap = QPixmap(str(ico_path))
        if not pixmap.isNull():
            scaled = pixmap.scaled(target_px, target_px, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            scaled.setDevicePixelRatio(dpr)
            return scaled
    return app_icon().pixmap(size, size)


def close_icon() -> QIcon:
    icon_path = resource_root() / "ui_assets" / "icons" / "window_close_dark.svg"
    if icon_path.exists():
        icon = QIcon(str(icon_path))
        if not icon.isNull():
            return icon
    pixmap = QPixmap(24, 24)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    pen = QPen(QColor("#e7edf9"), 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    painter.drawLine(7, 7, 17, 17)
    painter.drawLine(17, 7, 7, 17)
    painter.end()
    return QIcon(pixmap)


def close_pixmap(size: int) -> QPixmap:
    icon = close_icon()
    pixmap = icon.pixmap(size, size)
    if not pixmap.isNull():
        return pixmap
    fallback = QPixmap(size, size)
    fallback.fill(Qt.GlobalColor.transparent)
    painter = QPainter(fallback)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    pen = QPen(QColor("#e7edf9"), max(1.8, size / 10.0), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    inset = max(5, int(size * 0.28))
    painter.drawLine(inset, inset, size - inset, size - inset)
    painter.drawLine(size - inset, inset, inset, size - inset)
    painter.end()
    return fallback


def apply_native_window_icons(widget: QWidget) -> None:
    if not sys.platform.startswith("win"):
        return
    icon = app_icon()
    try:
        widget.setWindowIcon(icon)
        app = QApplication.instance()
        if app is not None:
            app.setWindowIcon(icon)
    except Exception:
        pass


def title_logo() -> QIcon:
    png_path = resource_root() / "ui_assets" / "icons" / "app.png"
    if png_path.exists():
        icon = QIcon(str(png_path))
        if not icon.isNull():
            return icon
    return app_icon()


def default_install_dir() -> Path:
    return Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Zapret Hub"


def _normalized_path_text(path: Path) -> str:
    try:
        return str(path.resolve()).rstrip("\\/").lower()
    except Exception:
        return str(path).rstrip("\\/").lower()


def _is_drive_root(path: Path) -> bool:
    try:
        resolved = path.resolve()
    except Exception:
        resolved = path
    return bool(resolved.anchor) and str(resolved).rstrip("\\/").lower() == resolved.anchor.rstrip("\\/").lower()


def _is_dangerous_install_dir(path: Path) -> bool:
    if _is_drive_root(path):
        return True
    dangerous: list[Path] = []
    for value in (
        os.environ.get("SystemRoot", r"C:\Windows"),
        os.environ.get("WINDIR", r"C:\Windows"),
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        os.environ.get("ProgramData", r"C:\ProgramData"),
        os.environ.get("USERPROFILE", ""),
        os.environ.get("PUBLIC", r"C:\Users\Public"),
    ):
        if value:
            dangerous.append(Path(value))
    target = _normalized_path_text(path)
    return any(target == _normalized_path_text(candidate) for candidate in dangerous)


def _looks_like_zapret_hub_dir(path: Path) -> bool:
    if not path.exists():
        return False
    return any((path / name).exists() for name in ("zapret_hub.exe", "Zapret_Hub.exe", "uninstall_zaprethub.exe"))


def _suggest_empty_install_dir(path: Path) -> Path:
    base = path / "Zapret Hub"
    if not base.exists() or not any(base.iterdir()):
        return base
    for index in range(2, 100):
        candidate = path / f"Zapret Hub {index}"
        if not candidate.exists() or not any(candidate.iterdir()):
            return candidate
    return path / f"Zapret Hub {int(time.time())}"


def _suggest_safe_install_dir(path: Path) -> Path:
    program_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    if _is_drive_root(path):
        return _suggest_empty_install_dir(path)
    if _normalized_path_text(path) == _normalized_path_text(program_files):
        return _suggest_empty_install_dir(path)
    return default_install_dir()


def _resolve_requested_install_dir(path: Path) -> Path:
    candidate = path.expanduser()
    if not candidate.is_absolute():
        candidate = (Path.cwd() / candidate).resolve()
    if _looks_like_zapret_hub_dir(candidate):
        return candidate
    try:
        has_items = candidate.exists() and any(candidate.iterdir())
    except OSError:
        has_items = True
    if not candidate.exists() or not has_items:
        return candidate
    if candidate.name.casefold() == "zapret hub":
        return candidate
    return candidate / "Zapret Hub"


def _native_windows_machine() -> str:
    if not sys.platform.startswith("win"):
        return platform.machine().lower()
    try:
        process_machine = ctypes.c_ushort(0)
        native_machine = ctypes.c_ushort(0)
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        is_wow64_process2 = getattr(kernel32, "IsWow64Process2", None)
        if is_wow64_process2:
            current_process = kernel32.GetCurrentProcess()
            ok = is_wow64_process2(current_process, ctypes.byref(process_machine), ctypes.byref(native_machine))
            if ok:
                machine_map = {
                    0x014c: "x86",
                    0x8664: "amd64",
                    0xAA64: "arm64",
                }
                return machine_map.get(int(native_machine.value), platform.machine().lower())
    except Exception:
        pass
    arch = (os.environ.get("PROCESSOR_ARCHITEW6432") or os.environ.get("PROCESSOR_ARCHITECTURE") or platform.machine()).lower()
    if "arm64" in arch or "aarch64" in arch:
        return "arm64"
    if "amd64" in arch or "x86_64" in arch or "x64" in arch:
        return "amd64"
    return arch


def detect_payload_name() -> str:
    machine = _native_windows_machine()
    if "arm" in machine or "aarch64" in machine:
        return "win_arm64.zip"
    return "win_x64.zip"


UPDATE_URL = "https://goshkow.ru/zapret-hub/update"
METADATA_TIMEOUT_SEC = 10.0
DOWNLOAD_CONNECT_TIMEOUT_SEC = 12.0
DOWNLOAD_STALL_TIMEOUT_SEC = 45.0
REMOTE_VERSION_TIMEOUT_SEC = 4.0
# Hard UI/connect ceiling: never leave the user on «Подключение…» longer than this.
CONNECT_WATCHDOG_SEC = 15.0
# Keep a generous ceiling for slow links (~130MB x64). Stall timeout covers freezes.
DOWNLOAD_TOTAL_TIMEOUT_SEC = 30 * 60
PROCESS_KILL_TIMEOUT_SEC = 12.0


class InstallAbort(Exception):
    """Raised when the user closes the installer mid-operation."""


def _check_cancel(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise InstallAbort(tr("Установка отменена.", "Installation cancelled."))


def _run_with_deadline(func, *, timeout: float, cancel_event: threading.Event | None = None):
    """Run func() with a hard wall-clock deadline on a daemon thread.

    Important: ``with ThreadPoolExecutor(): future.result(timeout=...)`` is unsafe here.
    On timeout the context manager still calls ``shutdown(wait=True)`` and can block
    forever while DNS/connect hangs. Daemon thread + polling avoids that freeze.
    """
    _check_cancel(cancel_event)
    box: dict[str, object] = {}
    errors: list[BaseException] = []
    done = threading.Event()

    def _target() -> None:
        try:
            box["value"] = func()
        except BaseException as exc:  # noqa: BLE001 - propagate any failure to caller
            errors.append(exc)
        finally:
            done.set()

    thread = threading.Thread(target=_target, name="zapret-hub-installer-net", daemon=True)
    thread.start()
    deadline = time.monotonic() + max(0.1, float(timeout))
    while True:
        _check_cancel(cancel_event)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(tr("goshkow.ru не отвечает (таймаут)", "goshkow.ru is not responding (timeout)"))
        if done.wait(timeout=min(0.25, remaining)):
            break
    if errors:
        raise errors[0]
    return box.get("value")


def _friendly_network_error(error: BaseException, *, context: str = "goshkow.ru") -> str:
    if isinstance(error, InstallAbort):
        return str(error)
    if isinstance(error, HTTPError):
        code = int(getattr(error, "code", 0) or 0)
        reason = str(getattr(error, "reason", "") or error)
        if 500 <= code <= 599:
            return tr(f"Ошибка HTTP {code}: {context} недоступен ({reason})", f"HTTP {code}: {context} unavailable ({reason})")
        if code == 404:
            return tr(f"Ошибка HTTP 404: сборка не найдена на {context}", f"HTTP 404: build not found on {context}")
        return tr(f"Ошибка HTTP {code}: {reason}", f"HTTP {code}: {reason}")
    if isinstance(error, ssl.SSLError):
        return tr(f"Ошибка TLS при подключении к {context}", f"TLS error connecting to {context}")
    if isinstance(error, socket.gaierror):
        return tr(f"Не удалось найти {context} (DNS)", f"Could not resolve {context} (DNS)")
    if isinstance(error, TimeoutError) or isinstance(error, socket.timeout) or isinstance(error, FuturesTimeout):
        return tr(f"{context} не отвечает (таймаут)", f"{context} is not responding (timeout)")
    if isinstance(error, URLError):
        reason = getattr(error, "reason", error)
        if isinstance(reason, BaseException):
            return _friendly_network_error(reason, context=context)
        text = str(reason or error)
        lower = text.lower()
        if "timed out" in lower or "timeout" in lower:
            return tr(f"{context} не отвечает (таймаут)", f"{context} is not responding (timeout)")
        if "getaddrinfo" in lower or "name or service not known" in lower or "nodename" in lower:
            return tr(f"Не удалось найти {context} (DNS)", f"Could not resolve {context} (DNS)")
        if "ssl" in lower or "certificate" in lower:
            return tr(f"Ошибка TLS при подключении к {context}", f"TLS error connecting to {context}")
        return tr(f"Сеть: не удалось подключиться к {context} ({text})", f"Network: could not reach {context} ({text})")
    if isinstance(error, json.JSONDecodeError):
        return tr("Не удалось разобрать ответ сайта (JSON)", "Failed to parse site response (JSON)")
    if isinstance(error, ValueError):
        text = str(error)
        lower = text.lower()
        if "sha" in lower or "digest" in lower or "checksum" in lower or "размер" in lower or "size" in lower:
            return tr(f"Не удалось проверить сборку: {text}", f"Failed to verify build: {text}")
        return text
    text = str(error).strip() or error.__class__.__name__
    lower = text.lower()
    if "timed out" in lower or "timeout" in lower:
        return tr(f"{context} не отвечает (таймаут)", f"{context} is not responding (timeout)")
    return text


def _urlopen_json(url: str, *, timeout: float, cancel_event: threading.Event | None = None) -> dict[str, object]:
    _check_cancel(cancel_event)
    request = Request(url, headers={"User-Agent": f"Zapret-Hub-Installer/{INSTALLER_VERSION}"})

    def _load() -> tuple[int, bytes]:
        with urlopen(request, timeout=timeout) as response:
            status = int(getattr(response, "status", 0) or response.getcode() or 0)
            return status, response.read()

    status, raw = _run_with_deadline(_load, timeout=timeout + 1.0, cancel_event=cancel_event)
    _check_cancel(cancel_event)
    if status and status >= 400:
        raise HTTPError(url, status, f"HTTP {status}", hdrs=None, fp=None)  # type: ignore[arg-type]
    try:
        payload = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as error:
        raise error
    if not isinstance(payload, dict):
        raise ValueError(tr("Некорректный формат метаданных обновления.", "Unexpected update metadata format."))
    return payload


def _ensure_host_resolvable(url: str, *, timeout: float, cancel_event: threading.Event | None = None) -> None:
    host = str(urlparse(url).hostname or "").strip()
    if not host:
        return

    def _resolve() -> None:
        socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)

    try:
        _run_with_deadline(_resolve, timeout=timeout, cancel_event=cancel_event)
    except Exception as error:
        raise RuntimeError(_friendly_network_error(error, context=host)) from error


def _fetch_mirror_release(*, timeout: float = METADATA_TIMEOUT_SEC, cancel_event: threading.Event | None = None) -> dict[str, object]:
    try:
        _ensure_host_resolvable(UPDATE_URL, timeout=min(timeout, 8.0), cancel_event=cancel_event)
        return _urlopen_json(UPDATE_URL, timeout=timeout, cancel_event=cancel_event)
    except Exception as error:
        raise RuntimeError(_friendly_network_error(error, context="goshkow.ru")) from error


def _remote_release_version(release: dict[str, object] | None = None, *, timeout: float = REMOTE_VERSION_TIMEOUT_SEC) -> str:
    try:
        if release is not None:
            payload = release
        else:
            # Never block the UI thread on a hung DNS/connect: hard deadline + non-waiting shutdown.
            payload = _urlopen_json(UPDATE_URL, timeout=timeout, cancel_event=None)
            if not isinstance(payload, dict):
                return ""
    except Exception:
        return ""
    for key in ("version", "tag", "name"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value.lstrip("vV")
    return ""


def _download_payload_from_mirror(
    progress_cb=None,
    *,
    cancel_event: threading.Event | None = None,
) -> tuple[Path, Path, str]:
    def report(percent: int, status: str, *, downloaded: int = 0, total: int = 0) -> None:
        _check_cancel(cancel_event)
        if progress_cb is None:
            return
        try:
            progress_cb(int(percent), status, downloaded=int(downloaded), total=int(total))
        except TypeError:
            try:
                progress_cb(int(percent), status)
            except Exception:
                return
        except Exception:
            return

    _installer_log("download_begin", update_url=UPDATE_URL)
    report(2, tr("Запрос метаданных goshkow.ru…", "Requesting goshkow.ru metadata…"))
    temp_root = Path(tempfile.mkdtemp(prefix="zapret_hub_installer_download_"))
    try:
        _installer_log("download_dns_metadata")
        release = _fetch_mirror_release(timeout=METADATA_TIMEOUT_SEC, cancel_event=cancel_event)
        _installer_log("download_metadata_ok", keys=sorted(str(k) for k in release.keys())[:12])
        report(4, tr("Метаданные получены, подготовка загрузки…", "Metadata received, preparing download…"))
        remote_version = _remote_release_version(release)
        asset_key = "arm64" if detect_payload_name() == "win_arm64.zip" else "x64"
        asset = dict((release.get("assets") or {}).get(asset_key) or {})
        download_url = str(asset.get("download_url") or "").strip()
        if not download_url:
            download_url = f"https://goshkow.ru/zapret-hub/{asset_key}"
        archive_path = temp_root / detect_payload_name()
        digest = hashlib.sha256()
        downloaded = 0
        expected_size = int(asset.get("size") or 0)
        last_logged_mb = -1
        report(5, tr("Подключение к загрузке сборки…", "Connecting to build download…"))
        _installer_log("download_dns_asset", url=download_url, expected_size=expected_size)
        _ensure_host_resolvable(download_url, timeout=min(DOWNLOAD_CONNECT_TIMEOUT_SEC, 8.0), cancel_event=cancel_event)
        _installer_log("download_dns_asset_ok", url=download_url)
        request = Request(download_url, headers={"User-Agent": f"Zapret-Hub-Installer/{INSTALLER_VERSION}"})
        try:
            # Hard wall-clock deadline: urllib timeout alone can miss DNS/IPv6/proxy hangs.
            response = _run_with_deadline(
                lambda: urlopen(request, timeout=DOWNLOAD_CONNECT_TIMEOUT_SEC),
                timeout=DOWNLOAD_CONNECT_TIMEOUT_SEC + 1.0,
                cancel_event=cancel_event,
            )
        except Exception as error:
            _installer_log("download_connect_failed", error=str(error))
            raise RuntimeError(_friendly_network_error(error, context="goshkow.ru")) from error
        download_started = time.monotonic()
        last_chunk_at = download_started
        first_byte_logged = False
        with response, archive_path.open("wb") as stream:
            status = int(getattr(response, "status", 0) or response.getcode() or 0)
            if status and status >= 400:
                raise RuntimeError(_friendly_network_error(HTTPError(download_url, status, f"HTTP {status}", hdrs=None, fp=None), context="goshkow.ru"))  # type: ignore[arg-type]
            total = expected_size or int(response.headers.get("Content-Length") or 0)
            report(6, tr("Скачивание сборки…", "Downloading build…"))
            while True:
                _check_cancel(cancel_event)
                if time.monotonic() - download_started > DOWNLOAD_TOTAL_TIMEOUT_SEC:
                    raise TimeoutError(tr("Скачивание превысило лимит времени", "Download exceeded time limit"))
                if time.monotonic() - last_chunk_at > DOWNLOAD_STALL_TIMEOUT_SEC:
                    raise TimeoutError(tr("Загрузка зависла (нет данных)", "Download stalled (no data)"))
                try:
                    chunk = response.read(256 * 1024)
                except Exception as error:
                    raise RuntimeError(_friendly_network_error(error, context="goshkow.ru")) from error
                if not chunk:
                    break
                if not first_byte_logged:
                    first_byte_logged = True
                    _installer_log("download_first_byte", bytes=len(chunk), elapsed_sec=round(time.monotonic() - download_started, 2))
                last_chunk_at = time.monotonic()
                stream.write(chunk)
                digest.update(chunk)
                downloaded += len(chunk)
                mb = downloaded // (1024 * 1024)
                if mb != last_logged_mb and (mb <= 1 or mb % 8 == 0):
                    last_logged_mb = mb
                    _installer_log("download_progress_mb", mb=mb, total_mb=(total // (1024 * 1024)) if total else 0)
                if total > 0:
                    percent = 6 + int(min(40, (downloaded / total) * 40))
                    report(
                        percent,
                        tr("Скачивание", "Downloading") + f" {mb} / {max(1, total // (1024 * 1024))} MB",
                        downloaded=downloaded,
                        total=total,
                    )
                else:
                    report(20, tr("Скачивание", "Downloading") + f" {mb} MB", downloaded=downloaded, total=0)
        if expected_size and downloaded != expected_size:
            raise ValueError(tr("размер пакета не совпадает с метаданными", "package size does not match metadata"))
        expected_digest = str(asset.get("digest") or "").strip().lower().removeprefix("sha256:")
        if expected_digest and digest.hexdigest().lower() != expected_digest:
            raise ValueError(tr("контрольная сумма SHA-256 не совпала", "SHA-256 checksum mismatch"))
        _installer_log("download_complete", bytes=downloaded, remote_version=remote_version, elapsed_sec=round(time.monotonic() - download_started, 2))
        report(46, tr("Проверка сборки…", "Verifying build…"))
        return archive_path, temp_root, remote_version
    except Exception as error:
        _installer_log("download_failed", error=str(error))
        shutil.rmtree(temp_root, ignore_errors=True)
        raise


def is_admin() -> bool:
    if not sys.platform.startswith("win"):
        return True
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
    except Exception:
        return False


def relaunch_with_elevation(args: list[str]) -> bool:
    if not sys.platform.startswith("win"):
        return True
    if not getattr(sys, "frozen", False):
        return False
    cmd = " ".join(f'"{arg}"' for arg in args)
    result = ctypes.windll.shell32.ShellExecuteW(  # type: ignore[attr-defined]
        None, "runas", sys.executable, cmd, None, 1
    )
    return int(result) > 32


class ButtonInteractionOverlay(QWidget):
    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._progress = 0.0
        self._pressed = False
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.hide()

    def _get_progress(self) -> float:
        return self._progress

    def _set_progress(self, value: float) -> None:
        self._progress = max(0.0, min(1.0, float(value)))
        self.setVisible(self._progress > 0.001)
        self.update()

    progress = Property(float, _get_progress, _set_progress)

    def set_pressed(self, pressed: bool) -> None:
        self._pressed = bool(pressed)
        self.update()

    def paintEvent(self, event: QEvent) -> None:
        if self._progress <= 0.001:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        base = self.parentWidget().palette().button().color() if self.parentWidget() is not None else QColor('#1f2430')
        if base.lightness() < 128:
            overlay = QColor(255, 255, 255)
            max_alpha = 28 if not self._pressed else 42
        else:
            overlay = QColor(31, 41, 55)
            max_alpha = 14 if not self._pressed else 22
        overlay.setAlpha(int(max_alpha * self._progress))
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = min(18.0, max(8.0, min(rect.width(), rect.height()) / 2.0))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(overlay)
        painter.drawRoundedRect(rect, radius, radius)


class ButtonInteractionFilter(QObject):
    def __init__(self, widget: QWidget) -> None:
        super().__init__(widget)
        self._widget = widget
        self._overlay = ButtonInteractionOverlay(widget)
        self._overlay.setGeometry(widget.rect())
        self._animation: QPropertyAnimation | None = None
        widget.installEventFilter(self)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self._widget:
            if event.type() in {QEvent.Type.Resize, QEvent.Type.Show, QEvent.Type.Move}:
                self._overlay.setGeometry(self._widget.rect())
                self._overlay.raise_()
            elif event.type() == QEvent.Type.Enter:
                self._overlay.raise_()
                self._overlay.set_pressed(False)
                self._animate(1.0, 180)
            elif event.type() == QEvent.Type.Leave:
                self._overlay.set_pressed(False)
                self._animate(0.0, 180)
            elif event.type() == QEvent.Type.MouseButtonPress:
                self._overlay.raise_()
                self._overlay.set_pressed(True)
                self._animate(1.0, 90)
            elif event.type() == QEvent.Type.MouseButtonRelease:
                self._overlay.set_pressed(False)
                self._animate(1.0 if self._widget.underMouse() else 0.0, 150)
        return super().eventFilter(watched, event)

    def _animate(self, target: float, duration: int) -> None:
        if self._animation is not None:
            self._animation.stop()
        animation = QPropertyAnimation(self._overlay, b"progress", self)
        animation.setDuration(duration)
        animation.setStartValue(self._overlay.progress)
        animation.setEndValue(target)
        animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        animation.start()
        self._animation = animation


def attach_button_animations(widget: QWidget) -> None:
    if not isinstance(widget, (QPushButton, QToolButton)):
        return
    if widget.property("_interactionBound"):
        return
    widget.setProperty("_interactionBound", True)
    ButtonInteractionFilter(widget)


def set_windows_app_id() -> None:
    if not sys.platform.startswith("win"):
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("goshkow.ZapretHub.NuitkaInstaller.1.4.2.pngsync2")  # type: ignore[attr-defined]
    except Exception:
        return


def disable_native_window_rounding(hwnd: int) -> None:
    if not sys.platform.startswith("win"):
        return
    try:
        DWMWA_WINDOW_CORNER_PREFERENCE = 33
        DWMWCP_DONOTROUND = 1
        value = ctypes.c_int(DWMWCP_DONOTROUND)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(  # type: ignore[attr-defined]
            ctypes.c_void_p(hwnd),
            ctypes.c_uint(DWMWA_WINDOW_CORNER_PREFERENCE),
            ctypes.byref(value),
            ctypes.sizeof(value),
        )
    except Exception:
        return


def bring_widget_to_front(widget: QWidget) -> None:
    widget.raise_()
    widget.activateWindow()
    if not sys.platform.startswith("win"):
        return
    try:
        hwnd = int(widget.winId())
        SW_RESTORE = 9
        HWND_TOPMOST = -1
        HWND_NOTOPMOST = -2
        SWP_NOMOVE = 0x0002
        SWP_NOSIZE = 0x0001
        SWP_SHOWWINDOW = 0x0040
        ctypes.windll.user32.ShowWindow(hwnd, SW_RESTORE)  # type: ignore[attr-defined]
        ctypes.windll.user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)  # type: ignore[attr-defined]
        ctypes.windll.user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)  # type: ignore[attr-defined]
        ctypes.windll.user32.SetForegroundWindow(hwnd)  # type: ignore[attr-defined]
    except Exception:
        return


def _run_hidden(command: list[str]) -> None:
    startup = None
    flags = 0
    if sys.platform.startswith("win"):
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 0
    subprocess.run(command, check=False, capture_output=True, creationflags=flags, startupinfo=startup)


def _run_hidden_script(script: str) -> None:
    startup = None
    flags = 0
    if sys.platform.startswith("win"):
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 0
    subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-Command", script],
        check=False,
        capture_output=True,
        creationflags=flags,
        startupinfo=startup,
    )


def _remove_autostart_entries() -> None:
    if not sys.platform.startswith("win"):
        return
    _run_hidden(["schtasks", "/Delete", "/F", "/TN", "ZapretHub"])
    ps = r"""
$paths = @(
  'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run',
  'HKLM:\Software\Microsoft\Windows\CurrentVersion\Run'
)
$names = @('ZapretHub', 'Zapret Hub', 'zapret_hub')
foreach ($path in $paths) {
  foreach ($name in $names) {
    try { Remove-ItemProperty -Path $path -Name $name -ErrorAction SilentlyContinue } catch {}
  }
}
"""
    _run_hidden_script(ps)


def _terminate_running_instances(install_dir: Path | None = None) -> None:
    # Path-scoped only — never taskkill by image name (portable copies elsewhere must survive).
    terminate_running_instances(install_dir)


def _remove_shortcuts() -> None:
    shortcut_paths = [
        Path(os.environ.get("USERPROFILE", "")) / "Desktop" / "Zapret Hub.lnk",
        Path(os.environ.get("PUBLIC", r"C:\Users\Public")) / "Desktop" / "Zapret Hub.lnk",
        Path(os.environ.get("APPDATA", "")) / r"Microsoft\Windows\Start Menu\Programs\Zapret Hub.lnk",
        Path(os.environ.get("ProgramData", r"C:\ProgramData")) / r"Microsoft\Windows\Start Menu\Programs\Zapret Hub.lnk",
    ]
    for path in shortcut_paths:
        try:
            if path.exists():
                path.unlink()
        except Exception:
            continue


def _clear_path_attributes(path: Path) -> None:
    if not sys.platform.startswith("win") or not path.exists():
        return
    if path.is_dir():
        _run_hidden(["cmd", "/c", f'attrib -r -s -h "{path}" /s /d'])
    else:
        _run_hidden(["attrib", "-r", "-s", "-h", str(path)])


def _schedule_delete_on_reboot(path: Path) -> None:
    if not sys.platform.startswith("win") or not path.exists():
        return
    try:
        MOVEFILE_DELAY_UNTIL_REBOOT = 0x4
        ctypes.windll.kernel32.MoveFileExW(str(path), None, MOVEFILE_DELAY_UNTIL_REBOOT)  # type: ignore[attr-defined]
    except Exception:
        return


def _quarantine_item(path: Path) -> bool:
    if not path.exists():
        return True
    try:
        quarantine_root = Path(tempfile.gettempdir()) / "zapret_hub_cleanup"
        quarantine_root.mkdir(parents=True, exist_ok=True)
        target = quarantine_root / f"{path.name}_{int(time.time() * 1000)}"
        shutil.move(str(path), str(target))
        try:
            _clear_path_attributes(target)
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            elif target.exists():
                target.unlink(missing_ok=True)
        finally:
            if target.exists():
                _schedule_delete_on_reboot(target)
        return not path.exists()
    except Exception:
        return False


def _safe_remove_item(path: Path, install_dir: Path | None = None) -> None:
    for _ in range(6):
        try:
            if not path.exists():
                return
            _clear_path_attributes(path)
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=False)
            else:
                path.unlink()
            return
        except PermissionError:
            _terminate_running_instances(install_dir or path.parent)
            time.sleep(0.45)
        except Exception:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
                return
            raise
    if path.exists():
        raise PermissionError(f"cannot replace: {path}")


def _wipe_install_dir(install_dir: Path) -> None:
    if not install_dir.exists():
        return
    ignored_leftovers = {"merged_runtime", "backups", "logs"}
    for _ in range(6):
        _terminate_running_instances(install_dir)
        for item in list(install_dir.iterdir()):
            try:
                _safe_remove_item(item, install_dir)
            except Exception:
                if item.name in ignored_leftovers:
                    if _quarantine_item(item):
                        continue
                    continue
                if _quarantine_item(item):
                    continue
                raise
        if not any(install_dir.iterdir()):
            return
        time.sleep(0.5)
    remaining = next((item for item in install_dir.iterdir() if item.name not in ignored_leftovers), None)
    if remaining is None:
        return
    raise PermissionError(f"cannot replace: {remaining}")


def _overlay_tree(
    source: Path,
    target: Path,
    install_dir: Path,
    preserve_names: set[str] | None = None,
    *,
    remove_extra: bool = False,
) -> None:
    if not _is_within_path(target, install_dir):
        raise PermissionError(f"write target escaped install dir: {target}")
    preserve_names = preserve_names or set()
    target.mkdir(parents=True, exist_ok=True)
    source_names = {item.name for item in source.iterdir()}
    if remove_extra:
        for existing in list(target.iterdir()):
            if existing.name in preserve_names:
                continue
            if existing.name in source_names:
                continue
            try:
                _safe_remove_item(existing, install_dir)
            except Exception:
                if not _quarantine_item(existing):
                    if existing.is_dir() and not _is_preserved_user_root(existing, install_dir):
                        continue
                    raise
    for item in source.iterdir():
        if item.name in preserve_names:
            continue
        dst = target / item.name
        if item.is_dir():
            _overlay_tree(item, dst, install_dir, preserve_names, remove_extra=remove_extra)
            continue
        if dst.exists():
            try:
                _safe_remove_item(dst, install_dir)
            except Exception:
                if not _quarantine_item(dst) and _is_preserved_user_root(dst, install_dir):
                    raise
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(item, dst)
        except Exception:
            if _is_preserved_user_root(dst, install_dir):
                raise


def _estimate_install_size_kb(install_dir: Path) -> int:
    total = 0
    try:
        for item in install_dir.rglob("*"):
            if item.is_file():
                try:
                    total += item.stat().st_size
                except OSError:
                    continue
    except Exception:
        return 0
    return max(1, total // 1024)


def _write_uninstall_registry(install_dir: Path, uninstaller_exe: Path, app_exe: Path) -> None:
    _write_uninstall_registry_common(install_dir, uninstaller_exe, app_exe)


def _user_data_dirs(install_dir: Path | None = None) -> list[Path]:
    roots: list[Path] = []
    explicit = str(os.environ.get("ZAPRET_HUB_WORK_ROOT", "") or "").strip()
    if explicit:
        roots.append(Path(explicit).expanduser())
    local_app_data = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    for name in ("Zapret_Hub", "Zapret Hub", "ZapretHub"):
        roots.append(local_app_data / name)
    roaming = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
    for name in ("Zapret_Hub", "Zapret Hub", "ZapretHub"):
        roots.append(roaming / name)
    if install_dir is not None:
        for name in ("user_data", "data", "configs", "mods", "mods_zapret2", "cache", "logs", "backups", "merged_runtime"):
            roots.append(install_dir / name)
    unique: list[Path] = []
    seen: set[str] = set()
    for path in roots:
        key = str(path).casefold()
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def _remove_app_data(install_dir: Path | None = None) -> None:
    for path in _user_data_dirs(install_dir):
        if not path.exists():
            continue
        try:
            _safe_remove_item(path, install_dir)
        except Exception:
            _quarantine_item(path)


def _copy_running_installer_to(target_path: Path) -> bool:
    """Legacy helper kept for compatibility; prefer bundled standalone uninstaller."""
    installed = copy_bundled_uninstaller(target_path.parent)
    return bool(installed and installed.exists())


def _remove_uninstall_registry() -> None:
    if not sys.platform.startswith("win"):
        return
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            access = winreg.KEY_WRITE
            if root == winreg.HKEY_LOCAL_MACHINE:
                access |= winreg.KEY_WOW64_64KEY
            winreg.DeleteKeyEx(root, UNINSTALL_KEY, access=access, reserved=0)
        except Exception:
            continue


def _install_dir_from_registry() -> Path | None:
    if not sys.platform.startswith("win"):
        return None
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            access = winreg.KEY_READ
            if root == winreg.HKEY_LOCAL_MACHINE:
                access |= winreg.KEY_WOW64_64KEY
            with winreg.OpenKey(root, UNINSTALL_KEY, 0, access) as key:
                value, _ = winreg.QueryValueEx(key, "InstallLocation")
                path = Path(str(value))
                if path.exists():
                    return path
        except Exception:
            continue
    return None


def _launch_folder_removal(install_dir: Path) -> None:
    script_path = Path(tempfile.gettempdir()) / f"zapret_hub_uninstall_{int(time.time() * 1000)}.ps1"
    target = str(install_dir).replace("'", "''")
    script = (
        "$target = '{target}'\n"
        "for ($i = 0; $i -lt 40; $i++) {\n"
        "  try {\n"
        "    if (-not (Test-Path -LiteralPath $target)) { break }\n"
        "    Remove-Item -LiteralPath $target -Recurse -Force -ErrorAction Stop\n"
        "    if (-not (Test-Path -LiteralPath $target)) { break }\n"
        "  } catch {}\n"
        "  Start-Sleep -Milliseconds 800\n"
        "}\n"
        "Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue\n"
    ).format(target=target)
    script_path.write_text(script, encoding="utf-8")
    startup = None
    flags = 0
    if sys.platform.startswith("win"):
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 0
    subprocess.Popen(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script_path)],
        creationflags=flags,
        startupinfo=startup,
    )


class InstallerDialog(QDialog):
    def __init__(
        self,
        title: str,
        text: str,
        with_yes_no: bool = False,
        parent: QWidget | None = None,
        yes_text: str | None = None,
        no_text: str | None = None,
    ) -> None:
        super().__init__(parent)
        self._drag_pos = None
        self._result_yes = False
        self._result_mode = "cancel"
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.setModal(True)
        self.setFixedSize(520, 230)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setWindowIcon(app_icon())

        root = QWidget(self)
        root.setObjectName("DlgRoot")
        root.setGeometry(0, 0, 520, 230)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.title_bar = QFrame()
        self.title_bar.setObjectName("DlgTitle")
        self.title_bar.setFixedHeight(46)
        title_row = QHBoxLayout(self.title_bar)
        title_row.setContentsMargins(12, 8, 12, 8)
        title_row.setSpacing(8)
        icon = QLabel()
        icon.setFixedSize(20, 20)
        icon.setPixmap(app_icon().pixmap(20, 20))
        title_row.addWidget(icon)
        title_row.addWidget(QLabel(title))
        title_row.addStretch(1)
        close_btn = QToolButton()
        close_btn.setProperty("role", "close")
        close_btn.setIcon(QIcon(close_pixmap(14)))
        close_btn.setIconSize(QSize(14, 14))
        close_btn.setFixedSize(26, 26)
        close_btn.clicked.connect(self.reject)
        attach_button_animations(close_btn)
        title_row.addWidget(close_btn)
        layout.addWidget(self.title_bar)

        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(16, 16, 16, 16)
        body_layout.setSpacing(14)
        message = QLabel(text)
        message.setWordWrap(True)
        body_layout.addWidget(message, 1)

        row = QHBoxLayout()
        row.addStretch(1)
        if with_yes_no:
            no_btn = QPushButton(no_text or tr("Нет", "No"))
            no_btn.clicked.connect(self._accept_no)
            yes_btn = QPushButton(yes_text or tr("Да", "Yes"))
            yes_btn.setObjectName("primary")
            yes_btn.clicked.connect(self._accept_yes)
            attach_button_animations(no_btn)
            attach_button_animations(yes_btn)
            row.addWidget(no_btn)
            row.addWidget(yes_btn)
        else:
            ok_btn = QPushButton("OK")
            ok_btn.setObjectName("primary")
            ok_btn.clicked.connect(self.accept)
            attach_button_animations(ok_btn)
            row.addWidget(ok_btn)
        body_layout.addLayout(row)
        layout.addWidget(body, 1)

        self.setStyleSheet(
            """
            #DlgRoot { background: #0b0910; color: #ece8f4; border: 1px solid rgba(180,154,241,0.28); border-radius: 8px; font-family: Segoe UI; font-size: 10pt; }
            #DlgTitle { background: #0f0d16; border-bottom: 1px solid rgba(196,172,238,0.12); }
            QLabel { background: transparent; color: #ece8f4; }
            QPushButton { background: #14121c; border: 1px solid rgba(180,154,241,0.34); border-radius: 6px; padding: 8px 14px; min-width: 88px; color: #ece8f4; }
            QPushButton#primary { background: #b49af1; border: 1px solid #c4acee; color: #120f1a; font-weight: 700; }
            QToolButton { border: none; background: transparent; min-width: 26px; min-height: 26px; max-width: 26px; max-height: 26px; border-radius: 6px; padding: 0px; margin: 0px; }
            QToolButton[role="close"]:hover { background: rgba(235, 164, 174, 0.28); border-radius: 6px; }
            """
        )

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        disable_native_window_rounding(int(self.winId()))
        apply_native_window_icons(self)
        bring_widget_to_front(self)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and event.position().y() <= self.title_bar.height():
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_pos is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._drag_pos = None
        super().mouseReleaseEvent(event)

    def _accept_yes(self) -> None:
        self._result_yes = True
        self._result_mode = "yes"
        self.accept()

    def _accept_no(self) -> None:
        self._result_yes = False
        self._result_mode = "no"
        self.accept()

    @property
    def result_yes(self) -> bool:
        return self._result_yes

    @property
    def result_mode(self) -> str:
        return self._result_mode


class InstallerWorker(QThread):
    progress = Signal(int, str)
    done = Signal(bool, str)

    def __init__(self, target_dir: Path, preserve_data: bool, *, clean_target: bool = False) -> None:
        super().__init__()
        self.target_dir = target_dir
        self.preserve_data = preserve_data
        self.clean_target = clean_target
        self.cancel_event = threading.Event()
        self._downloaded_payload_root: Path | None = None
        self._staging: Path | None = None
        self.bytes_downloaded = 0
        self.bytes_total = 0

    def request_cancel(self) -> None:
        self.cancel_event.set()

    def _emit(self, value: int, status: str = "", *, downloaded: int = 0, total: int = 0) -> None:
        _check_cancel(self.cancel_event)
        if downloaded:
            self.bytes_downloaded = int(downloaded)
        if total:
            self.bytes_total = int(total)
        self.progress.emit(int(value), status)

    def _cleanup_temps(self) -> None:
        if self._staging is not None:
            shutil.rmtree(self._staging, ignore_errors=True)
            self._staging = None
        if self._downloaded_payload_root is not None:
            shutil.rmtree(self._downloaded_payload_root, ignore_errors=True)
            self._downloaded_payload_root = None

    def run(self) -> None:
        try:
            _installer_log(
                "install_start",
                cwd=str(Path.cwd()),
                executable=str(sys.executable),
                target_dir=str(self.target_dir),
                preserve_data=bool(self.preserve_data),
                clean_target=bool(self.clean_target),
            )
            if _is_dangerous_install_dir(self.target_dir):
                raise PermissionError(
                    tr(
                        "Небезопасная папка установки. Выберите пустую подпапку для Zapret Hub.",
                        "Unsafe install folder. Please choose an empty subfolder for Zapret Hub.",
                    )
                )
            root = payload_root()
            payload_name = detect_payload_name()
            local_payload = root / "installer_payload" / payload_name
            if not local_payload.exists():
                direct_payload_zip = root / payload_name
                if direct_payload_zip.exists():
                    local_payload = direct_payload_zip
            payload_zip: Path | None = None
            remote_version = ""
            self._emit(1, tr("Запуск загрузки с goshkow.ru…", "Starting download from goshkow.ru…"))
            try:
                payload_zip, downloaded_payload_root, remote_version = _download_payload_from_mirror(
                    self._emit,
                    cancel_event=self.cancel_event,
                )
                self._downloaded_payload_root = downloaded_payload_root
                _installer_log("payload_downloaded", payload_zip=str(payload_zip), remote_version=remote_version)
            except InstallAbort:
                raise
            except Exception as download_error:
                friendly = _friendly_network_error(download_error, context="goshkow.ru")
                _installer_log("payload_download_failed", error=friendly, local_payload=str(local_payload))
                if local_payload.exists():
                    payload_zip = local_payload
                    self._emit(8, tr("Сайт недоступен — локальный пакет.", "Site unavailable — using local package."))
                else:
                    raise RuntimeError(friendly) from download_error
            assert payload_zip is not None
            _check_cancel(self.cancel_event)
            _installer_log("payload_resolved", payload_root=str(root), payload_zip=str(payload_zip))

            self._emit(48, tr("Остановка процессов в папке установки…", "Stopping processes in install folder…"))
            _terminate_running_instances(self.target_dir)
            _check_cancel(self.cancel_event)
            self.target_dir.mkdir(parents=True, exist_ok=True)
            if self.clean_target:
                self._emit(52, tr("Очистка папки установки…", "Cleaning install folder…"))
                _wipe_install_dir(self.target_dir)
            staging = Path(tempfile.mkdtemp(prefix="zapret_hub_install_"))
            self._staging = staging
            _installer_log("staging_created", staging=str(staging))
            self._emit(58, tr("Распаковка…", "Extracting…"))

            with zipfile.ZipFile(payload_zip, "r") as archive:
                archive.extractall(staging)
            _check_cancel(self.cancel_event)
            _installer_log("payload_extracted", staging=str(staging))
            self._emit(72, tr("Копирование файлов…", "Copying files…"))

            source_root = staging / "zapret_hub"
            if not source_root.exists():
                source_root = staging
            _installer_log("source_root_resolved", source_root=str(source_root))

            preserved_names = {"merged_runtime", "backups", "logs", "uninstall_zaprethub.exe"}
            if self.preserve_data:
                preserved_names.update({"data", "mods", "configs", "cache", "user_data"})
            _terminate_running_instances(self.target_dir)
            _check_cancel(self.cancel_event)
            if not self.preserve_data:
                for runtime_dir_name in ("merged_runtime", "backups", "logs"):
                    runtime_dir = self.target_dir / runtime_dir_name
                    if not runtime_dir.exists():
                        continue
                    try:
                        _safe_remove_item(runtime_dir, self.target_dir)
                    except Exception:
                        _quarantine_item(runtime_dir)

            self._emit(84, tr("Установка файлов…", "Installing files…"))
            _overlay_tree(source_root, self.target_dir, self.target_dir, preserved_names, remove_extra=self.clean_target)
            _installer_log("overlay_done", target_dir=str(self.target_dir))

            self._cleanup_temps()
            self._emit(100, tr("Готово", "Done"))
            _installer_log("install_done", target_dir=str(self.target_dir), remote_version=remote_version)
            self.done.emit(True, "")
        except InstallAbort as error:
            _installer_log("install_aborted", error=str(error))
            self._cleanup_temps()
            self.done.emit(False, str(error))
        except Exception as error:
            friendly = _friendly_network_error(error, context="goshkow.ru") if not str(error) else str(error)
            if "goshkow" in str(error).lower() or isinstance(error, (URLError, HTTPError, TimeoutError, socket.timeout)):
                friendly = _friendly_network_error(error, context="goshkow.ru")
            _installer_log("install_failed", error=friendly)
            self._cleanup_temps()
            self.done.emit(False, friendly)


class InstallerWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self._drag_pos = None
        self.worker: InstallerWorker | None = None
        self.install_path = default_install_dir()
        self.preserve_existing_data = True
        self.clean_target = False
        self.install_mode_locked = False
        self.setWindowTitle("Zapret Hub Installer")
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFixedSize(580, 380)
        self.setWindowIcon(app_icon())
        self._build_ui()
        self._load_existing_install()

    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("Root")
        self.setCentralWidget(root)
        shell = QVBoxLayout(root)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)

        self.title_bar = QFrame()
        self.title_bar.setObjectName("InstallerTitleBar")
        self.title_bar.setFixedHeight(46)
        title_row = QHBoxLayout(self.title_bar)
        title_row.setContentsMargins(12, 8, 12, 8)
        title_row.setSpacing(8)

        icon = QLabel()
        icon.setFixedSize(20, 20)
        icon.setPixmap(app_pixmap(20))
        title_row.addWidget(icon)
        title_row.addWidget(QLabel("Zapret Hub"))
        title_row.addStretch(1)
        close_btn = QToolButton()
        close_btn.setProperty("role", "close")
        close_btn.setIcon(QIcon(close_pixmap(14)))
        close_btn.setIconSize(QSize(14, 14))
        close_btn.setFixedSize(26, 26)
        close_btn.clicked.connect(self.close)
        attach_button_animations(close_btn)
        title_row.addWidget(close_btn)
        shell.addWidget(self.title_bar)

        self.stack = QStackedWidget()
        shell.addWidget(self.stack, 1)

        self.page_start = QWidget()
        start_layout = QVBoxLayout(self.page_start)
        start_layout.setContentsMargins(20, 20, 20, 20)
        start_layout.setSpacing(12)
        head = QLabel(tr("Добро пожаловать в установщик Zapret Hub", "Welcome to Zapret Hub Installer"))
        head.setObjectName("title")
        start_layout.addWidget(head)
        desc = QLabel(
            tr(
                "Приложение устанавливает Zapret Hub и автоматически выбирает подходящую версию под вашу систему.",
                "This installer deploys Zapret Hub and automatically picks the proper build for your system.",
            )
        )
        desc.setWordWrap(True)
        start_layout.addWidget(desc)
        path_row = QHBoxLayout()
        self.path_edit = QLineEdit(str(self.install_path))
        browse_btn = QPushButton(tr("Обзор", "Browse"))
        browse_btn.clicked.connect(self._choose_dir)
        attach_button_animations(browse_btn)
        path_row.addWidget(self.path_edit, 1)
        path_row.addWidget(browse_btn)
        start_layout.addLayout(path_row)
        start_layout.addStretch(1)
        install_btn = QPushButton(tr("Установить", "Install"))
        install_btn.setObjectName("primary")
        install_btn.setMinimumHeight(42)
        install_btn.clicked.connect(self._start_install)
        attach_button_animations(install_btn)
        start_layout.addWidget(install_btn)
        self.stack.addWidget(self.page_start)

        self.page_progress = QWidget()
        progress_layout = QVBoxLayout(self.page_progress)
        progress_layout.setContentsMargins(20, 20, 20, 20)
        progress_layout.setSpacing(12)
        progress_layout.addWidget(QLabel(tr("Установка...", "Installing...")))
        progress_layout.addStretch(1)
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.bar.setFixedHeight(24)
        progress_layout.addWidget(self.bar)
        progress_layout.addStretch(1)
        self.stack.addWidget(self.page_progress)

        self.page_done = QWidget()
        done_layout = QVBoxLayout(self.page_done)
        done_layout.setContentsMargins(20, 20, 20, 20)
        done_layout.setSpacing(12)
        done_layout.addWidget(QLabel(tr("Установка завершена", "Installation complete")))
        self.desktop_cb = QCheckBox(tr("Создать ярлык на рабочем столе", "Create desktop shortcut"))
        self.startmenu_cb = QCheckBox(tr("Создать ярлык в меню Пуск", "Create Start Menu shortcut"))
        self.desktop_cb.setChecked(True)
        self.startmenu_cb.setChecked(True)
        done_layout.addWidget(self.desktop_cb)
        done_layout.addWidget(self.startmenu_cb)
        done_layout.addStretch(1)
        finish_btn = QPushButton(tr("Готово", "Finish"))
        finish_btn.setObjectName("primary")
        finish_btn.setMinimumHeight(42)
        finish_btn.clicked.connect(self._finish)
        attach_button_animations(finish_btn)
        done_layout.addWidget(finish_btn)
        self.stack.addWidget(self.page_done)

        check_icon = str((resource_root() / "ui_assets" / "icons" / "check.svg").resolve()).replace("\\", "/")
        self.setStyleSheet(
            f"""
            QMainWindow {{ background: transparent; }}
            QWidget#Root {{ background: #0b0910; color: #ece8f4; font-family: Segoe UI; font-size: 10pt; border: 1px solid rgba(180,154,241,0.28); border-radius: 8px; }}
            #InstallerTitleBar {{ background: #0f0d16; border-bottom: 1px solid rgba(196,172,238,0.12); }}
            QLabel#title {{ font-size: 18pt; font-weight: 800; color: #ffffff; }}
            QLabel {{ background: transparent; }}
            QLineEdit {{ background: #14121c; border: 1px solid rgba(180,154,241,0.28); border-radius: 6px; padding: 9px; font-size: 11pt; color: #ece8f4; selection-background-color: #9b7fd4; }}
            QPushButton {{ background: #14121c; border: 1px solid rgba(180,154,241,0.34); border-radius: 6px; padding: 10px 14px; font-size: 11pt; color: #ece8f4; }}
            QPushButton#primary {{ background: #b49af1; border: 1px solid #c4acee; color: #120f1a; font-weight: 800; }}
            QToolButton {{ border: none; background: transparent; min-width: 26px; min-height: 26px; max-width: 26px; max-height: 26px; border-radius: 6px; padding: 0px; margin: 0px; }}
            QToolButton[role="close"]:hover {{ background: rgba(235, 164, 174, 0.28); border-radius: 6px; }}
            QProgressBar {{ background: #14121c; border: 1px solid rgba(180,154,241,0.28); border-radius: 6px; text-align: center; }}
            QProgressBar::chunk {{ background: #b49af1; border-radius: 5px; }}
            QCheckBox {{ color: #ece8f4; background: transparent; }}
            QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 4px; border: 1px solid rgba(180,154,241,0.34); background: transparent; }}
            QCheckBox::indicator:checked {{ background: #b49af1; border: 1px solid #c4acee; image: url("{check_icon}"); }}
            """
        )

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        disable_native_window_rounding(int(self.winId()))
        apply_native_window_icons(self)
        bring_widget_to_front(self)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and event.position().y() <= self.title_bar.height():
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_pos is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._drag_pos = None
        super().mouseReleaseEvent(event)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        worker = self.worker
        if worker is not None:
            try:
                worker.request_cancel()
            except Exception:
                pass
            if worker.isRunning():
                if not worker.wait(2500):
                    worker.terminate()
                    worker.wait(1500)
            self.worker = None
        super().closeEvent(event)

    def _load_existing_install(self) -> None:
        existing = _install_dir_from_registry()
        if existing and _looks_like_zapret_hub_dir(existing):
            self.path_edit.setText(str(existing))

    def _choose_dir(self) -> None:
        picked = QFileDialog.getExistingDirectory(self, tr("Выбор папки", "Choose install directory"), self.path_edit.text())
        if picked:
            self.path_edit.setText(picked)

    def _start_install(self) -> None:
        raw_path = self.path_edit.text().strip() or str(default_install_dir())
        self.install_path = Path(raw_path).expanduser()
        if not self.install_path.is_absolute():
            self.install_path = (Path.cwd() / self.install_path).resolve()
        _installer_log("ui_start_install", selected_path=raw_path, normalized_target=str(self.install_path))
        if not self.install_mode_locked:
            if _is_dangerous_install_dir(self.install_path):
                choice = self._ask_unsafe_install_dir()
                if choice != "folder":
                    return
                self.install_path = _suggest_safe_install_dir(self.install_path)
                self.path_edit.setText(str(self.install_path))
            try:
                existing_items = [item for item in self.install_path.iterdir()] if self.install_path.exists() else []
            except Exception as error:
                InstallerDialog("Error", str(error), parent=self).exec()
                return
            if existing_items:
                if _looks_like_zapret_hub_dir(self.install_path):
                    choice = self._ask_existing_install_mode()
                    if choice == "cancel":
                        return
                    self.preserve_existing_data = choice == "preserve"
                    self.clean_target = choice == "clean"
                else:
                    choice = self._ask_foreign_install_dir()
                    if choice == "cancel":
                        return
                    if choice == "folder":
                        self.install_path = _suggest_empty_install_dir(self.install_path)
                        self.path_edit.setText(str(self.install_path))
                        self.preserve_existing_data = True
                        self.clean_target = False
                    else:
                        self.preserve_existing_data = False
                        self.clean_target = True
            else:
                self.preserve_existing_data = True
                self.clean_target = False
        else:
            if _is_dangerous_install_dir(self.install_path):
                InstallerDialog(
                    "Error",
                    tr(
                        "Нельзя устанавливать Zapret Hub в корень диска или системную папку. Выберите пустую подпапку.",
                        "Zapret Hub cannot be installed into a drive root or system folder. Please choose an empty subfolder.",
                    ),
                    parent=self,
                ).exec()
                return
        if not self.install_path.exists():
            self.preserve_existing_data = True
            self.clean_target = False
        if sys.platform.startswith("win") and getattr(sys, "frozen", False) and not is_admin():
            args = [
                "--elevated-install",
                "--install-dir",
                str(self.install_path),
                "--preserve-data" if self.preserve_existing_data else "--clean-install",
            ]
            if self.clean_target:
                args.append("--clean-target")
            if relaunch_with_elevation(args):
                self.close()
                return
            InstallerDialog("Error", tr("Не удалось запросить права администратора.", "Failed to request administrator privileges."), parent=self).exec()
            return
        self.stack.setCurrentWidget(self.page_progress)
        self.worker = InstallerWorker(self.install_path, preserve_data=self.preserve_existing_data, clean_target=self.clean_target)
        self.worker.progress.connect(lambda value, _status="": self.bar.setValue(value))
        self.worker.done.connect(self._on_done)
        self.worker.start()

    def _ask_unsafe_install_dir(self) -> str:
        dialog = InstallerDialog(
            tr("Небезопасная папка установки", "Unsafe install folder"),
            tr(
                "Нельзя устанавливать Zapret Hub в корень диска или системную папку. Создайте отдельную пустую папку для приложения.",
                "Zapret Hub cannot be installed into a drive root or system folder. Create a dedicated empty folder for the app.",
            ),
            with_yes_no=True,
            parent=self,
            yes_text=tr("Создать папку", "Create folder"),
            no_text=tr("Отмена", "Cancel"),
        )
        dialog.exec()
        if dialog.result_mode == "yes":
            return "folder"
        return "cancel"

    def _ask_foreign_install_dir(self) -> str:
        dialog = InstallerDialog(
            tr("Папка не пуста", "Folder is not empty"),
            tr(
                "В выбранной папке нет zapret_hub.exe, поэтому она не похожа на старую папку приложения.\n\n"
                "Если продолжить, папка будет очищена перед установкой. Чтобы ничего не удалить, создайте пустую папку и установите Zapret Hub в нее.",
                "The selected folder does not contain zapret_hub.exe, so it does not look like an existing app folder.\n\n"
                "If you continue, the folder will be cleaned before installation. To avoid deleting anything, create an empty folder and install Zapret Hub there.",
            ),
            with_yes_no=True,
            parent=self,
            yes_text=tr("Продолжить", "Continue"),
            no_text=tr("Создать папку", "Create folder"),
        )
        dialog.exec()
        if dialog.result_mode == "yes":
            return "continue"
        if dialog.result_mode == "no":
            return "folder"
        return "cancel"

    def _ask_existing_install_mode(self) -> str:
        dialog = InstallerDialog(
            tr("Найдена предыдущая версия", "Existing installation found"),
            tr(
                "Хотите ли вы переустановить программу, удалив все данные, или обновить, сохранив все ваши пользовательские данные?",
                "Do you want to reinstall the app and remove all data, or update it while keeping all of your user data?",
            ),
            with_yes_no=True,
            parent=self,
            yes_text=tr("Обновить", "Update"),
            no_text=tr("Переустановить", "Reinstall"),
        )
        dialog.exec()
        if dialog.result_mode == "yes":
            return "preserve"
        if dialog.result_mode == "no":
            return "clean"
        return "cancel"

    def _on_done(self, ok: bool, error: str) -> None:
        if not ok:
            InstallerDialog("Error", error, parent=self).exec()
            self.stack.setCurrentWidget(self.page_start)
            return
        self._register_uninstaller()
        self.stack.setCurrentWidget(self.page_done)

    def _register_uninstaller(self) -> None:
        app_exe = self.install_path / "zapret_hub.exe"
        try:
            uninstaller_exe = copy_bundled_uninstaller(self.install_path)
            if uninstaller_exe is not None:
                _write_uninstall_registry(self.install_path, uninstaller_exe, app_exe)
        except Exception:
            pass

    def _create_shortcut(self, target: Path, name: str, desktop: bool) -> None:
        if desktop:
            base = Path(os.environ.get("USERPROFILE", "")) / "Desktop"
        else:
            base = Path(os.environ.get("APPDATA", "")) / r"Microsoft\Windows\Start Menu\Programs"
        base.mkdir(parents=True, exist_ok=True)
        lnk_path = base / f"{name}.lnk"
        ps = (
            "$WScriptShell = New-Object -ComObject WScript.Shell; "
            f"$Shortcut = $WScriptShell.CreateShortcut('{str(lnk_path)}'); "
            f"$Shortcut.TargetPath = '{str(target)}'; "
            f"$Shortcut.WorkingDirectory = '{str(target.parent)}'; "
            f"$Shortcut.IconLocation = '{str(target)},0'; "
            "$Shortcut.Save();"
        )
        _installer_log(
            "shortcut_prepare",
            shortcut_target=str(target),
            shortcut_workdir=str(target.parent),
            shortcut_path=str(lnk_path),
            desktop=bool(desktop),
        )
        startup = None
        flags = 0
        if sys.platform.startswith("win"):
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = 0
        subprocess.run(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
            capture_output=True,
            check=False,
            creationflags=flags,
            startupinfo=startup,
        )

    def _launch_installed_app(self, exe: Path) -> None:
        if not exe.exists():
            return
        _installer_log("launch_target", launch_target=str(exe), launch_workdir=str(exe.parent))
        if sys.platform.startswith("win"):
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = 1
            subprocess.Popen([str(exe)], cwd=str(exe.parent), startupinfo=startup)
            return
        subprocess.Popen([str(exe)], cwd=str(exe.parent))

    def _finish(self) -> None:
        exe = self.install_path / "zapret_hub.exe"
        if self.desktop_cb.isChecked():
            self._create_shortcut(exe, "Zapret Hub", desktop=True)
        if self.startmenu_cb.isChecked():
            self._create_shortcut(exe, "Zapret Hub", desktop=False)
        if exe.exists():
            try:
                self._launch_installed_app(exe)
            except Exception:
                pass
        self.close()


class WebInstallerBridge(QObject):
    # Do not name this Signal "event" — it shadows QObject.event() and breaks Qt event delivery.
    bridgeEvent = Signal(str, str)

    def __init__(self, window: "WebInstallerWindow", *, uninstall_mode: bool = False, install_dir: Path | None = None) -> None:
        super().__init__(window)
        self.window = window
        self.uninstall_mode = uninstall_mode
        self.install_path = install_dir or _install_dir_from_registry() or default_install_dir()
        self.selected_path = self.install_path
        self.selected_action = "update"
        self.create_desktop = True
        self.create_start_menu = True
        self.launch_after = True
        self.worker: InstallerWorker | None = None
        self._aborting = False
        self._install_started_at = 0.0
        self._last_progress_at = 0.0
        self._last_progress_value = 0
        self._snapshot: dict[str, object] = self._empty_snapshot()
        self._watchdog = QTimer(self)
        self._watchdog.setInterval(500)
        self._watchdog.timeout.connect(self._on_watchdog_tick)

    @staticmethod
    def _empty_snapshot() -> dict[str, object]:
        return {
            "phase": "idle",
            "status": "",
            "progress": 0,
            "error": "",
            "bytesDownloaded": 0,
            "bytesTotal": 0,
            "failed": False,
            "done": False,
            "running": False,
            "startedAt": 0.0,
        }

    @staticmethod
    def _phase_for(progress: int, status: str = "") -> str:
        text = (status or "").lower()
        if progress >= 100:
            return "done"
        if progress < 6 or any(
            token in text
            for token in (
                "подключ",
                "connect",
                "метаданн",
                "metadata",
                "запуск загрузки",
                "starting download",
                "запрос",
                "requesting",
            )
        ):
            return "connecting"
        if progress < 48:
            return "downloading"
        return "installing"

    @Slot(str, str, result=str)
    def call(self, command: str, raw_payload: str) -> str:
        try:
            payload = json.loads(raw_payload or "{}")
            value = self._dispatch(command, payload if isinstance(payload, dict) else {})
            return json.dumps({"value": value}, ensure_ascii=False)
        except Exception as error:
            _installer_log("web_command_failed", command=command, error=str(error))
            return json.dumps({"error": str(error)}, ensure_ascii=False)

    def abort_all(self) -> None:
        if self._aborting:
            return
        self._aborting = True
        _installer_log("installer_abort_requested")
        self._watchdog.stop()
        worker = self.worker
        if worker is not None:
            try:
                worker.request_cancel()
            except Exception:
                pass
            if worker.isRunning():
                if not worker.wait(2500):
                    worker.terminate()
                    worker.wait(1500)
            self.worker = None
        if not bool(self._snapshot.get("done")):
            self._snapshot.update(
                {
                    "running": False,
                    "phase": "aborted" if not self._snapshot.get("failed") else self._snapshot.get("phase") or "error",
                }
            )

    def _dispatch(self, command: str, payload: dict[str, object]) -> object:
        if command == "state.get":
            return self.build_state()
        if command == "install.snapshot":
            return self.build_snapshot()
        if command == "window.minimize":
            self.window.showMinimized()
            return None
        if command == "window.close":
            self.window.close()
            return None
        if command == "window.startDrag":
            handle = self.window.windowHandle()
            if handle is not None:
                handle.startSystemMove()
            return None
        if command == "path.preview":
            return self._preview_path(str(payload.get("path", "") or ""))
        if command == "folder.choose":
            selected = QFileDialog.getExistingDirectory(
                self.window,
                tr("Выбор папки", "Choose install directory"),
                str(payload.get("path", "") or self.selected_path),
            )
            return self._preview_path(selected) if selected else None
        if command == "install.start":
            self.selected_action = str(payload.get("action", "update") or "update")
            self.create_desktop = bool(payload.get("desktop", True))
            self.create_start_menu = bool(payload.get("startMenu", True))
            self.launch_after = bool(payload.get("launchAfter", True))
            preview = self._preview_path(str(payload.get("path", "") or self.selected_path))
            self.install_path = Path(str(preview["resolvedPath"]))
            self._start_install()
            return self.build_snapshot()
        if command == "install.abort":
            self.abort_all()
            return self.build_snapshot()
        if command == "install.finish":
            self._finish_install(
                desktop=bool(payload.get("desktop", self.create_desktop)),
                start_menu=bool(payload.get("startMenu", self.create_start_menu)),
                launch_after=bool(payload.get("launchAfter", self.launch_after)),
            )
            return None
        if command == "uninstall.start":
            self._start_uninstall()
            return self.build_snapshot()
        raise ValueError(f"Unknown installer command: {command}")

    def _discover_installed(self) -> Path | None:
        existing = _install_dir_from_registry()
        if existing is not None and _looks_like_zapret_hub_dir(existing):
            return existing
        if _looks_like_zapret_hub_dir(self.install_path):
            return self.install_path
        return None

    def build_state(self) -> dict[str, object]:
        existing = self._discover_installed()
        if existing is not None:
            self.install_path = existing
        self.selected_path = self.install_path
        preview = self._preview_path(str(self.selected_path))
        return {
            "locale": "ru" if RU else "en",
            "mode": "uninstall" if self.uninstall_mode else "install",
            "page": "uninstall" if self.uninstall_mode else "welcome",
            "installed": existing is not None,
            "selectedAction": "update",
            "selectedPath": preview["selectedPath"],
            "resolvedPath": preview["resolvedPath"],
            "progress": int(self._snapshot.get("progress") or 0),
            "status": str(self._snapshot.get("status") or ""),
            "version": INSTALLER_VERSION,
            # Never hit the network on the UI/WebChannel thread during state.get.
            "remoteVersion": "",
            "createDesktop": self.create_desktop,
            "createStartMenu": self.create_start_menu,
            "launchAfter": self.launch_after,
            "error": str(self._snapshot.get("error") or ""),
        }

    def build_snapshot(self) -> dict[str, object]:
        snap = dict(self._snapshot)
        worker = self.worker
        if worker is not None:
            snap["bytesDownloaded"] = int(getattr(worker, "bytes_downloaded", 0) or snap.get("bytesDownloaded") or 0)
            snap["bytesTotal"] = int(getattr(worker, "bytes_total", 0) or snap.get("bytesTotal") or 0)
            snap["running"] = bool(worker.isRunning())
            if not snap.get("failed") and not snap.get("done"):
                progress = int(snap.get("progress") or 0)
                status = str(snap.get("status") or "")
                snap["phase"] = self._phase_for(progress, status)
        else:
            snap["running"] = False
        snap["startedAt"] = float(self._install_started_at or 0.0)
        return snap

    def _preview_path(self, raw_path: str) -> dict[str, str]:
        selected = Path(raw_path.strip() or str(default_install_dir())).expanduser()
        resolved = _resolve_requested_install_dir(selected)
        self.selected_path = selected
        return {"selectedPath": str(selected), "resolvedPath": str(resolved)}

    def _start_install(self) -> None:
        if self._aborting and self.worker is None:
            self._aborting = False
        if _is_dangerous_install_dir(self.install_path):
            self.install_path = self.install_path / "Zapret Hub"
        preserve_data = self.selected_action == "update"
        clean_target = self.selected_action == "reinstall"
        _installer_log(
            "web_install_start",
            action=self.selected_action,
            target=str(self.install_path),
            preserve_data=preserve_data,
            clean_target=clean_target,
        )
        if self.worker is not None and self.worker.isRunning():
            self.abort_all()
            self._aborting = False
        status = tr("Запуск загрузки с goshkow.ru…", "Starting download from goshkow.ru…")
        now = time.monotonic()
        self._install_started_at = now
        self._last_progress_at = now
        self._last_progress_value = 1
        self._snapshot = {
            "phase": "connecting",
            "status": status,
            "progress": 1,
            "error": "",
            "bytesDownloaded": 0,
            "bytesTotal": 0,
            "failed": False,
            "done": False,
            "running": True,
            "startedAt": now,
        }
        self.worker = InstallerWorker(self.install_path, preserve_data=preserve_data, clean_target=clean_target)
        self.worker.progress.connect(self._on_worker_progress)
        self.worker.done.connect(self._on_install_done)
        self._watchdog.start()
        self._emit("progress", {"value": 1, "status": status})
        self.worker.start()

    @Slot(int, str)
    def _on_worker_progress(self, value: int, status: str = "") -> None:
        self._last_progress_at = time.monotonic()
        self._last_progress_value = int(value)
        worker = self.worker
        bytes_downloaded = int(getattr(worker, "bytes_downloaded", 0) or 0) if worker is not None else 0
        bytes_total = int(getattr(worker, "bytes_total", 0) or 0) if worker is not None else 0
        self._snapshot.update(
            {
                "phase": self._phase_for(int(value), status),
                "progress": int(value),
                "status": status,
                "error": "",
                "failed": False,
                "done": False,
                "running": True,
                "bytesDownloaded": bytes_downloaded,
                "bytesTotal": bytes_total,
            }
        )
        self._emit(
            "progress",
            {
                "value": int(value),
                "status": status,
                "bytesDownloaded": bytes_downloaded,
                "bytesTotal": bytes_total,
            },
        )

    def _on_watchdog_tick(self) -> None:
        worker = self.worker
        if worker is None or not worker.isRunning() or self._aborting:
            self._watchdog.stop()
            return
        # Only enforce connect/metadata hang detection before download bytes flow.
        if self._last_progress_value >= 6:
            return
        idle_for = time.monotonic() - self._install_started_at
        if idle_for >= CONNECT_WATCHDOG_SEC:
            message = tr(
                "Не удалось подключиться к goshkow.ru за 15 секунд. Проверьте сеть и попробуйте снова.",
                "Could not connect to goshkow.ru within 15 seconds. Check the network and try again.",
            )
            _installer_log(
                "install_watchdog_timeout",
                idle_for=round(idle_for, 1),
                progress=self._last_progress_value,
                error=message,
            )
            try:
                worker.request_cancel()
            except Exception:
                pass
            self._watchdog.stop()
            self._snapshot.update(
                {
                    "phase": "error",
                    "status": message,
                    "error": message,
                    "failed": True,
                    "done": False,
                    "running": False,
                }
            )
            self._emit("error", {"message": message})

    def _on_install_done(self, ok: bool, error: str) -> None:
        self._watchdog.stop()
        if self._aborting:
            self._snapshot.update({"running": False, "phase": "aborted"})
            return
        if not ok:
            message = error or tr("Неизвестная ошибка установки.", "Unknown install error.")
            if "отменен" in message.lower() or "cancelled" in message.lower():
                self._snapshot.update(
                    {
                        "phase": "aborted",
                        "status": message,
                        "error": "",
                        "failed": False,
                        "done": False,
                        "running": False,
                    }
                )
                self._emit("aborted", {"message": message})
                return
            self._snapshot.update(
                {
                    "phase": "error",
                    "status": message,
                    "error": message,
                    "failed": True,
                    "done": False,
                    "running": False,
                }
            )
            self._emit("error", {"message": message})
            return
        self._register_uninstaller()
        self._snapshot.update(
            {
                "phase": "done",
                "progress": 100,
                "status": tr("Готово", "Done"),
                "error": "",
                "failed": False,
                "done": True,
                "running": False,
            }
        )
        self._emit("done", {})

    def _register_uninstaller(self) -> None:
        app_exe = self._installed_executable()
        uninstaller_exe = copy_bundled_uninstaller(self.install_path)
        if uninstaller_exe is not None:
            _write_uninstall_registry(self.install_path, uninstaller_exe, app_exe)

    def _installed_executable(self) -> Path:
        for name in ("Zapret_Hub.exe", "zapret_hub.exe"):
            candidate = self.install_path / name
            if candidate.exists():
                return candidate
        return self.install_path / "Zapret_Hub.exe"

    def _finish_install(self, *, desktop: bool, start_menu: bool, launch_after: bool = True) -> None:
        executable = self._installed_executable()
        if desktop:
            InstallerWindow._create_shortcut(None, executable, "Zapret Hub", desktop=True)
        if start_menu:
            InstallerWindow._create_shortcut(None, executable, "Zapret Hub", desktop=False)
        try:
            if launch_after:
                InstallerWindow._launch_installed_app(None, executable)
        finally:
            self.window.close()

    def _start_uninstall(self) -> None:
        now = time.monotonic()
        self._install_started_at = now
        self._snapshot = {
            "phase": "installing",
            "status": tr("Остановка процессов…", "Stopping processes…"),
            "progress": 1,
            "error": "",
            "bytesDownloaded": 0,
            "bytesTotal": 0,
            "failed": False,
            "done": False,
            "running": True,
            "startedAt": now,
        }
        try:
            def _uninstall_progress(value: int, status: str = "", **_kwargs: object) -> None:
                self._snapshot.update(
                    {
                        "phase": "installing",
                        "progress": int(value),
                        "status": status,
                        "error": "",
                        "failed": False,
                        "done": False,
                        "running": True,
                    }
                )
                self._emit("progress", {"value": int(value), "status": status})

            perform_uninstall(self.install_path, progress_cb=_uninstall_progress)
            self._snapshot.update(
                {
                    "phase": "done",
                    "progress": 100,
                    "status": tr("Готово", "Done"),
                    "error": "",
                    "failed": False,
                    "done": True,
                    "running": False,
                }
            )
            self._emit("done", {})
        except Exception as error:
            message = str(error)
            self._snapshot.update(
                {
                    "phase": "error",
                    "status": message,
                    "error": message,
                    "failed": True,
                    "done": False,
                    "running": False,
                }
            )
            self._emit("error", {"message": message})

    def _emit(self, name: str, payload: dict[str, object]) -> None:
        # Best-effort push; WebEngine may not receive bridgeEvent — JS also polls install.snapshot.
        try:
            self.bridgeEvent.emit(name, json.dumps(payload, ensure_ascii=False))
        except Exception as error:
            _installer_log("bridge_event_emit_failed", name=name, error=str(error))


class WebInstallerWindow(QMainWindow):
    def __init__(self, *, uninstall_mode: bool = False, install_dir: Path | None = None) -> None:
        super().__init__()
        self.setWindowTitle("Zapret Hub Installer")
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFixedSize(720, 500)
        self.setWindowIcon(app_icon())

        self.view = QWebEngineView(self)
        self.view.page().setBackgroundColor(QColor(Qt.GlobalColor.transparent))
        self.view.settings().setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
        self.setCentralWidget(self.view)

        self.bridge = WebInstallerBridge(self, uninstall_mode=False, install_dir=install_dir)
        self.channel = QWebChannel(self.view.page())
        self.channel.registerObject("installerBridge", self.bridge)
        self.view.page().setWebChannel(self.channel)

        index_path = resource_root() / "installer_web" / "index.html"
        if not index_path.exists():
            raise FileNotFoundError(f"Installer web UI is missing: {index_path}")
        self.view.setUrl(QUrl.fromLocalFile(str(index_path.resolve())))

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        disable_native_window_rounding(int(self.winId()))
        apply_native_window_icons(self)
        bring_widget_to_front(self)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        try:
            self.bridge.abort_all()
        except Exception:
            pass
        super().closeEvent(event)


def _register_current_install(install_dir: Path) -> bool:
    app_exe = None
    for name in ("Zapret_Hub.exe", "zapret_hub.exe"):
        candidate = install_dir / name
        if candidate.exists():
            app_exe = candidate
            break
    if app_exe is None:
        app_exe = install_dir / "Zapret_Hub.exe"
    uninstaller_exe = copy_bundled_uninstaller(install_dir)
    if uninstaller_exe is None:
        existing = install_dir / "uninstall_zaprethub.exe"
        if not existing.exists():
            return False
        uninstaller_exe = existing
    _write_uninstall_registry(install_dir, uninstaller_exe, app_exe)
    return True


def main() -> int:
    set_windows_app_id()
    if "--register" in sys.argv:
        install_arg = ""
        if "--install-dir" in sys.argv:
            try:
                install_arg = sys.argv[sys.argv.index("--install-dir") + 1]
            except Exception:
                install_arg = ""
        install_dir = Path(install_arg) if install_arg else Path(sys.executable).resolve().parent
        return 0 if _register_current_install(install_dir) else 1

    if "--uninstall" in sys.argv:
        _installer_log(
            "uninstall_flag_ignored",
            hint="Use standalone uninstall_zaprethub.exe",
        )
        print("Use standalone uninstall_zaprethub.exe to remove Zapret Hub.", file=sys.stderr)
        return 2

    if (
        sys.platform.startswith("win")
        and getattr(sys, "frozen", False)
        and "--elevated-ui" not in sys.argv
        and not is_admin()
    ):
        if relaunch_with_elevation(["--elevated-ui", *sys.argv[1:]]):
            return 0
        return 1

    app = QApplication(sys.argv)
    app.setWindowIcon(app_icon())
    requested_dir: Path | None = None
    if "--install-dir" in sys.argv:
        try:
            requested_dir = Path(sys.argv[sys.argv.index("--install-dir") + 1])
        except Exception:
            requested_dir = None
    window = WebInstallerWindow(install_dir=requested_dir)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
