"""
tests/unit/conftest.py: shared test settings for the unit tests.

What this file does:
    Any test that runs the whole graph reaches the report node, which saves a PDF. This
    sends those PDFs to a temporary folder that pytest deletes afterwards, so running
    the tests never fills data/reports with test reports.

    It also gives the Source Health Monitor an in-memory database for every test, so
    tests that make calls fail never mark a real source down in data/ (step 14).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def reports_to_temp_folder(tmp_path, monkeypatch):
    from invest_agent.reporting import step

    monkeypatch.setattr(step, "get_settings", lambda: SimpleNamespace(reports_dir=str(tmp_path / "reports")))


@pytest.fixture(autouse=True)
def health_monitor_in_memory():
    from invest_agent.observability import health_monitor
    from invest_agent.storage.db import connect

    health_monitor.set_connection(connect(":memory:"))
    yield
    health_monitor.set_connection(None)
