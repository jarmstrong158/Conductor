"""
test_robustness.py — durability, atomicity and observability regressions.

Covers:
  * SQLite WAL + busy_timeout (three separate thread pools hit this DB)
  * POST /api/workers atomicity (a bad schedule must not commit an orphan row)
  * rotating file logging + APScheduler logger attachment
  * notification failures being logged rather than swallowed
"""

import logging
import sqlite3
import threading
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

import app as _app


# ---------------------------------------------------------------------------
# SQLite durability
# ---------------------------------------------------------------------------
class TestSqliteDurability:

    def test_wal_and_busy_timeout_are_set(self, client):
        conn = _app.get_db()
        try:
            assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 30000
            # 1 == NORMAL, the correct pairing with WAL
            assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1
        finally:
            conn.close()

    def test_pragmas_apply_to_every_connection(self, client):
        """busy_timeout and synchronous are per-connection, not persisted."""
        for _ in range(3):
            conn = _app.get_db()
            try:
                assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 30000
                assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1
            finally:
                conn.close()

    def test_concurrent_writers_do_not_raise_database_is_locked(self, client):
        """WAL + busy_timeout must let concurrent writers through."""
        errors = []

        def writer(n):
            try:
                for i in range(10):
                    with _app.get_db() as conn:
                        conn.execute(
                            "INSERT INTO workers (name, task_path, sched_type, sched_value) "
                            "VALUES (?,?,?,?)",
                            (f"w{n}_{i}", "/tmp/x.py", "interval", "1h"),
                        )
                        conn.commit()
            except sqlite3.OperationalError as e:  # pragma: no cover
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"concurrent writes failed: {errors}"


# ---------------------------------------------------------------------------
# Atomic worker creation
# ---------------------------------------------------------------------------
class TestWorkerCreateAtomicity:

    def _count(self):
        with _app.get_db() as conn:
            return conn.execute("SELECT COUNT(*) c FROM workers").fetchone()["c"]

    @pytest.mark.parametrize("sched_type,sched_value", [
        ("fixed", "banana"),
        ("fixed", "25:99"),
        ("cron", "99 99 99 99 99"),
    ])
    def test_bad_schedule_does_not_500(self, client, tmp_path, sched_type, sched_value):
        """The endpoint must survive _make_triggers raising.

        test_scheduler.py proves _make_triggers raises, but never proved the
        endpoint survives it. It did not: the row was INSERTed and committed
        first, then register_worker_jobs was called unguarded, so a malformed
        schedule produced a committed row, an unhandled ValueError, a 500, and
        a permanently orphaned worker with no jobs.
        """
        task = tmp_path / "t.py"
        task.write_text("print(1)\n")
        before = self._count()

        r = client.post("/api/workers", json={
            "name": "BadSchedule", "task_path": str(task),
            "sched_type": sched_type, "sched_value": sched_value,
        })

        assert r.status_code != 500, "endpoint 500'd on a malformed schedule"
        assert r.status_code == 400
        assert self._count() == before, "an orphaned worker row was committed"
        assert r.get_json()["errors"], "no error reported to the caller"

    def test_valid_worker_still_creates(self, client, tmp_path):
        task = tmp_path / "t.py"
        task.write_text("print(1)\n")
        before = self._count()
        r = client.post("/api/workers", json={
            "name": "GoodSchedule", "task_path": str(task),
            "sched_type": "interval", "sched_value": "1h",
        })
        assert r.status_code == 201
        assert self._count() == before + 1

    def test_batch_bad_one_does_not_block_good_one(self, client, tmp_path):
        task = tmp_path / "t.py"
        task.write_text("print(1)\n")
        r = client.post("/api/workers", json=[
            {"name": "Bad",  "task_path": str(task), "sched_type": "fixed",    "sched_value": "banana"},
            {"name": "Good", "task_path": str(task), "sched_type": "interval", "sched_value": "1h"},
        ])
        body = r.get_json()
        assert len(body["added"]) == 1
        assert len(body["errors"]) == 1

    def test_registration_failure_rolls_the_row_back(self, client, tmp_path, monkeypatch):
        """If register_worker_jobs blows up after the commit, undo the insert."""
        task = tmp_path / "t.py"
        task.write_text("print(1)\n")
        before = self._count()

        def boom(*a, **k):
            raise RuntimeError("scheduler exploded")

        monkeypatch.setattr(_app, "register_worker_jobs", boom)
        r = client.post("/api/workers", json={
            "name": "WillFail", "task_path": str(task),
            "sched_type": "interval", "sched_value": "1h",
        })
        assert r.status_code == 400
        assert self._count() == before, "row survived a failed registration"


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
class TestLogging:

    def test_setup_logging_attaches_apscheduler_logger(self):
        _app.setup_logging()
        ap = logging.getLogger("apscheduler")
        assert ap.handlers, "APScheduler logger still has no handler"

    def test_conductor_logger_has_handlers(self):
        _app.setup_logging()
        assert _app.logger.handlers

    def test_setup_logging_is_idempotent(self):
        _app.setup_logging()
        n = len(_app.logger.handlers)
        _app.setup_logging()
        assert len(_app.logger.handlers) == n, "handlers duplicated on second call"

    def test_rotating_file_handler_is_configured(self):
        _app.setup_logging()
        rotating = [h for h in _app.logger.handlers
                    if isinstance(h, logging.handlers.RotatingFileHandler)]
        # Only assert when a file handler could actually be created.
        if rotating:
            assert rotating[0].maxBytes > 0
            assert rotating[0].backupCount > 0


# ---------------------------------------------------------------------------
# Notifications are no longer silently swallowed
# ---------------------------------------------------------------------------
class TestNotificationsAreLogged:

    def test_worker_notification_failure_is_logged(self, client, tmp_path, monkeypatch, caplog):
        task = tmp_path / "t.py"
        task.write_text("print(1)\n")
        client.post("/api/workers", json={
            "name": "Notify", "task_path": str(task),
            "sched_type": "interval", "sched_value": "1h",
            "notify_email": "x@example.com", "notify_on": "always",
        })
        with _app.get_db() as conn:
            wid = conn.execute("SELECT id FROM workers WHERE name='Notify'").fetchone()["id"]

        monkeypatch.setattr(_app, "_send_email",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("smtp down")))
        monkeypatch.setattr(_app, "_run_script",
                            lambda *a, **k: MagicMock(returncode=0, stdout="", stderr=""))

        _app.logger.propagate = True
        try:
            with caplog.at_level(logging.ERROR, logger="conductor"):
                _app._task_runner(worker_id=wid, task_path=str(task),
                                  output_dir="", trigger_type="manual")
        finally:
            _app.logger.propagate = False

        assert any("notification failed" in r.message.lower() for r in caplog.records), (
            "notification failure was swallowed by `except Exception: pass`"
        )
