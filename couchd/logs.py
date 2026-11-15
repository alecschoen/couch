"""Logging setup.

Under systemd, stdout goes to the journal. We prefix each line with `<N>` (the syslog
priority), which journald reads to set the entry's level, so `journalctl -p warning` works.
Extra fields passed with `extra={...}` are appended as `key=value`.

    log.info("output switched", extra={"output": "bluetooth"})
    # journal: couchd.audio: output switched output=bluetooth
"""

import logging
import os
import sys
import time

# Attributes every LogRecord has; anything else came in through `extra`.
_STANDARD = set(vars(logging.makeLogRecord({}))) | {"message", "asctime", "taskName"}

_PRIORITY = {
    logging.CRITICAL: 2,
    logging.ERROR: 3,
    logging.WARNING: 4,
    logging.INFO: 6,
    logging.DEBUG: 7,
}


def _fields(record: logging.LogRecord) -> str:
    extras = {k: v for k, v in vars(record).items() if k not in _STANDARD}
    return "".join(f" {k}={_quote(v)}" for k, v in extras.items())


def _quote(value: object) -> str:
    text = str(value)
    return repr(text) if not text or any(c in text for c in " =\"'\n") else text


class JournalFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        priority = _PRIORITY.get(record.levelno, 6)
        text = f"{record.name}: {record.getMessage()}{_fields(record)}"
        if record.exc_info:
            text += "\n" + self.formatException(record.exc_info)
        # journald reads the prefix per line, so tracebacks keep the record's priority.
        return "\n".join(f"<{priority}>{line}" for line in text.splitlines())


class ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        stamp = time.strftime("%H:%M:%S", time.localtime(record.created))
        text = f"{stamp} {record.levelname:<7} {record.name}: {record.getMessage()}"
        text += _fields(record)
        if record.exc_info:
            text += "\n" + self.formatException(record.exc_info)
        return text


def under_journald(stream=None) -> bool:
    """True when `stream` (stdout) really is the journal, not just a child of something that was.

    systemd sets JOURNAL_STREAM to "device:inode" of the journal socket; desktop sessions leak
    it into terminals, so it has to be compared with what stdout actually is.
    """
    expected = os.environ.get("JOURNAL_STREAM", "")
    try:
        st = os.fstat((stream or sys.stdout).fileno())
    except (OSError, ValueError, AttributeError):
        return False
    return expected == f"{st.st_dev}:{st.st_ino}"


def setup_logging(level: str = "info") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JournalFormatter() if under_journald() else ConsoleFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # aiohttp's access log is noise on a TV; errors still come through.
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
