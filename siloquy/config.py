"""Configuration loading and validation.

Secrets come from Docker secret files (``*_FILE``) or, as a fallback, plain
environment variables. They are held in a ``Secret`` wrapper whose repr/str
never reveal the value, so an accidental log line or traceback can't leak them.
"""

from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

ALL_STATS = (
    "status",
    "streams",
    "transcodes",
    "direct",
    "bandwidth",
    "users",
    "cpu",
    "memory",
    "storage",
)


class ConfigError(Exception):
    """Raised for invalid or unsafe configuration. Never contains secret values."""


class Secret:
    """Holds a credential without ever exposing it through repr/str/format."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret('***')"

    __str__ = __repr__

    def __format__(self, spec: str) -> str:
        return "***"

    def __reduce__(self):  # refuse pickling, which would serialize the value
        raise TypeError("Secret cannot be pickled")


def _read_secret(name: str, env: dict[str, str]) -> Secret:
    file_var = f"{name}_FILE"
    path = env.get(file_var, "").strip()
    if path:
        p = Path(path)
        try:
            st = p.stat()
        except OSError as exc:
            raise ConfigError(f"{file_var} points to an unreadable file") from exc
        if st.st_size > 4096:
            raise ConfigError(f"{file_var} is unexpectedly large")
        # Warn-level refusal: secret files readable by everyone are a leak.
        if st.st_mode & 0o004:
            raise ConfigError(f"{file_var} is world-readable; chmod it to 400 or 600")
        value = p.read_text(encoding="utf-8").strip()
    else:
        value = env.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} (or {file_var}) is required")
    if any(c.isspace() for c in value) or len(value) > 512:
        raise ConfigError(f"{name} has an invalid format")
    return Secret(value)


def validate_silo_url(raw: str) -> str:
    """Return a normalized base URL or raise ConfigError.

    Rules (to keep the API key from ever crossing an untrusted network in
    cleartext):
      * scheme must be http or https;
      * no credentials, query string or fragment in the URL;
      * plain http is only allowed to a Docker service name (no dots),
        ``localhost``, or a loopback/private IP literal.
    """
    raw = raw.strip()
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https"):
        raise ConfigError("SILO_URL must start with http:// or https://")
    if parts.username or parts.password:
        raise ConfigError("SILO_URL must not contain credentials")
    if parts.query or parts.fragment:
        raise ConfigError("SILO_URL must not contain a query string or fragment")
    host = parts.hostname
    if not host:
        raise ConfigError("SILO_URL has no host")
    if parts.scheme == "http" and not _is_internal_host(host):
        raise ConfigError(
            "SILO_URL uses plain http to a non-internal host; use https, or point it "
            "at the Silo container on the Docker network (e.g. http://silo:8080)"
        )
    try:
        port = parts.port
    except ValueError as exc:
        raise ConfigError("SILO_URL has an invalid port") from exc
    netloc = host if ":" not in host else f"[{host}]"
    if port:
        netloc = f"{netloc}:{port}"
    path = parts.path.rstrip("/")
    return f"{parts.scheme}://{netloc}{path}"


def _is_internal_host(host: str) -> bool:
    # host.docker.internal is Docker's name for the machine running the container.
    if host in ("localhost", "host.docker.internal"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        # A single-label name (no dots) is a Docker service/container name that
        # only resolves on the local Docker network.
        return re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,62}", host) is not None
    return ip.is_loopback or ip.is_private


def _int(env: dict[str, str], name: str, default: int, lo: int, hi: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        val = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if not lo <= val <= hi:
        raise ConfigError(f"{name} must be between {lo} and {hi}")
    return val


def _bool(env: dict[str, str], name: str, default: bool) -> bool:
    raw = env.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name} must be true or false")


def _name(env: dict[str, str], name: str, default: str) -> str:
    val = env.get(name, "").strip() or default
    if len(val) > 100:
        raise ConfigError(f"{name} must be 100 characters or fewer")
    return val


@dataclass(frozen=True)
class Config:
    discord_token: Secret = field(repr=False)
    silo_api_key: Secret = field(repr=False)
    guild_id: int
    silo_url: str
    poll_seconds: int
    users_poll_seconds: int
    stats: tuple[str, ...]
    stats_category: str
    performance_category: str
    data_dir: Path
    allow_unscoped_key: bool
    log_level: str
    resource_scope: str = "silo"

    @property
    def secrets(self) -> tuple[str, ...]:
        return (self.discord_token.reveal(), self.silo_api_key.reveal())


def load_config(env: dict[str, str] | None = None) -> Config:
    env = dict(os.environ if env is None else env)

    guild_raw = env.get("DISCORD_GUILD_ID", "").strip()
    if not guild_raw.isdigit():
        raise ConfigError("DISCORD_GUILD_ID is required (your Discord server's numeric ID)")
    # Real Discord IDs are 17-20 digit snowflakes; anything else (e.g. the
    # all-zero placeholder) would make the bot leave the server it is in.
    if not 17 <= len(guild_raw) <= 20 or int(guild_raw) < 10**16:
        raise ConfigError(
            "DISCORD_GUILD_ID doesn't look like a real Discord server ID; set it in .env"
        )

    stats_raw = env.get("STATS", "").strip()
    if stats_raw:
        stats = tuple(s.strip().lower() for s in stats_raw.split(",") if s.strip())
        unknown = [s for s in stats if s not in ALL_STATS]
        if unknown:
            raise ConfigError(f"Unknown STATS entries: {', '.join(unknown)}")
        if len(set(stats)) != len(stats):
            raise ConfigError("STATS has duplicate entries")
    else:
        stats = ALL_STATS

    scope = env.get("RESOURCE_SCOPE", "silo").strip().lower() or "silo"
    if scope not in ("silo", "host"):
        raise ConfigError("RESOURCE_SCOPE must be silo or host")

    level = env.get("LOG_LEVEL", "INFO").strip().upper()
    if level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        raise ConfigError("LOG_LEVEL must be DEBUG, INFO, WARNING or ERROR")

    silo_api_key = _read_secret("SILO_API_KEY", env)
    if not silo_api_key.reveal().startswith("sa_"):
        raise ConfigError(
            "SILO_API_KEY must be the Silo API key, which starts with 'sa_'. The file "
            "may contain the browser session token (eyJ...) or quote marks instead."
        )

    return Config(
        discord_token=_read_secret("DISCORD_TOKEN", env),
        silo_api_key=silo_api_key,
        guild_id=int(guild_raw),
        silo_url=validate_silo_url(env.get("SILO_URL", "http://silo:8080")),
        poll_seconds=_int(env, "POLL_SECONDS", 30, 15, 240),
        users_poll_seconds=_int(env, "USERS_POLL_SECONDS", 900, 60, 86400),
        stats=stats,
        stats_category=_name(env, "STATS_CATEGORY", "📡 Silo Stats"),
        performance_category=_name(env, "PERFORMANCE_CATEGORY", "📊 Performance"),
        data_dir=Path(env.get("DATA_DIR", "/data")),
        allow_unscoped_key=_bool(env, "ALLOW_UNSCOPED_KEY", False),
        log_level=level,
        resource_scope=scope,
    )
