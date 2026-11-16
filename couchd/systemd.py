"""Tiny sd_notify client: readiness, stopping, and the watchdog. No libsystemd needed.

Plus `powering_off()`, so the TV can follow the Pi into standby on shutdown but not on a reboot.
"""

import asyncio
import logging
import os
import socket

log = logging.getLogger(__name__)


def notify(message: str) -> bool:
    """Send `message` (e.g. "READY=1") to systemd. False when not run under a notify unit."""
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return False
    if address.startswith("@"):
        address = "\0" + address[1:]  # abstract namespace
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_CLOEXEC) as sock:
            sock.connect(address)
            sock.sendall(message.encode())
    except OSError as e:
        log.warning("sd_notify failed", extra={"error": str(e)})
        return False
    return True


def watchdog_interval() -> float | None:
    """Seconds between pings (half the unit's WatchdogSec), or None if no watchdog is set."""
    usec = os.environ.get("WATCHDOG_USEC")
    if not usec:
        return None
    pid = os.environ.get("WATCHDOG_PID")
    if pid and pid != str(os.getpid()):
        return None
    try:
        return int(usec) / 1_000_000 / 2
    except ValueError:
        return None


async def watchdog_loop(interval: float) -> None:
    """Ping from the event loop itself, so a stuck loop is what trips the watchdog."""
    while True:
        notify("WATCHDOG=1")
        await asyncio.sleep(interval)


# Jobs systemd queues when the machine is turning off for good (not rebooting).
_POWEROFF_TARGETS = ("poweroff.target", "halt.target")


async def powering_off() -> bool:
    """True while systemd is shutting the machine down (not rebooting). False if unsure."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "systemctl",
            "list-jobs",
            "--no-legend",
            "--plain",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        return False
    try:
        async with asyncio.timeout(2.0):
            out, _ = await proc.communicate()
    except TimeoutError:
        proc.kill()
        return False
    return jobs_power_off(out.decode(errors="replace"))


def jobs_power_off(list_jobs: str) -> bool:
    """Whether `systemctl list-jobs --no-legend --plain` output has a power-off job queued."""
    for line in list_jobs.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[1] in _POWEROFF_TARGETS:
            return True
    return False
