"""Live tests against the real SiteWatch database.

Skipped unless ``SITEWATCH_LIVE=1``. Credentials come from the repo-root ``.env`` (loaded
with python-dotenv) or the environment. This is live production data shared by about 60
sites, so live tests must stay bounded: one site for up to 7 days, or the ICON sites for
at most one 14-day pay period, and ``FIRST n`` on raw queries. Run them one at a time and
assert aggregates only; never print or snapshot customer or employee values.

    SITEWATCH_LIVE=1 python -m pytest tests/live -v
"""

import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _live_enabled() -> bool:
    return os.environ.get("SITEWATCH_LIVE") == "1"


def pytest_collection_modifyitems(config, items):
    live_dir = Path(__file__).resolve().parent
    skip = pytest.mark.skip(reason="live SiteWatch tests: set SITEWATCH_LIVE=1 to run")
    for item in items:
        if live_dir in Path(str(item.fspath)).resolve().parents:
            item.add_marker(pytest.mark.integration)
            if not _live_enabled():
                item.add_marker(skip)


@pytest.fixture(scope="session")
def live_client():
    """A connected SiteWatchClient using ``.env`` / ``SITEWATCH_*`` credentials."""
    if not _live_enabled():
        pytest.skip("set SITEWATCH_LIVE=1 to run live tests")
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    from sitewatch_data_client import SiteWatchClient

    client = SiteWatchClient()
    client.connect()
    try:
        yield client
    finally:
        client.close()
