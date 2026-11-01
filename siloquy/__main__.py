"""Entry point: ``python -m siloquy``."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

from . import __version__
from .bot import Siloquy
from .config import ConfigError, load_config
from .silo import SiloClient


class RedactingFilter(logging.Filter):
    """Last line of defence: scrub secret values from every log record."""

    def __init__(self, secrets: tuple[str, ...]) -> None:
        super().__init__()
        self._secrets = tuple(s for s in secrets if len(s) >= 8)

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if record.exc_info:
            msg += "\n" + logging.Formatter().formatException(record.exc_info)
            record.exc_info = None
            record.exc_text = None
        for secret in self._secrets:
            msg = msg.replace(secret, "***")
        record.msg, record.args = msg, None
        return True


def _setup_logging(level: str, secrets: tuple[str, ...]) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s"))
    handler.addFilter(RedactingFilter(secrets))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # Library debug output can include raw gateway/HTTP payloads; keep it quiet.
    for noisy in ("discord", "aiohttp"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


async def _run() -> int:
    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    _setup_logging(cfg.log_level, cfg.secrets)
    log = logging.getLogger("siloquy")
    log.info(
        "Siloquy %s starting: silo=%s guild=%s stats=%s",
        __version__, cfg.silo_url, cfg.guild_id, ",".join(cfg.stats),
    )

    async with SiloClient(cfg.silo_url, cfg.silo_api_key) as silo:
        client = Siloquy(cfg, silo)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(client.close()))
        async with client:
            try:
                await client.start(cfg.discord_token.reveal())
            except Exception as exc:
                # discord.LoginFailure etc. Type only; never the token.
                log.critical("Discord connection failed (%s)", type(exc).__name__)
                return 1
        return client.exit_code


def main() -> None:
    sys.exit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
