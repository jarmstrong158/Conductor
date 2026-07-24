"""
conftest.py — pytest fixtures for Conductor tests.

app.py no longer calls create_app() at import time, so importing it is now a
side-effect-free operation: no scheduler, no threads, no network, no Redis.
Redis is gone from the product entirely.

pystray/PIL are still stubbed because they are optional GUI deps that may not
be installed in CI; tzlocal is stubbed to a fixed UTC zone so scheduler
assertions are deterministic regardless of the host timezone.
"""

import sys
import datetime
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# 1. Stub optional GUI deps that may not be installed in CI
# ---------------------------------------------------------------------------
sys.modules.setdefault("pystray", MagicMock())

# tzlocal must return a real tzinfo so APScheduler accepts it
_tzlocal_stub = MagicMock()
_tzlocal_stub.get_localzone.return_value = datetime.timezone.utc
sys.modules.setdefault("tzlocal", _tzlocal_stub)

# PIL sub-modules that pystray / build_icon.py reference
for _pil in ("PIL", "PIL.Image", "PIL.ImageDraw", "PIL.ImageFont"):
    sys.modules.setdefault(_pil, MagicMock())

# ---------------------------------------------------------------------------
# 2. Import app — safe now that create_app() is not called at import time
# ---------------------------------------------------------------------------
import app as _app_module          # noqa: E402
from app import app as _flask_app  # noqa: E402


# ---------------------------------------------------------------------------
# 4. Per-test isolated client fixture
# ---------------------------------------------------------------------------
@pytest.fixture
def client(tmp_path):
    """
    Flask test client backed by a fresh SQLite database in a temp directory.
    The APScheduler is replaced with a MagicMock so tests never actually
    schedule or fire jobs.
    """
    db_path       = tmp_path / "test_conductor.db"
    templates_dir = tmp_path / "templates" / "generated"
    templates_dir.mkdir(parents=True, exist_ok=True)

    # Snapshot module-level state we're about to mutate
    _orig_db        = _app_module.DB_PATH
    _orig_templates = _app_module.TEMPLATES_DIR
    _orig_scheduler = _app_module.scheduler

    # Point app at temp paths
    _app_module.DB_PATH       = db_path
    _app_module.TEMPLATES_DIR = templates_dir

    # Fresh schema every test
    _app_module.init_db()

    # Replace live scheduler with a no-op mock
    mock_sched = MagicMock()
    mock_sched.get_jobs.return_value = []
    _app_module.scheduler = mock_sched

    # Clear the in-memory log buffer so log tests start from a known state
    with _app_module.LOG_LOCK:
        _app_module.LOG_BUFFER.clear()

    _flask_app.config["TESTING"] = True

    with _flask_app.test_client() as test_client:
        yield test_client

    # Restore original module state after each test
    _app_module.DB_PATH       = _orig_db
    _app_module.TEMPLATES_DIR = _orig_templates
    _app_module.scheduler     = _orig_scheduler
