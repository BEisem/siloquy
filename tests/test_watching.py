"""Tests for the optional watching role. Run: python -m unittest discover -s tests -v"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import discord
from aiohttp import web

from siloquy import bot as botmod
from siloquy import watching as w
from siloquy.config import ConfigError, Secret, load_config
from siloquy.silo import NamedID, SessionInfo, SiloClient, SiloError

KEY = "sa_test_0123456789abcdef"
DISCORD = "MTAx.discord.token.value.for.tests"
GUILD = 123456789012345678
ROLE = 223456789012345678
ALICE, BOB, CAROL = 300000000000000001, 300000000000000002, 300000000000000003


def env(tmp, **kw):
    e = {"DISCORD_TOKEN": DISCORD, "SILO_API_KEY": KEY, "DISCORD_GUILD_ID": str(GUILD),
         "DATA_DIR": tmp, "STATS": "status", "WATCHING_ROLE_ID": str(ROLE)}
    e.update(kw)
    return {k: v for k, v in e.items() if v is not None}


def sess(user="7", profile="p1", paused=False):
    return SessionInfo("direct", paused, False, 0, user_id=user, profile_id=profile)


# ---------------------------------------------------------------- fakes

def make_role(guild, rid, position, perms=0, managed=False):
    r = discord.Role.__new__(discord.Role)
    r.id, r.name, r.position, r.guild = rid, f"role{rid}", position, guild
    r._permissions, r.managed, r.tags, r._state = perms, managed, None, None
    return r


class FakeMember:
    def __init__(self, mid, roles=()):
        self.id = mid
        self.roles = list(roles)
        self.calls: list[str] = []

    async def add_roles(self, role, reason=None):
        self.calls.append("add")
        self.roles.append(role)

    async def remove_roles(self, role, reason=None):
        self.calls.append("remove")
        self.roles.remove(role)


class FakeGuild:
    def __init__(self, *, watch_perms=0, bot_above=True, bot_manage_roles=True, extra_roles=()):
        self.id = GUILD
        self.everyone = make_role(self, GUILD, 0)
        self.watch = make_role(self, ROLE, 1 if bot_above else 9, watch_perms)
        self.bot_role = make_role(self, 999, 5)
        self.roles = [self.everyone, self.watch, self.bot_role, *extra_roles]
        perms = discord.Permissions(manage_roles=bot_manage_roles)
        self.me = SimpleNamespace(top_role=self.bot_role, guild_permissions=perms)
        self.members: dict[int, FakeMember] = {}
        self.fetches = 0

    def get_role(self, rid):
        return next((r for r in self.roles if r.id == rid), None)

    async def fetch_member(self, mid):
        self.fetches += 1
        if mid not in self.members:
            raise discord.NotFound(mock.Mock(status=404, reason="Not Found"), "gone")
        return self.members[mid]


def manager(tmp, **kw):
    cfg = load_config(env(tmp, **kw))
    m = w.WatchingRole(cfg, mock.Mock())
    m.load()
    return m


# ---------------------------------------------------------------- config

class ConfigTests(unittest.TestCase):
    def test_off_by_default(self):
        cfg = load_config(env("/tmp", WATCHING_ROLE_ID=None))
        self.assertIsNone(cfg.watching_role_id)
        self.assertEqual(cfg.watching_grace_seconds, 60)

    def test_role_id_validated(self):
        self.assertEqual(load_config(env("/tmp")).watching_role_id, ROLE)
        for bad in ("abc", "123", "0" * 25, str(GUILD)):
            with self.assertRaises(ConfigError):
                load_config(env("/tmp", WATCHING_ROLE_ID=bad))
        with self.assertRaises(ConfigError):
            load_config(env("/tmp", WATCHING_GRACE_SECONDS="5000"))


# ---------------------------------------------------------------- tracker

class TrackerTests(unittest.TestCase):
    def test_account_and_profile_links(self):
        t = w.WatchTracker(60)
        links = {ALICE: w.Link("7"), BOB: w.Link("7", "kids"), CAROL: w.Link("8")}
        # Account 7 plays on profile p1: Alice (any profile) yes, Bob (kids only) no.
        self.assertEqual(t.update(links, [sess("7", "p1")], 0), {ALICE})
        self.assertEqual(t.update(links, [sess("7", "kids")], 1), {ALICE, BOB})

    def test_grace_period(self):
        t = w.WatchTracker(60)
        links = {ALICE: w.Link("7")}
        self.assertEqual(t.update(links, [sess()], 0), {ALICE})
        self.assertEqual(t.update(links, [], 30), {ALICE})   # between episodes
        self.assertEqual(t.update(links, [], 61), set())     # really stopped
        t0 = w.WatchTracker(0)
        self.assertEqual(t0.update(links, [sess()], 0), {ALICE})
        self.assertEqual(t0.update(links, [], 1), set())

    def test_paused_still_watching_and_unlinked_ignored(self):
        t = w.WatchTracker(60)
        self.assertEqual(t.update({ALICE: w.Link("7")}, [sess(paused=True), sess("99")], 0), {ALICE})
        # Unlinking drops them even inside the grace period.
        self.assertEqual(t.update({}, [], 1), set())

    def test_sessions_without_ids(self):
        t = w.WatchTracker(60)
        self.assertEqual(t.update({ALICE: w.Link("7")}, [sess(None, None)], 0), set())


# ---------------------------------------------------------------- store

class StoreTests(unittest.TestCase):
    def test_roundtrip_private_and_validated(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = w.WatchingStore(Path(tmp, "watching.json"))
            s.role_id, s.links, s.assigned = ROLE, {ALICE: w.Link("7", "p1"), BOB: w.Link("8")}, {ALICE}
            s.save()
            self.assertEqual(os.stat(s.path).st_mode & 0o777, 0o600)
            s2 = w.WatchingStore(s.path)
            s2.load()
            self.assertEqual((s2.role_id, s2.links, s2.assigned), (ROLE, s.links, {ALICE}))

    def test_bad_entries_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp, "watching.json")
            p.write_text(json.dumps({
                "role_id": True,
                "links": {"x": {"account": "1"}, "5": {"account": "../etc"},
                          "6": {"account": "1", "profile": "a/b"}, "7": {"account": "2", "profile": None}},
                "assigned": [1, "2", True, -3],
            }))
            s = w.WatchingStore(p)
            s.load()
            self.assertIsNone(s.role_id)
            self.assertEqual(s.links, {7: w.Link("2")})
            self.assertEqual(s.assigned, {1})


# ---------------------------------------------------------------- role safety

class RoleSafetyTests(unittest.TestCase):
    def test_good_role(self):
        self.assertIsNone(w.role_problem(FakeGuild(), ROLE))

    def test_refuses_unsafe_roles(self):
        cases = [
            (FakeGuild(watch_perms=discord.Permissions(administrator=True).value), "moderator/admin"),
            (FakeGuild(watch_perms=discord.Permissions(manage_roles=True).value), "manage_roles"),
            (FakeGuild(watch_perms=discord.Permissions(mention_everyone=True).value), "mention_everyone"),
            (FakeGuild(bot_above=False), "drag Siloquy's role above"),
            (FakeGuild(bot_manage_roles=False), "Manage Roles"),
        ]
        for guild, expect in cases:
            self.assertIn(expect, w.role_problem(guild, ROLE))
        g = FakeGuild()
        self.assertIn("isn't a role", w.role_problem(g, 42))
        self.assertIn("@everyone", w.role_problem(g, GUILD))
        g.watch.managed = True
        self.assertIn("managed", w.role_problem(g, ROLE))

    def test_ordinary_permissions_are_fine(self):
        perms = discord.Permissions(view_channel=True, send_messages=True, connect=True).value
        self.assertIsNone(w.role_problem(FakeGuild(watch_perms=perms), ROLE))

    def test_exposed_roles_counted(self):
        g = FakeGuild()
        g.roles.append(make_role(g, 50, 3, discord.Permissions(kick_members=True).value))
        g.roles.append(make_role(g, 51, 4, discord.Permissions(send_messages=True).value))
        g.roles.append(make_role(g, 52, 7, discord.Permissions(administrator=True).value))  # above bot
        self.assertEqual([r.id for r in w.exposed_roles(g, ROLE)], [50])


# ---------------------------------------------------------------- manager

class ManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_gives_and_takes_role(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = manager(tmp, WATCHING_GRACE_SECONDS="0")
            g = FakeGuild()
            g.members = {ALICE: FakeMember(ALICE), BOB: FakeMember(BOB)}
            m.store.links = {ALICE: w.Link("7"), BOB: w.Link("8")}
            await m.on_ready(g)
            await m.apply(g, [sess("7")])
            self.assertIn(g.watch, g.members[ALICE].roles)
            self.assertNotIn(g.watch, g.members[BOB].roles)
            self.assertEqual(m.store.assigned, {ALICE})
            # Unchanged: no Discord calls at all.
            g.fetches = 0
            await m.apply(g, [sess("7")])
            self.assertEqual(g.fetches, 0)
            await m.apply(g, [])
            self.assertNotIn(g.watch, g.members[ALICE].roles)
            self.assertEqual(m.store.assigned, set())
            # Saved to disk.
            saved = json.loads(m.store.path.read_text())
            self.assertEqual(saved["assigned"], [])

    async def test_never_removes_a_role_given_by_hand(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = manager(tmp, WATCHING_GRACE_SECONDS="0")
            g = FakeGuild()
            g.members = {ALICE: FakeMember(ALICE, [g.watch])}
            m.store.links = {ALICE: w.Link("7")}
            await m.on_ready(g)
            await m.apply(g, [sess("7")])
            await m.apply(g, [])
            self.assertIn(g.watch, g.members[ALICE].roles)
            self.assertEqual(g.members[ALICE].calls, [])

    async def test_outage_leaves_roles_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = manager(tmp, WATCHING_GRACE_SECONDS="0")
            g = FakeGuild()
            g.members = {ALICE: FakeMember(ALICE)}
            m.store.links = {ALICE: w.Link("7")}
            await m.on_ready(g)
            await m.apply(g, [sess("7")])
            await m.apply(g, None)  # Silo unreadable
            self.assertIn(g.watch, g.members[ALICE].roles)

    async def test_waits_for_startup_and_seeds_grace(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = manager(tmp, WATCHING_GRACE_SECONDS="60")
            g = FakeGuild()
            g.members = {ALICE: FakeMember(ALICE, [g.watch])}
            m.store.links, m.store.assigned = {ALICE: w.Link("7")}, {ALICE}
            await m.apply(g, [])  # before on_ready: does nothing
            self.assertEqual(g.fetches, 0)
            await m.on_ready(g)
            await m.apply(g, [])  # restart: keeps the role for the grace period
            self.assertIn(g.watch, g.members[ALICE].roles)

    async def test_unsafe_role_blocks_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = manager(tmp)
            g = FakeGuild(watch_perms=discord.Permissions(administrator=True).value)
            g.members = {ALICE: FakeMember(ALICE)}
            m.store.links = {ALICE: w.Link("7")}
            with self.assertLogs("siloquy.watching", "ERROR"):
                await m.on_ready(g)
            await m.apply(g, [sess("7")])
            self.assertEqual(g.members[ALICE].roles, [])
            self.assertEqual(g.fetches, 0)

    async def test_forbidden_backs_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = manager(tmp)
            g = FakeGuild()
            member = FakeMember(ALICE)

            async def deny(role, reason=None):
                raise discord.Forbidden(mock.Mock(status=403, reason="Forbidden"), "no")

            member.add_roles = deny
            g.members = {ALICE: member}
            m.store.links = {ALICE: w.Link("7")}
            await m.on_ready(g)
            with self.assertLogs("siloquy.watching", "ERROR"):
                await m.apply(g, [sess("7")])
            fetches = g.fetches
            await m.apply(g, [sess("7")])
            self.assertEqual(g.fetches, fetches)  # not retried every poll

    async def test_member_left_server(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = manager(tmp, WATCHING_GRACE_SECONDS="0")
            g = FakeGuild()
            m.store.links, m.store.assigned = {ALICE: w.Link("7")}, {ALICE}
            await m.on_ready(g)
            await m.apply(g, [])  # removal of someone who left: just forgotten
            self.assertEqual(m.store.assigned, set())
            await m.apply(g, [sess("7")])
            fetches = g.fetches
            await m.apply(g, [sess("7")])
            self.assertEqual(g.fetches, fetches)  # skipped, not fetched every poll

    async def test_role_changed_releases_old_role(self):
        with tempfile.TemporaryDirectory() as tmp:
            g = FakeGuild()
            old = make_role(g, 777, 2)
            g.roles.append(old)
            g.members = {ALICE: FakeMember(ALICE, [old])}
            m = manager(tmp)
            m.store.role_id, m.store.assigned, m.store.links = 777, {ALICE}, {ALICE: w.Link("7")}
            await m.on_ready(g)
            self.assertNotIn(old, g.members[ALICE].roles)
            self.assertEqual(m.store.assigned, set())
            self.assertEqual(m.store.role_id, ROLE)
            self.assertEqual(m.store.links, {ALICE: w.Link("7")})  # links kept

    async def test_turned_off_releases_role(self):
        with tempfile.TemporaryDirectory() as tmp:
            g = FakeGuild()
            g.members = {ALICE: FakeMember(ALICE, [g.watch])}
            m = manager(tmp, WATCHING_ROLE_ID=None)
            m.store.role_id, m.store.assigned = ROLE, {ALICE}
            m.store.save()
            await m.on_ready(g)
            self.assertEqual(g.members[ALICE].roles, [])
            await m.apply(g, [sess("7")])  # off: does nothing
            self.assertEqual(g.members[ALICE].roles, [])

    async def test_off_and_unused_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = manager(tmp, WATCHING_ROLE_ID=None)
            await m.on_ready(FakeGuild())
            self.assertFalse(m.store.path.exists())

    async def test_unlink_removes_role_now(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = manager(tmp)
            g = FakeGuild()
            g.members = {ALICE: FakeMember(ALICE)}
            m.store.links = {ALICE: w.Link("7")}
            await m.on_ready(g)
            await m.apply(g, [sess("7")])
            self.assertTrue(await m.remove_link(g, ALICE))
            self.assertEqual(g.members[ALICE].roles, [])
            self.assertEqual((m.store.links, m.store.assigned), ({}, set()))
            self.assertFalse(await m.remove_link(g, ALICE))

    async def test_logs_hold_no_identities(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = manager(tmp, WATCHING_GRACE_SECONDS="0")
            g = FakeGuild()
            g.members = {ALICE: FakeMember(ALICE)}
            m.store.links = {ALICE: w.Link("7")}
            with self.assertLogs("siloquy.watching", "INFO") as logs:
                await m.on_ready(g)
                await m.apply(g, [sess("7")])
                await m.apply(g, [])
            text = "\n".join(logs.output)
            self.assertNotIn(str(ALICE), text)
            self.assertIn("+1 -0", text)


# ---------------------------------------------------------------- commands

class FakeResponse:
    def __init__(self):
        self.sent: list[str] = []
        self.done = False

    async def send_message(self, text, ephemeral=False):
        assert ephemeral
        self.sent.append(text)
        self.done = True

    async def defer(self, ephemeral=False, thinking=False):
        assert ephemeral
        self.done = True

    def is_done(self):
        return self.done


def interaction(guild, *, manage_roles=True, guild_id=GUILD, account=None):
    resp = FakeResponse()

    async def followup_send(text, ephemeral=False):
        assert ephemeral
        resp.sent.append(text)

    return SimpleNamespace(
        guild_id=guild_id, guild=guild, response=resp,
        permissions=discord.Permissions(manage_roles=manage_roles),
        followup=SimpleNamespace(send=followup_send),
        namespace=SimpleNamespace(account=account),
    )


def fake_member(mid, bot=False):
    return SimpleNamespace(id=mid, bot=bot, mention=f"<@{mid}>")


class CommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.m = manager(self.tmp.name)
        self.m.silo.accounts = mock.AsyncMock(return_value=[
            NamedID("7", "alice"), NamedID("8", "Bob_*bold*"), NamedID("9", "ALICE2"),
        ])
        self.m.silo.profiles = mock.AsyncMock(return_value=[NamedID("p1", "Alice"), NamedID("p2", "Kids")])
        self.cmds = w.WatchingCommands(self.m)
        self.g = FakeGuild()

    def tearDown(self):
        self.tmp.cleanup()

    async def run_cmd(self, name, inter, *args, **kw):
        cmd = next(c for c in self.cmds.commands if c.name == name)
        await cmd.callback(self.cmds, inter, *args, **kw)
        return inter.response.sent

    async def test_link_by_pick_and_by_name(self):
        sent = await self.run_cmd("link", interaction(self.g), fake_member(ALICE), "7")
        self.assertEqual(self.m.store.links[ALICE], w.Link("7"))
        self.assertIn("any profile", sent[-1])
        sent = await self.run_cmd("link", interaction(self.g), fake_member(BOB), "alice", "kids")
        self.assertEqual(self.m.store.links[BOB], w.Link("7", "p2"))
        self.assertIn("profile **Kids**", sent[-1])
        self.assertTrue(self.m.store.path.exists())

    async def test_link_rejects_unknowns_and_bots(self):
        await self.run_cmd("link", interaction(self.g), fake_member(ALICE), "nobody")
        await self.run_cmd("link", interaction(self.g), fake_member(ALICE), "7", "Nope")
        sent = await self.run_cmd("link", interaction(self.g), fake_member(ALICE, bot=True), "7")
        self.assertIn("Bots", sent[-1])
        self.assertEqual(self.m.store.links, {})

    async def test_silo_down(self):
        self.m.silo.accounts = mock.AsyncMock(side_effect=SiloError("x"))
        sent = await self.run_cmd("link", interaction(self.g), fake_member(ALICE), "7")
        self.assertIn("Couldn't reach Silo", sent[-1])

    async def test_needs_manage_roles_and_right_server(self):
        for inter in (interaction(self.g, manage_roles=False), interaction(self.g, guild_id=1)):
            sent = await self.run_cmd("link", inter, fake_member(ALICE), "7")
            self.assertIn("Manage Roles", sent[-1])
        self.assertEqual(self.m.store.links, {})
        self.assertEqual(self.m.silo.accounts.await_count, 0)

    async def test_autocomplete(self):
        ac = await self.cmds._account_ac(interaction(self.g), "ali")
        self.assertEqual([c.value for c in ac], ["7", "9"])
        self.assertEqual(await self.cmds._account_ac(interaction(self.g, manage_roles=False), ""), [])
        prof = await self.cmds._profile_ac(interaction(self.g, account="7"), "")
        self.assertEqual([c.name for c in prof], ["Alice", "Kids"])
        self.assertEqual(await self.cmds._profile_ac(interaction(self.g), ""), [])

    async def test_list_and_markdown_escaped(self):
        self.m.store.links = {ALICE: w.Link("8"), BOB: w.Link("7", "p2")}
        self.m.store.assigned = {BOB}
        sent = await self.run_cmd("list", interaction(self.g))
        text = sent[-1]
        self.assertIn("2 linked", text)
        self.assertIn(r"Bob\_\*bold\*", text)
        self.assertIn(f"🟢 <@{BOB}> → **alice** · Kids", text)

    async def test_unlinked(self):
        self.m.silo.accounts = mock.AsyncMock(return_value=[
            NamedID("7", "alice", last_active=1_793_000_000.0),
            NamedID("8", "Bob_*bold*", last_active=1_793_500_000.0),
            NamedID("9", "carol"),
            NamedID("10", "dave", enabled=False),
            NamedID("11", "erin"),
        ])
        self.m.store.links = {ALICE: w.Link("7"), BOB: w.Link("11", "kids")}
        sent = await self.run_cmd("unlinked", interaction(self.g))
        lines = sent[-1].splitlines()
        self.assertEqual(lines[0], "**2 of 4 active Silo accounts aren't linked:**")
        # Most recently active first, never-active last; disabled hidden; linked
        # (including profile-only links) excluded; names escaped.
        self.assertEqual(lines[1], r"• **Bob\_\*bold\*** · last active <t:1793500000:R>")
        self.assertEqual(lines[2], "• **carol** · never active")
        self.assertIn("1 disabled account not shown", sent[-1])
        self.assertNotIn("alice", sent[-1])
        self.assertNotIn("erin", sent[-1])

    async def test_unlinked_all_linked_and_silo_down(self):
        self.m.silo.accounts = mock.AsyncMock(return_value=[NamedID("7", "alice")])
        self.m.store.links = {ALICE: w.Link("7")}
        sent = await self.run_cmd("unlinked", interaction(self.g))
        self.assertIn("Every active Silo account is linked", sent[-1])
        self.m._accounts = None
        self.m.silo.accounts = mock.AsyncMock(side_effect=SiloError("x"))
        sent = await self.run_cmd("unlinked", interaction(self.g))
        self.assertIn("Couldn't reach Silo", sent[-1])
        sent = await self.run_cmd("unlinked", interaction(self.g, manage_roles=False))
        self.assertIn("Manage Roles", sent[-1])

    def test_unlinked_long_list_fits_one_message(self):
        accounts = [NamedID(str(i), f"user{i:04d}") for i in range(500)]
        text = w.render_unlinked(accounts, {})
        self.assertLessEqual(len(text), 2000)
        self.assertIn("more", text.splitlines()[-1])

    async def test_unlink(self):
        self.m.store.links = {ALICE: w.Link("7")}
        sent = await self.run_cmd("unlink", interaction(self.g), fake_member(ALICE))
        self.assertIn("Unlinked", sent[-1])
        self.assertEqual(self.m.store.links, {})

    def test_command_payload_is_admin_only(self):
        client = mock.Mock()
        client._connection._command_tree = None
        tree = discord.app_commands.CommandTree(client)
        payload = self.cmds.to_dict(tree)
        self.assertEqual(payload["name"], "watching")
        self.assertEqual(int(payload["default_member_permissions"]), discord.Permissions(manage_roles=True).value)
        self.assertFalse(payload.get("dm_permission", True))
        self.assertEqual(sorted(o["name"] for o in payload["options"]), ["link", "list", "unlink", "unlinked"])


# ---------------------------------------------------------------- Silo client additions

def silo_app():
    app = web.Application()
    seen: list[str] = []
    app["seen"] = seen

    def ok(request):
        seen.append(request.path_qs)
        return request.headers.get("Authorization") == f"Bearer {KEY}"

    async def users(request):
        if not ok(request):
            return web.json_response({}, status=401)
        return web.json_response({"items": [
            {"id": "7", "username": "alice", "email": "a@x", "enabled": True,
             "last_active_at": "2026-11-01T17:30:00.000Z"},
            {"id": "8", "username": " "}, {"id": "10", "username": "dave", "enabled": False,
                                           "last_active_at": None},
            {"id": "../9", "username": "evil"},
        ], "page": {"has_more": False}})

    async def profiles(request):
        if not ok(request):
            return web.json_response({}, status=401)
        if request.query:
            return web.json_response({"title": "unknown query parameter"}, status=422)
        return web.json_response({"items": [{"id": "p1", "name": "Alice", "avatar": "x"}]})

    async def sessions(request):
        if not ok(request):
            return web.json_response({}, status=401)
        return web.json_response({"items": [
            {"session_id": "s1", "user_id": "7", "username": "alice", "profile_id": "p1",
             "profile_name": "Alice", "play_method": "direct", "media_title": "Secret Movie"},
            {"session_id": "s2", "user_id": {"x": 1}, "profile_id": "a b"},
        ]})

    app.router.add_get("/api/v2/admin/users", users)
    app.router.add_get("/api/v2/admin/users/7/profiles", profiles)
    app.router.add_get("/api/v2/admin/sessions", sessions)
    return app


class SiloClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        runner = web.AppRunner(silo_app())
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        self.addAsyncCleanup(runner.cleanup)
        self.app = runner.app
        self.url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"

    async def test_sessions_keep_ids_only(self):
        async with SiloClient(self.url, Secret(KEY)) as c:
            s = await c.sessions()
        self.assertEqual((s[0].user_id, s[0].profile_id), ("7", "p1"))
        self.assertEqual((s[1].user_id, s[1].profile_id), (None, None))
        for leak in ("alice", "Alice", "Secret Movie", "s1"):
            self.assertNotIn(leak, repr(s))

    async def test_accounts_and_profiles(self):
        async with SiloClient(self.url, Secret(KEY)) as c:
            self.assertEqual(await c.accounts(), [
                NamedID("7", "alice", last_active=1793554200.0), NamedID("10", "dave", enabled=False),
            ])
            self.assertEqual(await c.profiles("7"), [NamedID("p1", "Alice")])
            for bad in ("../7", "7/../../settings", "", "abc"):
                with self.assertRaises(SiloError):
                    await c.profiles(bad)
        self.assertIn("/api/v2/admin/users/7/profiles", self.app["seen"])

    async def test_profiles_path_allowlist(self):
        async with SiloClient("http://127.0.0.1:1", Secret(KEY)) as c:
            for bad in ("/api/v2/admin/users/7/devices", "/api/v2/admin/users/7/profiles/x",
                        "/api/v2/admin/users/x/profiles"):
                with self.assertRaises(SiloError) as ctx:
                    await c._get(bad)
                self.assertIn("non-allowlisted", str(ctx.exception))


# ---------------------------------------------------------------- bot wiring

class BotWiringTests(unittest.IsolatedAsyncioTestCase):
    def make_bot(self, tmp, **kw):
        return botmod.Siloquy(load_config(env(tmp, **kw)), mock.Mock())

    async def test_invite_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            on = self.make_bot(tmp)
            off = self.make_bot(tmp, WATCHING_ROLE_ID=None)
            self.assertTrue(on.invite_permissions & w.MANAGE_ROLES)
            self.assertFalse(off.invite_permissions & w.MANAGE_ROLES)
            self.assertEqual(on.intents.value, discord.Intents(guilds=True).value)

    async def test_commands_registered_only_when_on(self):
        with tempfile.TemporaryDirectory() as tmp:
            for role, expect in ((str(ROLE), ["watching"]), (None, [])):
                b = self.make_bot(tmp, WATCHING_ROLE_ID=role)
                b.tree.sync = mock.AsyncMock()
                await b._sync_commands()
                names = [c.name for c in b.tree.get_commands(guild=discord.Object(GUILD))]
                self.assertEqual(names, expect)
                b.tree.sync.assert_awaited_once()  # off: still syncs, removing old commands

    async def test_watching_errors_never_stop_stats(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = self.make_bot(tmp)
            b.watching.apply = mock.AsyncMock(side_effect=RuntimeError("secret detail"))
            with self.assertLogs("siloquy.bot", "WARNING") as logs:
                await b._update_watching(FakeGuild(), [])
            self.assertNotIn("secret detail", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
