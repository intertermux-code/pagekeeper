"""Version plumbing: pyproject version, package __version__ and the CLI agree."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from pagekeeper import __version__ as package_version

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"


def _pyproject_version() -> str:
    text = PYPROJECT.read_text()
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert match, "version field not found in pyproject.toml"
    return match.group(1)


def test_pyproject_version_matches_package_init() -> None:
    assert _pyproject_version() == package_version


def test_cli_version_prints_the_version(capsys) -> None:
    from pagekeeper import cli

    with pytest.raises(SystemExit):
        cli.main(["--version"])

    out = capsys.readouterr().out
    assert package_version in out
    assert out.strip() == f"pagekeeper {package_version}"


def test_cli_version_subprocess() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "pagekeeper.cli", "--version"],
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0
    assert package_version in proc.stdout