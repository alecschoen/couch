"""Commands the UI (or curl) can send: `audio.set_output`, `bluetooth.pair`, `remote.press`...

Services register handlers while they're running. A handler takes the `args` object and
returns something JSON-ready (or None). Bad input and hardware failures come back to the
caller as an error message, never as a crash.
"""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from couchd.backends import BackendError

log = logging.getLogger(__name__)

Handler = Callable[[dict[str, Any]], Awaitable[Any]]


class CommandError(Exception):
    pass


class Commands:
    def __init__(self) -> None:
        self._handlers: dict[str, Handler] = {}

    def add(self, name: str, handler: Handler) -> None:
        if name in self._handlers:
            raise ValueError(f"command {name!r} already registered")
        self._handlers[name] = handler

    def remove_prefix(self, prefix: str) -> None:
        for name in [n for n in self._handlers if n.startswith(prefix)]:
            del self._handlers[name]

    def names(self) -> list[str]:
        return sorted(self._handlers)

    async def run(self, name: object, args: object = None) -> Any:
        if not isinstance(name, str) or name not in self._handlers:
            raise CommandError(f"unknown command {name!r}")
        if args is None:
            args = {}
        if not isinstance(args, dict):
            raise CommandError("args must be an object")
        log.debug("command", extra={"command": name})
        try:
            return await self._handlers[name](args)
        except BackendError as e:
            raise CommandError(str(e)) from e


_TYPE_NAMES = {str: "a string", int: "an integer", bool: "true or false"}


def arg[T](args: dict[str, Any], key: str, type_: type[T]) -> T:
    """args[key], checked to be a `type_`. Raises CommandError otherwise."""
    value = args.get(key)
    ok = isinstance(value, type_) and not (type_ is int and isinstance(value, bool))
    if not ok:
        raise CommandError(f"'{key}' must be {_TYPE_NAMES.get(type_, type_.__name__)}")
    return value
