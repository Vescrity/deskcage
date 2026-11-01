"""PyQt6 front end for the deskcage manager daemon."""

from __future__ import annotations

import json
import os
import shlex
import sys
import threading

from gi.repository import Gio, GLib
from PyQt6.QtCore import (
    QCoreApplication,
    QEvent,
    QObject,
    QSettings,
    QThread,
    QTimer,
    Qt,
    pyqtSignal,
)
from PyQt6.QtGui import QAction, QKeySequence
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QToolBar,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import __version__, logs, sandbox
from .client import ManagerClient, ManagerError  # noqa: F401
from .config import ConfigError, Store
from .profile import ENV_GROUPS, Profile, ProfileError
from .translation import TranslationManager


# --------------------------------------------------------------------- worker


class Worker(QThread):
    """Run one blocking daemon call off the GUI thread."""

    ok = pyqtSignal(object)
    fail = pyqtSignal(str)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self.fn = fn

    def run(self) -> None:  # noqa: D401 - Qt entry point
        try:
            self.ok.emit(self.fn())
        except Exception as exc:  # noqa: BLE001
            self.fail.emit(str(exc))


def parse_processes(info: dict) -> list[dict]:
    processes = info.get("processes", [])
    if isinstance(processes, str):
        try:
            processes = json.loads(processes)
        except json.JSONDecodeError:
            return []
    return processes


class DBusPump(QObject):
    """Daemon signals and log changes, delivered on the Qt event loop.

    GDBus only dispatches signals while a GLib main context runs, so one runs
    in a background thread and the results are queued back as Qt signals.
    """

    instanceChanged = pyqtSignal(str, str)
    processExited = pyqtSignal(str, int, int)
    logChanged = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._context: GLib.MainContext | None = None
        self._loop: GLib.MainLoop | None = None
        self._ready = threading.Event()
        self._monitors: dict[str, Gio.FileMonitor] = {}
        self._subscriptions: list[int] = []
        self._thread: threading.Thread | None = None
        self._failed = False
        self._stopped = False

    def start(self) -> None:
        if self._stopped:
            return
        if self._thread is not None and self._thread.is_alive():
            return
        if self._failed:
            # The previous pump never got a bus connection; try again with a
            # fresh thread instead of raising "threads can only be started
            # once".
            self._failed = False
            self._ready.clear()
        self._thread = threading.Thread(
            target=self._run, name="dcage-dbus", daemon=True
        )
        self._thread.start()
        self._ready.wait(5)

    def stop(self) -> None:
        self._stopped = True
        loop = self._loop
        if loop is not None:
            loop.quit()
        thread = self._thread
        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout=2)

    # ------------------------------------------------------------- pump side

    def _run(self) -> None:
        context = GLib.MainContext.new()
        context.push_thread_default()
        self._context = context
        self._loop = GLib.MainLoop.new(context, False)
        try:
            connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except GLib.Error:
            self._failed = True
            self._ready.set()
            return
        from .client import BUS_NAME, INTERFACE, OBJECT_PATH

        def on_instance(_conn, _sender, _path, _iface, _signal, params) -> None:
            instance, state = params.unpack()
            self.instanceChanged.emit(str(instance), str(state))

        def on_process(_conn, _sender, _path, _iface, _signal, params) -> None:
            instance, pid, status = params.unpack()
            self.processExited.emit(str(instance), int(pid), int(status))

        for name, callback in (
            ("InstanceChanged", on_instance),
            ("ProcessExited", on_process),
        ):
            self._subscriptions.append(
                connection.signal_subscribe(
                    BUS_NAME,
                    INTERFACE,
                    name,
                    OBJECT_PATH,
                    None,
                    Gio.DBusSignalFlags.NONE,
                    callback,
                )
            )
        self._ready.set()
        try:
            self._loop.run()
        finally:
            for subscription in self._subscriptions:
                connection.signal_unsubscribe(subscription)
            self._subscriptions.clear()
            for monitor in self._monitors.values():
                monitor.cancel()
            self._monitors.clear()

    def _call(self, fn) -> None:
        """Run ``fn`` inside the pump thread."""

        if self._stopped:
            return
        self.start()
        context = self._context
        thread = self._thread
        if context is None or thread is None or not thread.is_alive():
            return

        def invoke(*_args) -> bool:
            try:
                fn()
            except Exception:  # noqa: BLE001
                pass
            return GLib.SOURCE_REMOVE

        source = GLib.idle_source_new()
        source.set_callback(invoke)
        source.attach(context)

    # ------------------------------------------------------------ log files

    def watch(self, instance: str) -> None:
        self._call(lambda: self._add_monitor(instance))

    def unwatch(self, instance: str) -> None:
        self._call(lambda: self._remove_monitor(instance))

    def _add_monitor(self, instance: str) -> None:
        if instance in self._monitors:
            return
        directory = logs.directory(instance)
        directory.mkdir(parents=True, exist_ok=True)
        monitor = Gio.File.new_for_path(str(directory)).monitor_directory(
            Gio.FileMonitorFlags.NONE, None
        )
        monitor.connect("changed", lambda *_: self.logChanged.emit(instance))
        self._monitors[instance] = monitor

    def _remove_monitor(self, instance: str) -> None:
        monitor = self._monitors.pop(instance, None)
        if monitor is not None:
            monitor.cancel()


# -------------------------------------------------------------------- messages


def show_message(parent, icon, title: str, text: str) -> None:
    """A QMessageBox whose single button follows the selected language."""

    box = QMessageBox(parent)
    box.setIcon(icon)
    box.setWindowTitle(title)
    box.setText(text)
    button = box.addButton(
        QCoreApplication.translate("Gui", "OK"), QMessageBox.ButtonRole.AcceptRole
    )
    box.setDefaultButton(button)
    box.exec()


def critical(parent, title: str, text: str) -> None:
    show_message(parent, QMessageBox.Icon.Critical, title, text)


def information(parent, title: str, text: str) -> None:
    show_message(parent, QMessageBox.Icon.Information, title, text)


def confirm(parent, title: str, text: str, accept_text: str) -> bool:
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Question)
    box.setWindowTitle(title)
    box.setText(text)
    accept = box.addButton(accept_text, QMessageBox.ButtonRole.AcceptRole)
    box.addButton(
        QCoreApplication.translate("Gui", "Cancel"),
        QMessageBox.ButtonRole.RejectRole,
    )
    box.setDefaultButton(accept)
    box.exec()
    return box.clickedButton() is accept


def _dialog_buttons() -> QDialogButtonBox:
    buttons = QDialogButtonBox(
        QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
    )
    ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
    if ok is not None:
        ok.setText(QCoreApplication.translate("Gui", "OK"))
    cancel = buttons.button(QDialogButtonBox.StandardButton.Cancel)
    if cancel is not None:
        cancel.setText(QCoreApplication.translate("Gui", "Cancel"))
    return buttons


# --------------------------------------------------------------------- paths


def shorten_path(path: str) -> str:
    """Show paths under the home directory as ``~/...``."""

    home = os.path.expanduser("~")
    if path == home:
        return "~"
    if path.startswith(home + os.sep):
        return "~" + path[len(home) :]
    return path


class PathRow(QWidget):
    """One editable path with a file picker and a remove button."""

    changed = pyqtSignal()
    removed = pyqtSignal(object)

    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self.edit = QLineEdit(text)
        self.edit.setPlaceholderText(self.tr("path"))
        browse = QPushButton("...")
        browse.setFixedWidth(34)
        browse.setToolTip(self.tr("Choose a file"))
        remove = QPushButton("x")
        remove.setFixedWidth(30)
        remove.setToolTip(self.tr("Remove this path"))
        browse.clicked.connect(self._browse)
        remove.clicked.connect(lambda: self.removed.emit(self))
        self.edit.textChanged.connect(lambda _text: self.changed.emit())

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.edit)
        layout.addWidget(browse)
        layout.addWidget(remove)

    def text(self) -> str:
        return self.edit.text().strip()

    def _browse(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(self, self.tr("Select a file"))
        if path:
            self.edit.setText(shorten_path(path))


class PathListEditor(QWidget):
    """A one-path-per-row editor with file and directory pickers."""

    changed = pyqtSignal()
    hovered = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: list[PathRow] = []

        container = QWidget()
        self._rows_layout = QVBoxLayout(container)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(container)
        scroll.setMinimumHeight(110)

        add_file = QPushButton(self.tr("Add file..."))
        add_dir = QPushButton(self.tr("Add directory..."))
        add_path = QPushButton(self.tr("Add path"))
        add_file.clicked.connect(self._add_file)
        add_dir.clicked.connect(self._add_directory)
        add_path.clicked.connect(self._add_empty)

        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        for button in (add_file, add_dir, add_path):
            buttons.addWidget(button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(scroll)
        layout.addLayout(buttons)

        for widget in (self, container, scroll, add_file, add_dir, add_path):
            widget.installEventFilter(self)

    def _watch(self, widget: QWidget) -> None:
        widget.installEventFilter(self)
        for child in widget.findChildren(QWidget):
            child.installEventFilter(self)

    def eventFilter(self, obj, event):  # noqa: N802 - Qt hook
        if event.type() == QEvent.Type.Enter:
            self.hovered.emit()
        return super().eventFilter(obj, event)

    # ------------------------------------------------------------------ data

    def paths(self) -> list[str]:
        return [row.text() for row in self._rows if row.text()]

    def set_paths(self, values: list[str]) -> None:
        for row in list(self._rows):
            self._rows_layout.removeWidget(row)
            row.deleteLater()
        self._rows.clear()
        for value in values:
            self._new_row(value)
        self.changed.emit()

    def mark_missing(self, missing: set[str]) -> None:
        for row in self._rows:
            path = sandbox.expand_path(row.text()) if row.text() else ""
            if path in missing:
                row.edit.setStyleSheet("color: #c0392b;")
                row.edit.setToolTip(self.tr("This path does not exist on this host."))
            else:
                row.edit.setStyleSheet("")
                row.edit.setToolTip("")

    # ---------------------------------------------------------------- action

    def _new_row(self, text: str = "") -> PathRow:
        row = PathRow(text, self)
        row.changed.connect(lambda: self.changed.emit())
        row.removed.connect(self._remove_row)
        self._rows_layout.insertWidget(self._rows_layout.count() - 1, row)
        self._rows.append(row)
        self._watch(row)
        return row

    def _remove_row(self, row: PathRow) -> None:
        if row in self._rows:
            self._rows.remove(row)
        self._rows_layout.removeWidget(row)
        row.deleteLater()
        self.changed.emit()

    def _add_file(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(self, self.tr("Select a file"))
        if path:
            self._new_row(shorten_path(path))
            self.changed.emit()

    def _add_directory(self) -> None:
        path = QFileDialog.getExistingDirectory(self, self.tr("Select a directory"))
        if path:
            self._new_row(shorten_path(path))
            self.changed.emit()

    def _add_empty(self) -> None:
        row = self._new_row()
        row.edit.setFocus()
        self.changed.emit()


# --------------------------------------------------------------------- editor


class ProfileEditor(QDialog):
    _PREVIEW_MIN_HEIGHT = 160
    _PREVIEW_HEIGHT = 350

    def __init__(
        self, store: Store, profile_id: str, create: bool, parent=None
    ):
        super().__init__(parent)
        if parent is not None:
            self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.store = store
        self.profile_id = profile_id
        self.create = create
        self._preview_key: str | None = None
        self.setWindowTitle(
            self.tr("New profile - deskcage")
            if create
            else self.tr("Profile: {}").format(profile_id)
        )
        self.resize(820, 820)

        if create:
            self.profile = Profile(id=profile_id or "new-profile")
        else:
            self.profile = store.load(profile_id)

        tabs = QTabWidget()
        for page, label in (
            (self._general_tab(), self.tr("General")),
            (self._files_tab(), self.tr("Files")),
            (self._devices_tab(), self.tr("Devices")),
            (self._env_tab(), self.tr("Environment")),
        ):
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QScrollArea.Shape.NoFrame)
            scroll.setWidget(page)
            tabs.addTab(scroll, label)
        self._tabs = tabs
        self._env_page = tabs.widget(tabs.count() - 1)
        tabs.currentChanged.connect(self._tab_changed)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        save = buttons.button(QDialogButtonBox.StandardButton.Save)
        if save is not None:
            save.setText(self.tr("Save"))
        cancel = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        if cancel is not None:
            cancel.setText(self.tr("Cancel"))
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        self._preview_panel = self._preview_box()
        layout.addWidget(tabs, 1)
        layout.addWidget(self._preview_panel, 0)
        layout.addWidget(buttons)
        self._apply_preview_room()
        self._load(self.profile)
        self._connect_preview()
        self._update_warnings()
        self._refresh_preview()
        self._baseline = self._collect_profile().to_dict()

    def _preview_box(self) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)

        header = QHBoxLayout()
        title = QLabel(self.tr("Hover an option to see the arguments it generates"))
        header.addWidget(title)
        header.addStretch(1)
        copy = QPushButton(self.tr("Copy"))
        command = QPushButton(self.tr("Full command..."))
        copy.clicked.connect(
            lambda: QApplication.clipboard().setText(self.preview.toPlainText())
        )
        command.clicked.connect(self._show_command)
        header.addWidget(copy)
        header.addWidget(command)
        layout.addLayout(header)

        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setMinimumHeight(self._PREVIEW_MIN_HEIGHT)
        self.preview.setPlaceholderText(
            self.tr("Point at an option above to see its bind arguments.")
        )
        layout.addWidget(self.preview)

        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.timeout.connect(self._refresh_preview)
        return box

    def _connect_preview(self) -> None:
        # Hovering an option shows only that option's arguments.
        for widget, key in (
            (self.home_combo, "Home"),
            (self.bus_combo, "Session bus"),
            (self.pid_hint_check, "Session bus"),
            (self.dri_check, "GPU / DRI"),
            (self.snd_check, "Sound"),
            (self.video_check, "Camera"),
            (self.nvidia_check, "NVIDIA"),
            (self.wayland_check, "Wayland"),
            (self.x11_check, "X11"),
            (self.pipewire_check, "PipeWire"),
            (self.pulse_check, "PulseAudio"),
            (self.a11y_check, "Accessibility"),
            (self.ro_edit, "Read-only"),
            (self.rw_edit, "Read-write"),
            (self.tmpfs_edit, "Tmpfs"),
            (self.tmpbind_edit, "Tmpbind"),
        ):
            self._watch(widget, key)
        for editor, key in (
            (self.ro_edit, "Read-only"),
            (self.rw_edit, "Read-write"),
            (self.tmpfs_edit, "Tmpfs"),
            (self.tmpbind_edit, "Tmpbind"),
        ):
            editor.hovered.connect(
                lambda key=key: self._show_option_preview(key)
            )

        for widget in self.findChildren(QLineEdit):
            widget.textChanged.connect(self._schedule_preview)
        for widget in self.findChildren(QCheckBox):
            widget.toggled.connect(self._schedule_preview)
        for widget in self.findChildren(QComboBox):
            widget.currentIndexChanged.connect(self._schedule_preview)
        for widget in self.findChildren(QPlainTextEdit):
            if widget is not self.preview:
                widget.textChanged.connect(self._schedule_preview)
        for widget in self.findChildren(PathListEditor):
            widget.changed.connect(self._schedule_preview)
            widget.changed.connect(self._update_warnings)
        self.env_table.itemChanged.connect(self._schedule_preview)

    def _watch(self, widget: QWidget | None, key: str) -> None:
        if widget is None:
            return
        widget.setProperty("deskcage_preview_key", key)
        widget.installEventFilter(self)

    def eventFilter(self, obj, event):  # noqa: N802 - Qt hook
        if event.type() == QEvent.Type.Enter:
            key = obj.property("deskcage_preview_key")
            if isinstance(key, str) and key:
                self._show_option_preview(key)
        return super().eventFilter(obj, event)

    def _schedule_preview(self, *_args) -> None:
        if self._preview_key:
            self._preview_timer.start(150)

    def _tab_changed(self, index: int) -> None:
        self._apply_preview_room()
        if self._tabs.widget(index) is self._env_page:
            return
        self._preview_key = None
        self.preview.clear()

    def _show_option_preview(self, key: str) -> None:
        self._preview_key = key
        self._refresh_preview()

    def _preview_room(self) -> int:
        if self._tabs.currentWidget() is not self._env_page:
            return self._PREVIEW_MIN_HEIGHT
        # Keep two rows of the page above visible, up to a fixed 350 px.
        table = self.env_table
        above = (
            self._tabs.tabBar().sizeHint().height()
            + table.horizontalHeader().sizeHint().height()
            + 2 * table.verticalHeader().defaultSectionSize()
            + 2 * self.layout().spacing()
            + 80
        )
        return max(
            self._PREVIEW_MIN_HEIGHT,
            min(self._PREVIEW_HEIGHT, self.height() - above),
        )

    def _apply_preview_room(self) -> None:
        self.preview.setFixedHeight(self._preview_room())

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt hook
        super().resizeEvent(event)
        self._apply_preview_room()

    def _refresh_preview(self) -> None:
        key = self._preview_key
        if not key:
            self.preview.clear()
            return
        if key.startswith("preset:"):
            group = key.split(":", 1)[1]
            lines = [f"[{self._preset_label(group)}]"]
            unset = []
            for name in ENV_GROUPS.get(group, ()):
                if name in os.environ:
                    lines.append(f"  {name} = {os.environ[name]}")
                else:
                    unset.append(name)
            if unset:
                lines.append(f"  {self.tr('not set on the host')}: " + ", ".join(unset))
            self.preview.setPlainText("\n".join(lines))
            return
        try:
            profile = self._preview_profile(self._collect_profile(), key)
            layout = sandbox.preview_layout("<profile>")
            sections = dict(sandbox.option_sections(profile, layout))
        except (ValueError, ProfileError) as exc:
            self.preview.setPlainText(self.tr("Preview unavailable: {}").format(str(exc)))
            return
        values = sections.get(key)
        if values is None:
            if key in ("Read-only", "Read-write", "Tmpfs", "Tmpbind"):
                message = self.tr("{}: no paths configured.").format(key)
            else:
                message = self.tr("{}: no arguments generated.").format(key)
            self.preview.setPlainText(message)
            return
        lines = [f"[{key}]"]
        lines.extend(f"  {value}" for value in values)
        self.preview.setPlainText("\n".join(lines))

    #: Options that only generate mounts when their checkbox is on.  Hovering
    #: them previews what enabling them would add.
    _PREVIEW_TOGGLES = {
        "GPU / DRI": "dri",
        "Sound": "snd",
        "Camera": "video",
        "NVIDIA": "nvidia",
        "Wayland": "wayland",
        "X11": "x11",
        "PipeWire": "pipewire",
        "PulseAudio": "pulse",
        "Accessibility": "a11y",
    }

    def _preview_profile(self, profile: Profile, key: str) -> Profile:
        attribute = self._PREVIEW_TOGGLES.get(key)
        if attribute is None:
            return profile
        copy = Profile.from_dict(profile.id, profile.to_dict())
        setattr(copy, attribute, True)
        return copy

    def _show_command(self) -> None:
        try:
            profile = self._collect_profile()
        except (ValueError, ProfileError) as exc:
            critical(self, self.tr("Cannot build the command"), str(exc))
            return
        CommandDialog(profile, self).exec()

    def _update_warnings(self, *_args) -> None:
        paths = self.tmpbind_edit.paths()
        missing = {
            sandbox.expand_path(value)
            for value in paths
            if not os.path.exists(sandbox.expand_path(value))
        }
        self.tmpbind_edit.mark_missing(missing)
        messages: list[str] = []
        if missing:
            shown = ", ".join(sorted(shorten_path(path) for path in missing))
            if self.tmpbind_strict_check.isChecked():
                messages.append(
                    self.tr(
                        "Tmpbind paths will not be skipped because strict mode is on. "
                        "Starting the profile will fail: {}"
                    ).format(shown)
                )
            else:
                messages.append(
                    self.tr("Missing Tmpbind paths are skipped at start: {}").format(
                        shown
                    )
                )
        if (
            self.home_combo.currentData() == "tmpbind" or paths
        ) and not sandbox.overlay_supported():
            messages.append(
                self.tr(
                    "overlayfs is not available to user namespaces on this host; "
                    "Tmpbind cannot be used."
                )
            )
        self.files_warning.setText("\n".join(messages))
        self.files_warning.setVisible(bool(messages))

    # ------------------------------------------------------------------ tabs

    def _general_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)

        self.id_edit = QLineEdit(self.profile.id)
        self.id_edit.setReadOnly(not self.create)
        self.description_edit = QLineEdit()
        self.command_edit = QLineEdit()
        self.command_edit.setPlaceholderText(self.tr("e.g. firefox --new-instance"))
        self.keep_check = QCheckBox(
            self.tr("Keep the sandbox alive after its processes exit")
        )
        self.keep_check.setToolTip(
            self.tr("Keep the sandbox running after its last process exits.")
        )
        self.network_check = QCheckBox(self.tr("Allow network access"))
        self.network_check.setToolTip(
            self.tr("Share the host network namespace instead of creating a private one.")
        )

        self.bus_combo = QComboBox()
        self.bus_combo.addItem(self.tr("none - no session bus"), "none")
        self.bus_combo.setItemData(
            0,
            self.tr("Do not provide a D-Bus session bus at all."),
            Qt.ItemDataRole.ToolTipRole,
        )
        self.bus_combo.addItem(self.tr("isolated - sandbox only"), "isolated")
        self.bus_combo.setItemData(
            1,
            self.tr("Start a private dbus-daemon; only processes in this sandbox can talk to it."),
            Qt.ItemDataRole.ToolTipRole,
        )
        self.bus_combo.addItem(self.tr("bridge - desktop integration"), "bridge")
        self.bus_combo.setItemData(
            2,
            self.tr(
                "Use a private bus and let notifications, the tray and MPRIS cross "
                "to the host through bus-bridge. No portals or other host services."
            ),
            Qt.ItemDataRole.ToolTipRole,
        )
        self.pid_hint_check = QCheckBox(
            self.tr("Rewrite notification sender-pid hints (slower)")
        )
        self.pid_hint_check.setToolTip(
            self.tr("Make notification sender PIDs refer to processes inside the sandbox.")
        )
        bus_hint = QLabel(
            self.tr(
                "bridge exposes only notifications, the tray and MPRIS to the "
                "sandbox. No portals and no arbitrary host services."
            )
        )
        bus_hint.setWordWrap(True)

        self.home_combo = QComboBox()
        self.home_combo.addItem(self.tr("tmp - throw-away home"), "tmp")
        self.home_combo.setItemData(
            0,
            self.tr("Mount an empty tmpfs on $HOME; nothing from the real home is visible."),
            Qt.ItemDataRole.ToolTipRole,
        )
        self.home_combo.addItem(self.tr("bind - use the real home"), "bind")
        self.home_combo.setItemData(
            1,
            self.tr(
                "Bind the real home read-write; sandboxed programs can change or "
                "delete your files."
            ),
            Qt.ItemDataRole.ToolTipRole,
        )
        self.home_combo.addItem(self.tr("ro - read-only home"), "ro")
        self.home_combo.setItemData(
            2,
            self.tr("Bind the real home read-only."),
            Qt.ItemDataRole.ToolTipRole,
        )
        self.home_combo.addItem(self.tr("tmpbind - real home, writes discarded"), "tmpbind")
        self.home_combo.setItemData(
            3,
            self.tr(
                "Show the real home through an overlay: reads see your files, writes "
                "go to a temporary layer and disappear when the sandbox exits."
            ),
            Qt.ItemDataRole.ToolTipRole,
        )
        self.home_combo.currentIndexChanged.connect(self._update_warnings)

        self.hostname_edit = QLineEdit()
        self.hostname_edit.setPlaceholderText(self.tr("default: deskcage-<profile id>"))
        self.extra_edit = QPlainTextEdit()
        self.extra_edit.setPlaceholderText(self.tr("Extra bwrap arguments, one per line"))
        self.extra_edit.setMaximumHeight(90)
        self.extra_edit.setToolTip(
            self.tr(
                "Appended after all deskcage-generated arguments, so these can override "
                "them. '--' is not allowed."
            )
        )

        form.addRow(self.tr("Profile id"), self.id_edit)
        form.addRow(self.tr("Description"), self.description_edit)
        form.addRow(self.tr("Command"), self.command_edit)
        form.addRow(self.tr("Session bus"), self.bus_combo)
        form.addRow("", self.pid_hint_check)
        form.addRow(bus_hint)
        form.addRow(self.tr("Home directory"), self.home_combo)
        form.addRow("", self.network_check)
        form.addRow("", self.keep_check)
        form.addRow(self.tr("Hostname"), self.hostname_edit)
        form.addRow(self.tr("Extra bwrap args"), self.extra_edit)

        self._watch(form.labelForField(self.bus_combo), "Session bus")
        self._watch(form.labelForField(self.home_combo), "Home")
        self._watch(bus_hint, "Session bus")
        return page

    def _files_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        grid = QGridLayout()

        self.ro_edit = PathListEditor()
        self.rw_edit = PathListEditor()
        self.tmpfs_edit = PathListEditor()
        self.tmpbind_edit = PathListEditor()

        groups = (
            (
                "Read-only",
                self.tr("Read-only"),
                self.tr("Bind each path read-only. The sandbox cannot change it."),
                self.ro_edit,
            ),
            (
                "Read-write",
                self.tr("Read-write"),
                self.tr("Bind each path read-write. Changes are kept on the host."),
                self.rw_edit,
            ),
            (
                "Tmpfs",
                self.tr("Tmpfs"),
                self.tr("Mount an empty tmpfs on each path; host content is hidden."),
                self.tmpfs_edit,
            ),
            (
                "Tmpbind",
                self.tr("Tmpbind"),
                self.tr(
                    "Show the host content read-only and send writes to a temporary "
                    "layer that disappears when the sandbox exits."
                ),
                self.tmpbind_edit,
            ),
        )
        for index, (key, title, tip, editor) in enumerate(groups):
            box = QVBoxLayout()
            label = QLabel(title)
            label.setToolTip(tip)
            editor.setToolTip(tip)
            self._watch(label, key)
            box.addWidget(label)
            box.addWidget(editor)
            grid.addLayout(box, index // 2, index % 2)

        self.tmpbind_strict_check = QCheckBox(
            self.tr("Refuse to start when a Tmpbind path is missing")
        )
        self.tmpbind_strict_check.setToolTip(
            self.tr(
                "By default missing Tmpbind sources are skipped. Enable this to "
                "make starting the profile fail instead."
            )
        )
        self.tmpbind_strict_check.toggled.connect(self._update_warnings)

        self.files_warning = QLabel()
        self.files_warning.setWordWrap(True)
        self.files_warning.setStyleSheet("color: #b36b00;")
        self.files_warning.hide()

        layout.addLayout(grid)
        layout.addWidget(self.tmpbind_strict_check)
        layout.addWidget(self.files_warning)
        layout.addWidget(
            QLabel(
                self.tr(
                    "Paths are expanded on the host. One path per row; use the "
                    "buttons to pick files or directories."
                )
            )
        )
        return page

    def _devices_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.dri_check = QCheckBox(self.tr("GPU / DRI"))
        self.dri_check.setToolTip(
            self.tr("Give the sandbox the GPU and the DRM sysfs files it needs.")
        )
        self.snd_check = QCheckBox(self.tr("Sound"))
        self.snd_check.setToolTip(self.tr("Give the sandbox direct access to sound devices."))
        self.video_check = QCheckBox(self.tr("Camera"))
        self.video_check.setToolTip(
            self.tr("Give the sandbox direct access to video capture devices.")
        )
        self.wayland_check = QCheckBox(self.tr("Wayland"))
        self.wayland_check.setToolTip(
            self.tr(
                "Give the sandbox access to the compositor. This is more than "
                "drawing: it can capture the screen and inject input."
            )
        )
        self.x11_check = QCheckBox(self.tr("X11"))
        self.x11_check.setToolTip(
            self.tr("Give the sandbox full X11 access, including keyboard and pointer.")
        )
        self.pipewire_check = QCheckBox(self.tr("PipeWire"))
        self.pipewire_check.setToolTip(
            self.tr("Give the sandbox access to the host PipeWire socket.")
        )
        self.pulse_check = QCheckBox(self.tr("PulseAudio"))
        self.pulse_check.setToolTip(
            self.tr("Give the sandbox access to the host PulseAudio socket.")
        )
        self.nvidia_check = QCheckBox(self.tr("NVIDIA"))
        self.nvidia_check.setToolTip(
            self.tr("Give the sandbox the /dev/nvidia* nodes used by CUDA and NVENC.")
        )
        self.a11y_check = QCheckBox(self.tr("Accessibility"))
        self.a11y_check.setToolTip(
            self.tr("Give the sandbox the at-spi bus, used by screen readers.")
        )
        for check in (
            self.dri_check,
            self.snd_check,
            self.video_check,
            self.nvidia_check,
            self.wayland_check,
            self.x11_check,
            self.pipewire_check,
            self.pulse_check,
            self.a11y_check,
        ):
            layout.addWidget(check)
        note = QLabel(
            self.tr(
                "These options expose host channels directly. Hover an option to "
                "see the exact arguments it adds."
            )
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch(1)
        return page

    def _env_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self._updating_env = False
        self._updating_presets = False
        self._preset_groups: set[str] = set()

        blocks = QHBoxLayout()

        presets = QGroupBox(self.tr("Presets"))
        presets_layout = QVBoxLayout(presets)
        self.env_presets = QListWidget()
        for group in ENV_GROUPS:
            item = QListWidgetItem(self._preset_label(group))
            item.setData(Qt.ItemDataRole.UserRole, group)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            self.env_presets.addItem(item)
        self.env_presets.itemChanged.connect(self._preset_toggled)
        self.env_presets.setMouseTracking(True)
        self.env_presets.viewport().setMouseTracking(True)
        self.env_presets.itemEntered.connect(self._preset_hovered)
        metrics = self.env_presets.fontMetrics()
        widest = max(
            metrics.horizontalAdvance(self.env_presets.item(index).text())
            for index in range(self.env_presets.count())
        )
        self.env_presets.setFixedWidth(widest + 48)
        presets_layout.addWidget(self.env_presets)

        customize = QHBoxLayout()
        customize.addStretch(1)
        button = QPushButton(self.tr("Append as custom entries"))
        button.clicked.connect(self._customize_preset)
        customize.addWidget(button)
        presets_layout.addLayout(customize)
        blocks.addWidget(presets)

        variables = QVBoxLayout()
        self.env_table = QTableWidget(0, 3)
        self.env_table.setHorizontalHeaderLabels(
            [self.tr("Variable"), self.tr("Source"), self.tr("Value")]
        )
        header = self.env_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.env_table.verticalHeader().setVisible(False)
        self.env_table.itemChanged.connect(self._env_item_changed)
        variables.addWidget(self.env_table)

        row = QHBoxLayout()
        add = QPushButton(self.tr("Add variable"))
        remove = QPushButton(self.tr("Remove selected"))
        add.clicked.connect(lambda: self._add_env_row("", ""))
        remove.clicked.connect(self._remove_env_rows)
        row.addWidget(add)
        row.addWidget(remove)
        row.addStretch(1)
        variables.addLayout(row)
        blocks.addLayout(variables, 1)

        layout.addLayout(blocks)
        return page

    def _preset_label(self, group: str) -> str:
        return {
            "xdg": self.tr("XDG directories"),
            "session": self.tr("System and session"),
            "theme": self.tr("Qt/GTK theme and scaling"),
            "proxy": self.tr("Proxy"),
        }.get(group, group)

    def _env_mode(self, row: int) -> str:
        combo = self.env_table.cellWidget(row, 1)
        if isinstance(combo, QComboBox):
            return combo.currentData()
        return "custom"

    def _add_env_row(self, key: str, value: str | bool) -> int:
        row = self.env_table.rowCount()
        self._updating_env = True
        try:
            self.env_table.insertRow(row)
            self.env_table.setItem(row, 0, QTableWidgetItem(key))
            combo = QComboBox()
            combo.addItem(self.tr("Inherit"), "inherit")
            combo.addItem(self.tr("Custom"), "custom")
            combo.setCurrentIndex(
                combo.findData("inherit" if value is True else "custom")
            )
            self.env_table.setCellWidget(row, 1, combo)
            self.env_table.setItem(
                row, 2, QTableWidgetItem("" if value is True else str(value))
            )
            combo.currentIndexChanged.connect(
                lambda _index, widget=combo: self._env_mode_changed(widget)
            )
        finally:
            self._updating_env = False
        self._apply_env_mode(row)
        return row

    def _env_mode_changed(self, combo: QComboBox) -> None:
        if self._updating_env:
            return
        for row in range(self.env_table.rowCount()):
            if self.env_table.cellWidget(row, 1) is combo:
                self._apply_env_mode(row)
                return

    def _apply_env_mode(self, row: int) -> None:
        combo = self.env_table.cellWidget(row, 1)
        item = self.env_table.item(row, 2)
        if item is None or not isinstance(combo, QComboBox):
            return
        key_item = self.env_table.item(row, 0)
        key = key_item.text().strip() if key_item else ""
        self._updating_env = True
        try:
            if combo.currentData() == "inherit":
                if item.data(Qt.ItemDataRole.UserRole) is None:
                    item.setData(Qt.ItemDataRole.UserRole, item.text())
                if key and key in os.environ:
                    item.setText(os.environ[key])
                else:
                    item.setText(self.tr("not set"))
                item.setFlags(
                    Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled
                )
                item.setForeground(QApplication.palette().placeholderText())
            else:
                saved = item.data(Qt.ItemDataRole.UserRole)
                if saved is not None:
                    item.setText(saved)
                item.setFlags(
                    Qt.ItemFlag.ItemIsSelectable
                    | Qt.ItemFlag.ItemIsEnabled
                    | Qt.ItemFlag.ItemIsEditable
                )
                item.setForeground(QApplication.palette().text())
        finally:
            self._updating_env = False

    def _env_item_changed(self, item: QTableWidgetItem) -> None:
        if self._updating_env:
            return
        if item.column() == 0:
            self._apply_env_mode(item.row())

    def _env_rows(self) -> dict[str, str]:
        rows: dict[str, str] = {}
        for row in range(self.env_table.rowCount()):
            key_item = self.env_table.item(row, 0)
            key = key_item.text().strip() if key_item else ""
            if key:
                rows[key] = self._env_mode(row)
        return rows

    def _refresh_presets(self) -> None:
        self._updating_presets = True
        try:
            for index in range(self.env_presets.count()):
                item = self.env_presets.item(index)
                group = item.data(Qt.ItemDataRole.UserRole)
                item.setCheckState(
                    Qt.CheckState.Checked
                    if group in self._preset_groups
                    else Qt.CheckState.Unchecked
                )
        finally:
            self._updating_presets = False

    def _preset_toggled(self, item: QListWidgetItem) -> None:
        if self._updating_presets:
            return
        group = item.data(Qt.ItemDataRole.UserRole)
        if item.checkState() == Qt.CheckState.Unchecked:
            self._preset_groups.discard(group)
        else:
            self._preset_groups.add(group)
        self._refresh_presets()

    def _preset_hovered(self, item: QListWidgetItem) -> None:
        self._show_option_preview(f"preset:{item.data(Qt.ItemDataRole.UserRole)}")

    def _customize_preset(self) -> None:
        item = self.env_presets.currentItem()
        if item is None:
            return
        existing = self._env_rows()
        for key in ENV_GROUPS[item.data(Qt.ItemDataRole.UserRole)]:
            if key not in existing:
                self._add_env_row(key, os.environ.get(key, ""))

    def _remove_env_rows(self) -> None:
        for index in sorted(
            {item.row() for item in self.env_table.selectedItems()}, reverse=True
        ):
            self.env_table.removeRow(index)

    # ------------------------------------------------------------------ load

    def _load(self, profile: Profile) -> None:
        self.description_edit.setText(profile.description)
        self.command_edit.setText(shlex.join(profile.command))
        self.keep_check.setChecked(profile.keep_alive)
        self.network_check.setChecked(profile.network)
        index = self.home_combo.findData(profile.home)
        self.home_combo.setCurrentIndex(index if index >= 0 else 0)
        self.hostname_edit.setText(profile.hostname)
        self.extra_edit.setPlainText("\n".join(profile.extra_args))
        index = self.bus_combo.findData(profile.bus)
        self.bus_combo.setCurrentIndex(index if index >= 0 else 0)
        self.pid_hint_check.setChecked(profile.bus_pid_hint)
        self.ro_edit.set_paths(profile.readonly)
        self.rw_edit.set_paths(profile.readwrite)
        self.tmpfs_edit.set_paths(profile.tmpfs)
        self.tmpbind_edit.set_paths(profile.tmpbind)
        self.tmpbind_strict_check.setChecked(profile.tmpbind_strict)
        self.dri_check.setChecked(profile.dri)
        self.snd_check.setChecked(profile.snd)
        self.video_check.setChecked(profile.video)
        self.nvidia_check.setChecked(profile.nvidia)
        self.wayland_check.setChecked(profile.wayland)
        self.x11_check.setChecked(profile.x11)
        self.pipewire_check.setChecked(profile.pipewire)
        self.pulse_check.setChecked(profile.pulse)
        self.a11y_check.setChecked(profile.a11y)
        self.env_table.setRowCount(0)
        self._preset_groups = set(profile.env_presets)
        for key, value in profile.env.items():
            self._add_env_row(key, value)
        self._refresh_presets()

    @staticmethod
    def _lines(widget: QPlainTextEdit) -> list[str]:
        return [line.strip() for line in widget.toPlainText().splitlines() if line.strip()]

    # ------------------------------------------------------------------ save

    def _collect_profile(self) -> Profile:
        profile_id = self.id_edit.text().strip()
        profile = Profile(id=profile_id)
        profile.description = self.description_edit.text().strip()
        profile.command = (
            shlex.split(self.command_edit.text())
            if self.command_edit.text().strip()
            else []
        )
        profile.keep_alive = self.keep_check.isChecked()
        profile.network = self.network_check.isChecked()
        profile.home = self.home_combo.currentData()
        profile.hostname = self.hostname_edit.text().strip()
        profile.extra_args = self._lines(self.extra_edit)
        profile.bus = self.bus_combo.currentData()
        profile.bus_pid_hint = self.pid_hint_check.isChecked()
        profile.readonly = self.ro_edit.paths()
        profile.readwrite = self.rw_edit.paths()
        profile.tmpfs = self.tmpfs_edit.paths()
        profile.tmpbind = self.tmpbind_edit.paths()
        profile.tmpbind_strict = self.tmpbind_strict_check.isChecked()
        profile.dri = self.dri_check.isChecked()
        profile.snd = self.snd_check.isChecked()
        profile.video = self.video_check.isChecked()
        profile.nvidia = self.nvidia_check.isChecked()
        profile.wayland = self.wayland_check.isChecked()
        profile.x11 = self.x11_check.isChecked()
        profile.pipewire = self.pipewire_check.isChecked()
        profile.pulse = self.pulse_check.isChecked()
        profile.a11y = self.a11y_check.isChecked()
        profile.env_presets = [g for g in ENV_GROUPS if g in self._preset_groups]
        env: dict[str, str | bool] = {}
        for row in range(self.env_table.rowCount()):
            key_item = self.env_table.item(row, 0)
            key = key_item.text().strip() if key_item else ""
            if not key:
                continue
            if self._env_mode(row) == "inherit":
                env[key] = True
            else:
                value_item = self.env_table.item(row, 2)
                env[key] = value_item.text() if value_item else ""
        profile.env = env
        return profile

    def save(self) -> bool:
        profile_id = self.id_edit.text().strip()
        try:
            if self.create and self.store.exists(profile_id):
                raise ProfileError(
                    self.tr("profile already exists: {}").format(profile_id)
                )
            profile = self._collect_profile()
            profile.validate()
            self.store.save(profile)
        except (ProfileError, ConfigError, ManagerError, ValueError) as exc:
            critical(self, self.tr("Cannot save profile"), str(exc))
            return False
        self.saved_id = profile_id
        self.accept()
        return True

    def discard(self) -> None:
        super().reject()

    def renamed(self, profile_id: str) -> None:
        """Follow a rename done elsewhere, keeping unsaved edits in place."""

        self.profile_id = profile_id
        self.saved_id = profile_id
        self.profile.id = profile_id
        self.id_edit.setText(profile_id)
        if not self.create:
            self.setWindowTitle(self.tr("Profile: {}").format(profile_id))

    def reject(self) -> None:
        # Also reached by the window button: QDialog::closeEvent calls reject()
        # and ignores the close event when the dialog is still visible, so the
        # unsaved-changes prompt and the `finished` signal both stay intact.
        if self._confirm_close() == "close":
            super().reject()

    def _confirm_close(self) -> str:
        if not self._modified():
            return "close"
        box = QMessageBox(self)
        box.setWindowTitle(self.tr("Unsaved changes"))
        box.setText(self.tr("Save changes to this profile?"))
        box.setIcon(QMessageBox.Icon.Question)
        save_button = box.addButton(self.tr("Save"), QMessageBox.ButtonRole.AcceptRole)
        discard_button = box.addButton(
            self.tr("Discard"), QMessageBox.ButtonRole.DestructiveRole
        )
        box.addButton(self.tr("Cancel"), QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(save_button)
        box.exec()
        clicked = box.clickedButton()
        if clicked is save_button:
            return "saved" if self.save() else "cancel"
        if clicked is discard_button:
            return "close"
        return "cancel"

    def _modified(self) -> bool:
        if self._baseline is None:
            return True
        try:
            return self._collect_profile().to_dict() != self._baseline
        except (ProfileError, ValueError):
            return True


# -------------------------------------------------------------------- dialogs


class CommandDialog(QDialog):
    """Show the exact bwrap command a profile would run."""

    def __init__(self, profile: Profile, parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("Command - {}").format(profile.id))
        self.resize(900, 560)
        layout = QVBoxLayout(self)

        missing = sandbox.tmpbind_missing(profile)
        if missing:
            warning = QLabel(
                self.tr("Missing Tmpbind paths are skipped at start: {}").format(
                    ", ".join(shorten_path(path) for path in missing)
                )
            )
            warning.setWordWrap(True)
            warning.setStyleSheet("color: #b36b00;")
            layout.addWidget(warning)

        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setPlainText(sandbox.format_argv(sandbox.preview_argv(profile)))
        layout.addWidget(self.text)

        buttons = QHBoxLayout()
        copy = QPushButton(self.tr("Copy"))
        copy.clicked.connect(
            lambda: QApplication.clipboard().setText(self.text.toPlainText())
        )
        close = QPushButton(self.tr("Close"))
        close.clicked.connect(self.accept)
        buttons.addWidget(copy)
        buttons.addStretch(1)
        buttons.addWidget(close)
        layout.addLayout(buttons)


class StartDialog(QDialog):
    def __init__(self, profile: Profile, title: str):
        super().__init__()
        self.setWindowTitle(title)
        self.resize(520, 220)
        form = QFormLayout()
        self.command_edit = QLineEdit(shlex.join(profile.command))
        self.command_edit.setPlaceholderText(self.tr("leave empty to just hold the sandbox"))
        self.instance_edit = QLineEdit()
        self.instance_edit.setPlaceholderText(self.tr("optional, generated when empty"))
        self.cwd_edit = QLineEdit()
        self.cwd_edit.setPlaceholderText(self.tr("optional"))
        self.keep_check = QCheckBox(self.tr("Keep the sandbox alive when the command exits"))
        self.keep_check.setChecked(profile.keep_alive)
        form.addRow(self.tr("Command"), self.command_edit)
        form.addRow(self.tr("Instance id"), self.instance_edit)
        form.addRow(self.tr("Working directory"), self.cwd_edit)
        form.addRow("", self.keep_check)

        buttons = _dialog_buttons()
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def values(self) -> dict:
        return {
            "argv": shlex.split(self.command_edit.text())
            if self.command_edit.text().strip()
            else [],
            "instance": self.instance_edit.text().strip(),
            "cwd": self.cwd_edit.text().strip(),
            "keep": self.keep_check.isChecked(),
        }


class ExecDialog(QDialog):
    def __init__(self, instance: str):
        super().__init__()
        self.setWindowTitle(self.tr("Run inside {}").format(instance))
        self.resize(520, 180)
        form = QFormLayout()
        self.command_edit = QLineEdit()
        self.command_edit.setPlaceholderText(self.tr("e.g. bash -l"))
        self.cwd_edit = QLineEdit()
        self.detach_check = QCheckBox(self.tr("Do not wait for the process to exit"))
        form.addRow(self.tr("Command"), self.command_edit)
        form.addRow(self.tr("Working directory"), self.cwd_edit)
        form.addRow("", self.detach_check)
        buttons = _dialog_buttons()
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def values(self) -> dict:
        return {
            "argv": shlex.split(self.command_edit.text()),
            "cwd": self.cwd_edit.text().strip(),
            "detach": self.detach_check.isChecked(),
        }


class LogsDialog(QDialog):
    def __init__(self, pump: DBusPump, instance: str, parent=None):
        super().__init__(parent)
        self.pump = pump
        self.instance = instance
        self.setWindowTitle(self.tr("Logs - {}").format(instance))
        self.resize(820, 560)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.follow = QCheckBox(self.tr("Follow"))
        self.follow.setChecked(True)
        refresh = QPushButton(self.tr("Refresh"))
        refresh.clicked.connect(self.reload)
        row = QHBoxLayout()
        row.addWidget(self.follow)
        row.addStretch(1)
        row.addWidget(refresh)
        layout = QVBoxLayout(self)
        layout.addLayout(row)
        layout.addWidget(self.text)
        # Log writes are bursty; coalesce them instead of re-reading the whole
        # directory on every inotify event.
        self._pending = QTimer(self)
        self._pending.setSingleShot(True)
        self._pending.setInterval(150)
        self._pending.timeout.connect(self.reload)
        self._detached = False
        self.pump.logChanged.connect(self._log_changed)
        self.pump.instanceChanged.connect(self._instance_changed)
        self.pump.watch(instance)
        self.reload()

    def _detach(self) -> None:
        """Stop following the instance; safe to call more than once."""

        if self._detached:
            return
        self._detached = True
        self._pending.stop()
        try:
            self.pump.logChanged.disconnect(self._log_changed)
            self.pump.instanceChanged.disconnect(self._instance_changed)
        except TypeError:
            pass
        self.pump.unwatch(self.instance)

    def done(self, result: int) -> None:
        self._detach()
        super().done(result)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt hook
        # QDialog only turns close() into done() while it is visible.
        self._detach()
        super().closeEvent(event)

    def _log_changed(self, instance: str) -> None:
        if instance == self.instance and self.follow.isChecked():
            if not self._pending.isActive():
                self._pending.start()

    def _instance_changed(self, instance: str, _state: str) -> None:
        if instance == self.instance:
            if not self._pending.isActive():
                self._pending.start()

    def reload(self) -> None:
        body = logs.text(self.instance)
        self.text.setPlainText(body or self.tr("(no log yet)"))
        if self.follow.isChecked():
            bar = self.text.verticalScrollBar()
            bar.setValue(bar.maximum())


# ----------------------------------------------------------------- main window


def state_label(state: str) -> str:
    labels = {
        "new": QCoreApplication.translate("Gui", "New"),
        "starting": QCoreApplication.translate("Gui", "Starting"),
        "running": QCoreApplication.translate("Gui", "Running"),
        "exited": QCoreApplication.translate("Gui", "Exited"),
        "failed": QCoreApplication.translate("Gui", "Failed"),
        "stopped": QCoreApplication.translate("Gui", "Stopped"),
    }
    return labels.get(str(state), str(state))


class MainWindow(QMainWindow):
    def __init__(
        self,
        client: ManagerClient | None,
        translations: TranslationManager,
        store: Store | None = None,
    ):
        super().__init__()
        self.client = client
        self.store = store or Store()
        self.translations = translations
        self.setWindowTitle(self.tr("deskcage - sandbox manager"))
        self.resize(1000, 720)
        self._threads: set[Worker] = set()
        self._editors: dict[str, ProfileEditor] = {}
        self._build_ui()
        self.pump = DBusPump(self)
        self.pump.start()
        self.pump.instanceChanged.connect(self._instance_changed)
        self.pump.processExited.connect(self._process_exited)
        self.refresh_all()

    def _instance_changed(self, _instance: str, _state: str) -> None:
        self.refresh_instances()

    def _process_exited(self, _instance: str, _pid: int, _status: int) -> None:
        self.refresh_instances()

    # --------------------------------------------------------------------- ui

    def _build_ui(self) -> None:
        toolbar = QToolBar(self.tr("Profiles"), self)
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        refresh = QAction(self.tr("Refresh"), toolbar)
        refresh.setShortcut(QKeySequence.StandardKey.Refresh)
        refresh.triggered.connect(self.refresh_all)
        toolbar.addAction(refresh)

        spacer = QWidget(toolbar)
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        toolbar.addWidget(spacer)

        language_label = QLabel(self.tr("Language"), toolbar)
        toolbar.addWidget(language_label)
        self.language_combo = QComboBox(toolbar)
        self.language_combo.addItem(self.tr("System language"), "")
        self.language_combo.addItem("English", "en")
        self.language_combo.addItem("简体中文", "zh_CN")
        index = self.language_combo.findData(self.translations.current_code())
        self.language_combo.blockSignals(True)
        self.language_combo.setCurrentIndex(index if index >= 0 else 0)
        self.language_combo.blockSignals(False)
        self.language_combo.currentIndexChanged.connect(self._language_changed)
        toolbar.addWidget(self.language_combo)

        splitter = QSplitter(Qt.Orientation.Vertical)

        self.profiles = QTreeWidget()
        self.profiles.setHeaderLabels(
            [
                self.tr("ID"),
                self.tr("Description"),
                self.tr("Bus"),
                self.tr("Home"),
                self.tr("Keep"),
            ]
        )
        self.profiles.setRootIsDecorated(False)
        self.profiles.setAlternatingRowColors(True)
        self.profiles.itemDoubleClicked.connect(lambda *_: self.edit_profile())
        self.profiles.itemSelectionChanged.connect(self._update_title)
        self.profiles.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.profiles.customContextMenuRequested.connect(self._profile_context_menu)
        splitter.addWidget(self._wrap(self.tr("Profiles"), self.profiles))

        self.instances = QTreeWidget()
        self.instances.setHeaderLabels(
            [
                self.tr("Instance"),
                self.tr("Profile"),
                self.tr("State"),
                self.tr("Keep"),
                self.tr("PID"),
                self.tr("Started"),
            ]
        )
        self.instances.setRootIsDecorated(False)
        self.instances.setAlternatingRowColors(True)
        self.instances.itemDoubleClicked.connect(lambda *_: self.show_logs())
        self.instances.itemSelectionChanged.connect(self._update_title)
        self.instances.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.instances.customContextMenuRequested.connect(self._instance_context_menu)
        splitter.addWidget(self._wrap(self.tr("Instances"), self.instances))
        splitter.setSizes([320, 400])

        self.setCentralWidget(splitter)
        self.statusBar().showMessage(self.tr("ready"))

    @staticmethod
    def _menu_action(menu: QMenu, text: str, slot) -> QAction:
        action = menu.addAction(text)
        action.triggered.connect(slot)
        return action

    def _profile_menu(self, has_item: bool = True) -> QMenu:
        menu = QMenu(self.profiles)
        self._menu_action(menu, self.tr("New profile"), self.new_profile)
        if not has_item:
            return menu
        menu.addSeparator()
        self._menu_action(menu, self.tr("Edit"), self.edit_profile)
        self._menu_action(menu, self.tr("Duplicate"), self.duplicate_profile)
        self._menu_action(menu, self.tr("Rename"), self.rename_profile)
        self._menu_action(menu, self.tr("Delete"), self.delete_profile)
        menu.addSeparator()
        self._menu_action(menu, self.tr("Start"), self.start_profile)
        self._menu_action(menu, self.tr("Show command"), self.show_command)
        return menu

    def _instance_menu(self) -> QMenu:
        menu = QMenu(self.instances)
        self._menu_action(menu, self.tr("Logs"), self.show_logs)
        self._menu_action(menu, self.tr("Inspect"), self.inspect_instance)
        self._menu_action(menu, self.tr("Run command"), self.exec_instance)
        menu.addSeparator()
        self._menu_action(menu, self.tr("Keep"), self.toggle_keep)
        self._menu_action(menu, self.tr("Stop"), self.stop_instance)
        return menu

    def _profile_context_menu(self, position) -> None:
        item = self.profiles.itemAt(position)
        if item is not None:
            self.profiles.setCurrentItem(item)
        menu = self._profile_menu(item is not None)
        menu.exec(self.profiles.viewport().mapToGlobal(position))

    def _instance_context_menu(self, position) -> None:
        item = self.instances.itemAt(position)
        if item is None:
            return
        self.instances.setCurrentItem(item)
        menu = self._instance_menu()
        menu.exec(self.instances.viewport().mapToGlobal(position))

    def retranslate_ui(self) -> None:
        """Rebuild the hand-built widgets after the translator changed."""

        profile = self.selected_profile()
        instance = self.selected_instance()
        for toolbar in list(self.findChildren(QToolBar)):
            self.removeToolBar(toolbar)
            toolbar.deleteLater()
        central = self.takeCentralWidget()
        if central is not None:
            central.deleteLater()
        self._build_ui()
        self.refresh_all()
        self._select(self.profiles, profile)
        self._select(self.instances, instance)
        self._update_title()

    def _language_changed(self, index: int) -> None:
        code = self.language_combo.itemData(index) or ""
        if code == self.translations.current_code():
            return
        self.translations.set_language(code)
        self.retranslate_ui()

    @staticmethod
    def _select(tree: QTreeWidget, value: str | None) -> None:
        if not value:
            return
        for index in range(tree.topLevelItemCount()):
            item = tree.topLevelItem(index)
            if item.data(0, Qt.ItemDataRole.UserRole) == value:
                tree.setCurrentItem(item)
                return

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt hook
        if self._threads:
            information(
                self,
                "deskcage",
                self.tr(
                    "An operation is still running. Wait for it to finish before "
                    "closing the window."
                ),
            )
            event.ignore()
            return
        self.pump.stop()
        super().closeEvent(event)

    @staticmethod
    def _wrap(title: str, widget: QWidget) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        label = QLabel(title)
        font = label.font()
        font.setBold(True)
        label.setFont(font)
        layout.addWidget(label)
        layout.addWidget(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        return box

    def _update_title(self) -> None:
        profile = self.selected_profile()
        instance = self.selected_instance()
        if not profile and not instance:
            self.setWindowTitle(self.tr("deskcage - sandbox manager"))
            return
        parts = ["deskcage"]
        if profile:
            parts.append(self.tr("profile {}").format(profile))
        if instance:
            parts.append(self.tr("instance {}").format(instance))
        self.setWindowTitle(" - ".join(parts))

    # ---------------------------------------------------------------- helpers

    def selected_profile(self) -> str | None:
        item = self.profiles.currentItem()
        return item.data(0, Qt.ItemDataRole.UserRole) if item else None

    def selected_instance(self) -> str | None:
        item = self.instances.currentItem()
        return item.data(0, Qt.ItemDataRole.UserRole) if item else None

    def _run(self, fn, on_ok=None, message: str = "") -> None:
        if message:
            self.statusBar().showMessage(message)
        worker = Worker(fn, self)
        self._threads.add(worker)

        def cleanup() -> None:
            self._threads.discard(worker)

        def ok(result) -> None:
            cleanup()
            self.statusBar().showMessage(self.tr("ready"))
            if on_ok is not None:
                on_ok(result)

        def failed(text: str) -> None:
            cleanup()
            self.statusBar().showMessage(self.tr("failed"))
            critical(self, "deskcage", text)

        worker.ok.connect(ok)
        worker.fail.connect(failed)
        worker.finished.connect(cleanup)
        worker.start()

    def _profile_or_warn(self) -> str | None:
        profile = self.selected_profile()
        if not profile:
            information(self, "deskcage", self.tr("Select a profile first."))
        return profile

    def _instance_or_warn(self) -> str | None:
        instance = self.selected_instance()
        if not instance:
            information(self, "deskcage", self.tr("Select an instance first."))
        return instance

    # --------------------------------------------------------------- refresh

    def _ensure_client(self, quiet: bool = False) -> ManagerClient | None:
        if self.client is None:
            try:
                self.client = ManagerClient.connect()
            except (ManagerError, ConfigError) as exc:
                if not quiet:
                    critical(
                        self,
                        "deskcage",
                        self.tr("Cannot reach dcaged:\n{}").format(str(exc)),
                    )
                self.statusBar().showMessage(
                    self.tr("daemon: {}").format(str(exc))
                )
                return None
        return self.client

    def refresh_all(self) -> None:
        self._ensure_client(quiet=True)
        self.refresh_profiles()
        self.refresh_instances()

    def refresh_profiles(self) -> None:
        selected = self.selected_profile()
        self.profiles.clear()
        count = 0
        for profile_id in self.store.list_ids():
            try:
                profile = self.store.load(profile_id)
            except ProfileError:
                continue
            count += 1
            item = QTreeWidgetItem(
                [
                    profile.id,
                    profile.description,
                    profile.bus,
                    profile.home,
                    self.tr("yes") if profile.keep_alive else self.tr("no"),
                ]
            )
            item.setData(0, Qt.ItemDataRole.UserRole, profile.id)
            item.setToolTip(0, profile.description)
            self.profiles.addTopLevelItem(item)
            if profile.id == selected:
                self.profiles.setCurrentItem(item)
        self.statusBar().showMessage(self.tr("%n profile(s)", "", count))

    def refresh_instances(self) -> None:
        if self.client is None:
            return
        selected = self.selected_instance()
        try:
            rows = self.client.list_instances()
        except ManagerError as exc:
            self.statusBar().showMessage(self.tr("daemon: {}").format(str(exc)))
            return
        rows = [row for row in rows if row[2] in ("new", "starting", "running")]
        self.instances.clear()
        for instance_id, profile_id, state, started, keep, pid in rows:
            item = QTreeWidgetItem(
                [
                    instance_id,
                    profile_id,
                    state_label(state),
                    self.tr("yes") if keep else self.tr("no"),
                    str(pid),
                    started,
                ]
            )
            item.setData(0, Qt.ItemDataRole.UserRole, instance_id)
            self.instances.addTopLevelItem(item)
            if instance_id == selected:
                self.instances.setCurrentItem(item)
        running = sum(1 for row in rows if row[2] in ("new", "starting", "running"))
        self.statusBar().showMessage(
            self.tr("%n running instance(s)", "", running)
        )

    # -------------------------------------------------------------- profiles

    def new_profile(self) -> None:
        dialog = QInputDialog(self)
        dialog.setWindowTitle(self.tr("New profile"))
        dialog.setLabelText(self.tr("Profile id (letters, digits, . _ -):"))
        dialog.setOkButtonText(self.tr("OK"))
        dialog.setCancelButtonText(self.tr("Cancel"))
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        profile_id = dialog.textValue().strip()
        if not profile_id:
            return
        self._open_editor(profile_id, True)

    def edit_profile(self) -> None:
        profile_id = self._profile_or_warn()
        if not profile_id:
            return
        self._open_editor(profile_id, False)

    def _open_editor(self, profile_id: str, create: bool) -> ProfileEditor | None:
        open_editor = self._editors.get(profile_id)
        if open_editor is not None:
            open_editor.show()
            open_editor.raise_()
            open_editor.activateWindow()
            return open_editor
        try:
            editor = ProfileEditor(self.store, profile_id, create, self)
        except (ProfileError, ConfigError) as exc:
            critical(self, self.tr("Cannot open profile"), str(exc))
            return None
        self._editors[profile_id] = editor
        editor.accepted.connect(lambda: self._profile_saved(editor))
        editor.finished.connect(lambda _result: self._editor_finished(editor))
        editor.show()
        editor.raise_()
        editor.activateWindow()
        return editor

    def _profile_saved(self, editor: ProfileEditor) -> None:
        saved_id = editor.saved_id
        if saved_id != editor.profile_id:
            self._editors.pop(editor.profile_id, None)
            self._editors[saved_id] = editor
        self.refresh_profiles()
        self.refresh_instances()

    def _editor_finished(self, editor: ProfileEditor) -> None:
        for profile_id, open_editor in list(self._editors.items()):
            if open_editor is editor:
                del self._editors[profile_id]

    def _close_editor(self, profile_id: str) -> None:
        editor = self._editors.pop(profile_id, None)
        if editor is not None:
            editor.discard()

    def duplicate_profile(self) -> None:
        profile_id = self._profile_or_warn()
        if not profile_id:
            return
        dialog = QInputDialog(self)
        dialog.setWindowTitle(self.tr("Duplicate profile"))
        dialog.setLabelText(self.tr("New profile id:"))
        dialog.setTextValue(f"{profile_id}-copy")
        dialog.setOkButtonText(self.tr("OK"))
        dialog.setCancelButtonText(self.tr("Cancel"))
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        new_id = dialog.textValue().strip()
        if not new_id:
            return
        self._run(
            lambda: self._duplicate_profile(profile_id, new_id),
            on_ok=lambda _result: self.refresh_profiles(),
            message=self.tr("duplicating profile..."),
        )

    def rename_profile(self) -> None:
        profile_id = self._profile_or_warn()
        if not profile_id:
            return
        dialog = QInputDialog(self)
        dialog.setWindowTitle(self.tr("Rename profile"))
        dialog.setLabelText(self.tr("New profile id:"))
        dialog.setTextValue(profile_id)
        dialog.setOkButtonText(self.tr("OK"))
        dialog.setCancelButtonText(self.tr("Cancel"))
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        new_id = dialog.textValue().strip()
        if not new_id or new_id == profile_id:
            return
        self._run(
            lambda: self._rename_profile(profile_id, new_id),
            on_ok=lambda renamed: self._profile_renamed(profile_id, renamed),
            message=self.tr("renaming profile..."),
        )

    def _rename_profile(self, profile_id: str, new_id: str) -> str:
        """Rename through the daemon so running instances follow the name."""

        if self.client is not None:
            try:
                return self.client.rename_profile(profile_id, new_id)
            except ManagerError:
                # The daemon is gone or predates RenameProfile; the files are
                # the source of truth, so do it locally then.
                pass
        return self.store.rename(profile_id, new_id)

    def _profile_renamed(self, old_id: str, new_id: str) -> None:
        editor = self._editors.pop(old_id, None)
        if editor is not None:
            editor.renamed(new_id)
            self._editors[new_id] = editor
        self.refresh_profiles()
        self.refresh_instances()
        self._select(self.profiles, new_id)

    def delete_profile(self) -> None:
        profile_id = self._profile_or_warn()
        if not profile_id:
            return
        if not confirm(
            self,
            self.tr("Delete profile"),
            self.tr(
                "Delete profile {}? Running instances are not stopped."
            ).format(repr(profile_id)),
            self.tr("Delete"),
        ):
            return
        self._run(
            lambda: self.store.delete(profile_id),
            on_ok=lambda _result: self._profile_deleted(profile_id),
            message=self.tr("deleting profile..."),
        )

    def _duplicate_profile(self, profile_id: str, new_id: str) -> str:
        profile = self.store.load(profile_id)
        profile.id = self.store.validate_id(new_id)
        if self.store.exists(profile.id):
            raise ProfileError(
                self.tr("profile already exists: {}").format(profile.id)
            )
        self.store.save(profile)
        return profile.id

    def _profile_deleted(self, profile_id: str) -> None:
        self._close_editor(profile_id)
        self.refresh_profiles()

    # ------------------------------------------------------------- instances

    def _profile_object(self, profile_id: str) -> Profile:
        return self.store.load(profile_id)

    def start_profile(self) -> None:
        profile_id = self._profile_or_warn()
        if not profile_id:
            return
        try:
            profile = self._profile_object(profile_id)
        except (ProfileError, ManagerError) as exc:
            critical(self, self.tr("Cannot load profile"), str(exc))
            return
        if sandbox.uses_tmpbind(profile):
            missing = sandbox.tmpbind_missing(profile)
            if missing and profile.tmpbind_strict:
                critical(
                    self,
                    self.tr("Tmpbind paths are missing"),
                    self.tr(
                        "Strict mode is on and these paths do not exist:\n{}"
                    ).format("\n".join(missing)),
                )
                return
            if not sandbox.overlay_supported():
                critical(
                    self,
                    self.tr("Tmpbind is not available"),
                    self.tr(
                        "overlayfs is not available to user namespaces on this "
                        "host. Load the overlay kernel module or turn Tmpbind off."
                    ),
                )
                return
            if missing and not confirm(
                self,
                self.tr("Tmpbind paths will be skipped"),
                self.tr(
                    "These Tmpbind paths do not exist and will be skipped:\n{}"
                ).format("\n".join(missing)),
                self.tr("Start"),
            ):
                return
        client = self._ensure_client()
        if client is None:
            return
        dialog = StartDialog(profile, self.tr("Start {}").format(profile_id))
        if not dialog.exec():
            return
        values = dialog.values()

        def launch():
            return client.launch(
                profile_id,
                values["instance"],
                values["keep"],
                values["argv"],
                {},
                values["cwd"],
            )

        self._run(
            launch,
            on_ok=self._started,
            message=self.tr("starting {}...").format(profile_id),
        )

    def _started(self, instance) -> None:
        self.refresh_instances()
        self.statusBar().showMessage(self.tr("started {}").format(instance))

    def stop_instance(self) -> None:
        instance = self._instance_or_warn()
        if not instance:
            return
        client = self._ensure_client()
        if client is None:
            return
        self._run(
            lambda: client.stop_instance(instance),
            on_ok=lambda _result: self.refresh_instances(),
            message=self.tr("stopping {}...").format(instance),
        )

    def toggle_keep(self) -> None:
        instance = self._instance_or_warn()
        if not instance:
            return
        client = self._ensure_client()
        if client is None:
            return
        try:
            info = client.get_instance(instance)
        except ManagerError as exc:
            critical(self, "deskcage", str(exc))
            return
        keep = not bool(info.get("keep"))
        self._run(
            lambda: client.set_keep(instance, keep),
            on_ok=lambda _result: self.refresh_instances(),
            message=self.tr("keep_alive={}...").format(keep),
        )

    def exec_instance(self) -> None:
        instance = self._instance_or_warn()
        if not instance:
            return
        client = self._ensure_client()
        if client is None:
            return
        dialog = ExecDialog(instance)
        if not dialog.exec():
            return
        values = dialog.values()
        if not values["argv"]:
            information(self, "deskcage", self.tr("Enter a command."))
            return

        def spawn():
            return client.spawn(instance, values["argv"], {}, values["cwd"])

        def done(pid) -> None:
            self.refresh_instances()
            if values["detach"]:
                self.statusBar().showMessage(
                    self.tr("{}: pid {}").format(instance, str(pid))
                )
            else:
                LogsDialog(self.pump, instance, self).exec()

        self._run(spawn, on_ok=done, message=self.tr("running command..."))

    def inspect_instance(self) -> None:
        instance = self._instance_or_warn()
        if not instance:
            return
        client = self._ensure_client()
        if client is None:
            return
        try:
            info = client.get_instance(instance)
        except ManagerError as exc:
            critical(self, "deskcage", str(exc))
            return
        info["processes"] = parse_processes(info)
        dialog = QDialog(self)
        dialog.setWindowTitle(self.tr("Inspect - {}").format(instance))
        dialog.resize(760, 560)
        text = QPlainTextEdit()
        text.setReadOnly(True)
        text.setPlainText(json.dumps(info, indent=2, sort_keys=True))
        layout = QVBoxLayout(dialog)
        layout.addWidget(text)
        dialog.exec()

    def show_logs(self) -> None:
        instance = self._instance_or_warn()
        if not instance:
            return
        client = self._ensure_client()
        if client is None:
            return
        LogsDialog(self.pump, instance, self).exec()

    def show_command(self) -> None:
        profile_id = self._profile_or_warn()
        if not profile_id:
            return
        try:
            profile = self._profile_object(profile_id)
        except (ProfileError, ManagerError) as exc:
            critical(self, self.tr("Cannot load profile"), str(exc))
            return
        CommandDialog(profile, self).exec()


# ------------------------------------------------------------------------ main


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    app = QApplication(argv[:1])
    app.setApplicationName("deskcage")
    app.setApplicationDisplayName("deskcage")
    app.setApplicationVersion(__version__)

    settings = QSettings("deskcage", "deskcage")
    translations = TranslationManager(app, settings)
    translations.install(translations.locale())

    try:
        client = ManagerClient.connect()
        error = ""
    except (ManagerError, ConfigError) as exc:
        client = None
        error = str(exc)

    window = MainWindow(client, translations, Store())
    window.show()
    if error:
        window.statusBar().showMessage(
            QCoreApplication.translate("Gui", "daemon: {}").format(error)
        )
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
