"""lib.sh notify(): the desktop popup is skipped when JARVIS_NO_DESKTOP_NOTIFY=1, and the alert is still logged."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parent.parent / "deploy" / "mint" / "bin" / "lib.sh"

pytestmark = pytest.mark.skipif(not shutil.which("bash"), reason="bash required")


def _run(tmp_path, monkeypatch, *, silenced: bool) -> tuple[list[str], str]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    calls = tmp_path / "calls"
    stub = bindir / "notify-send"
    stub.write_text(f'#!/bin/sh\necho "$@" >> "{calls}"\n')
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    home = tmp_path / "home"
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "JARVIS_HOME": str(home),
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/nonexistent",
    }
    env.pop("JARVIS_NO_DESKTOP_NOTIFY", None)
    if silenced:
        env["JARVIS_NO_DESKTOP_NOTIFY"] = "1"
    subprocess.run(
        ["bash", "-c", f'source "{LIB}"; notify "Jarvis ledger: seal failed" "the ledger refused the API key (HTTP 401)" critical'],
        env=env, capture_output=True, text=True, timeout=30, check=True,
    )
    shown = calls.read_text().splitlines() if calls.exists() else []
    alerts = next(home.rglob("alerts.log")).read_text()
    return shown, alerts


def test_the_popup_is_shown_by_default(tmp_path, monkeypatch):
    shown, alerts = _run(tmp_path, monkeypatch, silenced=False)
    assert len(shown) == 1 and "seal failed" in shown[0]
    assert "refused the API key" in alerts


def test_the_switch_silences_the_popup_but_keeps_the_alert_log(tmp_path, monkeypatch):
    shown, alerts = _run(tmp_path, monkeypatch, silenced=True)
    assert shown == []
    assert "refused the API key" in alerts
