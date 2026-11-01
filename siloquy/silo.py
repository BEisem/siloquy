"""Minimal, read-only client for the Silo admin API.

Security properties:
  * Only GET requests, only to a fixed allowlist of paths.
  * Redirects are never followed, so the bearer token can't be forwarded to
    another host.
  * Proxy environment variables are ignored (trust_env=False), so the token
    only ever goes to SILO_URL.
  * Response bodies are size-capped and must be JSON.
  * Only the handful of fields Siloquy needs are extracted; titles, IPs and
    device details are dropped immediately and never logged or posted.
    Sessions keep only the viewer's opaque account/profile IDs (for the
    optional watching role). Usernames and profile names are read only to
    answer an admin's /watching command, never stored, logged or posted.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import aiohttp

from . import __version__
from .config import Secret

log = logging.getLogger(__name__)

MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_PAGES = 25

# The only paths this client will ever request.
PATH_HEALTH = "/api/v1/health"
PATH_SESSIONS = "/api/v2/admin/sessions"
PATH_RESOURCES = "/api/v2/admin/system/resources"
PATH_USERS = "/api/v2/admin/users"
# Admin-only route that is *outside* the admin:monitor:read scope. Used once at
# startup to prove the key is scoped: a correctly scoped key gets 403 here.
PATH_SCOPE_PROBE = "/api/v2/admin/system/build"

_ALLOWED_PATHS = frozenset(
    {PATH_HEALTH, PATH_SESSIONS, PATH_RESOURCES, PATH_USERS, PATH_SCOPE_PROBE}
)
# One templated path: an account's household profiles (watching role only).
# The ID must be all digits, as Silo's own scope check requires, so nothing
# else can ever be spliced into the URL.
_PROFILES_RE = re.compile(r"/api/v2/admin/users/[0-9]{1,20}/profiles")
# Silo IDs are opaque strings; accept only short, plain ones.
_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _is_allowed(path: str) -> bool:
    return path in _ALLOWED_PATHS or _PROFILES_RE.fullmatch(path) is not None


def clean_id(v: Any) -> str | None:
    """A Silo ID as a short plain string, or None."""
    if isinstance(v, int) and not isinstance(v, bool):
        v = str(v)
    return v if isinstance(v, str) and _ID_RE.fullmatch(v) else None


class SiloError(Exception):
    """API failure. Messages contain status codes and paths only, never bodies."""


class SiloAuthError(SiloError):
    pass


@dataclass(frozen=True)
class SessionInfo:
    method: str  # direct | remux | direct_stream | transcode | unknown
    paused: bool
    remote: bool
    bitrate_kbps: int
    # Opaque IDs of who is watching; never names. None when Silo omits them.
    user_id: str | None = None
    profile_id: str | None = None


@dataclass(frozen=True)
class NamedID:
    """An account or profile, for /watching answers only (never stored)."""

    id: str
    name: str
    # Accounts only (profiles leave the defaults).
    enabled: bool = True
    last_active: float | None = None  # epoch seconds


@dataclass(frozen=True)
class Resources:
    cpu_pct: float | None
    mem_used_mb: int | None
    mem_total_mb: int | None
    storage_used_gb: float | None
    storage_total_gb: float | None
    # Silo's own usage (process + its ffmpeg children), from "attribution".
    cores: int | None = None
    sampled_at: float | None = None  # epoch seconds of Silo's sample
    silo_cpu_seconds: float | None = None  # cumulative
    silo_rss_bytes: int | None = None


class SiloClient:
    def __init__(self, base_url: str, api_key: Secret) -> None:
        self._base = base_url
        self._key = api_key
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "SiloClient":
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15, connect=5),
            trust_env=False,
            headers={"User-Agent": f"Siloquy/{__version__}", "Accept": "application/json"},
            connector=aiohttp.TCPConnector(limit=4),
            cookie_jar=aiohttp.DummyCookieJar(),
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._session:
            await self._session.close()

    async def _get(
        self, path: str, params: dict[str, str] | None = None, *, auth: bool = True
    ) -> tuple[int, Any]:
        if not _is_allowed(path):
            raise SiloError("refusing to request a non-allowlisted path")
        assert self._session is not None
        headers = {"Authorization": f"Bearer {self._key.reveal()}"} if auth else {}
        try:
            async with self._session.get(
                self._base + path,
                params=params,
                headers=headers,
                allow_redirects=False,
            ) as resp:
                status = resp.status
                if status != 200:
                    return status, None
                ctype = resp.headers.get("Content-Type", "")
                if "json" not in ctype:
                    raise SiloError(f"{path} returned non-JSON content")
                body = await resp.content.read(MAX_BODY_BYTES + 1)
                if len(body) > MAX_BODY_BYTES:
                    raise SiloError(f"{path} response exceeded size limit")
                try:
                    return status, json.loads(body)
                except ValueError as exc:
                    raise SiloError(f"{path} returned invalid JSON") from exc
        except aiohttp.ClientError as exc:
            # Only the exception class: aiohttp messages can include URLs/hosts.
            raise SiloError(f"{path} request failed ({type(exc).__name__})") from None
        except TimeoutError:
            raise SiloError(f"{path} request timed out") from None

    async def _get_ok(self, path: str, params: dict[str, str] | None = None) -> Any:
        status, data = await self._get(path, params)
        if status in (401, 403):
            raise SiloAuthError(
                f"{path} returned {status}: check the API key and that it has the "
                "admin:monitor:read scope and belongs to an admin account"
            )
        if status != 200 or data is None:
            raise SiloError(f"{path} returned HTTP {status}")
        return data

    async def healthy(self) -> bool:
        try:
            status, _ = await self._get(PATH_HEALTH, auth=False)
        except SiloError:
            return False
        return status == 200

    async def key_is_unscoped(self) -> bool:
        """True if the key can reach a route outside admin:monitor:read."""
        status, _ = await self._get(PATH_SCOPE_PROBE)
        if status == 200:
            return True
        if status == 403:
            return False
        if status == 401:
            raise SiloAuthError("API key was rejected (HTTP 401)")
        raise SiloError(f"scope check returned HTTP {status}")

    async def _paged(self, path: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(MAX_PAGES):
            params = {"limit": "200"}
            if cursor:
                params["cursor"] = cursor
            data = await self._get_ok(path, params)
            page_items = data.get("items") if isinstance(data, dict) else None
            if not isinstance(page_items, list):
                raise SiloError(f"{path} returned an unexpected shape")
            items.extend(i for i in page_items if isinstance(i, dict))
            page = data.get("page") or {}
            cursor = page.get("next_cursor") if isinstance(page, dict) else None
            if not cursor or not isinstance(cursor, str) or len(cursor) > 8192:
                break
        return items

    async def sessions(self) -> list[SessionInfo]:
        raw = await self._paged(PATH_SESSIONS)
        return [_session_from(item) for item in raw]

    async def user_count(self) -> int:
        return len(await self._paged(PATH_USERS))

    async def accounts(self) -> list[NamedID]:
        """Silo accounts (ID + username), for linking."""
        return _named(await self._paged(PATH_USERS), "username")

    async def profiles(self, user_id: str) -> list[NamedID]:
        """One account's household profiles (ID + name), for linking."""
        if not re.fullmatch(r"[0-9]{1,20}", user_id or ""):
            raise SiloError("invalid account ID")
        # Not paginated: Silo answers 422 to unknown query parameters.
        path = f"/api/v2/admin/users/{user_id}/profiles"
        data = await self._get_ok(path)
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise SiloError("profiles returned an unexpected shape")
        return _named([i for i in items if isinstance(i, dict)], "name")

    async def resources(self) -> Resources:
        data = await self._get_ok(PATH_RESOURCES)
        if not isinstance(data, dict) or not data.get("available"):
            return Resources(None, None, None, None, None)
        system = data.get("system") if isinstance(data.get("system"), dict) else {}
        used, total = _library_storage(system.get("disks"))
        attr = data.get("attribution") if isinstance(data.get("attribution"), dict) else {}
        proc = attr.get("process") if isinstance(attr.get("process"), dict) else {}
        kids = attr.get("children") if isinstance(attr.get("children"), dict) else {}
        proc_cpu, proc_rss = _num(proc.get("cpu_seconds")), _int(proc.get("resident_bytes"))
        silo_cpu = None if proc_cpu is None else proc_cpu + (_num(kids.get("cpu_seconds")) or 0.0)
        silo_rss = None if proc_rss is None else proc_rss + (_int(kids.get("resident_bytes")) or 0)
        return Resources(
            cpu_pct=_num(system.get("cpu_pct")),
            mem_used_mb=_int(system.get("mem_used_mb")),
            mem_total_mb=_int(system.get("mem_total_mb")),
            storage_used_gb=used,
            storage_total_gb=total,
            cores=_int(system.get("cores")),
            sampled_at=_instant(data.get("sampled_at")),
            silo_cpu_seconds=silo_cpu,
            silo_rss_bytes=silo_rss,
        )


def _instant(v: Any) -> float | None:
    if not isinstance(v, str) or len(v) > 40:
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _int(v: Any) -> int | None:
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _session_from(item: dict[str, Any]) -> SessionInfo:
    method = item.get("effective_play_method") or item.get("play_method") or "unknown"
    if method not in ("direct", "remux", "direct_stream", "transcode"):
        method = "unknown"
    bitrate = _int(item.get("stream_bitrate_kbps"))
    if bitrate is None:
        bitrate = _int(item.get("target_bitrate_kbps")) or 0
    return SessionInfo(
        method=method,
        paused=item.get("is_paused") is True,
        remote=item.get("stream_location") == "remote",
        bitrate_kbps=max(0, bitrate),
        user_id=clean_id(item.get("user_id")),
        profile_id=clean_id(item.get("profile_id")),
    )


def _named(items: list[dict[str, Any]], name_field: str) -> list[NamedID]:
    out: list[NamedID] = []
    for item in items:
        iid, name = clean_id(item.get("id")), item.get(name_field)
        if iid and isinstance(name, str) and name.strip():
            out.append(NamedID(
                iid, name.strip()[:100],
                enabled=item.get("enabled") is not False,
                last_active=_instant(item.get("last_active_at")),
            ))
    return out


def _library_storage(disks: Any) -> tuple[float | None, float | None]:
    """Sum library mounts, de-duplicating roots that share one filesystem."""
    if not isinstance(disks, list):
        return None, None
    seen: set[tuple[float, float]] = set()
    used_sum = total_sum = 0.0
    for d in disks:
        if not isinstance(d, dict) or d.get("unavailable") or d.get("scratch"):
            continue
        if not str(d.get("role", "")).startswith("library"):
            continue
        used, total = _num(d.get("used_gb")), _num(d.get("total_gb"))
        if used is None or not total:
            continue
        key = (round(used, 0), round(total, 0))
        if key in seen:
            continue
        seen.add(key)
        used_sum += used
        total_sum += total
    if not seen:
        return None, None
    return used_sum, total_sum
