"""Test fixtures for sitewatch-data-client."""

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from sitewatch_data_client import SiteWatchClient


@pytest.fixture
def mock_client():
    """Create a SiteWatchClient with a mocked Firebird connection.

    ``client._conn.query`` is a MagicMock; set ``.return_value`` to a DataFrame built with
    ``make_df`` and inspect ``.call_args`` for the SQL and params sent.
    """
    with patch("sitewatch_data_client._config.SiteWatchConfig.load_config") as mock_config:
        mock_config.return_value = MagicMock(
            host="test-host",
            database="test.fdb",
            user="test",
            password="test",
            port=3050,
            fb_client=None,
        )
        client = SiteWatchClient()

    client._conn = MagicMock()
    client._conn.query.return_value = make_df({})
    return client


def make_df(data: dict) -> pd.DataFrame:
    """Helper to create DataFrames for mock returns."""
    return pd.DataFrame(data)
