"""Only six navigation keys; activation repeats never become second toggles."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from typing import Protocol

# linux input codes, deliberately not an arbitrary keyboard capability.
KEYS = {0x00: 28, 0x01: 103, 0x02: 108, 0x03: 105, 0x04: 106, 0x0D: 1}
DIRECTION_KEYS = {103, 108, 105, 106}
FRAME = re.compile(r"(?:^|\s)>>\s+([0-9a-fA-F]{2}(?::[0-9a-fA-F]{2})+)\s*$")


class Keyboard(Protocol):
    def emit(self, key: int, value: int) -> None: ...
    def close(self) -> None: ...


class EvdevKeyboard:
    def __init__(self) -> None:
        from evdev import UInput, ecodes

        self._device = UInput(
            {ecodes.EV_KEY: sorted(set(KEYS.values()))}, name="aOS CEC navigation"
        )

    def emit(self, key: int, value: int) -> None:
        self._device.write(1, key, value)
        self._device.syn()

    def close(self) -> None:
        self._device.close()


def incoming(line: str) -> list[int] | None:
    match = FRAME.search(line)
    return [int(part, 16) for part in match[1].split(":")] if match else None


class Navigation:
    def __init__(
        self,
        keyboard: Keyboard,
        logical_address: int = 4,
        clock: Callable[[], float] = time.monotonic,
        release_s: float = 0.8,
    ) -> None:
        self.keyboard = keyboard
        self.logical_address = logical_address
        self.clock = clock
        self.release_s = release_s
        self.held: int | None = None
        self.last_seen = 0.0
        self.last_repeat = 0.0
        self.pressed_at = 0.0

    def feed(self, line: str) -> None:
        frame = incoming(line)
        # TV-origin frames addressed to this player only: no tx echo or broadcasts.
        if not frame or frame[0] != self.logical_address:
            return
        if frame[1:] == [0x45]:
            self.release()
        elif len(frame) == 3 and frame[1] == 0x44:
            key = KEYS.get(frame[2])
            if key is None:
                self.release()
                return
            now = self.clock()
            self.last_seen = now
            if self.held != key:
                self.release()
                self.keyboard.emit(key, 1)
                self.held = key
                self.pressed_at = self.last_repeat = now
            elif key in DIRECTION_KEYS and now - self.pressed_at >= 0.35:
                if now - self.last_repeat >= 0.12:
                    self.keyboard.emit(key, 2)
                    self.last_repeat = now

    def expire(self) -> None:
        if self.held is not None and self.clock() - self.last_seen >= self.release_s:
            self.release()

    def release(self) -> None:
        if self.held is not None:
            self.keyboard.emit(self.held, 0)
            self.held = None
