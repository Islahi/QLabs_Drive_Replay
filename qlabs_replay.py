"""QLabs QCar transform-based replay helper.

Imports the Quanser qvl package lazily so the map/video replay can run on a
machine without QLabs installed.
"""

from __future__ import annotations

import math


class QLabsReplayController:
    def __init__(self) -> None:
        self.qlabs = None
        self.qcar = None
        self.host: str | None = None
        self.actor_number: int | None = None
        self.spawned_by_us = False

    @property
    def connected(self) -> bool:
        return self.qlabs is not None and self.qcar is not None

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

        self.qlabs = qlabs
        self.qcar = qcar
        self.host = host
        self.actor_number = int(actor_number)
        self.spawned_by_us = spawned

        self.update_pose(
            initial_location[0],
            initial_location[1],
            initial_location[2],
            initial_yaw_rad,
            wait=True,
        )
        return "spawned" if spawned else "reused"

    def update_pose(
        self,
        x: float,
        y: float,
        z: float,
        yaw_rad: float,
        wait: bool = False,
    ) -> None:
        if not self.connected:
            raise RuntimeError("QLabs replay is not connected.")

        # QLabs documents this transform API specifically as suitable for
        # playback of previously recorded position data. Dynamics are disabled
        # because this actor is a visualization of recorded ground truth.
        self.qcar.set_transform_and_request_state(
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
        if not self.qcar.possess(self.qcar.CAMERA_TRAILING):
            raise RuntimeError("QLabs could not possess the replay QCar trailing camera.")

    def possess_overhead(self) -> None:
        if not self.connected:
            raise RuntimeError("QLabs replay is not connected.")
        if not self.qcar.possess(self.qcar.CAMERA_OVERHEAD):
            raise RuntimeError("QLabs could not possess the replay QCar overhead camera.")

    def possess_front(self) -> None:
        if not self.connected:
            raise RuntimeError("QLabs replay is not connected.")
        if not self.qcar.possess(self.qcar.CAMERA_CSI_FRONT):
            raise RuntimeError("QLabs could not possess the replay QCar front camera.")

    def disconnect(self, destroy_spawned: bool = True) -> None:
        if self.qcar is not None and destroy_spawned and self.spawned_by_us:
            try:
                self.qcar.destroy()
            except Exception:
                pass
        if self.qlabs is not None:
            try:
                self.qlabs.close()
            except Exception:
                pass

        self.qlabs = None
        self.qcar = None
        self.host = None
        self.actor_number = None
        self.spawned_by_us = False
