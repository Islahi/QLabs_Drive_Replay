"""QLabs QCar transform-based replay sink.

This concrete class inherits ReplaySink, so ReplayCoordinator can synchronize
it through the same interface used for map and video outputs.
"""

from __future__ import annotations

import time

from core.oop_interfaces import ReplaySink
from core.replay_core import PoseSample, SessionData


class QLabsReplaySink(ReplaySink):
    """Replay a recorded pose as a non-dynamic QCar2 actor in QLabs."""

    def __init__(self, minimum_update_period_s: float = 0.05) -> None:
        self._qlabs = None
        self._qcar = None
        self._host: str | None = None
        self._actor_number: int | None = None
        self._spawned_by_us = False
        self._minimum_update_period_s = max(0.0, float(minimum_update_period_s))
        self._last_send_wall = 0.0
        self._force_next = True
        self._playing = False
        self._session: SessionData | None = None

    @property
    def name(self) -> str:
        return "QLabs"

    @property
    def connected(self) -> bool:
        return self._qlabs is not None and self._qcar is not None

    @property
    def actor_number(self) -> int | None:
        return self._actor_number

    def connect(
        self,
        host: str,
        actor_number: int,
        initial_location: list[float],
        initial_yaw_rad: float,
    ) -> str:
        self.disconnect(destroy_spawned=True)

        from qvl.qlabs import QuanserInteractiveLabs
        from qvl.qcar2 import QLabsQCar2

        qlabs = QuanserInteractiveLabs()
        connection_result = qlabs.open(host)
        if connection_result is False:
            raise ConnectionError(f"QLabs rejected the connection to {host!r}.")

        qcar = QLabsQCar2(qlabs, verbose=True)
        qcar.actorNumber = int(actor_number)
        spawned = False

        if not qcar.ping():
            status = qcar.spawn_id(
                actorNumber=int(actor_number),
                location=[float(v) for v in initial_location],
                rotation=[0.0, 0.0, float(initial_yaw_rad)],
                scale=[1.0, 1.0, 1.0],
                waitForConfirmation=True,
            )
            if status != 0:
                qlabs.close()
                raise RuntimeError(
                    f"Could not spawn replay QCar actor {actor_number}; status={status}."
                )
            spawned = True

        self._qlabs = qlabs
        self._qcar = qcar
        self._host = host
        self._actor_number = int(actor_number)
        self._spawned_by_us = spawned
        self._force_next = True

        self._send_pose(
            float(initial_location[0]),
            float(initial_location[1]),
            float(initial_location[2]),
            float(initial_yaw_rad),
            wait=True,
        )
        return "spawned" if spawned else "reused"

    # ReplaySink ---------------------------------------------------------

    def set_session(self, session: SessionData | None) -> None:
        self._session = session
        self._force_next = True

    def update_time(self, time_s: float, pose: PoseSample) -> None:
        if not self.connected:
            return

        now = time.monotonic()
        if not self._force_next and now - self._last_send_wall < self._minimum_update_period_s:
            return

        self._send_pose(pose.x, pose.y, pose.z, pose.yaw_rad, wait=False)
        self._last_send_wall = now
        self._force_next = False

    def set_playing(self, playing: bool) -> None:
        self._playing = bool(playing)
        # Pausing or resuming should synchronize the next pose exactly.
        self._force_next = True

    def set_rate(self, rate: float) -> None:
        # Transform playback itself is time-driven by ReplayCoordinator, so no
        # QLabs-specific playback-rate setting is required.
        pass

    def force_sync(self) -> None:
        self._force_next = True

    def close(self) -> None:
        self.disconnect(destroy_spawned=True)

    # QLabs-specific behavior ------------------------------------------

    def _send_pose(
        self,
        x: float,
        y: float,
        z: float,
        yaw_rad: float,
        wait: bool = False,
    ) -> None:
        if not self.connected:
            raise RuntimeError("QLabs replay is not connected.")

        self._qcar.set_transform_and_request_state(
            location=[float(x), float(y), float(z)],
            rotation=[0.0, 0.0, float(yaw_rad)],
            enableDynamics=False,
            headlights=False,
            leftTurnSignal=False,
            rightTurnSignal=False,
            brakeSignal=False,
            reverseSignal=False,
            waitForConfirmation=bool(wait),
        )

    def possess_trailing(self) -> None:
        if not self.connected:
            raise RuntimeError("QLabs replay is not connected.")
        if not self._qcar.possess(self._qcar.CAMERA_TRAILING):
            raise RuntimeError("QLabs could not possess the replay QCar trailing camera.")

    def possess_overhead(self) -> None:
        if not self.connected:
            raise RuntimeError("QLabs replay is not connected.")
        if not self._qcar.possess(self._qcar.CAMERA_OVERHEAD):
            raise RuntimeError("QLabs could not possess the replay QCar overhead camera.")

    def possess_front(self) -> None:
        if not self.connected:
            raise RuntimeError("QLabs replay is not connected.")
        if not self._qcar.possess(self._qcar.CAMERA_CSI_FRONT):
            raise RuntimeError("QLabs could not possess the replay QCar front camera.")

    def disconnect(self, destroy_spawned: bool = True) -> None:
        if self._qcar is not None and destroy_spawned and self._spawned_by_us:
            try:
                self._qcar.destroy()
            except Exception:
                pass
        if self._qlabs is not None:
            try:
                self._qlabs.close()
            except Exception:
                pass

        self._qlabs = None
        self._qcar = None
        self._host = None
        self._actor_number = None
        self._spawned_by_us = False
        self._force_next = True


# Backward-compatible alias for code written against v1.
QLabsReplayController = QLabsReplaySink
