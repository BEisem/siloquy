"""Optional @Watching role: given to a linked member while they play on Silo.

Turned on by setting WATCHING_ROLE_ID. Server admins link Discord members to
Silo accounts with /watching link; Siloquy then adds the role when a linked
account starts playing and removes it a short while after playback ends.

Security properties:
  * Only ever adds or removes the one configured role, and only removes it
    from members Siloquy gave it to. A role an admin gave by hand is left alone.
  * Refuses a role that carries moderator or admin permissions, a role managed
    by an integration, or @everyone.
  * /watching needs Manage Roles, works only in the configured server, and its
    answers are visible only to the admin who ran it.
  * Stores only Discord user IDs and Silo's opaque account/profile IDs.
    Usernames and profile names are fetched to answer an admin's command and
    are never stored, logged or posted publicly.
  * Uses the same read-only Silo key as the stats; no extra Silo access.
  * No extra Discord intents: members are looked up one at a time over REST.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

import discord
from discord import app_commands

from .silo import NamedID, SessionInfo, SiloError, clean_id

if TYPE_CHECKING:
    from .config import Config
    from .silo import SiloClient

log = logging.getLogger(__name__)

# Permissions a watching role must never carry: anything that would let the
# bot hand out moderator or admin powers by "giving someone the role".
DANGEROUS = discord.Permissions.elevated() | discord.Permissions(
    mention_everyone=True, view_audit_log=True, manage_nicknames=True, manage_events=True,
)
MANAGE_ROLES = discord.Permissions(manage_roles=True).value
BACKOFF = 600.0  # seconds to pause after Discord says "no permission"
ACCOUNTS_TTL = 60.0
MAX_LINKS = 500


@dataclass(frozen=True)
class Link:
    account: str  # Silo account ID
    profile: str | None = None  # Silo profile ID; None = any profile


class WatchingStore:
    """JSON file: links, which members Siloquy gave the role to, and which role."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.role_id: int | None = None
        self.links: dict[int, Link] = {}
        self.assigned: set[int] = set()

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            log.warning("Watching file unreadable; starting with no links")
            return
        if not isinstance(raw, dict):
            return
        rid = raw.get("role_id")
        self.role_id = rid if isinstance(rid, int) and not isinstance(rid, bool) else None
        links = raw.get("links") if isinstance(raw.get("links"), dict) else {}
        for key, val in links.items():
            if not (isinstance(key, str) and key.isdigit() and isinstance(val, dict)):
                continue
            account = clean_id(val.get("account"))
            profile = clean_id(val.get("profile")) if val.get("profile") is not None else None
            if account and (profile or val.get("profile") is None):
                self.links[int(key)] = Link(account, profile)
        assigned = raw.get("assigned") if isinstance(raw.get("assigned"), list) else []
        self.assigned = {
            int(v) for v in assigned if isinstance(v, int) and not isinstance(v, bool) and v > 0
        }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        data = json.dumps(
            {
                "role_id": self.role_id,
                "links": {
                    str(k): {"account": v.account, "profile": v.profile}
                    for k, v in sorted(self.links.items())
                },
                "assigned": sorted(self.assigned),
            },
            indent=2,
        )
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
        os.replace(tmp, self.path)


class WatchTracker:
    """Decides who counts as watching, with a grace period after playback ends.

    The grace period stops the role flickering off and on between episodes or
    during a brief reconnect.
    """

    def __init__(self, grace_seconds: float) -> None:
        self.grace = grace_seconds
        self.last_seen: dict[int, float] = {}

    def seed(self, member_ids: Iterable[int], now: float) -> None:
        """Members already holding the role keep it for one grace period."""
        for mid in member_ids:
            self.last_seen.setdefault(mid, now)

    def update(
        self, links: dict[int, Link], sessions: list[SessionInfo], now: float
    ) -> set[int]:
        accounts = {s.user_id for s in sessions if s.user_id}
        pairs = {(s.user_id, s.profile_id) for s in sessions if s.user_id}
        for mid, link in links.items():
            if link.profile is None:
                hit = link.account in accounts
            else:
                hit = (link.account, link.profile) in pairs
            if hit:
                self.last_seen[mid] = now
        watching: set[int] = set()
        for mid, seen in list(self.last_seen.items()):
            if mid in links and now - seen <= self.grace:
                watching.add(mid)
            else:
                del self.last_seen[mid]
        return watching


def role_problem(guild: discord.Guild, role_id: int) -> str | None:
    """Why this role can't safely be used, or None if it's fine."""
    role = guild.get_role(role_id)
    if role is None:
        return "WATCHING_ROLE_ID isn't a role in this server"
    if role.is_default():
        return "WATCHING_ROLE_ID can't be the @everyone role"
    if role.managed:
        return "the watching role is managed by a bot or integration; create a normal role"
    if role.permissions.value & DANGEROUS.value:
        bad = ", ".join(n for n, v in discord.Permissions(role.permissions.value & DANGEROUS.value) if v)
        return (
            f"the watching role has moderator/admin permissions ({bad}). Turn them off; "
            "the role only needs to be mentionable"
        )
    me = guild.me
    if me is None:
        return "Siloquy isn't in the server yet"
    if not me.guild_permissions.manage_roles:
        return "Siloquy's role needs the Manage Roles permission"
    if role >= me.top_role:
        return "drag Siloquy's role above the watching role in Server Settings -> Roles"
    return None


def exposed_roles(guild: discord.Guild, keep_id: int) -> int:
    """How many *other* roles with mod/admin powers sit below Siloquy's role.

    With Manage Roles, a leaked bot token could hand any of them out.
    """
    me = guild.me
    if me is None:
        return 0
    return sum(
        1
        for r in guild.roles
        if r < me.top_role
        and r.id != keep_id
        and not r.is_default()
        and not r.managed
        and r.permissions.value & DANGEROUS.value
    )


class WatchingRole:
    def __init__(self, cfg: Config, silo: SiloClient) -> None:
        self.role_id = cfg.watching_role_id
        self.guild_id = cfg.guild_id
        self.silo = silo
        self.store = WatchingStore(cfg.data_dir / "watching.json")
        self.tracker = WatchTracker(cfg.watching_grace_seconds)
        self._lock = asyncio.Lock()
        self._last_problem: str | None = None
        self._paused_until = 0.0
        self._skip_until: dict[int, float] = {}  # left the server / given by hand
        self._accounts: tuple[float, list[NamedID]] | None = None
        self._started = False

    @property
    def enabled(self) -> bool:
        return self.role_id is not None

    def load(self) -> None:
        self.store.load()

    def _save(self) -> None:
        try:
            self.store.save()
        except OSError:
            log.error("Could not save the watching links file in DATA_DIR")

    def _report(self, problem: str | None) -> None:
        if problem != self._last_problem:
            if problem:
                log.error("Watching role paused: %s", problem)
            elif self._last_problem:
                log.info("Watching role is working again")
            self._last_problem = problem

    # ----- startup ---------------------------------------------------------

    async def on_ready(self, guild: discord.Guild) -> None:
        async with self._lock:
            old = self.store.role_id
            if old is not None and old != self.role_id:
                await self._release_old_role(guild, old)
            if self.store.role_id != self.role_id:
                self.store.role_id = self.role_id
                if self.enabled or self.store.path.exists():
                    self._save()
            if not self.enabled:
                return
            self._report(role_problem(guild, self.role_id))
            if not self._started:
                self._started = True
                self.tracker.seed(self.store.assigned, time.monotonic())
                exposed = exposed_roles(guild, self.role_id)
                if exposed:
                    log.warning(
                        "Siloquy's role sits above %d other role(s) that have moderator/admin "
                        "permissions. With Manage Roles, a leaked bot token could hand those "
                        "out. Move Siloquy's role down to just above the watching role.",
                        exposed,
                    )
                log.info("Watching role enabled (%d linked member(s))", len(self.store.links))

    async def _release_old_role(self, guild: discord.Guild, old_id: int) -> None:
        """The role was changed or the feature turned off: take back the old role."""
        role = guild.get_role(old_id)
        taken = 0
        if role is not None:
            for mid in sorted(self.store.assigned):
                try:
                    member = await guild.fetch_member(mid)
                    if role in member.roles:
                        await member.remove_roles(role, reason="Siloquy: watching role changed")
                        taken += 1
                except discord.NotFound:
                    continue
                except (discord.HTTPException, discord.RateLimited):
                    log.warning(
                        "Could not remove the old watching role from a member; remove it by hand"
                    )
                    break
        if self.store.assigned:
            log.info("Removed the previous watching role from %d member(s)", taken)
        self.store.assigned.clear()
        self.tracker.last_seen.clear()
        self._save()

    # ----- each poll -------------------------------------------------------

    async def apply(self, guild: discord.Guild, sessions: list[SessionInfo] | None) -> None:
        """Bring role holders in line with who is watching right now.

        ``sessions`` is None when Silo couldn't be read; roles are then left as
        they are, rather than stripping everyone during an outage.
        """
        # Wait for on_ready, which seeds the grace period for current holders.
        if not self.enabled or sessions is None or not self._started:
            return
        async with self._lock:
            now = time.monotonic()
            watching = self.tracker.update(self.store.links, sessions, now)
            problem = role_problem(guild, self.role_id)
            self._report(problem)
            if problem or now < self._paused_until:
                return
            role = guild.get_role(self.role_id)
            added = removed = 0
            for mid in sorted(watching - self.store.assigned):
                if self._skip_until.get(mid, 0.0) > now:
                    continue
                result = await self._give(guild, role, mid, now)
                if result is None:
                    return
                added += result
            for mid in sorted(self.store.assigned - watching):
                result = await self._take(guild, role, mid, "stopped watching")
                if result is None:
                    return
                removed += result
            # Forget skips for members who are no longer watching.
            for mid in [m for m in self._skip_until if m not in watching]:
                del self._skip_until[mid]
            if added or removed:
                log.info(
                    "Watching role: +%d -%d (%d watching now)",
                    added, removed, len(self.store.assigned),
                )

    async def _give(self, guild: discord.Guild, role: discord.Role, mid: int, now: float) -> int | None:
        """1 if the role was added, 0 if skipped, None to stop this round."""
        try:
            member = await guild.fetch_member(mid)
        except discord.NotFound:
            self._skip_until[mid] = now + BACKOFF
            log.info("A linked member is no longer in the server; use /watching unlink")
            return 0
        except (discord.HTTPException, discord.RateLimited) as exc:
            return self._discord_error(exc)
        if role in member.roles:
            # Given by hand: never track it, so Siloquy never takes it away.
            self._skip_until[mid] = now + BACKOFF
            return 0
        try:
            await member.add_roles(role, reason="Siloquy: watching on Silo")
        except (discord.HTTPException, discord.RateLimited) as exc:
            return self._discord_error(exc)
        self.store.assigned.add(mid)
        self._save()
        return 1

    async def _take(self, guild: discord.Guild, role: discord.Role, mid: int, why: str) -> int | None:
        try:
            member = await guild.fetch_member(mid)
            if role in member.roles:
                await member.remove_roles(role, reason=f"Siloquy: {why}")
        except discord.NotFound:
            pass  # left the server; nothing to remove
        except (discord.HTTPException, discord.RateLimited) as exc:
            return self._discord_error(exc)
        self.store.assigned.discard(mid)
        self.tracker.last_seen.pop(mid, None)
        self._save()
        return 1

    def _discord_error(self, exc: Exception) -> None:
        if isinstance(exc, discord.Forbidden):
            self._paused_until = time.monotonic() + BACKOFF
            log.error(
                "Discord refused a role change. Give Siloquy's role Manage Roles and drag it "
                "above the watching role. Retrying in 10 minutes."
            )
        elif isinstance(exc, discord.RateLimited):
            log.info("Discord rate limit on role changes; retrying next poll")
        else:
            log.warning("Discord role change failed (HTTP %s); retrying next poll",
                        getattr(exc, "status", "?"))
        return None

    # ----- used by /watching ----------------------------------------------

    async def accounts(self) -> list[NamedID]:
        now = time.monotonic()
        if self._accounts and now - self._accounts[0] < ACCOUNTS_TTL:
            return self._accounts[1]
        accounts = await self.silo.accounts()
        self._accounts = (now, accounts)
        return accounts

    async def set_link(self, member_id: int, link: Link) -> None:
        async with self._lock:
            self.store.links[member_id] = link
            self._skip_until.pop(member_id, None)
            self._save()

    async def remove_link(self, guild: discord.Guild, member_id: int) -> bool:
        async with self._lock:
            existed = self.store.links.pop(member_id, None) is not None
            self.tracker.last_seen.pop(member_id, None)
            self._skip_until.pop(member_id, None)
            if member_id in self.store.assigned:
                role = guild.get_role(self.role_id) if self.role_id else None
                if role is not None:
                    await self._take(guild, role, member_id, "unlinked")
                else:
                    self.store.assigned.discard(member_id)
            self._save()
            return existed


def resolve(items: list[NamedID], value: str) -> NamedID | None:
    """Match an autocomplete pick (an ID) or a typed name (exact, any case)."""
    value = value.strip()
    for item in items:
        if item.id == value:
            return item
    matches = [i for i in items if i.name.lower() == value.lower()]
    return matches[0] if len(matches) == 1 else None


def choices(items: list[NamedID], current: str) -> list[app_commands.Choice[str]]:
    cur = current.strip().lower()
    hits = [i for i in items if cur in i.name.lower()]
    hits.sort(key=lambda i: (not i.name.lower().startswith(cur), i.name.lower()))
    return [app_commands.Choice(name=i.name[:100], value=i.id) for i in hits[:25]]


def _safe(text: str) -> str:
    return discord.utils.escape_mentions(discord.utils.escape_markdown(text))


class WatchingCommands(app_commands.Group):
    """/watching link | unlink | list. Admin-only, answers only the admin."""

    def __init__(self, manager: WatchingRole) -> None:
        super().__init__(
            name="watching",
            description="Link Discord members to Silo accounts for the watching role",
            guild_only=True,
            default_permissions=discord.Permissions(manage_roles=True),
        )
        self.m = manager

    def _allowed(self, interaction: discord.Interaction) -> bool:
        return (
            interaction.guild_id == self.m.guild_id
            and interaction.guild is not None
            and interaction.permissions.manage_roles
        )

    async def _deny(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            "You need the Manage Roles permission to use this.", ephemeral=True
        )

    @app_commands.command(name="link", description="Give a member the watching role while a Silo account plays")
    @app_commands.describe(
        member="The Discord member",
        account="Their Silo account",
        profile="Only this profile on the account (leave empty for any profile)",
    )
    async def link(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        account: str,
        profile: str | None = None,
    ) -> None:
        if not self._allowed(interaction):
            return await self._deny(interaction)
        if member.bot:
            return await interaction.response.send_message("Bots can't be linked.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        if member.id not in self.m.store.links and len(self.m.store.links) >= MAX_LINKS:
            return await interaction.followup.send(f"There's a limit of {MAX_LINKS} links.", ephemeral=True)
        try:
            acct = resolve(await self.m.accounts(), account)
            if acct is None:
                return await interaction.followup.send(
                    "No Silo account matches that. Pick one from the list as you type.", ephemeral=True
                )
            prof = None
            if profile:
                prof = resolve(await self.m.silo.profiles(acct.id), profile)
                if prof is None:
                    return await interaction.followup.send(
                        f"**{_safe(acct.name)}** has no profile like that. Pick one from the list, "
                        "or leave it empty for any profile.", ephemeral=True,
                    )
        except SiloError:
            return await interaction.followup.send(
                "Couldn't reach Silo just now. Try again in a moment.", ephemeral=True
            )
        await self.m.set_link(member.id, Link(acct.id, prof.id if prof else None))
        which = f"profile **{_safe(prof.name)}**" if prof else "any profile"
        log.info("Watching link saved (%d linked)", len(self.m.store.links))
        await interaction.followup.send(
            f"Linked {member.mention} to Silo account **{_safe(acct.name)}** ({which}). "
            "They'll get the watching role the next time it plays.",
            ephemeral=True,
        )

    @link.autocomplete("account")
    async def _account_ac(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        if not self._allowed(interaction):
            return []
        try:
            return choices(await self.m.accounts(), current)
        except SiloError:
            return []

    @link.autocomplete("profile")
    async def _profile_ac(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        if not self._allowed(interaction):
            return []
        picked = getattr(interaction.namespace, "account", None)
        if not isinstance(picked, str) or not picked:
            return []
        try:
            acct = resolve(await self.m.accounts(), picked)
            if acct is None:
                return []
            return choices(await self.m.silo.profiles(acct.id), current)
        except SiloError:
            return []

    @app_commands.command(name="unlink", description="Stop giving a member the watching role")
    @app_commands.describe(member="The Discord member")
    async def unlink(self, interaction: discord.Interaction, member: discord.Member) -> None:
        if not self._allowed(interaction):
            return await self._deny(interaction)
        await interaction.response.defer(ephemeral=True, thinking=True)
        existed = await self.m.remove_link(interaction.guild, member.id)
        if existed:
            log.info("Watching link removed (%d linked)", len(self.m.store.links))
        await interaction.followup.send(
            f"Unlinked {member.mention}." if existed else f"{member.mention} wasn't linked.",
            ephemeral=True,
        )

    @app_commands.command(name="list", description="Show who is linked to which Silo account")
    async def list_links(self, interaction: discord.Interaction) -> None:
        if not self._allowed(interaction):
            return await self._deny(interaction)
        await interaction.response.defer(ephemeral=True, thinking=True)
        await interaction.followup.send(await self.render_list(interaction.guild), ephemeral=True)

    async def render_list(self, guild: discord.Guild) -> str:
        links = dict(self.m.store.links)
        lines: list[str] = []
        problem = role_problem(guild, self.m.role_id) if self.m.role_id else "feature is off"
        if problem:
            lines.append(f"⚠️ Watching role isn't working: {problem}.")
        if not links:
            lines.append("No one is linked yet. Use `/watching link`.")
            return "\n".join(lines)
        names: dict[str, str] = {}
        profile_names: dict[tuple[str, str], str] = {}
        try:
            names = {a.id: a.name for a in await self.m.accounts()}
            for acct in sorted({l.account for l in links.values() if l.profile}):
                for p in await self.m.silo.profiles(acct):
                    profile_names[(acct, p.id)] = p.name
        except SiloError:
            lines.append("(Couldn't reach Silo, so some names are missing.)")
        lines.append(f"**{len(links)} linked** (🟢 = has the role now)")
        for mid, link in sorted(links.items(), key=lambda kv: names.get(kv[1].account, "").lower()):
            acct = _safe(names.get(link.account, f"account {link.account}"))
            if link.profile:
                prof = _safe(profile_names.get((link.account, link.profile), "a removed profile"))
                which = f" · {prof}"
            else:
                which = ""
            dot = "🟢 " if mid in self.m.store.assigned else ""
            lines.append(f"{dot}<@{mid}> → **{acct}**{which}")
        out, used = [], 0
        for i, line in enumerate(lines):
            if used + len(line) + 1 > 1900:
                out.append(f"…and {len(lines) - i} more")
                break
            out.append(line)
            used += len(line) + 1
        return "\n".join(out)