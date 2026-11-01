"""Packaging: metadata, the wheel layout and the installed tree must work."""

from __future__ import annotations

import contextlib
import importlib
import io
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
import zipfile
from pathlib import Path
from xml.etree import ElementTree

import harness

ROOT = harness.ROOT


def build(target: Path, command: str) -> Path:
    from setuptools import build_meta

    project = target / f"project-{command}"
    if project.exists():
        shutil.rmtree(project)
    shutil.copytree(
        ROOT,
        project,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info"),
    )
    out = target / f"dist-{command}"
    out.mkdir(exist_ok=True)
    with contextlib.chdir(project), contextlib.redirect_stdout(io.StringIO()):
        return out / getattr(build_meta, command)(str(out))


def check_wheel(wheel: Path) -> None:
    from deskcage import __version__

    dist_info = f"deskcage-{__version__}.dist-info"
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        metadata = archive.read(f"{dist_info}/METADATA").decode("utf-8")
        entry_points = archive.read(f"{dist_info}/entry_points.txt").decode("utf-8")

    assert f"{dist_info}/METADATA" in names, names
    assert "deskcage/agent.py" in names, names
    assert "deskcage/logs.py" in names, names
    assert "deskcage/complete.py" in names, names
    assert "deskcage/data/dcage" in names, names
    assert "deskcage/data/_dcage" in names, names
    assert "deskcage/translations/deskcage_zh_CN.qm" in names, names
    assert f"{dist_info}/licenses/LICENSE" in names, names
    assert any(
        name.endswith("share/applications/org.deskcage.DeskCage.desktop")
        for name in names
    ), names
    assert any(
        name.endswith("share/metainfo/org.deskcage.DeskCage.metainfo.xml")
        for name in names
    ), names
    assert any(
        name.endswith("share/bash-completion/completions/dcage") for name in names
    ), names
    assert any(
        name.endswith("share/zsh/site-functions/_dcage") for name in names
    ), names

    assert f"Version: {__version__}\n" in metadata, metadata
    assert "License-Expression: GPL-3.0-or-later\n" in metadata, metadata
    assert "[gui_scripts]" in entry_points, entry_points
    assert "dcage-gui = deskcage.gui_entry:main" in entry_points, entry_points


def check_sdist(sdist: Path, version: str) -> None:
    prefix = f"deskcage-{version}/"
    with tarfile.open(sdist) as archive:
        names = set(archive.getnames())
    for path in (
        "LICENSE",
        "MANIFEST.in",
        "data/org.deskcage.DeskCage.desktop",
        "data/org.deskcage.DeskCage.metainfo.xml",
        "src/deskcage/complete.py",
        "src/deskcage/data/dcage",
        "src/deskcage/data/_dcage",
        "src/deskcage/translations/deskcage_zh_CN.ts",
        "src/deskcage/translations/normalize.py",
        "tests/test_completion.py",
        "tests/test_gui.py",
    ):
        assert prefix + path in names, path


def check_desktop_files() -> None:
    from deskcage import __version__

    data = ROOT / "data"
    desktop = data / "org.deskcage.DeskCage.desktop"
    metainfo = data / "org.deskcage.DeskCage.metainfo.xml"
    assert desktop.is_file() and metainfo.is_file()

    entries = {}
    for line in desktop.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("["):
            key, value = line.split("=", 1)
            entries[key] = value
    assert entries["Type"] == "Application"
    assert entries["Exec"] == "dcage-gui"
    assert entries["Icon"]
    assert entries["StartupWMClass"] == "deskcage"

    root = ElementTree.parse(metainfo).getroot()
    assert root.get("type") == "desktop-application"
    assert root.findtext("id") == desktop.stem
    launchable = root.find("launchable")
    assert launchable is not None
    assert launchable.get("type") == "desktop-id"
    assert launchable.text == desktop.name
    binaries = {node.text for node in root.findall("provides/binary")}
    assert binaries == {"dcage", "dcage-gui"}, binaries
    assert root.findtext("project_license") == "GPL-3.0-or-later"
    assert root.findtext("metadata_license") == "CC0-1.0"
    releases = root.findall("releases/release")
    assert releases and releases[0].get("version") == __version__, releases

    validator = shutil.which("desktop-file-validate")
    if validator:
        proc = subprocess.run(
            [validator, desktop.name], cwd=data, text=True, capture_output=True
        )
        assert proc.returncode == 0, proc.stderr or proc.stdout

    validator = shutil.which("appstreamcli")
    if validator:
        proc = subprocess.run(
            [validator, "validate", "--no-net", metainfo.name],
            cwd=data,
            text=True,
            capture_output=True,
        )
        errors = [line for line in proc.stdout.splitlines() if line.startswith("E:")]
        assert not errors, proc.stdout


def main() -> int:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = data["project"]
    assert project["readme"] == "README.md"
    assert project["dynamic"] == ["version"]
    assert project["license"] == "GPL-3.0-or-later"
    assert project["license-files"] == ["LICENSE"]
    assert "dcage-gui" not in project["scripts"]
    assert project["gui-scripts"]["dcage-gui"] == "deskcage.gui_entry:main"
    package_data = data["tool"]["setuptools"]["package-data"]["deskcage"]
    assert "translations/*.qm" in package_data, package_data
    assert "data/*" in package_data, package_data
    data_files = data["tool"]["setuptools"]["data-files"]
    assert data_files["share/bash-completion/completions"] == [
        "src/deskcage/data/dcage"
    ], data_files
    assert data_files["share/zsh/site-functions"] == ["src/deskcage/data/_dcage"], data_files

    # Console and GUI entry points stay importable without importing PyQt6.
    for target in {**project["scripts"], **project["gui-scripts"]}.values():
        module, attribute = target.split(":")
        assert callable(getattr(importlib.import_module(module), attribute))

    check_desktop_files()

    tmp = Path(tempfile.mkdtemp(prefix="deskcage-pack-"))
    site = tmp / "site"
    site.mkdir()
    shutil.copytree(ROOT / "src" / "deskcage", site / "deskcage")
    assert (site / "deskcage" / "agent.py").is_file()
    assert (site / "deskcage" / "translations" / "deskcage_zh_CN.qm").is_file()

    try:
        check_wheel(build(tmp, "build_wheel"))
        from deskcage import __version__

        check_sdist(build(tmp, "build_sdist"), __version__)

        with harness.TestEnv() as env:
            values = env.env()
            values["PYTHONPATH"] = str(site)

            def run(*args: str, check: bool = True):
                proc = subprocess.run(
                    [sys.executable, "-m", "deskcage", *args],
                    env=values,
                    text=True,
                    capture_output=True,
                    timeout=120,
                )
                if check and proc.returncode != 0:
                    raise AssertionError(
                        f"dcage {' '.join(args)} -> {proc.returncode}\n"
                        f"{proc.stdout}\n{proc.stderr}"
                    )
                return proc

            version = run("--version").stdout
            assert version.startswith("dcage "), version

            # Autostart must find dcaged through ``-m`` in this layout.
            run(
                "profile",
                "create",
                "p",
                "--bus",
                "isolated",
                "--command",
                "echo installed-layout-ok",
            )
            instance = run("start", "p", "--no-keep").stdout.strip()
            assert instance, instance
            deadline = time.monotonic() + 10
            output = ""
            while time.monotonic() < deadline:
                output = run("logs", instance).stdout
                if "installed-layout-ok" in output:
                    break
                time.sleep(0.1)
            assert "installed-layout-ok" in output, output

            out = run("ps").stdout
            assert "p-" in out or "no instances" in out, out

            # Completion scripts travel with the package.
            bash_script = run("completion", "bash").stdout
            assert "complete -o default -F _dcage dcage" in bash_script, bash_script
            zsh_script = run("completion", "zsh").stdout
            assert zsh_script.startswith("#compdef dcage"), zsh_script
            run("daemon", "--stop")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("packaging: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
