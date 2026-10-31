"""A one-shot signal that the person already finished the page check."""

from __future__ import annotations

import threading


class UserConfirmation:
    def __init__(self) -> None:
        self._event = threading.Event()

    def confirm(self) -> None:
        self._event.set()

    def is_set(self) -> bool:
        return self._event.is_set()

    def consume(self) -> bool:
        if not self._event.is_set():
            return False
        self._event.clear()
        return True

    def clear(self) -> None:
        self._event.clear()


user_confirmation = UserConfirmation()
