"""Turn Silo data into channel names.

Channel names only ever contain aggregate numbers and fixed labels. No
usernames, titles, IPs or device names can reach Discord from here.
"""

from __future__ import annotations

from dataclasses import dataclass

from .silo import Resources, SessionInfo

STATUS_ONLINE = "online"
STATUS_DEGRADED = "degraded"
STATUS_OFFLINE = "offline"

DASH = "—"

# stat key -> which category it lives in
CATEGORY_OF: dict[str, str] = {
    "status": "stats",
    "streams": "stats",
    "transcodes": "stats",
    "direct": "stats",
    "bandwidth": "stats",
    "users": "performance",
    "cpu": "performance",
    "memory": "performance",
    "storage": "performance",
}


SCOPE_SILO = "silo"
SCOPE_HOST = "host"


@dataclass
class Snapshot:
    status: str
    sessions: list[SessionInfo] | None
    resources: Resources | None
    users: int | None
    silo_cpu_pct: float | None = None  # from SiloCpuTracker


class SiloCpuTracker:
    """Turns Silo's cumulative CPU seconds into a % of the whole machine.

    Uses Silo's own sample timestamps, so our polling rhythm doesn't skew it.
    Needs two samples, so the first reading after a start is None.
    """

    def __init__(self) -> None:
        self._prev: tuple[float, float] | None = None  # (sampled_at, cpu_seconds)
        self._last: float | None = None

    def update(self, r: Resources | None) -> float | None:
        if r is None or r.silo_cpu_seconds is None or r.sampled_at is None or not r.cores:
            return None
        now = (r.sampled_at, r.silo_cpu_seconds)
        prev, self._prev = self._prev, now
        if prev is None:
            return None
        dt = now[0] - prev[0]
        if dt <= 0:
            # Silo hasn't taken a new sample since last poll.
            if dt < 0:
                self._last = None
            else:
                self._prev = prev
            return self._last
        used = now[1] - prev[1]
        if used < 0:
            # Silo restarted, or transcoders exited and took their CPU time
            # with them. Keep showing the last value; measure from here.
            return self._last
        self._last = max(0.0, min(100.0, 100.0 * used / dt / r.cores))
        return self._last


def fmt_bitrate(kbps: int) -> str:
    if kbps >= 1_000_000:
        return f"{kbps / 1_000_000:.2f} Gbps"
    if kbps >= 10_000:
        return f"{kbps / 1000:.0f} Mbps"
    return f"{kbps / 1000:.1f} Mbps"


def step5(pct: float) -> int:
    """Round to the nearest 5% so small wobbles don't use up Discord renames."""
    return int(5 * round(pct / 5))


def fmt_storage(used_gb: float, total_gb: float) -> str:
    if total_gb >= 1000:
        return f"{used_gb / 1000:.0f}/{total_gb / 1000:.0f} TB"
    return f"{used_gb:.0f}/{total_gb:.0f} GB"


def fmt_pct(pct: float) -> str:
    if pct < 0.5:
        return "0%"
    if pct < 2.5:
        return "<5%"
    return f"{step5(pct)}%"


def fmt_gb(n_bytes: int) -> str:
    gb = n_bytes / 1024**3
    if gb < 1:
        return f"{max(0.1, round(gb, 1)):.1f} GB"
    return f"{round(gb * 2) / 2:g} GB"  # nearest 0.5 GB keeps renames rare


def render(snap: Snapshot, scope: str = SCOPE_SILO) -> dict[str, str]:
    """Return {stat_key: channel_name} for every known stat."""
    s = snap.sessions
    r = snap.resources
    out: dict[str, str] = {}

    out["status"] = {
        STATUS_ONLINE: "🟢 Silo: Online",
        STATUS_DEGRADED: "🟡 Silo: Degraded",
        STATUS_OFFLINE: "🔴 Silo: Offline",
    }[snap.status]

    if s is None:
        streams = transcodes = direct = bandwidth = DASH
    else:
        paused = sum(1 for x in s if x.paused)
        streams = f"{len(s)}" + (f" ({paused} ⏸)" if paused else "")
        transcodes = str(sum(1 for x in s if x.method == "transcode"))
        direct = str(sum(1 for x in s if x.method in ("direct", "remux", "direct_stream")))
        bandwidth = fmt_bitrate(sum(x.bitrate_kbps for x in s if x.remote and not x.paused))

    out["streams"] = f"▶️ Streams: {streams}"
    out["transcodes"] = f"🔥 Transcodes: {transcodes}"
    out["direct"] = f"⚡ Direct Play: {direct}"
    out["bandwidth"] = f"🌐 Remote: {bandwidth}"

    out["users"] = f"👥 Users: {snap.users if snap.users is not None else DASH}"

    if scope == SCOPE_HOST:
        cpu = fmt_pct(r.cpu_pct) if r and r.cpu_pct is not None else DASH
        out["cpu"] = f"🧠 Host CPU: {cpu}"
        if r and r.mem_used_mb is not None and r.mem_total_mb:
            mem = f"{step5(100 * r.mem_used_mb / r.mem_total_mb)}% of {r.mem_total_mb / 1024:.0f} GB"
        else:
            mem = DASH
        out["memory"] = f"🐏 Host RAM: {mem}"
    else:
        if snap.silo_cpu_pct is not None:
            out["cpu"] = f"🧠 Silo CPU: {fmt_pct(snap.silo_cpu_pct)}"
        elif not (r and r.silo_cpu_seconds is not None):
            out["cpu"] = f"🧠 Silo CPU: {DASH}"
        # else: still warming up (needs two samples): leave the channel as is.
        mem = fmt_gb(r.silo_rss_bytes) if r and r.silo_rss_bytes is not None else DASH
        out["memory"] = f"🐏 Silo RAM: {mem}"

    if r and r.storage_used_gb is not None and r.storage_total_gb:
        storage = fmt_storage(r.storage_used_gb, r.storage_total_gb)
    else:
        storage = DASH
    out["storage"] = f"💾 Storage: {storage}"

    # Discord's hard limit is 100 characters.
    return {k: v[:100] for k, v in out.items()}
