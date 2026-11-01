"""Headless smoke test for the Qt front end."""

from __future__ import annotations

import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import harness

try:
    from PyQt6.QtCore import QSettings, Qt
    from PyQt6.QtWidgets import QApplication, QDialog, QTabWidget
except ImportError:  # pragma: no cover - optional dependency
    print("gui: skipped (PyQt6 not installed)")
    raise SystemExit(0)

from deskcage.client import ManagerClient
from deskcage.config import Store
from deskcage.gui import CommandDialog, LogsDialog, MainWindow, ProfileEditor
from deskcage.translation import TranslationManager


def set_language(app, settings, code: str) -> TranslationManager:
    translations = TranslationManager(app, settings)
    assert translations.set_language(code)
    return translations


def main() -> int:
    with harness.TestEnv() as env:
        env.cli("profile", "create", "g", "--bus", "none", "--command", "echo hi")
        app = QApplication.instance() or QApplication([])
        settings = QSettings(
            str(env.dir / "gui-settings.ini"), QSettings.Format.IniFormat
        )
        translations = set_language(app, settings, "en")
        client = ManagerClient.connect()
        store = Store()
        window = MainWindow(client, translations, store)
        try:
            assert window.profiles.topLevelItemCount() == 1, "expected only g"
            ids = [
                window.profiles.topLevelItem(i).text(0)
                for i in range(window.profiles.topLevelItemCount())
            ]
            assert ids == ["g"], ids

            editor = ProfileEditor(store, "g", create=False)
            try:
                assert editor.id_edit.text() == "g"
                assert editor.bus_combo.currentData() == "none"
                tabs = editor.findChild(QTabWidget)
                assert [
                    tabs.tabText(i) for i in range(tabs.count())
                ] == ["General", "Files", "Devices", "Environment"]
                assert editor.home_combo.findData("tmpbind") >= 0
                assert editor.dri_check.toolTip()
                assert "screen" in editor.wayland_check.toolTip()
                assert editor.nvidia_check.toolTip()
                assert editor.a11y_check.toolTip()

                # No option hovered: the preview stays empty.
                editor._refresh_preview()
                assert editor.preview.toPlainText() == ""
                assert "option" in editor.preview.placeholderText().lower()

                existing = env.dir / "overlay-source"
                existing.mkdir()
                editor.tmpbind_edit.set_paths([str(existing)])
                editor._show_option_preview("Tmpbind")
                preview = editor.preview.toPlainText()
                assert "--overlay-src" in preview and "[Tmpbind]" in preview, preview
                assert "[Wayland]" not in preview, preview
                assert editor._collect_profile().tmpbind == [str(existing)]

                # Hovering an unchecked device shows what enabling it would add,
                # and only that option.
                editor.wayland_check.setChecked(False)
                editor._show_option_preview("Wayland")
                preview = editor.preview.toPlainText()
                assert "[Wayland]" in preview, preview
                assert "--ro-bind-try" in preview, preview
                assert "wayland" in preview, preview
                assert "--unshare-user" not in preview, preview
                assert "[Tmpbind]" not in preview, preview

                # A real Enter event on a widget is what drives the preview.
                from PyQt6.QtCore import QEvent

                editor._show_option_preview("Tmpbind")
                QApplication.sendEvent(
                    editor.wayland_check, QEvent(QEvent.Type.Enter)
                )
                assert "[Wayland]" in editor.preview.toPlainText()

                editor._show_option_preview("Accessibility")
                assert "at-spi" in editor.preview.toPlainText()
                editor.a11y_check.setChecked(True)
                editor.nvidia_check.setChecked(True)
                collected = editor._collect_profile()
                assert collected.a11y and collected.nvidia

                editor.tmpbind_edit.set_paths(["/no/such/tmpbind/path"])
                editor._update_warnings()
                assert not editor.files_warning.isHidden()
                assert "skipped" in editor.files_warning.text()

                dialog = CommandDialog(editor._collect_profile(), editor)
                try:
                    text = dialog.text.toPlainText()
                    assert text.startswith("bwrap \\\n"), text[:60]
                    assert "\n  --unshare-user" in text, text[:200]
                finally:
                    dialog.reject()
            finally:
                editor.discard()

            env_editor = ProfileEditor(store, "g", create=False)
            try:
                assert env_editor.env_table.columnCount() == 3
                assert [
                    env_editor.env_table.horizontalHeaderItem(i).text()
                    for i in range(3)
                ] == ["Variable", "Source", "Value"]

                # Checked presets are stored by name, not expanded.
                assert env_editor._env_rows() == {}
                collected = env_editor._collect_profile()
                assert collected.env_presets == ["xdg", "session"]
                assert collected.env == {}
                text = collected.to_toml()
                assert "[env-preset]\nxdg = true\nsession = true\n" in text, text
                assert "LANG = true" not in text, text

                states = {
                    env_editor.env_presets.item(i).text(): env_editor.env_presets.item(
                        i
                    ).checkState()
                    for i in range(env_editor.env_presets.count())
                }
                assert states["XDG directories"] == Qt.CheckState.Checked
                assert states["System and session"] == Qt.CheckState.Checked
                assert states["Qt/GTK theme and scaling"] == Qt.CheckState.Unchecked
                assert states["Proxy"] == Qt.CheckState.Unchecked

                # Hovering a preset previews it in the shared box, which
                # grows to fit the content.
                env_editor._tabs.setCurrentIndex(3)
                env_editor.env_presets.itemEntered.emit(env_editor.env_presets.item(1))
                app.processEvents()
                detail = env_editor.preview.toPlainText()
                assert "[System and session]" in detail, detail
                assert "LANG" in detail and "TERM" in detail, detail

                # The environment page keeps one fixed height.
                assert env_editor.preview.height() == 350

                # Other pages use the compact preview again.
                env_editor._tabs.setCurrentIndex(0)
                app.processEvents()
                assert env_editor.preview.toPlainText() == ""
                assert env_editor.preview.height() == 160
                env_editor._tabs.setCurrentIndex(3)
                app.processEvents()
                assert env_editor.preview.height() == 350

                # "Customize" snapshots a preset into editable rows without
                # touching the other presets.
                env_editor.env_presets.setCurrentRow(2)
                env_editor._customize_preset()
                rows = env_editor._env_rows()
                assert rows["QT_QPA_PLATFORMTHEME"] == "custom", rows
                assert "LANG" not in rows, rows

                # Custom entries are stored next to the presets.
                env_editor._add_env_row("LANG", "C")
                collected = env_editor._collect_profile()
                assert collected.env["LANG"] == "C"
                assert "TERM" not in collected.env
                assert collected.env_presets == ["xdg", "session"]

                # Unchecking a preset only drops the group name.
                env_editor.env_presets.item(1).setCheckState(Qt.CheckState.Unchecked)
                collected = env_editor._collect_profile()
                assert collected.env_presets == ["xdg"]
                assert collected.env["LANG"] == "C"
                assert (
                    env_editor.env_presets.item(0).checkState()
                    == Qt.CheckState.Checked
                )

                # Checking it again restores it.
                env_editor.env_presets.item(1).setCheckState(Qt.CheckState.Checked)
                collected = env_editor._collect_profile()
                assert collected.env_presets == ["xdg", "session"]
                assert collected.env["LANG"] == "C"
            finally:
                env_editor.discard()

            check = ProfileEditor(store, "g", create=False)
            assert not check._modified()
            check.description_edit.setText("changed")
            assert check._modified()
            check.description_edit.setText("")
            assert not check._modified()
            check.discard()  # discard never prompts
            assert check.result() == QDialog.DialogCode.Rejected

            instance = client.launch("g", "", False, ["sleep", "5"], {}, "")

            def wait_rows(wanted: int) -> bool:
                """The list follows InstanceChanged; no timer, no polling."""

                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    app.processEvents()
                    if window.instances.topLevelItemCount() == wanted:
                        return True
                    time.sleep(0.02)
                return False

            assert wait_rows(1), "the instance list did not follow InstanceChanged"
            row = window.instances.topLevelItem(0)
            assert row is not None and row.text(0) == instance, row

            # The log dialog follows the per-instance log directory.
            dialog = LogsDialog(window.pump, instance)
            try:
                app.processEvents()
                log_dir = env.runtime_dir / "dcage" / "logs" / instance
                deadline = time.monotonic() + 10
                seen = False
                index = 0
                while time.monotonic() < deadline and not seen:
                    index += 1
                    (log_dir / "1-1.log").write_text(
                        f"live-line-{index}\n", encoding="utf-8"
                    )
                    app.processEvents()
                    seen = f"live-line-{index}" in dialog.text.toPlainText()
                    time.sleep(0.05)
                assert seen, "log monitor did not fire"
            finally:
                dialog.close()
            # Closing the dialog drops its file monitor in the pump thread.
            assert harness.wait_until(lambda: not window.pump._monitors, 5), (
                window.pump._monitors
            )

            client.stop_instance(instance)
            assert wait_rows(0), "the stopped instance stayed in the list"

            profile_menu = window._profile_menu(True)
            try:
                texts = [
                    action.text()
                    for action in profile_menu.actions()
                    if not action.isSeparator()
                ]
                assert texts == [
                    "New profile",
                    "Edit",
                    "Duplicate",
                    "Rename",
                    "Delete",
                    "Start",
                    "Show command",
                ], texts
            finally:
                profile_menu.deleteLater()

            empty_menu = window._profile_menu(False)
            try:
                assert [
                    action.text()
                    for action in empty_menu.actions()
                    if not action.isSeparator()
                ] == ["New profile"]
            finally:
                empty_menu.deleteLater()

            instance_menu = window._instance_menu()
            try:
                texts = [
                    action.text()
                    for action in instance_menu.actions()
                    if not action.isSeparator()
                ]
                assert texts == [
                    "Logs",
                    "Inspect",
                    "Run command",
                    "Keep",
                    "Stop",
                ], texts
            finally:
                instance_menu.deleteLater()

            # The editor is a non-modal window: the main window stays usable,
            # opening the same profile twice reuses the window, and saving
            # refreshes the list.
            editor = window._open_editor("g", False)
            assert editor is not None
            assert not editor.isModal()
            assert window.isEnabled()
            assert window._open_editor("g", False) is editor
            assert len(window._editors) == 1
            editor.description_edit.setText("edited")
            editor.save()
            assert window.profiles.topLevelItem(0).text(1) == "edited"
            assert "g" not in window._editors

            editor = window._open_editor("g", False)
            assert editor is not None
            window._close_editor("g")
            assert "g" not in window._editors

            # Closing with the window button must emit finished and forget the
            # editor.  A stale entry there makes the next double-click call
            # show() on a deleted C++ object, which aborts the process.
            editor = window._open_editor("g", False)
            assert editor is not None
            editor._confirm_close = lambda: "cancel"
            editor.close()
            app.processEvents()
            assert "g" in window._editors and editor.isVisible()
            editor._confirm_close = lambda: "close"
            editor.close()
            app.processEvents()
            assert "g" not in window._editors
            reopened = window._open_editor("g", False)
            assert reopened is not None and reopened is not editor
            reopened.close()
            app.processEvents()
            assert "g" not in window._editors

            # Renaming rekeys an open editor, keeps its unsaved edits, and
            # leaves the renamed profile selected in the list.
            env.cli("profile", "create", "ren", "--bus", "none")
            window.refresh_profiles()
            window._select(window.profiles, "ren")
            editor = window._open_editor("ren", False)
            assert editor is not None
            editor.description_edit.setText("pending edit")
            assert window._rename_profile("ren", "ren2") == "ren2"
            window._profile_renamed("ren", "ren2")
            assert "ren2" in window._editors and "ren" not in window._editors
            assert editor.id_edit.text() == "ren2"
            assert editor.windowTitle() == "Profile: ren2"
            assert editor._modified(), "an unsaved edit survived the rename"
            assert store.load("ren2").description == ""
            assert window.selected_profile() == "ren2"
            assert window.profiles.topLevelItemCount() == 2
            window._close_editor("ren2")
            env.cli("profile", "delete", "ren2")
            window.refresh_profiles()
            assert window.profiles.topLevelItemCount() == 1

            # Switching language installs the .qm and rebuilds the widgets.
            assert translations.set_language("zh_CN")
            window.retranslate_ui()
            assert window.profiles.headerItem().text(1) == "描述"
            assert window.language_combo.currentData() == "zh_CN"
            assert window.windowTitle() == "deskcage - 沙盒管理器"
            editor = ProfileEditor(store, "g", create=False)
            try:
                assert editor.windowTitle() == "配置：g"
                assert editor.bus_combo.itemText(0) == "none - 不使用会话总线"
            finally:
                editor.discard()
            assert translations.set_language("en")
            window.retranslate_ui()
            assert window.profiles.headerItem().text(1) == "Description"
        finally:
            window.close()
            # closeEvent stops the pump and joins its thread.
            thread = window.pump._thread
            assert thread is not None and not thread.is_alive()

        # Profile editing reads and writes the files directly.
        env.cli("daemon", "--stop")
        editor = ProfileEditor(store, "g", create=False)
        editor.description_edit.setText("offline")
        assert editor.save()
        editor.discard()
        assert store.load("g").description == "offline"

        # The window also opens without a reachable daemon.
        saved_bus = os.environ.get("DBUS_SESSION_BUS_ADDRESS")
        os.environ["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=/nonexistent"
        try:
            offline = MainWindow(None, translations, store)
            try:
                assert offline.profiles.topLevelItemCount() == 1
                assert offline.instances.topLevelItemCount() == 0
                assert offline.profiles.topLevelItem(0).text(1) == "offline"
                # Without a daemon the rename still works: only files exist.
                assert offline._rename_profile("g", "g2") == "g2"
                assert store.load("g2").description == "offline"
                assert offline._rename_profile("g2", "g") == "g"
            finally:
                offline.close()
        finally:
            if saved_bus is None:
                os.environ.pop("DBUS_SESSION_BUS_ADDRESS", None)
            else:
                os.environ["DBUS_SESSION_BUS_ADDRESS"] = saved_bus
    print("gui: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
