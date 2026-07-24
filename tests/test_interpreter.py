"""
test_interpreter.py — regression tests for the frozen-exe false-success bug.

Background
----------
conductor.spec freezes launch.py as the PyInstaller entry point, so in the
shipped executable `sys.executable` is Conductor.exe, not a Python. app.py used
to build worker commands as `[sys.executable, task_path]`, which in the frozen
build meant:

    Conductor.exe user_script.py

launch.py:main() ignores sys.argv entirely. If Conductor was already listening
on port 5000 it printed a banner, opened a browser tab and called sys.exit(0).
The subprocess therefore returned 0, _task_runner treated that as success and
wrote success=1 to run_history — for a script that never ran. If the port was
free it instead booted a second full Conductor.

These tests pin the fixed behaviour:
  * a real interpreter is resolved when frozen
  * the Conductor binary is NEVER returned or executed
  * failure to find any interpreter is LOUD, never a silent success
"""

import os
import subprocess
import sys
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

import app as _app


# ---------------------------------------------------------------------------
# _looks_like_conductor
# ---------------------------------------------------------------------------
class TestLooksLikeConductor:

    @pytest.mark.parametrize("path", [
        r"C:\Program Files\Conductor\Conductor.exe",
        r"C:\Users\me\AppData\Local\Conductor\conductor.EXE",
        "/opt/conductor/Conductor",
        "conductor",
        "",
    ])
    def test_conductor_binaries_are_detected(self, path):
        assert _app._looks_like_conductor(path) is True

    @pytest.mark.parametrize("path", [
        r"C:\Python312\python.exe",
        "/usr/bin/python3",
        "python",
    ])
    def test_real_pythons_are_not_flagged(self, path):
        assert _app._looks_like_conductor(path) is False


# ---------------------------------------------------------------------------
# _assert_not_self — the guard that makes false success impossible
# ---------------------------------------------------------------------------
class TestAssertNotSelf:

    def test_conductor_command_raises(self):
        with pytest.raises(_app.InterpreterNotFound) as exc:
            _app._assert_not_self([r"C:\Program Files\Conductor\Conductor.exe", "script.py"])
        assert "not a Python interpreter" in str(exc.value)

    def test_real_python_command_passes(self):
        _app._assert_not_self([sys.executable, "script.py"])  # must not raise


# ---------------------------------------------------------------------------
# _resolve_python
# ---------------------------------------------------------------------------
class TestResolvePython:

    def test_unfrozen_returns_real_interpreter(self):
        exe = _app._resolve_python(force=True)
        assert exe
        assert not _app._looks_like_conductor(exe)

    def test_frozen_never_returns_the_conductor_binary(self, monkeypatch):
        """THE core regression: when frozen, sys.executable is Conductor.exe
        and must never be handed back as an interpreter."""
        monkeypatch.setattr(_app.sys, "frozen", True, raising=False)
        monkeypatch.setattr(_app.sys, "executable",
                            r"C:\Program Files\Conductor\Conductor.exe", raising=False)
        exe = _app._resolve_python(force=True)
        assert not _app._looks_like_conductor(exe), (
            f"_resolve_python returned the Conductor binary: {exe}"
        )
        assert Path(exe).exists()

    def test_conductor_exe_is_rejected_as_a_probe_candidate(self):
        assert _app._probe_python(r"C:\Program Files\Conductor\Conductor.exe") == ""

    def test_raises_loudly_when_no_interpreter_exists(self, monkeypatch):
        """If nothing can be found, FAIL — never silently continue."""
        monkeypatch.setattr(_app.sys, "frozen", True, raising=False)
        monkeypatch.setattr(_app.sys, "executable",
                            r"C:\Program Files\Conductor\Conductor.exe", raising=False)
        monkeypatch.setattr(_app.shutil, "which", lambda n: None)
        monkeypatch.setattr(_app, "_probe_python", lambda c: "")
        monkeypatch.setattr(_app, "_probe_python_from_registry", lambda: "")
        monkeypatch.delenv("CONDUCTOR_PYTHON", raising=False)

        with pytest.raises(_app.InterpreterNotFound) as exc:
            _app._resolve_python(force=True)
        assert "No Python interpreter could be found" in str(exc.value)

    def test_env_override_is_honoured(self, monkeypatch):
        monkeypatch.setenv("CONDUCTOR_PYTHON", sys.executable)
        assert _app._resolve_python(force=True) == sys.executable


# ---------------------------------------------------------------------------
# End-to-end: the exact scenario that silently reported success
# ---------------------------------------------------------------------------
class TestFrozenTaskRunnerDoesNotFakeSuccess:

    def _fake_conductor_exe(self, tmp_path):
        """A stand-in for Conductor.exe: ignores argv, prints, exits 0 —
        exactly what launch.py does when the port is already bound."""
        if sys.platform == "win32":
            p = tmp_path / "Conductor.bat"
            p.write_text("@echo off\r\necho Conductor is already running.\r\nexit /b 0\r\n")
        else:
            p = tmp_path / "Conductor"
            p.write_text("#!/bin/sh\necho 'Conductor is already running.'\nexit 0\n")
            p.chmod(0o755)
        return str(p)

    def test_worker_command_is_never_the_conductor_binary(self, tmp_path, monkeypatch):
        """_run_script must not build [Conductor.exe, script]."""
        script = tmp_path / "work.py"
        script.write_text("print('hi')\n")

        monkeypatch.setattr(_app.sys, "frozen", True, raising=False)
        monkeypatch.setattr(_app.sys, "executable",
                            self._fake_conductor_exe(tmp_path), raising=False)
        _app._RESOLVED_PYTHON = None
        # Resolve (and cache) before stubbing subprocess.run, since the probe
        # itself shells out.
        _app._resolve_python(force=True)

        captured = {}

        def fake_run(cmd, **kw):
            captured["cmd"] = cmd
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(_app.subprocess, "run", fake_run)
        _app._run_script(str(script))

        assert not _app._looks_like_conductor(str(captured["cmd"][0])), (
            f"worker command resolved to the Conductor binary: {captured['cmd']}"
        )

    def test_missing_interpreter_records_failure_not_success(self, tmp_path, monkeypatch):
        """The headline regression: no interpreter must yield success=False."""
        script = tmp_path / "work.py"
        script.write_text("print('hi')\n")

        monkeypatch.setattr(_app.sys, "frozen", True, raising=False)
        monkeypatch.setattr(_app.sys, "executable",
                            self._fake_conductor_exe(tmp_path), raising=False)
        monkeypatch.setattr(_app.shutil, "which", lambda n: None)
        monkeypatch.setattr(_app, "_probe_python", lambda c: "")
        monkeypatch.setattr(_app, "_probe_python_from_registry", lambda: "")
        monkeypatch.delenv("CONDUCTOR_PYTHON", raising=False)
        _app._RESOLVED_PYTHON = None

        recorded = {}
        monkeypatch.setattr(_app, "_record_run", lambda **kw: recorded.update(kw))

        _app._task_runner(worker_id=4242, task_path=str(script),
                          output_dir="", trigger_type="manual")

        assert recorded["success"] is False, (
            "Conductor reported SUCCESS for a script it never ran"
        )
        assert "No Python interpreter could be found" in recorded["error_msg"]

    def test_script_actually_runs_when_interpreter_is_found(self, tmp_path, monkeypatch):
        """Positive case: frozen build + a real python on the system means the
        user's script genuinely executes."""
        proof = tmp_path / "PROOF.txt"
        script = tmp_path / "work.py"
        script.write_text(f"open(r'{proof}', 'w').write('ran')\n")

        monkeypatch.setattr(_app.sys, "frozen", True, raising=False)
        monkeypatch.setattr(_app.sys, "executable",
                            self._fake_conductor_exe(tmp_path), raising=False)
        _app._RESOLVED_PYTHON = None

        recorded = {}
        monkeypatch.setattr(_app, "_record_run", lambda **kw: recorded.update(kw))

        _app._task_runner(worker_id=4243, task_path=str(script),
                          output_dir="", trigger_type="manual")

        assert proof.exists(), "the user's script did not actually run"
        assert recorded["success"] is True


# ---------------------------------------------------------------------------
# Unpinned auto-install gate
# ---------------------------------------------------------------------------
class TestScrapedPipInstallGate:

    def test_scraped_install_is_refused_by_default(self, monkeypatch):
        monkeypatch.setattr(_app, "AUTO_PIP_INSTALL", False)
        called = []
        monkeypatch.setattr(_app.subprocess, "run",
                            lambda *a, **k: called.append(a) or MagicMock(returncode=0))
        assert _app._pip_install(["totally-not-requests"], scraped=True) is False
        assert not called, "ran an unpinned pip install for a traceback-scraped name"

    def test_user_declared_requirements_still_install(self, monkeypatch):
        monkeypatch.setattr(_app, "AUTO_PIP_INSTALL", False)
        monkeypatch.setattr(_app.subprocess, "run",
                            lambda *a, **k: MagicMock(returncode=0, stderr=""))
        assert _app._pip_install(["requests"], scraped=False) is True
