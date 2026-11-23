"""The interface both video players share, and the ffpyplayer one.

    play(path, mute=...)   start a video (mute: a paired voice note plays instead)
    stop()                 stop it and release the player
    pause(paused=None)     pause / resume (None toggles)
    poll() -> [events]     what happened since the last call: STARTED, ENDED, FAILED
    active                 whether a video is loaded

ffpyplayer hands its frames to pygame, so the viewer keeps pulling them with
get_frame() exactly as before; mpv draws on the screen itself (app/video/mpv.py).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum
from pathlib import Path
from typing import Any, Callable


class VideoEvent(str, Enum):
    STARTED = "started"   # the first frame is on screen
    ENDED = "ended"       # played to the end
    FAILED = "failed"     # the player gave up or had to be killed


class VideoBackend(ABC):
    name: str = "?"

    @abstractmethod
    def play(self, path: Path | str, *, mute: bool = False) -> None: ...

    @abstractmethod
    def stop(self) -> None: ...

    @abstractmethod
    def pause(self, paused: bool | None = None) -> None: ...

    @abstractmethod
    def poll(self) -> list[VideoEvent]: ...

    @property
    @abstractmethod
    def active(self) -> bool: ...


def _media_player_class():
    from ffpyplayer.player import MediaPlayer
    return MediaPlayer


class FfpyplayerBackend(VideoBackend):
    """ffpyplayer's MediaPlayer, opened with the same ff_opts as always (from the HAL)."""

    name = "ffpyplayer"

    def __init__(self, ff_opts_for: Callable[[bool], dict], player_factory: Callable[..., Any] | None = None):
        self._ff_opts_for = ff_opts_for
        self._factory = player_factory
        self._player = None
        self._events: list[VideoEvent] = []
        self._started = False
        self._ended = False

    def play(self, path: Path | str, *, mute: bool = False) -> None:
        self.stop()
        factory = self._factory or _media_player_class()
        self._player = factory(str(path), ff_opts=self._ff_opts_for(mute))
        self._events, self._started, self._ended = [], False, False

    def get_frame(self):
        """MediaPlayer.get_frame(), passed through: (frame, val), val 'eof' at the end."""
        frame, val = self._player.get_frame()
        if frame is not None and not self._started:
            self._started = True
            self._events.append(VideoEvent.STARTED)
        if val == 'eof' and not self._ended:
            self._ended = True
            self._events.append(VideoEvent.ENDED)
        return frame, val

    def stop(self) -> None:
        player, self._player = self._player, None
        if player is not None:
            player.close_player()

    # The viewer has always called close_player() on the player it holds
    close_player = stop

    def pause(self, paused: bool | None = None) -> None:
        if self._player is None:
            return
        if paused is None:
            self._player.toggle_pause()
        else:
            self._player.set_pause(paused)

    def poll(self) -> list[VideoEvent]:
        events, self._events = self._events, []
        return events

    @property
    def active(self) -> bool:
        return self._player is not None
