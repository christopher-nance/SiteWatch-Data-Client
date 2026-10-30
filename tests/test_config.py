"""Tests for SiteWatch configuration."""

import os
from unittest.mock import patch

import pytest

from sitewatch_data_client._config import SiteWatchConfig
from sitewatch_data_client._exceptions import ConfigurationError

BASE_ENV = {
    "SITEWATCH_HOST": "example-host",
    "SITEWATCH_DATABASE": r"E:\path\to\Example.FDB",
    "SITEWATCH_USER": "REPORTS",
    "SITEWATCH_PASSWORD": "not-a-real-password",
}


class TestSiteWatchConfig:
    def test_load_config_success(self):
        env = {**BASE_ENV, "SITEWATCH_PORT": "3051", "SITEWATCH_FB_CLIENT": r"C:\fb\fbclient.dll"}
        with patch.dict(os.environ, env, clear=True):
            config = SiteWatchConfig.load_config()
        assert config.host == "example-host"
        assert config.user == "REPORTS"
        assert config.port == 3051
        assert config.fb_client == r"C:\fb\fbclient.dll"
        assert config.charset == "NONE"
        assert config.sql_dialect == 1

    def test_defaults(self):
        with patch.dict(os.environ, BASE_ENV, clear=True):
            config = SiteWatchConfig.load_config()
        assert config.port == 3050
        assert config.fb_client is None

    def test_dsn(self):
        with patch.dict(os.environ, BASE_ENV, clear=True):
            config = SiteWatchConfig.load_config()
        assert config.dsn == r"example-host/3050:E:\path\to\Example.FDB"

    def test_missing_raises_and_names_vars(self):
        with patch.dict(os.environ, {"SITEWATCH_HOST": "h"}, clear=True):
            with pytest.raises(ConfigurationError, match="Missing required") as exc:
                SiteWatchConfig.load_config()
        msg = str(exc.value)
        assert "SITEWATCH_DATABASE" in msg
        assert "SITEWATCH_PASSWORD" in msg
        assert "SITEWATCH_HOST" not in msg

    def test_missing_message_never_contains_values(self):
        env = {k: v for k, v in BASE_ENV.items() if k != "SITEWATCH_USER"}
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(ConfigurationError) as exc:
                SiteWatchConfig.load_config()
        assert "not-a-real-password" not in str(exc.value)

    def test_bad_port_raises(self):
        with patch.dict(os.environ, {**BASE_ENV, "SITEWATCH_PORT": "abc"}, clear=True):
            with pytest.raises(ConfigurationError, match="SITEWATCH_PORT"):
                SiteWatchConfig.load_config()

    def test_password_not_in_repr(self):
        with patch.dict(os.environ, BASE_ENV, clear=True):
            config = SiteWatchConfig.load_config()
        assert "not-a-real-password" not in repr(config)
