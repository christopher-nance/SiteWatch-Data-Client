"""SiteWatch (Firebird) configuration management."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from sitewatch_data_client._exceptions import ConfigurationError

DEFAULT_PORT = 3050


@dataclass
class SiteWatchConfig:
    """Configuration for the SiteWatch Firebird connection.

    Attributes:
        host: Query server host name or IP address.
        database: Server-side path of the ``.FDB`` file on the query server.
        user: Firebird user (normally the read-only ``REPORTS`` user).
        password: Firebird password. Excluded from ``repr()`` so it never leaks into logs.
        port: Firebird port. Defaults to 3050.
        fb_client: Absolute path to the Firebird 2.5 ``fbclient.dll``. None uses fdb's
            default library lookup.
        charset: Connection charset. SiteWatch stores text as ``NONE`` (Windows-1252 bytes).
        sql_dialect: SQL dialect. SiteWatch is dialect 1 (``DATE`` is a timestamp).
    """

    host: str
    database: str
    user: str
    password: str = field(repr=False)
    port: int = DEFAULT_PORT
    fb_client: str | None = None
    charset: str = "NONE"
    sql_dialect: int = 1

    @property
    def dsn(self) -> str:
        """Firebird DSN in ``host/port:database`` form."""
        return f"{self.host}/{self.port}:{self.database}"

    @classmethod
    def load_config(cls) -> SiteWatchConfig:
        """Load configuration from environment variables.

        Required env vars: SITEWATCH_HOST, SITEWATCH_DATABASE, SITEWATCH_USER,
        SITEWATCH_PASSWORD. Optional: SITEWATCH_PORT (default 3050), SITEWATCH_FB_CLIENT.

        The library does not read ``.env`` itself; call ``dotenv.load_dotenv()`` first if
        you keep credentials in a ``.env`` file.

        Returns:
            A populated ``SiteWatchConfig``.

        Raises:
            ConfigurationError: If a required variable is missing or SITEWATCH_PORT is not
                an integer. The message names the variables but never their values.
        """
        required_vars = [
            ("SITEWATCH_HOST", "host"),
            ("SITEWATCH_DATABASE", "database"),
            ("SITEWATCH_USER", "user"),
            ("SITEWATCH_PASSWORD", "password"),
        ]

        missing: list[str] = []
        values: dict[str, object] = {}

        for env_var, field_name in required_vars:
            value = os.environ.get(env_var)
            if not value or not value.strip():
                missing.append(env_var)
            else:
                values[field_name] = value.strip()

        if missing:
            raise ConfigurationError(
                f"Missing required environment variables: {', '.join(missing)}. "
                "Set these variables or check your .env file."
            )

        port = os.environ.get("SITEWATCH_PORT")
        if port and port.strip():
            try:
                values["port"] = int(port.strip())
            except ValueError:
                raise ConfigurationError(
                    "SITEWATCH_PORT must be an integer (for example 3050)."
                ) from None

        fb_client = os.environ.get("SITEWATCH_FB_CLIENT")
        if fb_client and fb_client.strip():
            values["fb_client"] = fb_client.strip()

        return cls(**values)  # type: ignore[arg-type]
