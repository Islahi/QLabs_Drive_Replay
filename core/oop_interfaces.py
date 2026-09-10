"""Application-level abstractions used by the recorder and replay system.

The module makes the four OOP principles explicit:

* Encapsulation: concrete classes keep implementation state private to the class.
* Abstraction: LocationSource and ReplaySink expose small stable contracts.
* Inheritance: concrete sources/sinks inherit these abstract base classes.
* Polymorphism: recorder/replay controllers work with the abstractions rather
  than concrete QLabs, map, or video implementations.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.replay_core import PoseSample, SessionData


@dataclass(frozen=True)
class LocationReading:
    """One valid QCar pose measurement from a location source.

    Rotation fields are optional so existing/mock XYZ-only sources remain
    compatible. Recorder code stores them when the concrete source provides
    them.
    """

    x: float
    y: float
    z: float
    roll_rad: float | None = None
    pitch_rad: float | None = None
    yaw_rad: float | None = None


class LocationSource(ABC):
    """Abstract source of timestamp-independent XYZ measurements.

    RecorderWorker owns the timing. A source only knows how to connect, read a
    position, and close. This keeps the recorder independent of Quanser QLabs
    and makes a future LSL or file-backed source possible without rewriting the
    recording loop.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable source name."""
        raise NotImplementedError

    @property
    @abstractmethod
    def metadata(self) -> dict:
        """Serializable metadata describing the concrete source."""
        raise NotImplementedError

    @abstractmethod
    def connect(self) -> None:
        """Connect and validate that the source is ready."""
        raise NotImplementedError

    @abstractmethod
    def read_position(self) -> LocationReading | None:
        """Return a valid pose sample, or None when this sample is unavailable."""
        raise NotImplementedError

    @abstractmethod
    def close(self) -> None:
        """Release all external resources."""
        raise NotImplementedError


class ReplaySink(ABC):
    """Abstract output synchronized by ReplayCoordinator.

    The coordinator sends the same session time/pose/playback state to every
    sink. A sink can render a map marker, move a QLabs QCar, synchronize a
    video window, or later consume LSL-related replay state.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable sink name used in diagnostics."""
        raise NotImplementedError

    @abstractmethod
    def set_session(self, session: "SessionData | None") -> None:
        """Notify the sink that the active drive session changed."""
        raise NotImplementedError

    @abstractmethod
    def update_time(self, time_s: float, pose: "PoseSample") -> None:
        """Synchronize the sink to a replay time and interpolated pose."""
        raise NotImplementedError

    @abstractmethod
    def set_playing(self, playing: bool) -> None:
        """Notify the sink whether the master clock is playing."""
        raise NotImplementedError

    @abstractmethod
    def set_rate(self, rate: float) -> None:
        """Notify the sink about master playback-rate changes."""
        raise NotImplementedError

    def force_sync(self) -> None:
        """Request that the next update bypass optional sink rate limiting."""
        # Most sinks do not rate-limit, so the default implementation is a no-op.

    @abstractmethod
    def close(self) -> None:
        """Release sink resources."""
        raise NotImplementedError
