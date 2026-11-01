from __future__ import annotations

import ctypes
import hashlib
import sqlite3
import sys
import tempfile
import time
import winreg
from queue import Empty, Queue
from threading import Event, Thread
from dataclasses import dataclass
from itertools import count
from pathlib import Path
from time import monotonic
from ctypes import wintypes

from PySide6.QtCore import QAbstractNativeEventFilter, QEasingCurve, QEvent, QSettings, QSize, QVariantAnimation, Qt, QTimer, QRect, QPoint, QMimeData, QUrl
from PySide6.QtGui import QAction, QColor, QGuiApplication, QIcon, QImage, QKeySequence, QPixmap, QShortcut, QPainter, QPen
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QKeySequenceEdit,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QStatusBar,
    QSystemTrayIcon,
    QMenu,
    QVBoxLayout,
    QWidget,
)


MAX_IMAGES = 10
DB_HISTORY_LIMIT = 20
WINDOW_MIN_WIDTH = 520
WINDOW_MIN_HEIGHT = 720
MERGE_GAP = 0
APP_SERVER_NAME = "clipboard-image-merger-single-instance"
SHOW_WINDOW_MESSAGE = b"show-window"
HOTKEY_ID = 1
WM_HOTKEY = 0x0312
MOD_CONTROL = 0x0002
MOD_ALT = 0x0001
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
VK_OEM_3 = 0xC0
SETTINGS_ORG = "ArnabSenapati"
SETTINGS_APP = "ClipboardImageMerger"
HOTKEY_SETTINGS_KEY = "global_hotkey"
BATCH_PASTE_SETTINGS_KEY = "batch_paste_as_files"
DEFAULT_HOTKEY = "Ctrl+`"
APP_USER_MODEL_ID = "ArnabSenapati.ClipboardImageMerger"
HISTORY_DB_NAME = "clipboard_history.db"
RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
STARTUP_VALUE_NAME = "ClipboardImageMerger"
SW_RESTORE = 9
SW_MINIMIZE = 6
VK_CONTROL = 0x11
KEYEVENTF_KEYUP = 0x0002
GWL_STYLE = -16
WS_MINIMIZE = 0x20000000


class POINT(ctypes.Structure):
    _fields_ = [
        ("x", wintypes.LONG),
        ("y", wintypes.LONG),
    ]


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", POINT),
    ]


def is_frozen_app() -> bool:
    return bool(getattr(sys, "frozen", False))


def runtime_app_dir() -> Path:
    if is_frozen_app():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resource_root() -> Path:
    if is_frozen_app():
        return Path(getattr(sys, "_MEIPASS", runtime_app_dir()))
    return Path(__file__).resolve().parent.parent


def get_foreground_window() -> int | None:
    if sys.platform != "win32":
        return None
    hwnd = int(ctypes.windll.user32.GetForegroundWindow())
    return hwnd or None


def set_windows_app_user_model_id(app_id: str) -> None:
    if sys.platform == "win32":
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)


def startup_command(app_dir: Path) -> str:
    if is_frozen_app():
        return f'"{Path(sys.executable).resolve()}"'
    python_executable = Path(sys.executable)
    if python_executable.name.lower() == "python.exe":
        pythonw_candidate = python_executable.with_name("pythonw.exe")
        if pythonw_candidate.exists():
            python_executable = pythonw_candidate
    return f'"{python_executable}" "{app_dir / "app.py"}"'


def is_startup_enabled() -> bool:
    if sys.platform != "win32":
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, STARTUP_VALUE_NAME)
            return bool(str(value).strip())
    except FileNotFoundError:
        return False


def set_startup_enabled(enabled: bool, app_dir: Path) -> None:
    if sys.platform != "win32":
        return
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH) as key:
        if enabled:
            winreg.SetValueEx(key, STARTUP_VALUE_NAME, 0, winreg.REG_SZ, startup_command(app_dir))
        else:
            try:
                winreg.DeleteValue(key, STARTUP_VALUE_NAME)
            except FileNotFoundError:
                pass


def show_window(hwnd: int, command: int) -> None:
    if sys.platform == "win32" and hwnd:
        ctypes.windll.user32.ShowWindow(hwnd, command)


def is_window_minimized(hwnd: int) -> bool:
    if sys.platform != "win32" or not hwnd:
        return False
    style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_STYLE)
    return bool(style & WS_MINIMIZE)


def set_foreground_window(hwnd: int) -> bool:
    if sys.platform != "win32" or not hwnd:
        return False
    return bool(ctypes.windll.user32.SetForegroundWindow(hwnd))


def send_ctrl_v() -> None:
    if sys.platform != "win32":
        return
    user32 = ctypes.windll.user32
    user32.keybd_event(VK_CONTROL, 0, 0, 0)
    user32.keybd_event(ord("V"), 0, 0, 0)
    user32.keybd_event(ord("V"), 0, KEYEVENTF_KEYUP, 0)
    user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)


def is_explorer_window(hwnd: int | None) -> bool:
    if sys.platform != "win32" or not hwnd:
        return False
    buffer = ctypes.create_unicode_buffer(256)
    ctypes.windll.user32.GetClassNameW(hwnd, buffer, 256)
    class_name = buffer.value
    return class_name in ("CabinetWClass", "ExploreWClass", "Progman", "WorkerW")


@dataclass
class HotkeyRegistration:
    sequence_text: str
    native_modifiers: int
    native_vk: int


@dataclass
class ClipboardHistoryRecord:
    capture_id: int
    png_bytes: bytes
    digest: str


def qt_key_to_virtual_key(key: int) -> int | None:
    if Qt.Key_A <= key <= Qt.Key_Z:
        return key
    if Qt.Key_0 <= key <= Qt.Key_9:
        return key
    if Qt.Key_F1 <= key <= Qt.Key_F24:
        return 0x70 + (key - Qt.Key_F1)
    special_keys = {
        Qt.Key_QuoteLeft: VK_OEM_3,
        Qt.Key_Escape: 0x1B,
        Qt.Key_Tab: 0x09,
        Qt.Key_Space: 0x20,
        Qt.Key_Return: 0x0D,
        Qt.Key_Enter: 0x0D,
        Qt.Key_Backspace: 0x08,
        Qt.Key_Insert: 0x2D,
        Qt.Key_Delete: 0x2E,
        Qt.Key_Home: 0x24,
        Qt.Key_End: 0x23,
        Qt.Key_PageUp: 0x21,
        Qt.Key_PageDown: 0x22,
        Qt.Key_Left: 0x25,
        Qt.Key_Up: 0x26,
        Qt.Key_Right: 0x27,
        Qt.Key_Down: 0x28,
    }
    return special_keys.get(key)


def hotkey_from_sequence_text(sequence_text: str) -> tuple[HotkeyRegistration | None, str | None]:
    sequence = QKeySequence(sequence_text)
    if sequence.count() != 1:
        return None, "Choose exactly one shortcut combination."

    combination = sequence[0]
    try:
        key = combination.key()
        modifiers = combination.keyboardModifiers()
    except AttributeError:
        return None, "This Qt version could not read the selected shortcut."

    if key in (Qt.Key_Control, Qt.Key_Shift, Qt.Key_Alt, Qt.Key_Meta, Qt.Key_unknown):
        return None, "The shortcut needs one non-modifier key."

    native_modifiers = 0
    if modifiers & Qt.ControlModifier:
        native_modifiers |= MOD_CONTROL
    if modifiers & Qt.AltModifier:
        native_modifiers |= MOD_ALT
    if modifiers & Qt.ShiftModifier:
        native_modifiers |= MOD_SHIFT
    if modifiers & Qt.MetaModifier:
        native_modifiers |= MOD_WIN

    if native_modifiers == 0:
        return None, "Use at least one modifier key like Ctrl, Alt, or Shift."

    native_vk = qt_key_to_virtual_key(key)
    if native_vk is None:
        return None, "That key is not supported for the global hotkey yet."

    return HotkeyRegistration(
        sequence_text=QKeySequence(combination).toString(QKeySequence.NativeText),
        native_modifiers=native_modifiers,
        native_vk=native_vk,
    ), None


def image_to_png_bytes(image: QImage) -> bytes:
    from PySide6.QtCore import QBuffer, QByteArray

    byte_array = QByteArray()
    buffer = QBuffer(byte_array)
    buffer.open(QBuffer.WriteOnly)
    image.save(buffer, "PNG")
    buffer.close()
    return bytes(byte_array)


def png_bytes_to_image(png_bytes: bytes) -> QImage:
    image = QImage()
    image.loadFromData(png_bytes, "PNG")
    return image


def image_to_raw_pixel_signature(image: QImage) -> tuple[int, int, bytes]:
    normalized = image.convertToFormat(QImage.Format_RGBA8888)
    bits = normalized.bits()
    return normalized.width(), normalized.height(), bytes(bits[: normalized.sizeInBytes()])


def scale_image_to_width(image: QImage, width: int) -> QImage:
    if image.width() == width:
        return image
    return image.scaledToWidth(width, Qt.SmoothTransformation)


def merge_images_vertically(images: list[QImage], target_width: int, gap: int = MERGE_GAP) -> QImage:
    scaled_images = [scale_image_to_width(image, target_width) for image in images]
    total_height = sum(image.height() for image in scaled_images)
    if scaled_images:
        total_height += gap * (len(scaled_images) - 1)
    merged = QImage(target_width, total_height, QImage.Format_ARGB32)
    merged.fill(Qt.white)

    y_offset = 0
    painter = None
    try:
        from PySide6.QtGui import QPainter

        painter = QPainter(merged)
        for index, image in enumerate(scaled_images):
            painter.drawImage(0, y_offset, image)
            y_offset += image.height()
            if index < len(scaled_images) - 1:
                y_offset += gap
    finally:
        if painter is not None:
            painter.end()

    return merged


class ClipboardHistoryStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.queue: Queue[ClipboardHistoryRecord] = Queue()
        self.stop_event = Event()
        self.worker = Thread(target=self._worker_loop, name="clipboard-history-writer", daemon=True)
        self._initialize_db()
        self.worker.start()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _initialize_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS clipboard_history (
                    capture_id INTEGER PRIMARY KEY,
                    digest TEXT NOT NULL,
                    image_png BLOB NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            connection.commit()

    def enqueue_save(self, record: ClipboardHistoryRecord) -> None:
        self.queue.put(record)

    def load_recent(self, limit: int = MAX_IMAGES) -> list[ClipboardHistoryRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT capture_id, image_png, digest
                FROM clipboard_history
                ORDER BY capture_id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        rows.reverse()
        return [
            ClipboardHistoryRecord(capture_id=row[0], png_bytes=row[1], digest=row[2])
            for row in rows
        ]

    def close(self) -> None:
        self.stop_event.set()
        self.worker.join(timeout=2)

    def _worker_loop(self) -> None:
        while not self.stop_event.is_set() or not self.queue.empty():
            try:
                record = self.queue.get(timeout=0.2)
            except Empty:
                continue

            try:
                with self._connect() as connection:
                    connection.execute(
                        """
                        INSERT OR REPLACE INTO clipboard_history (capture_id, digest, image_png)
                        VALUES (?, ?, ?)
                        """,
                        (record.capture_id, record.digest, record.png_bytes),
                    )
                    connection.execute(
                        """
                        DELETE FROM clipboard_history
                        WHERE capture_id NOT IN (
                            SELECT capture_id
                            FROM clipboard_history
                            ORDER BY capture_id DESC
                            LIMIT ?
                        )
                        """,
                        (DB_HISTORY_LIMIT,),
                    )
                    connection.commit()
            finally:
                self.queue.task_done()


@dataclass
class ClipboardImage:
    capture_id: int
    image: QImage
    digest: str


@dataclass
class MergeGroup:
    group_id: int
    images: list[QImage]


class ClickableImageLabel(QLabel):
    def __init__(self, click_callback, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.click_callback = click_callback
        self.setCursor(Qt.PointingHandCursor)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.click_callback(event.modifiers())
            event.accept()
            return
        super().mousePressEvent(event)


class AnimatedButton(QPushButton):
    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.base_color = QColor("#1d4ed8")
        self.hover_color = QColor("#2563eb")
        self.pressed_color = QColor("#1e40af")
        self.current_color = QColor(self.base_color)

        self.shadow = QGraphicsDropShadowEffect(self)
        self.shadow.setBlurRadius(0)
        self.shadow.setOffset(0, 0)
        self.shadow.setColor(QColor(37, 99, 235, 0))
        self.setGraphicsEffect(self.shadow)

        self.color_animation = QVariantAnimation(self)
        self.color_animation.setDuration(160)
        self.color_animation.setEasingCurve(QEasingCurve.OutCubic)
        self.color_animation.valueChanged.connect(self.on_color_value_changed)

        self.shadow_animation = QVariantAnimation(self)
        self.shadow_animation.setDuration(160)
        self.shadow_animation.setEasingCurve(QEasingCurve.OutCubic)
        self.shadow_animation.valueChanged.connect(self.on_shadow_value_changed)

        self.refresh_style()

    def setText(self, text: str) -> None:
        super().setText(text)
        self.refresh_style()

    def refresh_style(self) -> None:
        padding = "8px" if not self.text() else "8px 12px"
        self.setStyleSheet(
            f"""
            QPushButton {{
                background: {self.current_color.name()};
                border: none;
                border-radius: 8px;
                color: white;
                font-weight: 600;
                padding: {padding};
            }}
            QPushButton:disabled {{
                background: #94a3b8;
                color: #e2e8f0;
            }}
            """
        )

    def on_color_value_changed(self, value) -> None:
        self.current_color = QColor(value)
        self.refresh_style()

    def on_shadow_value_changed(self, value) -> None:
        blur_radius = float(value)
        self.shadow.setBlurRadius(blur_radius)
        self.shadow.setOffset(0, max(0, blur_radius / 8))
        color = QColor(37, 99, 235, min(90, int(blur_radius * 3)))
        self.shadow.setColor(color)

    def animate_to(self, color: QColor, blur_radius: float) -> None:
        self.color_animation.stop()
        self.color_animation.setStartValue(QColor(self.current_color))
        self.color_animation.setEndValue(QColor(color))
        self.color_animation.start()

        self.shadow_animation.stop()
        self.shadow_animation.setStartValue(self.shadow.blurRadius())
        self.shadow_animation.setEndValue(blur_radius)
        self.shadow_animation.start()

    def enterEvent(self, event) -> None:
        if self.isEnabled():
            self.animate_to(self.hover_color, 24)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        if self.isEnabled():
            self.animate_to(self.base_color, 0)
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:
        if self.isEnabled() and event.button() == Qt.LeftButton:
            self.animate_to(self.pressed_color, 8)
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        super().mouseReleaseEvent(event)
        if not self.isEnabled():
            return
        if self.rect().contains(event.position().toPoint()):
            self.animate_to(self.hover_color, 24)
        else:
            self.animate_to(self.base_color, 0)


class HotkeySettingsDialog(QDialog):
    def __init__(self, current_hotkey: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Hotkey Settings")
        self.setModal(True)
        self.setMinimumWidth(420)

        self.info_label = QLabel(
            "Choose a global shortcut to show the running app. "
            "If it is already taken, the current hotkey will stay unchanged."
        )
        self.info_label.setWordWrap(True)

        self.hotkey_edit = QKeySequenceEdit()
        self.hotkey_edit.setKeySequence(QKeySequence(current_hotkey))

        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet("color: #57534e;")

        form = QFormLayout()
        form.addRow("Shortcut", self.hotkey_edit)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(self.info_label)
        layout.addLayout(form)
        layout.addWidget(self.status_label)
        layout.addWidget(buttons)

        self.hotkey_edit.keySequenceChanged.connect(self.update_status)
        self.update_status(self.hotkey_edit.keySequence())

    def update_status(self, sequence: QKeySequence) -> None:
        text = sequence.toString(QKeySequence.NativeText)
        if not text:
            self.status_label.setText("Enter a shortcut like Ctrl+` or Ctrl+Alt+M.")
            return
        registration, error = hotkey_from_sequence_text(text)
        if error or registration is None:
            self.status_label.setText(error or "The selected shortcut is not valid.")
            return
        self.status_label.setText(f"Will try to register: {registration.sequence_text}")

    def selected_hotkey_text(self) -> str:
        return self.hotkey_edit.keySequence().toString(QKeySequence.NativeText)


class WindowsHotkeyFilter(QAbstractNativeEventFilter):
    def __init__(self, activate_callback) -> None:
        super().__init__()
        self.activate_callback = activate_callback
        self.registered = False
        self.current_hotkey: HotkeyRegistration | None = None
        self.shutting_down = False

    def nativeEventFilter(self, event_type, message):
        if sys.platform != "win32" or self.shutting_down:
            return False, 0

        try:
            msg = MSG.from_address(int(message))
            if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                self.activate_callback()
                return True, 0
        except Exception:
            return False, 0
        return False, 0

    def register(self, registration: HotkeyRegistration) -> bool:
        if sys.platform != "win32":
            return False
        self.registered = bool(
            ctypes.windll.user32.RegisterHotKey(
                None,
                HOTKEY_ID,
                registration.native_modifiers,
                registration.native_vk,
            )
        )
        if self.registered:
            self.current_hotkey = registration
        return self.registered

    def unregister(self) -> None:
        self.shutting_down = True
        if self.registered and sys.platform == "win32":
            ctypes.windll.user32.UnregisterHotKey(None, HOTKEY_ID)
            self.registered = False
            self.current_hotkey = None


class SingleInstanceBridge:
    def __init__(self, server_name: str, activate_callback) -> None:
        self.server_name = server_name
        self.activate_callback = activate_callback
        self.server: QLocalServer | None = None

    def try_notify_existing_instance(self) -> bool:
        socket = QLocalSocket()
        socket.connectToServer(self.server_name)
        if not socket.waitForConnected(250):
            return False
        socket.write(SHOW_WINDOW_MESSAGE)
        socket.flush()
        socket.waitForBytesWritten(250)
        socket.disconnectFromServer()
        return True

    def start_listening(self) -> bool:
        QLocalServer.removeServer(self.server_name)
        self.server = QLocalServer()
        self.server.newConnection.connect(self.on_new_connection)
        return self.server.listen(self.server_name)

    def on_new_connection(self) -> None:
        if self.server is None:
            return
        socket = self.server.nextPendingConnection()
        if socket is None:
            return
        socket.readyRead.connect(lambda sock=socket: self.handle_socket(sock))
        socket.disconnected.connect(socket.deleteLater)

    def handle_socket(self, socket: QLocalSocket) -> None:
        payload = bytes(socket.readAll())
        if payload == SHOW_WINDOW_MESSAGE:
            self.activate_callback()
        socket.disconnectFromServer()

    def cleanup(self) -> None:
        if self.server is not None:
            self.server.close()
            self.server.deleteLater()
            QLocalServer.removeServer(self.server_name)
            self.server = None


class ImageCard(QFrame):
    def __init__(self, item: ClipboardImage, toggle_callback, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.item = item
        self.toggle_callback = toggle_callback
        self.selection_position: int | None = None
        self.preview_label = ClickableImageLabel(self.on_image_clicked)
        self.preview_label.setAlignment(Qt.AlignCenter)
        self.preview_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.preview_label.setMinimumHeight(120)

        self.order_badge = QLabel()
        self.order_badge.setAlignment(Qt.AlignCenter)
        self.order_badge.setMinimumWidth(96)
        self.order_badge.hide()

        header = QHBoxLayout()
        header.addStretch(1)
        header.addWidget(self.order_badge)

        layout = QVBoxLayout(self)
        layout.addLayout(header)
        layout.addWidget(self.preview_label)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        self.setFrameShape(QFrame.StyledPanel)
        self.setObjectName("imageCard")

        self.refresh_preview(320)
        self.refresh_selection_state(None)

    def refresh_preview(self, target_width: int) -> None:
        scaled = QPixmap.fromImage(scale_image_to_width(self.item.image, target_width))
        self.preview_label.setPixmap(scaled)
        self.preview_label.setFixedHeight(scaled.height())

    def refresh_selection_state(self, position: int | None) -> None:
        self.selection_position = position
        selected = position is not None
        if selected:
            self.order_badge.setText(f"#{position}")
            self.order_badge.show()
        else:
            self.order_badge.hide()

        self.setProperty("selected", selected)
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def on_image_clicked(self, modifiers=Qt.NoModifier) -> None:
        self.toggle_callback(self.item.capture_id, bool(modifiers & Qt.ControlModifier))


class MergeGroupCard(QFrame):
    def __init__(self, group: MergeGroup, select_callback, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.group = group
        self.select_callback = select_callback
        self.preview_label = ClickableImageLabel(self.on_card_clicked)
        self.preview_label.setAlignment(Qt.AlignCenter)
        self.preview_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.preview_label.setMinimumHeight(120)

        self.title_label = QLabel()
        self.title_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)

        layout = QVBoxLayout(self)
        layout.addWidget(self.title_label)
        layout.addWidget(self.preview_label)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        self.setFrameShape(QFrame.StyledPanel)
        self.setObjectName("mergeGroupCard")
        self.refresh_preview(320)
        self.refresh_selection_state(False)

    def merged_image(self) -> QImage:
        target_width = max(image.width() for image in self.group.images)
        return merge_images_vertically(self.group.images, target_width, gap=0)

    def refresh_preview(self, target_width: int) -> None:
        merged = self.merged_image()
        scaled = QPixmap.fromImage(scale_image_to_width(merged, target_width))
        self.preview_label.setPixmap(scaled)
        self.preview_label.setFixedHeight(scaled.height())
        self.title_label.setText(f"Group {self.group.group_id} ({len(self.group.images)} image(s))")

    def refresh_selection_state(self, selected: bool) -> None:
        self.setProperty("selected", selected)
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def on_card_clicked(self, modifiers=Qt.NoModifier) -> None:
        self.select_callback(self.group.group_id)


class SplitImageCanvas(QWidget):
    def __init__(self, editor: ImageSplitEditor, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.editor = editor
        self.image: QImage | None = None
        self.orientation = "horizontal"  # "horizontal" or "vertical"
        self.splits: list[float] = [0.5]  # default to one split in the middle
        self.active_split_index: int | None = None
        self.hovered_split_index: int | None = None
        self.image_rect = QRect()
        self.setMouseTracking(True)

    def set_image(self, image: QImage) -> None:
        self.image = image
        self.splits = [0.5]
        self.active_split_index = None
        self.hovered_split_index = None
        self.update()

    def widget_to_normalized(self, pos: QPoint) -> float:
        if self.image_rect.height() == 0 or self.image_rect.width() == 0:
            return 0.0
        if self.orientation == "horizontal":
            y = pos.y() - self.image_rect.y()
            val = y / self.image_rect.height()
        else:
            x = pos.x() - self.image_rect.x()
            val = x / self.image_rect.width()
        return max(0.0, min(1.0, val))

    def get_split_at_mouse(self, pos: QPoint) -> int | None:
        if self.image is None or self.image_rect.isEmpty():
            return None

        threshold = 8  # grab margin in pixels

        for idx, val in enumerate(self.splits):
            if self.orientation == "horizontal":
                line_y = self.image_rect.y() + int(val * self.image_rect.height())
                if (abs(pos.y() - line_y) <= threshold and 
                    self.image_rect.x() <= pos.x() <= self.image_rect.x() + self.image_rect.width()):
                    return idx
            else:
                line_x = self.image_rect.x() + int(val * self.image_rect.width())
                if (abs(pos.x() - line_x) <= threshold and 
                    self.image_rect.y() <= pos.y() <= self.image_rect.y() + self.image_rect.height()):
                    return idx
        return None

    def mousePressEvent(self, event) -> None:
        if self.image is None:
            return

        pos = event.position().toPoint()
        idx = self.get_split_at_mouse(pos)

        if event.button() == Qt.LeftButton:
            if idx is not None:
                self.active_split_index = idx
                self.update()
                event.accept()
                return
        elif event.button() == Qt.RightButton:
            if idx is not None:
                if len(self.splits) > 1:
                    self.splits.pop(idx)
                    if self.hovered_split_index == idx:
                        self.hovered_split_index = None
                    self.update()
                event.accept()
                return

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self.image is None:
            return

        pos = event.position().toPoint()

        if self.active_split_index is not None:
            val = self.widget_to_normalized(pos)
            # Clamp to [0.01, 0.99] to prevent lines going off image edge
            val = max(0.01, min(0.99, val))
            self.splits[self.active_split_index] = val
            self.update()
            event.accept()
            return

        idx = self.get_split_at_mouse(pos)
        if idx != self.hovered_split_index:
            self.hovered_split_index = idx
            self.update()

        if idx is not None:
            if self.orientation == "horizontal":
                self.setCursor(Qt.SplitVCursor)
            else:
                self.setCursor(Qt.SplitHCursor)
        else:
            self.setCursor(Qt.ArrowCursor)

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and self.active_split_index is not None:
            self.active_split_index = None
            self.update()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def paintEvent(self, event) -> None:
        if self.image is None or self.image.isNull():
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        widget_width = self.width()
        widget_height = self.height()
        img_w = self.image.width()
        img_h = self.image.height()

        scale = min(widget_width / img_w, widget_height / img_h)
        draw_w = int(img_w * scale)
        draw_h = int(img_h * scale)

        # Center draw rect
        self.image_rect = QRect((widget_width - draw_w) // 2, (widget_height - draw_h) // 2, draw_w, draw_h)

        # Draw image
        painter.drawImage(self.image_rect, self.image)

        # Draw image border
        painter.setPen(QPen(QColor("#d6d3d1"), 1))
        painter.drawRect(self.image_rect)

        # Draw split lines
        for idx, val in enumerate(self.splits):
            is_active = (idx == self.active_split_index) or (idx == self.hovered_split_index)

            if is_active:
                pen_color = QColor("#ea580c")  # Highlight orange
                pen_width = 3
            else:
                pen_color = QColor("#2563eb")  # Default blue
                pen_width = 2

            pen = QPen(pen_color, pen_width)
            if not is_active:
                pen.setStyle(Qt.DashLine)
            painter.setPen(pen)

            if self.orientation == "horizontal":
                y = self.image_rect.y() + int(val * self.image_rect.height())
                painter.drawLine(self.image_rect.x(), y, self.image_rect.x() + self.image_rect.width(), y)
            else:
                x = self.image_rect.x() + int(val * self.image_rect.width())
                painter.drawLine(x, self.image_rect.y(), x, self.image_rect.y() + self.image_rect.height())


class ImageSplitEditor(QWidget):
    def __init__(self, main_window, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.main_window = main_window
        self.current_image: QImage | None = None
        self.current_capture_id: int | None = None

        # UI Setup
        # Top Toolbar
        self.toolbar_layout = QHBoxLayout()
        self.toolbar_layout.setSpacing(6)

        self.add_split_btn = AnimatedButton("")
        self.add_split_btn.setIcon(self.main_window.get_icon("add_split"))
        self.add_split_btn.setIconSize(QSize(20, 20))
        self.add_split_btn.setToolTip("Add Split Line")
        self.add_split_btn.clicked.connect(self.add_split)

        self.toggle_dir_btn = AnimatedButton("")
        self.toggle_dir_btn.setIcon(self.main_window.get_icon("split_horizontal"))
        self.toggle_dir_btn.setIconSize(QSize(20, 20))
        self.toggle_dir_btn.setToolTip("Split Direction: Horizontal")
        self.toggle_dir_btn.clicked.connect(self.toggle_direction)

        self.toolbar_layout.addWidget(self.add_split_btn)
        self.toolbar_layout.addWidget(self.toggle_dir_btn)
        self.toolbar_layout.addStretch(1)

        # Canvas
        self.canvas = SplitImageCanvas(self)

        # Info label
        self.info_label = QLabel("Drag lines to move. Right-click a line to remove.")
        self.info_label.setStyleSheet("color: #78716c; font-size: 11px;")
        self.info_label.setAlignment(Qt.AlignCenter)

        # Bottom Buttons
        self.bottom_layout = QHBoxLayout()
        self.bottom_layout.setSpacing(8)

        self.cancel_btn = AnimatedButton("")
        self.cancel_btn.setIcon(self.main_window.get_icon("cancel_split"))
        self.cancel_btn.setIconSize(QSize(20, 20))
        self.cancel_btn.setToolTip("Cancel Split")
        self.cancel_btn.base_color = QColor("#78716c")  # stone-500
        self.cancel_btn.hover_color = QColor("#a8a29e")  # stone-400
        self.cancel_btn.pressed_color = QColor("#57534e")  # stone-600
        self.cancel_btn.refresh_style()
        self.cancel_btn.clicked.connect(self.cancel_split)

        self.done_btn = AnimatedButton("")
        self.done_btn.setIcon(self.main_window.get_icon("done_split"))
        self.done_btn.setIconSize(QSize(20, 20))
        self.done_btn.setToolTip("Done / Save Splits")
        self.done_btn.clicked.connect(self.apply_split)

        self.bottom_layout.addWidget(self.cancel_btn)
        self.bottom_layout.addWidget(self.done_btn)
        self.bottom_layout.addStretch(1)

        # Main Layout
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(6)
        layout.addLayout(self.toolbar_layout)
        layout.addWidget(self.canvas, 1)
        layout.addWidget(self.info_label)
        layout.addLayout(self.bottom_layout)

    def add_split(self) -> None:
        if self.current_image is None:
            return

        sorted_splits = sorted(self.canvas.splits)
        points = [0.0] + sorted_splits + [1.0]

        max_gap = 0.0
        insert_pos = 0.5
        for i in range(len(points) - 1):
            gap = points[i+1] - points[i]
            if gap > max_gap:
                max_gap = gap
                insert_pos = points[i] + gap / 2.0

        insert_pos = max(0.01, min(0.99, insert_pos))
        self.canvas.splits.append(insert_pos)
        self.canvas.update()

    def toggle_direction(self) -> None:
        if self.canvas.orientation == "horizontal":
            self.canvas.orientation = "vertical"
            self.toggle_dir_btn.setIcon(self.main_window.get_icon("split_vertical"))
            self.toggle_dir_btn.setToolTip("Split Direction: Vertical")
        else:
            self.canvas.orientation = "horizontal"
            self.toggle_dir_btn.setIcon(self.main_window.get_icon("split_horizontal"))
            self.toggle_dir_btn.setToolTip("Split Direction: Horizontal")
        self.canvas.update()

    def cancel_split(self) -> None:
        self.main_window.cancel_split_editor()

    def apply_split(self) -> None:
        if self.current_image is None:
            self.main_window.cancel_split_editor()
            return

        sorted_splits = sorted(self.canvas.splits)
        points = [0.0] + sorted_splits + [1.0]

        split_images = []
        img_w = self.current_image.width()
        img_h = self.current_image.height()

        for i in range(len(points) - 1):
            p_prev = points[i]
            p_curr = points[i+1]

            if self.canvas.orientation == "horizontal":
                y_start = int(p_prev * img_h)
                y_end = int(p_curr * img_h)
                height = y_end - y_start
                if height > 0:
                    split_images.append(self.current_image.copy(0, y_start, img_w, height))
            else:
                x_start = int(p_prev * img_w)
                x_end = int(p_curr * img_w)
                width = x_end - x_start
                if width > 0:
                    split_images.append(self.current_image.copy(x_start, 0, width, img_h))

        if split_images:
            self.main_window.add_split_images_to_groups(split_images)

        self.main_window.cancel_split_editor()


class ClipboardImageMergerWindow(QMainWindow):
    def get_icon(self, name: str) -> QIcon:
        icon_path = self.repo_root / "icons" / f"{name}.svg"
        if icon_path.exists():
            return QIcon(str(icon_path))
        return QIcon()

    def __init__(
        self,
        settings: QSettings,
        hotkey_filter: WindowsHotkeyFilter | None,
        history_store: ClipboardHistoryStore,
        app_dir: Path,
    ) -> None:
        super().__init__()
        self.settings = settings
        self.hotkey_filter = hotkey_filter
        self.history_store = history_store
        self.app_dir = app_dir
        self.repo_root = resource_root()
        self.setWindowTitle("Clipboard Image Merger")
        self.setMinimumSize(QSize(WINDOW_MIN_WIDTH, WINDOW_MIN_HEIGHT))

        self.captures: list[ClipboardImage] = []
        self.hidden_captures: list[ClipboardImage] = []
        self.selected_capture_ids: list[int] = []
        self.cards: dict[int, ImageCard] = {}
        self.merge_groups: list[MergeGroup] = []
        self.group_cards: dict[int, MergeGroupCard] = {}
        self.selected_group_id: int | None = None
        self.capture_counter = count(1)
        self.group_counter = count(1)
        self.ignore_next_clipboard_event = False
        self.last_captured_digest: str | None = None
        self.last_captured_pixel_signature: tuple[int, int, bytes] | None = None
        self.last_capture_time = 0.0
        self.last_external_hwnd: int | None = None
        self.is_quitting = False
        self.pending_paste_images: list[QImage] = []
        self.pending_paste_index = 0

        self.clipboard = QGuiApplication.clipboard()
        self.clipboard.dataChanged.connect(self.on_clipboard_changed)

        self.paste_button = AnimatedButton("")
        self.paste_button.setIcon(self.get_icon("paste_merged"))
        self.paste_button.setIconSize(QSize(20, 20))
        self.paste_button.setToolTip("Paste Merged Image")
        self.paste_button.clicked.connect(self.paste_merged_selection)

        self.paste_delayed_button = AnimatedButton("")
        self.paste_delayed_button.setIcon(self.get_icon("paste_delayed"))
        self.paste_delayed_button.setIconSize(QSize(20, 20))
        self.paste_delayed_button.setToolTip("Paste Selected Images one-by-one with timer (Delayed)")
        self.paste_delayed_button.clicked.connect(self.paste_selected_images_delayed)

        self.paste_batch_button = AnimatedButton("")
        self.paste_batch_button.setIcon(self.get_icon("paste_batch"))
        self.paste_batch_button.setIconSize(QSize(20, 20))
        self.paste_batch_button.setToolTip("Paste Selected Images all-at-once as files (Batch)")
        self.paste_batch_button.clicked.connect(self.paste_selected_images_batch)

        self.clear_selection_button = AnimatedButton("")
        self.clear_selection_button.setIcon(self.get_icon("clear_selection"))
        self.clear_selection_button.setIconSize(QSize(20, 20))
        self.clear_selection_button.setToolTip("Clear Selection")
        self.clear_selection_button.clicked.connect(self.clear_selection)

        self.more_button = AnimatedButton("")
        self.more_button.setIcon(self.get_icon("more"))
        self.more_button.setIconSize(QSize(20, 20))
        self.more_button.setToolTip("Load More History")
        self.more_button.clicked.connect(self.show_more_history)

        self.add_group_button = AnimatedButton("")
        self.add_group_button.setIcon(self.get_icon("add_right"))
        self.add_group_button.setIconSize(QSize(20, 20))
        self.add_group_button.setToolTip("Add selected images to right groups")
        self.add_group_button.clicked.connect(self.add_selection_to_groups)

        self.split_group_button = AnimatedButton("")
        self.split_group_button.setIcon(self.get_icon("split_right"))
        self.split_group_button.setIconSize(QSize(20, 20))
        self.split_group_button.setToolTip("Split selected image")
        self.split_group_button.clicked.connect(self.start_split_editor)

        self.remove_group_button = AnimatedButton("")
        self.remove_group_button.setIcon(self.get_icon("remove_group"))
        self.remove_group_button.setIconSize(QSize(20, 20))
        self.remove_group_button.setToolTip("Remove selected group")
        self.remove_group_button.clicked.connect(self.remove_selected_group)

        self.clear_groups_button = AnimatedButton("")
        self.clear_groups_button.setIcon(self.get_icon("clear_groups"))
        self.clear_groups_button.setIconSize(QSize(20, 20))
        self.clear_groups_button.setToolTip("Clear all groups on right")
        self.clear_groups_button.clicked.connect(self.clear_merge_groups)

        top_bar = QHBoxLayout()
        top_bar.setContentsMargins(0, 0, 0, 0)
        top_bar.setSpacing(8)
        top_bar.addWidget(self.paste_button)
        top_bar.addWidget(self.paste_delayed_button)
        top_bar.addWidget(self.paste_batch_button)
        top_bar.addWidget(self.clear_selection_button)
        top_bar.addWidget(self.more_button)
        top_bar.addStretch(1)

        self.selection_summary = QLabel("Selected order: none")
        self.selection_summary.setWordWrap(False)
        self.selection_summary.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.selection_summary.setMaximumHeight(18)

        self.empty_label = QLabel("Copy an image to the clipboard to start capturing.")
        self.empty_label.setAlignment(Qt.AlignCenter)
        self.empty_label.setStyleSheet("color: #6b7280; padding: 24px;")

        self.cards_container = QWidget()
        self.cards_layout = QVBoxLayout(self.cards_container)
        self.cards_layout.setContentsMargins(0, 0, 0, 0)
        self.cards_layout.setSpacing(12)
        self.cards_layout.setAlignment(Qt.AlignTop)
        self.cards_layout.addWidget(self.empty_label)

        self.left_scroll = QScrollArea()
        self.left_scroll.setWidgetResizable(True)
        self.left_scroll.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.left_scroll.setFocusPolicy(Qt.StrongFocus)
        self.left_scroll.setWidget(self.cards_container)
        self.left_scroll.viewport().installEventFilter(self)

        self.groups_empty_label = QLabel("Add selected images to the right to create merged groups.")
        self.groups_empty_label.setAlignment(Qt.AlignCenter)
        self.groups_empty_label.setStyleSheet("color: #6b7280; padding: 24px;")

        self.groups_container = QWidget()
        self.groups_layout = QVBoxLayout(self.groups_container)
        self.groups_layout.setContentsMargins(0, 0, 0, 0)
        self.groups_layout.setSpacing(12)
        self.groups_layout.setAlignment(Qt.AlignTop)
        self.groups_layout.addWidget(self.groups_empty_label)

        self.groups_scroll = QScrollArea()
        self.groups_scroll.setWidgetResizable(True)
        self.groups_scroll.setAlignment(Qt.AlignTop | Qt.AlignHCenter)
        self.groups_scroll.setFocusPolicy(Qt.StrongFocus)
        self.groups_scroll.setWidget(self.groups_container)
        self.groups_scroll.viewport().installEventFilter(self)

        middle_panel = QWidget()
        middle_layout = QVBoxLayout(middle_panel)
        middle_layout.setContentsMargins(0, 0, 0, 0)
        middle_layout.setSpacing(8)
        middle_layout.addStretch(1)
        middle_layout.addWidget(self.add_group_button)
        middle_layout.addWidget(self.split_group_button)
        middle_layout.addWidget(self.remove_group_button)
        middle_layout.addWidget(self.clear_groups_button)
        middle_layout.addStretch(1)

        self.right_stack = QStackedWidget()
        self.right_stack.addWidget(self.groups_scroll)
        self.split_editor = ImageSplitEditor(self)
        self.right_stack.addWidget(self.split_editor)

        content_layout = QHBoxLayout()
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(8)
        content_layout.addWidget(self.left_scroll, 1)
        content_layout.addWidget(middle_panel)
        content_layout.addWidget(self.right_stack, 1)

        central = QWidget()
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(10, 8, 10, 10)
        central_layout.setSpacing(4)
        central_layout.addLayout(top_bar)
        central_layout.addWidget(self.selection_summary)
        central_layout.addLayout(content_layout)

        self.setCentralWidget(central)
        self.setStatusBar(QStatusBar(self))
        self.build_menu()
        self.apply_styles()
        self.setup_shortcuts()
        self.setup_system_tray()
        self.load_history()
        self.refresh_actions()

    def eventFilter(self, watched, event) -> bool:
        if event.type() == QEvent.Resize:
            if watched is self.left_scroll.viewport():
                QTimer.singleShot(0, self.handle_left_resize)
            elif watched is self.groups_scroll.viewport():
                QTimer.singleShot(0, self.handle_groups_resize)
        return super().eventFilter(watched, event)

    def handle_left_resize(self) -> None:
        self.refresh_card_previews()
        QTimer.singleShot(0, self.scroll_list_to_bottom)

    def handle_groups_resize(self) -> None:
        self.refresh_group_previews()
        QTimer.singleShot(0, self.ensure_group_visible)

    def setup_shortcuts(self) -> None:
        self.shortcut_up = QShortcut(QKeySequence(Qt.Key_Up), self)
        self.shortcut_up.setContext(Qt.WindowShortcut)
        self.shortcut_up.activated.connect(lambda: self.move_selection(-1))

        self.shortcut_down = QShortcut(QKeySequence(Qt.Key_Down), self)
        self.shortcut_down.setContext(Qt.WindowShortcut)
        self.shortcut_down.activated.connect(lambda: self.move_selection(1))

        self.shortcut_enter = QShortcut(QKeySequence(Qt.Key_Return), self)
        self.shortcut_enter.setContext(Qt.WindowShortcut)
        self.shortcut_enter.activated.connect(self.trigger_paste_shortcut)

        self.shortcut_numpad_enter = QShortcut(QKeySequence(Qt.Key_Enter), self)
        self.shortcut_numpad_enter.setContext(Qt.WindowShortcut)
        self.shortcut_numpad_enter.activated.connect(self.trigger_paste_shortcut)

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if (
            event.type() == QEvent.WindowStateChange
            and not self.is_quitting
            and self.isMinimized()
            and self.tray_icon.isVisible()
        ):
            QTimer.singleShot(0, self.hide_to_tray)

    def closeEvent(self, event) -> None:
        if self.tray_icon.isVisible():
            self.tray_icon.hide()
        super().closeEvent(event)

    def setup_system_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self.tray_icon = QSystemTrayIcon(self)
            return

        tray_menu = QMenu(self)
        show_action = tray_menu.addAction("Show")
        quit_action = tray_menu.addAction("Quit")
        show_action.triggered.connect(self.restore_from_tray)
        quit_action.triggered.connect(self.quit_from_tray)

        self.tray_icon = QSystemTrayIcon(self)
        self.tray_icon.setIcon(self.windowIcon() or QApplication.instance().windowIcon())
        self.tray_icon.setToolTip("Clipboard Image Merger")
        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.activated.connect(self.on_tray_activated)
        self.tray_icon.show()

    def hide_to_tray(self) -> None:
        self.hide()
        if self.tray_icon.isVisible():
            self.tray_icon.showMessage(
                "Clipboard Image Merger",
                "The app is still running in the system tray.",
                QSystemTrayIcon.Information,
                2500,
            )

    def restore_from_tray(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()
        self.select_latest_capture()

    def quit_from_tray(self) -> None:
        self.is_quitting = True
        self.tray_icon.hide()
        app = QApplication.instance()
        if app is not None:
            app.quit()
        else:
            self.close()

    def on_tray_activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self.restore_from_tray()

    def load_history(self) -> None:
        history = self.history_store.load_recent(DB_HISTORY_LIMIT)
        if not history:
            return

        restored: list[ClipboardImage] = []
        max_capture_id = 0
        for record in history:
            image = png_bytes_to_image(record.png_bytes)
            if image.isNull():
                continue
            restored.append(
                ClipboardImage(
                    capture_id=record.capture_id,
                    image=image,
                    digest=record.digest,
                )
            )
            max_capture_id = max(max_capture_id, record.capture_id)

        if restored:
            self.hidden_captures = restored[:-MAX_IMAGES]
            self.captures = restored[-MAX_IMAGES:]
            self.capture_counter = count(max_capture_id + 1)
            self.last_captured_digest = self.captures[-1].digest
            self.last_captured_pixel_signature = image_to_raw_pixel_signature(self.captures[-1].image)
            self.rebuild_cards()
            self.select_latest_capture()

    def show_and_activate(self) -> None:
        current_hwnd = get_foreground_window()
        own_hwnd = int(self.winId()) if self.winId() is not None else 0
        if current_hwnd and current_hwnd != own_hwnd:
            self.last_external_hwnd = current_hwnd

        if self.isMinimized():
            self.showNormal()
        else:
            self.show()
        self.raise_()
        self.activateWindow()
        self.select_latest_capture()

    def focusInEvent(self, event) -> None:
        self.select_latest_capture()
        super().focusInEvent(event)

    def keyPressEvent(self, event) -> None:
        super().keyPressEvent(event)

    def trigger_paste_shortcut(self) -> None:
        if self.paste_button.isEnabled():
            self.paste_merged_selection()

    def open_hotkey_settings(self) -> None:
        current_hotkey = self.settings.value(HOTKEY_SETTINGS_KEY, DEFAULT_HOTKEY, type=str)
        dialog = HotkeySettingsDialog(current_hotkey, self)
        if dialog.exec() != QDialog.Accepted:
            return

        chosen_hotkey = dialog.selected_hotkey_text()
        if not chosen_hotkey:
            QMessageBox.information(self, "Hotkey Settings", "No hotkey was entered.")
            return

        success, message = self.apply_hotkey(chosen_hotkey)
        if success:
            QMessageBox.information(self, "Hotkey Updated", message)
        else:
            QMessageBox.warning(self, "Hotkey Unavailable", message)

    def apply_hotkey(self, sequence_text: str) -> tuple[bool, str]:
        if self.hotkey_filter is None or sys.platform != "win32":
            return False, "Global hotkeys are only supported on Windows in this app."

        registration, error = hotkey_from_sequence_text(sequence_text)
        if error or registration is None:
            return False, error or "The selected hotkey is not valid."

        previous = self.hotkey_filter.current_hotkey
        self.hotkey_filter.unregister()
        if self.hotkey_filter.register(registration):
            self.settings.setValue(HOTKEY_SETTINGS_KEY, registration.sequence_text)
            self.statusBar().showMessage(f"Hotkey set to {registration.sequence_text}", 5000)
            return True, f"Hotkey set to {registration.sequence_text}."

        if previous is not None:
            self.hotkey_filter.register(previous)

        return (
            False,
            f"{registration.sequence_text} is already in use by another app or reserved by Windows. "
            "Windows does not let this app overwrite another app's global hotkey, so the previous hotkey was kept.",
        )

    def build_menu(self) -> None:
        hotkey_action = QAction("Hotkey Settings", self)
        hotkey_action.triggered.connect(self.open_hotkey_settings)
        self.startup_action = QAction("Start on System Startup", self)
        self.startup_action.setCheckable(True)
        self.startup_action.setChecked(is_startup_enabled() if sys.platform == "win32" else False)
        self.startup_action.triggered.connect(self.toggle_startup_setting)
        exit_action = QAction("Exit", self)
        exit_action.triggered.connect(self.close)
        file_menu = self.menuBar().addMenu("File")
        file_menu.addAction(hotkey_action)
        file_menu.addAction(self.startup_action)
        file_menu.addAction(exit_action)

    def toggle_startup_setting(self, enabled: bool) -> None:
        if sys.platform != "win32":
            QMessageBox.information(
                self,
                "Start on System Startup",
                "This setting is currently supported only on Windows.",
            )
            self.startup_action.setChecked(False)
            return

        set_startup_enabled(enabled, self.app_dir)
        actual_state = is_startup_enabled()
        self.startup_action.setChecked(actual_state)
        if actual_state == enabled:
            state_text = "enabled" if actual_state else "disabled"
            self.statusBar().showMessage(f"Start on system startup {state_text}.", 4000)
            return

        QMessageBox.warning(
            self,
            "Start on System Startup",
            "The startup setting could not be updated as requested.",
        )

    def apply_styles(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow {
                background: #f5f5f4;
            }
            QLabel {
                color: #1c1917;
            }
            #imageCard {
                background: white;
                border: 2px solid #d6d3d1;
                border-radius: 12px;
            }
            #imageCard[selected="true"] {
                border-color: #ea580c;
                background: #fff7ed;
            }
            #mergeGroupCard {
                background: white;
                border: 2px solid #d6d3d1;
                border-radius: 12px;
            }
            #mergeGroupCard[selected="true"] {
                border-color: #2563eb;
                background: #eff6ff;
            }
            QLabel {
                font-size: 13px;
            }
            """
        )

    def on_clipboard_changed(self) -> None:
        if self.ignore_next_clipboard_event:
            self.ignore_next_clipboard_event = False
            return

        mime = self.clipboard.mimeData()
        if mime is None or not mime.hasImage():
            return

        image = self.clipboard.image()
        if image.isNull():
            return

        pixel_signature = image_to_raw_pixel_signature(image)
        if self.last_captured_pixel_signature is not None and pixel_signature == self.last_captured_pixel_signature:
            self.statusBar().showMessage("Ignored consecutive duplicate clipboard image", 2500)
            return

        capture = ClipboardImage(
            capture_id=next(self.capture_counter),
            image=image.copy(),
            digest=hashlib.sha256(image_to_png_bytes(image)).hexdigest(),
        )

        if (
            capture.digest == self.last_captured_digest
            and monotonic() - self.last_capture_time < 0.75
        ):
            return

        combined_captures = [*self.hidden_captures, *self.captures, capture]
        combined_captures = combined_captures[-DB_HISTORY_LIMIT:]
        self.hidden_captures = combined_captures[:-MAX_IMAGES]
        self.captures = combined_captures[-MAX_IMAGES:]
        self.last_captured_digest = capture.digest
        self.last_captured_pixel_signature = pixel_signature
        self.last_capture_time = monotonic()
        self.history_store.enqueue_save(
            ClipboardHistoryRecord(
                capture_id=capture.capture_id,
                png_bytes=image_to_png_bytes(capture.image),
                digest=capture.digest,
            )
        )

        self.rebuild_cards()
        self.select_latest_capture()
        self.statusBar().showMessage(f"Captured image #{capture.capture_id}", 3000)

    def rebuild_cards(self) -> None:
        while self.cards_layout.count():
            item = self.cards_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                if widget is self.empty_label:
                    widget.setParent(None)
                else:
                    widget.deleteLater()

        self.cards.clear()

        if not self.captures:
            self.cards_layout.addWidget(self.empty_label)
            self.refresh_actions()
            return

        self.empty_label.setParent(None)

        for capture in self.captures:
            card = ImageCard(capture, self.toggle_selection)
            selection_index = self.selection_position_for(capture.capture_id)
            card.refresh_selection_state(selection_index)
            self.cards[capture.capture_id] = card
            self.cards_layout.addWidget(card)
        self.refresh_card_previews()
        QTimer.singleShot(0, self.scroll_list_to_bottom)
        self.refresh_actions()

    def scroll_list_to_bottom(self) -> None:
        scrollbar = self.left_scroll.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def list_preview_width(self) -> int:
        return max(180, self.left_scroll.viewport().width() - 28)

    def refresh_card_previews(self) -> None:
        target_width = self.list_preview_width()
        for card in self.cards.values():
            card.refresh_preview(target_width)

    def show_more_history(self) -> None:
        if not self.hidden_captures:
            return
        self.captures = [*self.hidden_captures, *self.captures]
        self.hidden_captures = []
        self.rebuild_cards()
        QTimer.singleShot(0, self.scroll_list_to_bottom)

    def group_preview_width(self) -> int:
        return max(180, self.groups_scroll.viewport().width() - 28)

    def refresh_group_previews(self) -> None:
        target_width = self.group_preview_width()
        for card in self.group_cards.values():
            card.refresh_preview(target_width)

    def ensure_selection_visible(self) -> None:
        if not self.selected_capture_ids:
            return
        current_id = self.selected_capture_ids[-1]
        card = self.cards.get(current_id)
        if card is None:
            return

        self.cards_layout.activate()
        viewport = self.left_scroll.viewport()
        scrollbar = self.left_scroll.verticalScrollBar()
        card_top = card.y()
        card_bottom = card_top + card.height()
        view_top = scrollbar.value()
        view_bottom = view_top + viewport.height()
        margin = 24

        if card_top < view_top + margin:
            scrollbar.setValue(max(0, card_top - margin))
        elif card_bottom > view_bottom - margin:
            scrollbar.setValue(card_bottom - viewport.height() + margin)

    def selection_position_for(self, capture_id: int) -> int | None:
        if capture_id not in self.selected_capture_ids:
            return None
        return self.selected_capture_ids.index(capture_id) + 1

    def toggle_selection(self, capture_id: int, additive: bool = False) -> None:
        if additive:
            if capture_id in self.selected_capture_ids:
                self.selected_capture_ids.remove(capture_id)
            else:
                self.selected_capture_ids.append(capture_id)
        else:
            self.selected_capture_ids = [capture_id]

        for current_id, card in self.cards.items():
            card.refresh_selection_state(self.selection_position_for(current_id))
        self.refresh_actions()
        QTimer.singleShot(0, self.ensure_selection_visible)

    def move_selection(self, offset: int) -> None:
        if not self.captures:
            return

        capture_ids = [capture.capture_id for capture in self.captures]
        if not self.selected_capture_ids:
            target_index = len(capture_ids) - 1 if offset >= 0 else 0
        else:
            current_id = self.selected_capture_ids[-1]
            try:
                current_index = capture_ids.index(current_id)
            except ValueError:
                current_index = len(capture_ids) - 1
            target_index = max(0, min(len(capture_ids) - 1, current_index + offset))

        self.selected_capture_ids = [capture_ids[target_index]]
        for current_id, card in self.cards.items():
            card.refresh_selection_state(self.selection_position_for(current_id))
        self.refresh_actions()
        QTimer.singleShot(0, self.ensure_selection_visible)

    def select_latest_capture(self) -> None:
        if not self.captures:
            return
        latest_capture_id = self.captures[-1].capture_id
        self.selected_capture_ids = [latest_capture_id]
        for current_id, card in self.cards.items():
            card.refresh_selection_state(self.selection_position_for(current_id))
        self.refresh_actions()
        QTimer.singleShot(0, self.ensure_selection_visible)

    def clear_selection(self) -> None:
        if not self.selected_capture_ids:
            return
        self.selected_capture_ids.clear()
        for card in self.cards.values():
            card.refresh_selection_state(None)
        self.refresh_actions()

    def selected_images(self) -> list[ClipboardImage]:
        capture_map = {capture.capture_id: capture for capture in self.captures}
        return [capture_map[capture_id] for capture_id in self.selected_capture_ids if capture_id in capture_map]

    def add_selection_to_groups(self) -> None:
        selected = self.selected_images()
        if not selected:
            QMessageBox.information(self, "No selection", "Select one or more images on the left first.")
            return

        group = MergeGroup(
            group_id=next(self.group_counter),
            images=[item.image.copy() for item in selected],
        )
        self.merge_groups.append(group)
        self.rebuild_group_cards()
        self.select_group(group.group_id)

    def start_split_editor(self) -> None:
        selected = self.selected_images()
        if len(selected) != 1:
            QMessageBox.information(self, "Invalid Selection", "Please select exactly one image on the left to split.")
            return

        capture = selected[0]
        self.split_editor.current_image = capture.image.copy()
        self.split_editor.current_capture_id = capture.capture_id
        self.split_editor.canvas.set_image(self.split_editor.current_image)
        self.right_stack.setCurrentIndex(1)
        self.statusBar().showMessage(f"Splitting image #{capture.capture_id}", 3000)

    def cancel_split_editor(self) -> None:
        self.right_stack.setCurrentIndex(0)
        self.split_editor.current_image = None
        self.split_editor.current_capture_id = None
        self.statusBar().clearMessage()

    def add_split_images_to_groups(self, images: list[QImage]) -> None:
        for img in images:
            group = MergeGroup(
                group_id=next(self.group_counter),
                images=[img],
            )
            self.merge_groups.append(group)
        self.rebuild_group_cards()
        if self.merge_groups:
            self.select_group(self.merge_groups[-1].group_id)

    def rebuild_group_cards(self) -> None:
        while self.groups_layout.count():
            item = self.groups_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                if widget is self.groups_empty_label:
                    widget.setParent(None)
                else:
                    widget.deleteLater()

        self.group_cards.clear()

        if not self.merge_groups:
            self.groups_layout.addWidget(self.groups_empty_label)
            self.refresh_actions()
            return

        self.groups_empty_label.setParent(None)
        for group in self.merge_groups:
            card = MergeGroupCard(group, self.select_group)
            card.refresh_selection_state(group.group_id == self.selected_group_id)
            self.group_cards[group.group_id] = card
            self.groups_layout.addWidget(card)
        self.refresh_group_previews()
        self.refresh_actions()

    def select_group(self, group_id: int) -> None:
        self.selected_group_id = group_id
        for current_id, card in self.group_cards.items():
            card.refresh_selection_state(current_id == group_id)
        self.refresh_actions()
        QTimer.singleShot(0, self.ensure_group_visible)

    def ensure_group_visible(self) -> None:
        if self.selected_group_id is None:
            return
        card = self.group_cards.get(self.selected_group_id)
        if card is None:
            return

        self.groups_layout.activate()
        viewport = self.groups_scroll.viewport()
        scrollbar = self.groups_scroll.verticalScrollBar()
        card_top = card.y()
        card_bottom = card_top + card.height()
        view_top = scrollbar.value()
        view_bottom = view_top + viewport.height()
        margin = 24

        if card_top < view_top + margin:
            scrollbar.setValue(max(0, card_top - margin))
        elif card_bottom > view_bottom - margin:
            scrollbar.setValue(card_bottom - viewport.height() + margin)

    def remove_selected_group(self) -> None:
        if self.selected_group_id is None:
            return
        self.merge_groups = [group for group in self.merge_groups if group.group_id != self.selected_group_id]
        self.selected_group_id = self.merge_groups[-1].group_id if self.merge_groups else None
        self.rebuild_group_cards()

    def clear_merge_groups(self) -> None:
        if not self.merge_groups:
            return
        self.merge_groups = []
        self.selected_group_id = None
        self.rebuild_group_cards()

    def paste_merged_selection(self) -> None:
        selected = self.selected_images()
        if not selected:
            QMessageBox.information(self, "No selection", "Select at least one image first.")
            return

        target_width = max(item.image.width() for item in selected)
        merged = merge_images_vertically([item.image for item in selected], target_width, gap=0)

        self.ignore_next_clipboard_event = True
        self.clear_selection()

        if sys.platform == "win32" and is_explorer_window(self.last_external_hwnd):
            temp_dir = Path(tempfile.gettempdir())
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            temp_file_path = temp_dir / f"MergedImage_{timestamp}.png"
            merged.save(str(temp_file_path))
            
            mime_data = QMimeData()
            mime_data.setUrls([QUrl.fromLocalFile(str(temp_file_path))])
            self.clipboard.setMimeData(mime_data)
            self.statusBar().showMessage(f"Prepared merged image file for folder paste", 5000)
        else:
            self.clipboard.setImage(merged)
            self.statusBar().showMessage(f"Prepared merged image from {len(selected)} capture(s)", 5000)

        if sys.platform != "win32":
            QMessageBox.information(
                self,
                "Paste Merged Image",
                "The merged image is in the clipboard. Automated paste is only supported on Windows.",
            )
            return

        if not self.last_external_hwnd:
            QMessageBox.information(
                self,
                "Paste Merged Image",
                "The merged image is in the clipboard, but no previous target window was captured yet. "
                "Open the app using the global hotkey from the target app first.",
            )
            return

        self.showMinimized()
        QTimer.singleShot(180, self.restore_target_and_paste)

    def prepare_pending_images(self) -> bool:
        if self.merge_groups:
            self.pending_paste_images = [
                merge_images_vertically(group.images, max(image.width() for image in group.images), gap=0)
                for group in self.merge_groups
            ]
            self.clear_merge_groups()
        else:
            selected = self.selected_images()
            if not selected:
                QMessageBox.information(
                    self,
                    "No selection",
                    "Select images on the left or add merge groups on the right first.",
                )
                return False
            self.pending_paste_images = [item.image.copy() for item in selected]
            self.clear_selection()
        self.pending_paste_index = 0
        return True

    def paste_selected_images_delayed(self) -> None:
        if sys.platform != "win32":
            QMessageBox.information(
                self,
                "Paste Selected Images",
                "Automated separate-image paste is only supported on Windows.",
            )
            return

        if not self.last_external_hwnd:
            QMessageBox.information(
                self,
                "Paste Selected Images",
                "No previous target window was captured yet. Open the app using the global hotkey from the target app first.",
            )
            return

        if not self.prepare_pending_images():
            return

        self.statusBar().showMessage(f"Preparing to paste {len(self.pending_paste_images)} separate image(s)", 5000)
        self.showMinimized()
        QTimer.singleShot(180, self.restore_target_and_paste_separately)

    def paste_selected_images_batch(self) -> None:
        if sys.platform != "win32":
            QMessageBox.information(
                self,
                "Paste Selected Images",
                "Automated separate-image paste is only supported on Windows.",
            )
            return

        if not self.last_external_hwnd:
            QMessageBox.information(
                self,
                "Paste Selected Images",
                "No previous target window was captured yet. Open the app using the global hotkey from the target app first.",
            )
            return

        if not self.prepare_pending_images():
            return

        temp_dir = Path(tempfile.gettempdir())
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        
        urls = []
        for idx, img in enumerate(self.pending_paste_images, start=1):
            temp_file_path = temp_dir / f"Image_{timestamp}_{idx}.png"
            img.save(str(temp_file_path))
            urls.append(QUrl.fromLocalFile(str(temp_file_path)))
            
        self.ignore_next_clipboard_event = True
        mime_data = QMimeData()
        mime_data.setUrls(urls)
        self.clipboard.setMimeData(mime_data)
        
        if is_explorer_window(self.last_external_hwnd):
            self.statusBar().showMessage(f"Prepared {len(self.pending_paste_images)} separate image(s) for folder paste", 5000)
        else:
            self.statusBar().showMessage(f"Prepared {len(self.pending_paste_images)} separate image(s) for batch paste", 5000)
        self.showMinimized()
        QTimer.singleShot(180, self.restore_target_and_paste)

    def restore_target_and_paste(self) -> None:
        if not self.last_external_hwnd:
            return
        if is_window_minimized(self.last_external_hwnd):
            show_window(self.last_external_hwnd, SW_RESTORE)
        set_foreground_window(self.last_external_hwnd)
        QTimer.singleShot(140, send_ctrl_v)

    def restore_target_and_paste_separately(self) -> None:
        if not self.last_external_hwnd or not self.pending_paste_images:
            return
        if is_window_minimized(self.last_external_hwnd):
            show_window(self.last_external_hwnd, SW_RESTORE)
        set_foreground_window(self.last_external_hwnd)
        QTimer.singleShot(140, self.paste_next_selected_image)

    def paste_next_selected_image(self) -> None:
        if self.pending_paste_index >= len(self.pending_paste_images):
            self.pending_paste_images = []
            self.pending_paste_index = 0
            return

        self.ignore_next_clipboard_event = True
        self.clipboard.setImage(self.pending_paste_images[self.pending_paste_index])
        send_ctrl_v()
        self.pending_paste_index += 1
        if self.pending_paste_index < len(self.pending_paste_images):
            QTimer.singleShot(180, self.paste_next_selected_image)
        else:
            self.pending_paste_images = []
            self.pending_paste_index = 0

    def refresh_actions(self) -> None:
        selected = self.selected_images()
        if selected:
            summary = ", ".join(f"#{index}" for index, _item in enumerate(selected, start=1))
            self.selection_summary.setText(f"Selected order: {summary}")
        else:
            self.selection_summary.setText("Selected order: none")

        has_selection = bool(selected)
        has_paste_separate = bool(self.merge_groups) or has_selection
        self.paste_button.setEnabled(has_selection)
        self.paste_delayed_button.setEnabled(has_paste_separate)
        self.paste_batch_button.setEnabled(has_paste_separate)
        self.clear_selection_button.setEnabled(has_selection)
        self.more_button.setEnabled(bool(self.hidden_captures))
        self.more_button.setText(str(len(self.hidden_captures)) if self.hidden_captures else "")


def main() -> None:
    set_windows_app_user_model_id(APP_USER_MODEL_ID)
    app = QApplication(sys.argv)
    app.setOrganizationName(SETTINGS_ORG)
    app.setApplicationName(SETTINGS_APP)

    bridge = SingleInstanceBridge(APP_SERVER_NAME, lambda: None)
    if bridge.try_notify_existing_instance():
        return

    settings = QSettings()

    repo_root = resource_root()
    app_dir = runtime_app_dir()
    icon_path = repo_root / "icons" / "camera-big.png"
    history_store = ClipboardHistoryStore(app_dir / HISTORY_DB_NAME)
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))

    hotkey_filter = WindowsHotkeyFilter(lambda: None)
    window = ClipboardImageMergerWindow(
        settings,
        hotkey_filter if sys.platform == "win32" else None,
        history_store,
        app_dir,
    )
    bridge.activate_callback = window.show_and_activate
    if not bridge.start_listening():
        QMessageBox.critical(None, "Clipboard Image Merger", "Could not start the single-instance listener.")
        return

    hotkey_filter.activate_callback = window.show_and_activate
    saved_hotkey = settings.value(HOTKEY_SETTINGS_KEY, DEFAULT_HOTKEY, type=str)
    hotkey_registration, hotkey_error = hotkey_from_sequence_text(saved_hotkey)
    if hotkey_registration is None:
        hotkey_registration, hotkey_error = hotkey_from_sequence_text(DEFAULT_HOTKEY)

    if hotkey_registration is not None and hotkey_filter.register(hotkey_registration):
        app.installNativeEventFilter(hotkey_filter)
        app.aboutToQuit.connect(hotkey_filter.unregister)
    elif sys.platform == "win32" and hotkey_registration is not None:
        QMessageBox.warning(
            None,
            "Clipboard Image Merger",
            f"Could not register global hotkey {hotkey_registration.sequence_text}. "
            "It may already be in use by another app or reserved by Windows.",
        )
    elif hotkey_error:
        QMessageBox.warning(None, "Clipboard Image Merger", hotkey_error)

    app.aboutToQuit.connect(bridge.cleanup)
    app.aboutToQuit.connect(history_store.close)
    if icon_path.exists():
        window.setWindowIcon(QIcon(str(icon_path)))
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
