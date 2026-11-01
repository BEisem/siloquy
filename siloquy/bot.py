"""Discord side: keeps a set of read-only voice channels named after live stats.

Security properties:
  * Gateway intents are limited to ``guilds``: no message content, no members,
    no presences. The bot has no commands and reads no messages.
  * It only works in the one configured server and leaves any other server it
    is added to.
  * It only ever renames or deletes channels it created itself (tracked by ID in
    the state file). It never touches any other channel.
  * Channels it creates deny Connect to @everyone, so they act as display-only
    labels.
  * The optional watching role (watching.py) adds admin-only /watching
    commands and the Manage Roles permission; it is off unless configured.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path

import discord
from discord import app_commands

from .config import Config
from .silo import SiloAuthError, SiloClient, SiloError
from .stats import (
    CATEGORY_OF,
    SiloCpuTracker,
    STATUS_DEGRADED,
    STATUS_OFFLINE,
    STATUS_ONLINE,
    Snapshot,
    render,
)
from .watching import MANAGE_ROLES, WatchingCommands, WatchingRole

log = logging.getLogger(__name__)

# Discord allows 2 renames per channel per 10 minutes; stay at or under that.
RENAMES_PER_WINDOW = 2
RENAME_WINDOW = 610.0  # Discord's 10 minutes plus a small safety margin
# View Channels + Manage Channels + Connect. Connect is required only so the
# bot is allowed to *deny* Connect to @everyone on the channels it creates.
INVITE_PERMISSIONS = 1024 | 16 | 1048576
HEARTBEAT_FILE = Path("/tmp/siloquy-heartbeat")


class _Stop(Exception):
    """Internal signal to end the update loop after the client has closed."""


class ExitCode:
    OK = 0
    UNSAFE_KEY = 2
    AUTH = 3


class StateStore:
    """Tiny JSON file mapping stat keys to the channel IDs this bot created."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.categories: dict[str, int] = {}
        self.channels: dict[str, int] = {}
        self.guild_id: int | None = None
        # Wall-clock (epoch) times, so Discord's limits survive restarts.
        self.renames: dict[str, list[float]] = {}
        self.blocked_until: dict[str, float] = {}

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            log.warning("State file unreadable; starting fresh")
            return
        if not isinstance(raw, dict):
            return
        self.guild_id = raw.get("guild_id") if isinstance(raw.get("guild_id"), int) else None
        self.categories = {
            k: v for k, v in (raw.get("categories") or {}).items()
            if isinstance(k, str) and isinstance(v, int)
        }
        self.channels = {
            k: v for k, v in (raw.get("channels") or {}).items()
            if k in CATEGORY_OF and isinstance(v, int)
        }
        self.renames = {
            k: [float(t) for t in v if isinstance(t, (int, float))][-RENAMES_PER_WINDOW:]
            for k, v in (raw.get("renames") or {}).items()
            if k in CATEGORY_OF and isinstance(v, list)
        }
        self.blocked_until = {
            k: float(v) for k, v in (raw.get("blocked_until") or {}).items()
            if k in CATEGORY_OF and isinstance(v, (int, float))
        }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        data = json.dumps(
            {
                "guild_id": self.guild_id,
                "categories": self.categories,
                "channels": self.channels,
                "renames": self.renames,
                "blocked_until": self.blocked_until,
            },
            indent=2,
        )
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
        os.replace(tmp, self.path)


class Siloquy(discord.Client):
    def __init__(self, config: Config, silo: SiloClient) -> None:
        # Siloquy never joins voice; skip the "voice not supported" warnings.
        discord.VoiceClient.warn_nacl = False
        discord.VoiceClient.warn_dave = False
        intents = discord.Intents.none()
        intents.guilds = True
        super().__init__(
            intents=intents,
            allowed_mentions=discord.AllowedMentions.none(),
            max_messages=None,
            chunk_guilds_at_startup=False,
            member_cache_flags=discord.MemberCacheFlags.none(),
            # Raise RateLimited instead of sleeping (which froze every counter).
            max_ratelimit_timeout=30.0,
        )
        self.cfg = config
        self.silo = silo
        self.state = StateStore(config.data_dir / "state.json")
        self.exit_code = ExitCode.OK
        self._layout_ready = asyncio.Event()
        self._layout_lock = asyncio.Lock()
        # key -> when we first logged that it's waiting (log once per wait)
        self._deferred: dict[str, float] = {}
        # key -> the value computed on the previous poll (for debouncing)
        self._seen: dict[str, str] = {}
        self._scope_verified = False
        self._users: int | None = None
        self._users_at = 0.0
        self._cpu = SiloCpuTracker()
        self._last_problem: str | None = None
        self.tree = app_commands.CommandTree(self)
        self.tree.on_error = self._on_command_error
        self.watching = WatchingRole(config, silo)

    @property
    def invite_permissions(self) -> int:
        return INVITE_PERMISSIONS | (MANAGE_ROLES if self.watching.enabled else 0)

    # ----- lifecycle -----------------------------------------------------

    async def setup_hook(self) -> None:
        self.state.load()
        if self.state.guild_id not in (None, self.cfg.guild_id):
            # A different server than last time: don't touch the old IDs.
            self.state.categories, self.state.channels = {}, {}
        self.state.guild_id = self.cfg.guild_id
        self.watching.load()
        await self._sync_commands()
        self.loop.create_task(self._update_loop(), name="siloquy-updater")

    async def _on_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        # Type only: Discord errors can echo request payloads.
        cause = getattr(error, "original", error)
        log.warning("/watching command failed (%s)", type(cause).__name__)
        try:
            if interaction.response.is_done():
                await interaction.followup.send("Something went wrong; check Siloquy's log.", ephemeral=True)
            else:
                await interaction.response.send_message("Something went wrong; check Siloquy's log.", ephemeral=True)
        except discord.HTTPException:
            pass

    async def _sync_commands(self) -> None:
        """Register /watching in our one server, or remove it when switched off."""
        guild = discord.Object(self.cfg.guild_id)
        self.tree.clear_commands(guild=guild)
        if self.watching.enabled:
            self.tree.add_command(WatchingCommands(self.watching), guild=guild)
        try:
            await self.tree.sync(guild=guild)
        except discord.Forbidden:
            if self.watching.enabled:
                log.error(
                    "Discord refused to register /watching. Re-invite the bot with: "
                    "https://discord.com/oauth2/authorize?client_id=%s"
                    "&scope=bot+applications.commands&permissions=%s",
                    self.application_id, self.invite_permissions,
                )
        except (discord.HTTPException, discord.RateLimited) as exc:
            log.warning("Could not register slash commands (%s)", type(exc).__name__)

    async def on_ready(self) -> None:
        log.info("Connected to Discord as bot user %s", self.user.id if self.user else "?")
        for guild in list(self.guilds):
            if guild.id != self.cfg.guild_id:
                await self._leave_foreign(guild)
        guild = self.get_guild(self.cfg.guild_id)
        if guild is None:
            log.error(
                "The bot is not in server %s. Invite it with: "
                "https://discord.com/oauth2/authorize?client_id=%s"
                "&scope=bot+applications.commands&permissions=%s",
                self.cfg.guild_id, self.application_id, self.invite_permissions,
            )
            return
        await self._ensure_layout(guild)
        await self._watching_ready(guild)

    async def _watching_ready(self, guild: discord.Guild) -> None:
        try:
            await self.watching.on_ready(guild)
        except Exception as exc:  # type only: Discord errors can echo payloads
            log.warning("Watching role setup failed (%s)", type(exc).__name__)

    async def on_guild_join(self, guild: discord.Guild) -> None:
        if guild.id != self.cfg.guild_id:
            await self._leave_foreign(guild)
        else:
            await self._ensure_layout(guild)
            await self._watching_ready(guild)

    async def _leave_foreign(self, guild: discord.Guild) -> None:
        log.warning("Leaving server %s: it is not DISCORD_GUILD_ID", guild.id)
        try:
            await guild.leave()
        except discord.HTTPException:
            log.warning("Could not leave server %s", guild.id)

    # ----- channel layout -------------------------------------------------

    def _category_name(self, cat: str) -> str:
        return self.cfg.stats_category if cat == "stats" else self.cfg.performance_category

    async def _ensure_layout(self, guild: discord.Guild) -> None:
        async with self._layout_lock:
            try:
                await self._ensure_layout_locked(guild)
            except discord.Forbidden:
                log.error(
                    "Missing permissions in the server. The bot needs View Channels, "
                    "Manage Channels and Connect (permissions=%s).", INVITE_PERMISSIONS,
                )
                return
            except discord.HTTPException as exc:
                log.error("Discord API error while setting up channels (HTTP %s)", exc.status)
                return
            self._layout_ready.set()

    async def _ensure_layout_locked(self, guild: discord.Guild) -> None:
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(connect=False, speak=False),
            # Discord strips Manage Channels from anyone who can't Connect to a
            # voice channel, so the bot needs its own explicit allow.
            guild.me: discord.PermissionOverwrite(
                view_channel=True, connect=True, manage_channels=True
            ),
        }
        enabled = list(self.cfg.stats)
        needed_cats = {CATEGORY_OF[k] for k in enabled}
        changed = False

        categories: dict[str, discord.CategoryChannel] = {}
        for cat in ("stats", "performance"):
            existing = guild.get_channel(self.state.categories.get(cat, 0))
            if cat not in needed_cats:
                if isinstance(existing, discord.CategoryChannel) and not existing.channels:
                    await existing.delete(reason="Siloquy: category no longer used")
                if cat in self.state.categories:
                    del self.state.categories[cat]
                    changed = True
                continue
            if isinstance(existing, discord.CategoryChannel):
                categories[cat] = existing
                continue
            created = await guild.create_category(
                self._category_name(cat), overwrites=overwrites, reason="Siloquy setup"
            )
            categories[cat] = created
            self.state.categories[cat] = created.id
            changed = True
            log.info("Created category %s", created.id)

        # Remove channels for stats that were switched off (only our own).
        for key in list(self.state.channels):
            if key not in enabled:
                ch = guild.get_channel(self.state.channels.pop(key))
                changed = True
                if isinstance(ch, discord.VoiceChannel):
                    await ch.delete(reason="Siloquy: stat disabled")

        placeholder = render(Snapshot(STATUS_OFFLINE, None, None, None), self.cfg.resource_scope)
        placeholder.setdefault("cpu", "🧠 CPU: —")
        for key in enabled:
            ch = guild.get_channel(self.state.channels.get(key, 0))
            if isinstance(ch, discord.VoiceChannel):
                continue
            ch = await guild.create_voice_channel(
                placeholder[key],
                category=categories[CATEGORY_OF[key]],
                overwrites=overwrites,
                reason="Siloquy setup",
            )
            self.state.channels[key] = ch.id
            changed = True
            log.info("Created channel for %s", key)

        if changed:
            self.state.save()

    # ----- polling -------------------------------------------------------

    def _problem(self, msg: str | None) -> None:
        """Log a problem once when it starts, and once when it clears."""
        if msg != self._last_problem:
            if msg:
                log.warning("%s", msg)
            elif self._last_problem:
                log.info("Silo API is healthy again")
            self._last_problem = msg

    async def _collect(self) -> Snapshot:
        if not await self.silo.healthy():
            self._problem("Silo health check failed; showing Offline")
            return Snapshot(STATUS_OFFLINE, None, None, None)

        if not self._scope_verified:
            if await self.silo.key_is_unscoped():
                if not self.cfg.allow_unscoped_key:
                    log.critical(
                        "Refusing to run: the Silo API key is NOT scoped (it has full admin "
                        "access). Create a key with only the admin:monitor:read scope."
                    )
                    self.exit_code = ExitCode.UNSAFE_KEY
                    await self.close()
                    raise _Stop
                log.warning("Running with an UNSCOPED Silo key because ALLOW_UNSCOPED_KEY=true")
            self._scope_verified = True
            log.info("Silo API key scope verified")

        problems: list[str] = []
        sessions = resources = None
        try:
            sessions = await self.silo.sessions()
        except SiloError as exc:
            problems.append(str(exc))
        try:
            resources = await self.silo.resources()
        except SiloError as exc:
            problems.append(str(exc))

        if "users" in self.cfg.stats and (
            self._users is None or time.monotonic() - self._users_at >= self.cfg.users_poll_seconds
        ):
            try:
                self._users = await self.silo.user_count()
                self._users_at = time.monotonic()
            except SiloError as exc:
                problems.append(str(exc))

        self._problem("; ".join(problems) or None)
        status = STATUS_ONLINE if not problems else STATUS_DEGRADED
        return Snapshot(status, sessions, resources, self._users, self._cpu.update(resources))

    async def _update_loop(self) -> None:
        await self.wait_until_ready()
        auth_failures = 0
        while not self.is_closed():
            try:
                HEARTBEAT_FILE.touch()
                guild = self.get_guild(self.cfg.guild_id)
                if guild is not None:
                    snap = await self._collect()
                    await self._update_watching(guild, snap.sessions)
                    await self._apply(guild, render(snap, self.cfg.resource_scope))
                auth_failures = 0
            except SiloAuthError as exc:
                auth_failures += 1
                self._problem(str(exc))
                # Silo is up but refusing the key: say Degraded, not Offline.
                guild = self.get_guild(self.cfg.guild_id)
                if guild is not None:
                    try:
                        await self._apply(guild, render(Snapshot(STATUS_DEGRADED, None, None, None), self.cfg.resource_scope))
                    except Exception:
                        log.warning("Could not update channels after an auth failure")
                if auth_failures >= 10:
                    log.critical("Silo keeps rejecting the API key; stopping")
                    self.exit_code = ExitCode.AUTH
                    await self.close()
                    return
            except SiloError as exc:
                self._problem(str(exc))
            except (_Stop, asyncio.CancelledError):
                return
            except Exception:  # keep the loop alive; log type only, no data
                log.exception("Unexpected error in update loop")
            await asyncio.sleep(self.cfg.poll_seconds)

    async def _update_watching(self, guild: discord.Guild, sessions) -> None:
        try:
            await self.watching.apply(guild, sessions)
        except Exception as exc:  # never let it stop the stats; type only
            log.warning("Watching role update failed (%s)", type(exc).__name__)

    def _renames_in_window(self, key: str, now: float) -> int:
        return sum(1 for t in self.state.renames.get(key, []) if 0 <= now - t < RENAME_WINDOW)

    def _rename_wait(self, key: str, now: float) -> float:
        """Seconds until this channel may be renamed again (0 = now)."""
        wait = self.state.blocked_until.get(key, 0.0) - now
        recent = [t for t in self.state.renames.get(key, []) if 0 <= now - t < RENAME_WINDOW]
        if len(recent) >= RENAMES_PER_WINDOW:
            wait = max(wait, RENAME_WINDOW - (now - recent[0]))
        return max(0.0, wait)

    async def _apply(self, guild: discord.Guild, names: dict[str, str]) -> None:
        missing = False
        now = time.time()
        for key in self.cfg.stats:
            ch = guild.get_channel(self.state.channels.get(key, 0))
            if not isinstance(ch, discord.VoiceChannel):
                missing = True
                continue
            want = names.get(key)
            if want is None:  # no new value yet (e.g. CPU warming up)
                continue
            prev, self._seen[key] = self._seen.get(key), want
            if ch.name == want:
                self._deferred.pop(key, None)
                continue
            wait = self._rename_wait(key, now)
            if wait > 0:
                if key not in self._deferred:
                    self._deferred[key] = now
                    log.info(
                        "%s: waiting on Discord's rename limit; next update in ~%d min",
                        key, max(1, round(wait / 60)),
                    )
                continue
            # The last rename in Discord's window is precious: only spend it on
            # a value that held for a full poll, not a momentary blip.
            if self._renames_in_window(key, now) >= RENAMES_PER_WINDOW - 1 and prev != want:
                continue
            try:
                await ch.edit(name=want, reason="Siloquy stats update")
                recent = self.state.renames.setdefault(key, [])
                recent.append(time.time())
                del recent[:-RENAMES_PER_WINDOW]
                self._deferred.pop(key, None)
                self.state.save()
                log.info("%s -> %s", key, want)
            except discord.RateLimited as exc:
                # Discord wants a longer pause than our own count expected
                # (e.g. renames made before a restart). Skip this channel
                # until then; the other channels keep updating.
                self.state.blocked_until[key] = time.time() + exc.retry_after + 5
                self.state.save()
                log.info(
                    "%s: Discord asked to wait ~%d min before renaming again",
                    key, max(1, round(exc.retry_after / 60)),
                )
            except discord.Forbidden:
                # Pause like a rate limit so this isn't retried and logged every poll.
                self.state.blocked_until[key] = time.time() + RENAME_WINDOW
                self.state.save()
                log.error(
                    "No permission to rename the %s channel. On each Siloquy category, "
                    "give the Siloquy role View Channel, Connect and Manage Channels, "
                    "then sync the channels.", key,
                )
            except discord.HTTPException as exc:
                log.warning("Discord rename failed for %s (HTTP %s)", key, exc.status)
        if missing:
            log.info("A Siloquy channel was deleted; recreating it")
            await self._ensure_layout(guild)
