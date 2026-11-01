"""Tests for Siloquy. Run: python -m unittest discover -s tests -v"""

from __future__ import annotations

import io
import json
import logging
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from aiohttp import web

from siloquy.__main__ import RedactingFilter
from siloquy.config import ConfigError, Secret, load_config, validate_silo_url
from siloquy.silo import Resources, SessionInfo, SiloAuthError, SiloClient, SiloError
from siloquy.stats import (
    STATUS_DEGRADED, STATUS_OFFLINE, STATUS_ONLINE, SiloCpuTracker, Snapshot, fmt_gb, render,
)

KEY = "sa_test_0123456789abcdef"
DISCORD = "MTAx.discord.token.value.for.tests"


# ---------------------------------------------------------------- config

class ConfigTests(unittest.TestCase):
    def base_env(self, **kw):
        env = {"DISCORD_TOKEN": DISCORD, "SILO_API_KEY": KEY, "DISCORD_GUILD_ID": "123456789012345678"}
        env.update(kw)
        return env

    def test_defaults(self):
        cfg = load_config(self.base_env())
        self.assertEqual(cfg.silo_url, "http://silo:8080")
        self.assertFalse(cfg.allow_unscoped_key)
        self.assertEqual(cfg.resource_scope, "silo")
        self.assertEqual(load_config(self.base_env(RESOURCE_SCOPE="HOST")).resource_scope, "host")
        self.assertEqual(len(cfg.stats), 9)

    def test_secrets_never_in_repr(self):
        cfg = load_config(self.base_env())
        for text in (repr(cfg), str(cfg), f"{cfg.silo_api_key}", repr(cfg.discord_token)):
            self.assertNotIn(KEY, text)
            self.assertNotIn(DISCORD, text)

    def test_secret_cannot_be_pickled(self):
        import pickle
        with self.assertRaises(TypeError):
            pickle.dumps(Secret("x"))

    def test_http_only_to_internal_hosts(self):
        for ok in ("http://silo:8080", "http://localhost:8080", "http://172.18.0.5:8080",
                   "http://127.0.0.1", "http://host.docker.internal:8090", "http://192.168.1.50:8090",
                   "https://silo.example.com", "https://silo.example.com/"):
            validate_silo_url(ok)
        for bad in ("http://silo.example.com", "http://host.docker.internal.evil.com", "http://8.8.8.8:8080", "ftp://silo",
                    "https://user:pw@silo.example.com", "https://silo.example.com/?x=1",
                    "https://silo.example.com/#f", "silo:8080", "http://"):
            with self.assertRaises(ConfigError, msg=bad):
                validate_silo_url(bad)

    def test_trailing_slash_stripped(self):
        self.assertEqual(validate_silo_url("https://s.example.com/silo/"), "https://s.example.com/silo")

    def test_secret_file_must_not_be_world_readable(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, "tok")
            p.write_text(DISCORD + "\n")
            os.chmod(p, 0o644)
            env = self.base_env(DISCORD_TOKEN="", DISCORD_TOKEN_FILE=str(p))
            with self.assertRaises(ConfigError):
                load_config(env)
            os.chmod(p, 0o400)
            self.assertEqual(load_config(env).discord_token.reveal(), DISCORD)

    def test_config_errors_do_not_echo_secrets(self):
        try:
            load_config(self.base_env(SILO_API_KEY="has space " + KEY))
        except ConfigError as exc:
            self.assertNotIn(KEY, str(exc))
        else:
            self.fail("expected ConfigError")

    def test_key_must_be_sa_key(self):
        for bad in ("eyJhbGciOiJIUzI1NiJ9.x.y", "'sa_quoted_key_value'", "Bearer_sa_x"):
            with self.assertRaises(ConfigError) as ctx:
                load_config(self.base_env(SILO_API_KEY=bad))
            self.assertIn("sa_", str(ctx.exception))
            self.assertNotIn(bad, str(ctx.exception))

    def test_bad_values(self):
        for kw in ({"DISCORD_GUILD_ID": "abc"}, {"DISCORD_GUILD_ID": "000000000000000000"},
                   {"DISCORD_GUILD_ID": "0"}, {"DISCORD_GUILD_ID": "123"}, {"STATS": "streams,bogus"},
                   {"STATS": "cpu,cpu"}, {"POLL_SECONDS": "5"}, {"ALLOW_UNSCOPED_KEY": "maybe"},
                   {"SILO_API_KEY": ""}, {"RESOURCE_SCOPE": "plex"}):
            with self.assertRaises(ConfigError, msg=kw):
                load_config(self.base_env(**kw))


# ---------------------------------------------------------------- logging

class RedactionTests(unittest.TestCase):
    def test_secrets_scrubbed_from_messages_and_tracebacks(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.addFilter(RedactingFilter((KEY, DISCORD)))
        lg = logging.getLogger("redaction-test")
        lg.addHandler(handler)
        lg.propagate = False
        lg.error("token=%s key=%s", DISCORD, KEY)
        try:
            raise ValueError(f"boom {KEY}")
        except ValueError:
            lg.exception("failure")
        out = stream.getvalue()
        self.assertNotIn(KEY, out)
        self.assertNotIn(DISCORD, out)
        self.assertIn("***", out)
        self.assertIn("ValueError", out)


# ---------------------------------------------------------------- stats

def sess(method="direct", paused=False, remote=True, kbps=8000):
    return SessionInfo(method=method, paused=paused, remote=remote, bitrate_kbps=kbps)


class RenderTests(unittest.TestCase):
    def test_full_render(self):
        snap = Snapshot(
            STATUS_ONLINE,
            [sess("transcode", kbps=12000), sess("direct", kbps=40000),
             sess("remux", paused=True, kbps=20000), sess("direct", remote=False, kbps=90000)],
            Resources(4.9, 16384, 65536, 98_200.0, 130_000.0),
            48,
        )
        n = render(snap, "host")
        self.assertEqual(n["status"], "🟢 Silo: Online")
        self.assertEqual(n["streams"], "▶️ Streams: 4 (1 ⏸)")
        self.assertEqual(n["transcodes"], "🔥 Transcodes: 1")
        self.assertEqual(n["direct"], "⚡ Direct Play: 3")
        # remote and not paused: 12000 + 40000 kbps
        self.assertEqual(n["bandwidth"], "🌐 Remote: 52 Mbps")
        self.assertEqual(n["users"], "👥 Users: 48")
        self.assertEqual(n["cpu"], "🧠 Host CPU: 5%")
        self.assertEqual(n["memory"], "🐏 Host RAM: 25% of 64 GB")
        self.assertEqual(n["storage"], "💾 Storage: 98/130 TB")

    def test_offline_render_has_placeholders(self):
        n = render(Snapshot(STATUS_OFFLINE, None, None, None))
        self.assertEqual(n["status"], "🔴 Silo: Offline")
        self.assertTrue(all(v.endswith("—") for k, v in n.items() if k != "status"))

    def test_cpu_and_ram_rounded_to_5(self):
        for cpu, want in ((4.9, "5%"), (7.4, "5%"), (7.6, "10%"), (0.4, "0%")):
            n = render(Snapshot(STATUS_ONLINE, [], Resources(cpu, 1, 4, None, None), 1), "host")
            self.assertEqual(n["cpu"], f"🧠 Host CPU: {want}")
        n = render(Snapshot(STATUS_ONLINE, [], Resources(1, 23_000, 65_536, None, None), 1), "host")
        self.assertEqual(n["memory"], "🐏 Host RAM: 35% of 64 GB")

    def res(self, t, cpu_s, rss=2 * 1024**3, cores=16):
        return Resources(50, 1, 4, None, None, cores=cores, sampled_at=t,
                         silo_cpu_seconds=cpu_s, silo_rss_bytes=rss)

    def test_silo_cpu_tracker(self):
        tr = SiloCpuTracker()
        self.assertIsNone(tr.update(self.res(1000, 100.0)))       # first sample
        # 16 cores x 30 s = 480 core-seconds; 24 used -> 5%
        self.assertAlmostEqual(tr.update(self.res(1030, 124.0)), 5.0)
        # Same Silo sample polled again: keep the value, don't divide by zero.
        self.assertAlmostEqual(tr.update(self.res(1030, 124.0)), 5.0)
        # A transcoder exits (total drops): keep last value, then resume.
        self.assertAlmostEqual(tr.update(self.res(1060, 110.0)), 5.0)
        self.assertAlmostEqual(tr.update(self.res(1090, 158.0)), 10.0)
        # Missing data -> None; capped at 100%.
        self.assertIsNone(tr.update(self.res(1120, None)))
        tr2 = SiloCpuTracker()
        tr2.update(self.res(0, 0.0, cores=1))
        self.assertEqual(tr2.update(self.res(10, 50.0, cores=1)), 100.0)

    def test_silo_scope_render(self):
        r = self.res(1000, 1.0, rss=int(2.3 * 1024**3))
        n = render(Snapshot(STATUS_ONLINE, [], r, 1, silo_cpu_pct=12.6))
        self.assertEqual(n["cpu"], "🧠 Silo CPU: 15%")
        self.assertEqual(n["memory"], "🐏 Silo RAM: 2.5 GB")
        n = render(Snapshot(STATUS_ONLINE, [], r, 1, silo_cpu_pct=1.2))
        self.assertEqual(n["cpu"], "🧠 Silo CPU: <5%")
        # Warming up: CPU omitted so the channel isn't renamed to a dash.
        n = render(Snapshot(STATUS_ONLINE, [], r, 1, silo_cpu_pct=None))
        self.assertNotIn("cpu", n)
        # No attribution data at all: show a dash.
        n = render(Snapshot(STATUS_ONLINE, [], Resources(5, 1, 4, None, None), 1))
        self.assertEqual(n["cpu"], "🧠 Silo CPU: —")
        self.assertEqual(n["memory"], "🐏 Silo RAM: —")
        self.assertEqual(fmt_gb(300 * 1024**2), "0.3 GB")

    def test_degraded_and_gbps(self):
        n = render(Snapshot(STATUS_DEGRADED, [sess(kbps=1_500_000)], None, None))
        self.assertEqual(n["status"], "🟡 Silo: Degraded")
        self.assertEqual(n["bandwidth"], "🌐 Remote: 1.50 Gbps")

    def test_no_personal_data_can_appear(self):
        # Rendering only accepts aggregate types; check names are short and clean.
        n = render(Snapshot(STATUS_ONLINE, [sess()], Resources(None, None, None, None, None), 1))
        for v in n.values():
            self.assertLessEqual(len(v), 100)


# ---------------------------------------------------------------- Silo client

def make_app(*, scoped=True, healthy=True, sessions=None, users=40, redirect=False, huge=False):
    app = web.Application()
    seen: list[dict] = []
    app["seen"] = seen

    def authed(request):
        seen.append({"path": request.path, "auth": request.headers.get("Authorization"),
                     "method": request.method})
        return request.headers.get("Authorization") == f"Bearer {KEY}"

    async def health(request):
        seen.append({"path": request.path, "auth": request.headers.get("Authorization"),
                     "method": request.method})
        return web.json_response({"status": "ok"}, status=200 if healthy else 503)

    async def build(request):
        if not authed(request):
            return web.json_response({}, status=401)
        return web.json_response({}, status=403 if scoped else 200)

    async def sess_handler(request):
        if not authed(request):
            return web.json_response({}, status=401)
        if redirect:
            raise web.HTTPFound("http://evil.example/steal")
        if huge:
            return web.json_response({"items": [], "pad": "x" * (3 * 1024 * 1024)})
        data = sessions if sessions is not None else [
            {"session_id": "s1", "username": "alice", "client_ip": "203.0.113.9",
             "media_title": "Secret Movie", "effective_play_method": "transcode",
             "play_method": "transcode", "is_paused": False, "stream_location": "remote",
             "stream_bitrate_kbps": 10000},
            {"session_id": "s2", "username": "bob", "play_method": "direct",
             "is_paused": True, "stream_location": "local", "stream_bitrate_kbps": None,
             "target_bitrate_kbps": 5000},
        ]
        return web.json_response({"items": data, "page": {"has_more": False}})

    async def users_handler(request):
        if not authed(request):
            return web.json_response({}, status=401)
        cursor = request.query.get("cursor")
        assert request.query.get("limit") == "200"
        if cursor is None:
            return web.json_response({"items": [{"id": str(i)} for i in range(25)],
                                      "page": {"next_cursor": "p2", "has_more": True}})
        return web.json_response({"items": [{"id": str(i)} for i in range(users - 25)],
                                  "page": {"has_more": False}})

    async def resources(request):
        if not authed(request):
            return web.json_response({}, status=401)
        return web.json_response({
            "available": True, "stale": False, "gpu": [],
            "sampled_at": "2026-11-01T17:30:00.000Z",
            "attribution": {
                "instance_id": "x", "process": {"cpu_seconds": 120.5, "resident_bytes": 1_000_000_000},
                "children": {"workload": "ffmpeg", "sampled": 1, "unavailable": 0, "truncated": False,
                             "resident_bytes": 500_000_000, "cpu_seconds": 30.0},
            },
            "system": {"cpu_pct": 12, "load1": 1.0, "cores": 16, "mem_used_mb": 32768,
                       "mem_total_mb": 65536, "net_rx_bps": 0, "net_tx_bps": 0,
                       "disks": [
                           {"role": "scratch", "used_gb": 10, "total_gb": 100, "scratch": True},
                           {"role": "library-0", "used_gb": 90000, "total_gb": 130000},
                           {"role": "library-1", "used_gb": 90000, "total_gb": 130000},
                       ]},
        })

    app.router.add_get("/api/v1/health", health)
    app.router.add_get("/api/v2/admin/system/build", build)
    app.router.add_get("/api/v2/admin/sessions", sess_handler)
    app.router.add_get("/api/v2/admin/users", users_handler)
    app.router.add_get("/api/v2/admin/system/resources", resources)
    return app


class SiloClientTests(unittest.IsolatedAsyncioTestCase):
    async def start(self, **kw):
        app = make_app(**kw)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.addAsyncCleanup(runner.cleanup)
        return app, f"http://127.0.0.1:{port}"

    async def test_happy_path(self):
        app, url = await self.start()
        async with SiloClient(url, Secret(KEY)) as c:
            self.assertTrue(await c.healthy())
            self.assertFalse(await c.key_is_unscoped())
            s = await c.sessions()
            self.assertEqual([x.method for x in s], ["transcode", "direct"])
            self.assertEqual(s[1].bitrate_kbps, 5000)
            self.assertTrue(s[1].paused)
            self.assertEqual(await c.user_count(), 40)
            r = await c.resources()
            self.assertEqual(r.cpu_pct, 12)
            self.assertEqual(r.cores, 16)
            self.assertEqual(r.silo_cpu_seconds, 150.5)
            self.assertEqual(r.silo_rss_bytes, 1_500_000_000)
            self.assertEqual(r.sampled_at, 1793554200.0)
            # Two library roots on one filesystem are counted once; scratch excluded.
            self.assertEqual((r.storage_used_gb, r.storage_total_gb), (90000, 130000))
        # Every request was a GET; health was sent WITHOUT the key.
        for req in app["seen"]:
            self.assertEqual(req["method"], "GET")
            if req["path"] == "/api/v1/health":
                self.assertIsNone(req["auth"])

    async def test_session_info_drops_personal_fields(self):
        _, url = await self.start()
        async with SiloClient(url, Secret(KEY)) as c:
            s = await c.sessions()
        dumped = repr(s)
        for leak in ("alice", "bob", "203.0.113.9", "Secret Movie", "s1"):
            self.assertNotIn(leak, dumped)

    async def test_unscoped_key_detected(self):
        _, url = await self.start(scoped=False)
        async with SiloClient(url, Secret(KEY)) as c:
            self.assertTrue(await c.key_is_unscoped())

    async def test_wrong_key(self):
        _, url = await self.start()
        async with SiloClient(url, Secret("sa_wrong_key_value")) as c:
            with self.assertRaises(SiloAuthError):
                await c.sessions()
            with self.assertRaises(SiloAuthError):
                await c.key_is_unscoped()

    async def test_redirects_not_followed(self):
        _, url = await self.start(redirect=True)
        async with SiloClient(url, Secret(KEY)) as c:
            with self.assertRaises(SiloError) as ctx:
                await c.sessions()
        self.assertIn("302", str(ctx.exception))

    async def test_oversized_body_rejected(self):
        _, url = await self.start(huge=True)
        async with SiloClient(url, Secret(KEY)) as c:
            with self.assertRaises(SiloError):
                await c.sessions()

    async def test_unhealthy_and_unreachable(self):
        _, url = await self.start(healthy=False)
        async with SiloClient(url, Secret(KEY)) as c:
            self.assertFalse(await c.healthy())
        async with SiloClient("http://127.0.0.1:1", Secret(KEY)) as c:
            self.assertFalse(await c.healthy())
            with self.assertRaises(SiloError) as ctx:
                await c.sessions()
            self.assertNotIn(KEY, str(ctx.exception))

    async def test_only_allowlisted_paths(self):
        async with SiloClient("http://127.0.0.1:1", Secret(KEY)) as c:
            with self.assertRaises(SiloError):
                await c._get("/api/v1/admin/settings")

    async def test_ignores_proxy_env(self):
        _, url = await self.start()
        with mock.patch.dict(os.environ, {"HTTP_PROXY": "http://127.0.0.1:9",
                                          "http_proxy": "http://127.0.0.1:9",
                                          "NO_PROXY": "", "no_proxy": ""}):
            async with SiloClient(url, Secret(KEY)) as c:
                self.assertTrue(await c.healthy())


# ---------------------------------------------------------------- bot logic

import discord  # noqa: E402

from siloquy import bot as botmod  # noqa: E402


class FakeVoice(discord.VoiceChannel):
    def __init__(self, cid, name):  # bypass discord's state-backed constructor
        self.id = cid
        self.name = name
        self.renames: list[str] = []

    async def edit(self, *, name, reason=None):
        self.renames.append(name)
        self.name = name


class FakeGuild:
    def __init__(self, channels):
        self._ch = {c.id: c for c in channels}

    def get_channel(self, cid):
        return self._ch.get(cid)


class BotLogicTests(unittest.IsolatedAsyncioTestCase):
    def make_bot(self, tmp, **env):
        base = {"DISCORD_TOKEN": DISCORD, "SILO_API_KEY": KEY, "DISCORD_GUILD_ID": "123456789012345678",
                "DATA_DIR": tmp, "STATS": "status,streams"}
        base.update(env)
        cfg = load_config(base)
        b = botmod.Siloquy(cfg, mock.Mock())
        return b

    async def test_rename_throttle(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = self.make_bot(tmp)
            a, s = FakeVoice(10, "old"), FakeVoice(11, "old")
            b.state.channels = {"status": 10, "streams": 11}
            g = FakeGuild([a, s])
            # The first change goes out at once.
            await b._apply(g, {"status": "🟢 Silo: Online", "streams": "▶️ Streams: 1"})
            self.assertEqual(s.renames, ["▶️ Streams: 1"])
            # The last rename in the window waits for the value to hold one poll,
            # so a momentary blip (2) doesn't use it up...
            await b._apply(g, {"status": "🟢 Silo: Online", "streams": "▶️ Streams: 2"})
            await b._apply(g, {"status": "🟢 Silo: Online", "streams": "▶️ Streams: 0"})
            self.assertEqual(s.renames, ["▶️ Streams: 1"])
            # ...but a value that holds does go out.
            await b._apply(g, {"status": "🟢 Silo: Online", "streams": "▶️ Streams: 0"})
            self.assertEqual(s.renames, ["▶️ Streams: 1", "▶️ Streams: 0"])
            # A third change inside 10 minutes is held back, and logged once.
            # Logged once per wait, even when the waiting value keeps changing.
            with self.assertLogs("siloquy.bot", "INFO") as logs:
                for v in ("2", "3", "2", "4"):
                    await b._apply(g, {"status": "🟢 Silo: Online", "streams": f"▶️ Streams: {v}"})
            self.assertEqual(len(s.renames), 2)
            self.assertEqual(sum("rename limit" in m for m in logs.output), 1)
            # Once the window passes, the latest value goes out.
            b.state.renames = {k: [time.time() - 611] * 2 for k in b.state.renames}
            await b._apply(g, {"status": "🔴 Silo: Offline", "streams": "▶️ Streams: 2"})
            self.assertEqual(a.renames[-1], "🔴 Silo: Offline")
            self.assertEqual(s.renames[-1], "▶️ Streams: 2")
            # Unchanged names cause no API call.
            b.state.renames = {}
            await b._apply(g, {"status": "🔴 Silo: Offline", "streams": "▶️ Streams: 2"})
            self.assertEqual(len(a.renames), 2)

    async def test_unscoped_key_stops_bot(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = self.make_bot(tmp)
            b.silo.healthy = mock.AsyncMock(return_value=True)
            b.silo.key_is_unscoped = mock.AsyncMock(return_value=True)
            b.close = mock.AsyncMock()
            with self.assertRaises(botmod._Stop):
                await b._collect()
            b.close.assert_awaited()
            self.assertEqual(b.exit_code, botmod.ExitCode.UNSAFE_KEY)

    async def test_offline_then_degraded(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = self.make_bot(tmp, STATS="status,users")
            b.silo.healthy = mock.AsyncMock(return_value=False)
            self.assertEqual((await b._collect()).status, STATUS_OFFLINE)
            b.silo.healthy = mock.AsyncMock(return_value=True)
            b.silo.key_is_unscoped = mock.AsyncMock(return_value=False)
            b.silo.sessions = mock.AsyncMock(return_value=[])
            b.silo.resources = mock.AsyncMock(side_effect=SiloError("x returned HTTP 500"))
            b.silo.user_count = mock.AsyncMock(return_value=7)
            snap = await b._collect()
            self.assertEqual(snap.status, STATUS_DEGRADED)
            self.assertEqual(snap.users, 7)
            # Users are cached, not refetched every poll.
            await b._collect()
            self.assertEqual(b.silo.user_count.await_count, 1)

    async def test_state_file_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            st = botmod.StateStore(Path(tmp, "state.json"))
            st.guild_id, st.channels = 1, {"cpu": 5, "evil": 6}
            st.save()
            self.assertEqual(os.stat(st.path).st_mode & 0o777, 0o600)
            st2 = botmod.StateStore(st.path)
            st2.load()
            self.assertEqual(st2.channels, {"cpu": 5})  # unknown keys dropped
            self.assertNotIn(KEY, st.path.read_text())

    async def test_forbidden_rename_backs_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = self.make_bot(tmp, STATS="status")
            ch = FakeVoice(10, "old")
            calls = []

            async def deny(*, name, reason=None):
                calls.append(name)
                raise discord.Forbidden(mock.Mock(status=403, reason="Forbidden"), "no")

            ch.edit = deny
            b.state.channels = {"status": 10}
            g = FakeGuild([ch])
            with self.assertLogs("siloquy.bot", "ERROR"):
                await b._apply(g, {"status": "🟢 Silo: Online"})
            await b._apply(g, {"status": "🟢 Silo: Online"})
            self.assertEqual(len(calls), 1)  # not retried every poll

    async def test_rate_limit_skips_channel_without_blocking_others(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = self.make_bot(tmp)
            a, s = FakeVoice(10, "old"), FakeVoice(11, "old")
            calls = []

            async def limited(*, name, reason=None):
                calls.append(name)
                raise discord.RateLimited(540.0)

            a.edit = limited
            b.state.channels = {"status": 10, "streams": 11}
            g = FakeGuild([a, s])
            await b._apply(g, {"status": "🟢 Silo: Online", "streams": "▶️ Streams: 1"})
            self.assertEqual(s.renames, ["▶️ Streams: 1"])  # other channel still updated
            await b._apply(g, {"status": "🟢 Silo: Online", "streams": "▶️ Streams: 1"})
            self.assertEqual(len(calls), 1)  # blocked channel not retried
            self.assertGreater(b._rename_wait("status", time.time()), 500)

    async def test_rename_history_survives_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = self.make_bot(tmp, STATS="streams")
            s = FakeVoice(11, "old")
            b.state.channels = {"streams": 11}
            g = FakeGuild([s])
            await b._apply(g, {"streams": "▶️ Streams: 1"})
            await b._apply(g, {"streams": "▶️ Streams: 0"})
            await b._apply(g, {"streams": "▶️ Streams: 0"})
            # "Restart": a new bot loading the same state file.
            b2 = self.make_bot(tmp, STATS="streams")
            b2.state.load()
            s2 = FakeVoice(11, "▶️ Streams: 0")
            await b2._apply(FakeGuild([s2]), {"streams": "▶️ Streams: 3"})
            self.assertEqual(s2.renames, [])  # still inside Discord's window
            self.assertGreater(b2._rename_wait("streams", time.time()), 500)

    def test_rate_limit_option_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = self.make_bot(tmp)
            self.assertEqual(b.http.max_ratelimit_timeout, 30.0)

    def test_minimal_intents(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = self.make_bot(tmp)
            self.assertEqual(b.intents.value, discord.Intents(guilds=True).value)
            self.assertFalse(b.intents.message_content)
            self.assertFalse(b.intents.members)


if __name__ == "__main__":
    unittest.main()
