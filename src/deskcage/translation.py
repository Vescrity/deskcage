"""Qt translation loading.

The GUI follows the usual Qt workflow: source strings are wrapped in
``self.tr`` / ``QCoreApplication.translate``, translators edit
``translations/deskcage_zh_CN.ts``, and ``lrelease6`` compiles it into
``deskcage_zh_CN.qm`` which is shipped with the package.  This module only owns
the ``QTranslator`` and the language remembered by the GUI.
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QLocale, QSettings, QTranslator

TRANSLATIONS_DIR = Path(__file__).resolve().parent / "translations"


class TranslationManager:
    """Install the ``.qm`` matching a locale and remember the choice."""

    def __init__(self, app, settings: QSettings):
        self._app = app
        self._settings = settings
        self._translator: QTranslator | None = None

    def current_code(self) -> str:
        """The saved language code; empty means "follow the system"."""

        return str(self._settings.value("language", "") or "")

    def locale(self) -> QLocale:
        code = self.current_code()
        return QLocale.system() if not code else QLocale(code)

    def install(self, locale: QLocale) -> bool:
        """Load the catalogue for *locale*, falling back to the source text."""

        if self._translator is not None:
            self._app.removeTranslator(self._translator)
            self._translator.deleteLater()
            self._translator = None
        if locale.language() == QLocale.Language.English:
            return True
        translator = QTranslator(self._app)
        if translator.load(locale, "deskcage", "_", str(TRANSLATIONS_DIR)):
            self._app.installTranslator(translator)
            self._translator = translator
            return True
        translator.deleteLater()
        return False

    def set_language(self, code: str) -> bool:
        """Save *code* (empty = system) and install its catalogue."""

        code = code or ""
        self._settings.setValue("language", code)
        return self.install(QLocale.system() if not code else QLocale(code))
