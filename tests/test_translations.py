"""Qt translation catalogue tests (.ts source, .qm runtime, QTranslator)."""

from __future__ import annotations

import ast
import os
import shutil
import sys
import tempfile
import xml.etree.ElementTree as ET

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import harness

from PyQt6.QtCore import QCoreApplication, QLocale, QSettings, QTranslator
from PyQt6.QtWidgets import QApplication

from deskcage.translation import TRANSLATIONS_DIR, TranslationManager

TS = TRANSLATIONS_DIR / "deskcage_zh_CN.ts"
QM = TRANSLATIONS_DIR / "deskcage_zh_CN.qm"
GUI = harness.ROOT / "src" / "deskcage" / "gui.py"


def check_catalogue_files() -> None:
    assert TS.is_file(), f"missing {TS}"
    assert QM.is_file(), f"missing {QM}"

    root = ET.parse(TS).getroot()
    unfinished: list[str] = []
    empty: list[str] = []
    for message in root.iter("message"):
        source = message.findtext("source") or ""
        translation = message.find("translation")
        assert translation is not None, source
        if translation.get("type") == "unfinished":
            unfinished.append(source)
        if message.get("numerus") == "yes":
            forms = [form.text or "" for form in translation.findall("numerusform")]
            if not forms or any(not form.strip() for form in forms):
                empty.append(source)
        elif not (translation.text or "").strip():
            empty.append(source)
    assert not unfinished, f"unfinished translations: {unfinished}"
    assert not empty, f"empty translations: {empty}"


def check_sources_are_in_catalogue() -> None:
    """Every literal passed to tr/translate must be extractable into the .ts."""

    root = ET.parse(TS).getroot()
    catalogued = {
        message.findtext("source")
        for message in root.iter("message")
        if message.findtext("source")
    }
    tree = ast.parse(GUI.read_text(encoding="utf-8"))
    used: set[str] = set()
    dynamic: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        function = node.func
        is_self_tr = (
            isinstance(function, ast.Attribute)
            and function.attr == "tr"
            and isinstance(function.value, ast.Name)
            and function.value.id == "self"
        )
        is_translate = (
            isinstance(function, ast.Attribute)
            and function.attr == "translate"
            and isinstance(function.value, ast.Name)
            and function.value.id == "QCoreApplication"
            and len(node.args) >= 2
        )
        argument = node.args[0]
        if is_translate:
            argument = node.args[1]
        if not (is_self_tr or is_translate):
            continue
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
            used.add(argument.value)
        else:
            dynamic.append(node.lineno)
    missing = used - catalogued
    assert not missing, f"strings not extracted into the .ts: {sorted(missing)}"
    assert not dynamic, f"non-literal translation calls on lines {dynamic}"


def check_runtime_translation() -> None:
    app = QApplication.instance() or QApplication([])
    translator = QTranslator()
    assert translator.load(QLocale("zh_CN"), "deskcage", "_", str(TRANSLATIONS_DIR))
    app.installTranslator(translator)
    try:
        assert QCoreApplication.translate("ProfileEditor", "Save") == "保存"
        assert (
            QCoreApplication.translate("MainWindow", "deskcage - sandbox manager")
            == "deskcage - 沙盒管理器"
        )
        assert (
            QCoreApplication.translate("ProfileEditor", "Profile: {}").format("g")
            == "配置：g"
        )
        assert (
            QCoreApplication.translate("MainWindow", "%n profile(s)", "", 2)
            == "共 2 个配置"
        )
        assert QCoreApplication.translate("Gui", "OK") == "确定"
    finally:
        app.removeTranslator(translator)
    assert QCoreApplication.translate("ProfileEditor", "Save") == "Save"

    tmp = tempfile.mkdtemp(prefix="deskcage-translation-")
    settings = QSettings(
        os.path.join(tmp, "gui.ini"), QSettings.Format.IniFormat
    )
    manager = TranslationManager(app, settings)
    try:
        assert manager.set_language("zh_CN")
        assert manager.current_code() == "zh_CN"
        assert QCoreApplication.translate("ProfileEditor", "Save") == "保存"
        assert manager.set_language("en")
        assert QCoreApplication.translate("ProfileEditor", "Save") == "Save"
    finally:
        settings.clear()
        shutil.rmtree(tmp, ignore_errors=True)


def check_catalogue_format() -> None:
    """The committed .ts is exactly what `make` produces before lrelease."""

    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "deskcage_ts_normalize", TRANSLATIONS_DIR / "normalize.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    text = TS.read_text(encoding="utf-8")
    assert module.normalize(text) == text, (
        "translations/deskcage_zh_CN.ts is not normalized; "
        "run `make` in src/deskcage/translations"
    )


def main() -> int:
    check_catalogue_files()
    check_catalogue_format()
    check_sources_are_in_catalogue()
    check_runtime_translation()
    print("translations: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
