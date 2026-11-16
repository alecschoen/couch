"""Stands in for Chromium in cage-check (#23). It maps a white window, as Chromium's first
frames are, and keeps drawing on every frame callback, like a page's requestAnimationFrame.
After DARK_AFTER seconds it draws #121214, as if the launcher had painted. Every second it
prints how many frame callbacks it got, to show that it isn't starved while the splash
covers it."""

import importlib.machinery
import importlib.util
import os
import sys
import time

loader = importlib.machinery.SourceFileLoader("couch_splash", "/couch/system/couch-splash")
spec = importlib.util.spec_from_loader(loader.name, loader)
cs = importlib.util.module_from_spec(spec)
loader.exec_module(cs)

DARK_AFTER = float(sys.argv[1]) if len(sys.argv) > 1 else 2.0
XRGB8888 = 1


class FakeBrowser(cs.Splash):
    def __init__(self) -> None:
        self.callbacks = 0
        self.callback = None
        self.dark = None
        super().__init__(pixels_for=lambda w, h: b"\xff" * (w * h * 4))

    def bind(self) -> None:
        self.compositor = self.bind_global("wl_compositor", 4)
        self.shm = self.bind_global("wl_shm", 1)
        self.wm_base = self.bind_global("xdg_wm_base", 1)
        self.presentation = self.bind_global("wp_presentation", 1)
        self.top, self.spare = self.make_window(), None

    def covered(self) -> None:
        pass

    def map(self, window) -> None:
        super().map(window)
        self.next_frame()

    def next_frame(self) -> None:
        self.callback = self.new_id()
        self.send(self.top.surface, 3, self.callback)  # wl_surface.frame
        self.send(self.top.surface, 6)

    def feed(self, obj, opcode, body) -> None:
        if obj != self.callback:
            return super().feed(obj, opcode, body)
        self.callbacks += 1
        w, h = self.buffer_size
        if self.dark is None and self.clock() - self.started > DARK_AFTER:
            pixels = bytes((0x14, 0x12, 0x12, 0xFF)) * (w * h)
            fd = os.memfd_create("dark")
            os.write(fd, pixels)
            pool, self.dark = self.new_id(), self.new_id()
            self.send(self.shm, 0, pool, len(pixels), fd=fd)
            self.send(pool, 0, self.dark, 0, w, h, w * 4, XRGB8888)
            cs.log("fake browser: the page painted")
        if self.dark:
            self.send(self.top.surface, 1, self.dark, 0, 0)
        self.send(self.top.surface, 2, 0, 0, w, h)
        self.next_frame()
        return None


sock = cs.connect()
fake = FakeBrowser()
cs.flush(sock, fake)
sock.settimeout(0.1)
buf, last, seen = b"", time.monotonic(), 0
while True:
    try:
        data = sock.recv(65536)
        if not data:
            break
        messages, buf = cs.split_messages(buf + data)
        for m in messages:
            fake.feed(*m)
        cs.flush(sock, fake)
    except TimeoutError:
        pass
    if time.monotonic() - last >= 1:
        cs.log("fake browser: frame callbacks", per_second=fake.callbacks - seen)
        seen, last = fake.callbacks, time.monotonic()
