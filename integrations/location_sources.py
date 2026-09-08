"""Concrete LocationSource implementations."""

from __future__ import annotations

import math

from core.oop_interfaces import LocationReading, LocationSource


class QLabsQCarLocationSource(LocationSource):
    """Read XYZ from one existing QLabs QCar2 actor.

    Quanser imports are intentionally lazy. The rest of the recorder can be
    imported, tested, and documented without a QLabs installation.
    """

    def __init__(self, host: str = "localhost", actor_number: int = 0) -> None:
        self._host = str(host)
        self._actor_number = int(actor_number)
        self._qlabs = None
        self._qcar = None

    @property
    def name(self) -> str:
        return "QLabs QCar2 world transform"

    @property
    def metadata(self) -> dict:
        return {
            "kind": "qlabs_qcar2_world_transform",
            "host": self._host,
            "actor_number": self._actor_number,
        }

    @property
    def connected(self) -> bool:
        return self._qlabs is not None and self._qcar is not None

    def connect(self) -> None:
        self.close()

        from qvl.qlabs import QuanserInteractiveLabs
        from qvl.qcar2 import QLabsQCar2

        qlabs = QuanserInteractiveLabs()
        result = qlabs.open(self._host)
        if result is False:
            raise ConnectionError(f"QLabs rejected connection to {self._host!r}.")

        qcar = QLabsQCar2(qlabs)
        qcar.actorNumber = self._actor_number
        if not qcar.ping():
            qlabs.close()
            raise RuntimeError(
                f"QCar2 actor {self._actor_number} does not exist in QLabs."
            )

        self._qlabs = qlabs
        self._qcar = qcar

    def read_position(self) -> LocationReading | None:
        if not self.connected:
            raise RuntimeError("QLabs location source is not connected.")

        status, location, _rotation, _scale = self._qcar.get_world_transform()
        if not status or len(location) < 3:
            return None

        x, y, z = map(float, location[:3])
        if not all(math.isfinite(value) for value in (x, y, z)):
            return None
        return LocationReading(x=x, y=y, z=z)

    def close(self) -> None:
        if self._qlabs is not None:
            try:
                self._qlabs.close()
            except Exception:
                pass
        self._qlabs = None
        self._qcar = None
