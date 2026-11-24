"""
A simulated Do-More PLC on a conveyor, for trying the plugin without
hardware and for its tests: a Modbus TCP server on this PC (127.0.0.1)
whose registers a pretend conveyor updates.

THE PRETEND CONVEYOR
Bottles pass the photo-eye every `pitch_mm / speed_mm_s` seconds. At each
one the "ladder" adds 1 to the bottle count (MHR1 by default, wrapping at
65535, as INC on an MHR does) after `scan_ms` (the PLC's scan), and keeps
the belt speed in MHR2 (mm/s). `detections` lists when each bottle really
crossed the photo-eye (time.monotonic()), so a test can check when the
camera fired against when the bottle reached the camera.

Only on 127.0.0.1, only while it runs; nothing is written to disk.
"""
from __future__ import annotations

import socket
import struct
import threading
import time
from typing import List, Optional

REGISTERS = 2048


class SimulatedPlc:
    def __init__(self, speed_mm_s: float = 500.0, pitch_mm: float = 200.0,
                 scan_ms: float = 1.0, count_offset: int = 0,
                 speed_offset: int = 1, running: bool = True) -> None:
        self.holding: List[int] = [0] * REGISTERS
        self.inputs: List[int] = [0] * REGISTERS
        self.speed_mm_s = float(speed_mm_s)
        self.pitch_mm = float(pitch_mm)
        self.scan_ms = float(scan_ms)
        self.count_offset = int(count_offset)
        self.speed_offset = int(speed_offset)
        self.belt_running = bool(running)
        self.detections: List[float] = []
        self.requests = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._server: Optional[socket.socket] = None
        self._threads: List[threading.Thread] = []
        self.port = 0

    # -- life -------------------------------------------------------------
    def start(self) -> "SimulatedPlc":
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))
        server.listen(4)
        server.settimeout(0.1)
        self._server = server
        self.port = server.getsockname()[1]
        self._set_speed_register()
        for target in (self._accept, self._conveyor):
            t = threading.Thread(target=target, daemon=True,
                                 name=f"sim-plc-{target.__name__}")
            t.start()
            self._threads.append(t)
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
        for t in self._threads:
            t.join(1.0)

    def __enter__(self) -> "SimulatedPlc":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # -- the conveyor -----------------------------------------------------
    def set_speed(self, mm_s: float) -> None:
        with self._lock:
            self.speed_mm_s = float(mm_s)
            self._set_speed_register()

    def _set_speed_register(self) -> None:
        self.holding[self.speed_offset] = int(round(self.speed_mm_s)) & 0xFFFF

    @property
    def count(self) -> int:
        return self.holding[self.count_offset]

    def bottle(self) -> None:
        """One bottle crosses the photo-eye now."""
        crossed = time.monotonic()
        with self._lock:
            self.detections.append(crossed)
        delay = self.scan_ms / 1000.0
        if delay > 0:
            time.sleep(delay)
        with self._lock:
            off = self.count_offset
            self.holding[off] = (self.holding[off] + 1) & 0xFFFF

    def _conveyor(self) -> None:
        next_at = time.monotonic() + 0.05
        while not self._stop.is_set():
            with self._lock:
                speed, pitch = self.speed_mm_s, self.pitch_mm
                running = self.belt_running
            if not running or speed <= 0 or pitch <= 0:
                next_at = time.monotonic() + 0.05
                self._stop.wait(0.02)
                continue
            wait = next_at - time.monotonic()
            if wait > 0:
                self._stop.wait(min(wait, 0.05))
                continue
            self.bottle()
            next_at += pitch / speed

    # -- the Modbus server ------------------------------------------------
    def _accept(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()          # type: ignore[union-attr]
            except socket.timeout:
                continue
            except OSError:
                return
            t = threading.Thread(target=self._serve, args=(conn,),
                                 daemon=True, name="sim-plc-conn")
            t.start()

    def _serve(self, conn: socket.socket) -> None:
        conn.settimeout(0.2)
        buf = b""
        with conn:
            while not self._stop.is_set():
                try:
                    part = conn.recv(512)
                except socket.timeout:
                    continue
                except OSError:
                    return
                if not part:
                    return
                buf += part
                while len(buf) >= 7:
                    tid, proto, length, unit = struct.unpack(">HHHB", buf[:7])
                    if len(buf) < 6 + length:
                        break
                    pdu, buf = buf[7:6 + length], buf[6 + length:]
                    reply = self._answer(pdu)
                    try:
                        conn.sendall(struct.pack(">HHHB", tid, proto,
                                                 len(reply) + 1, unit) + reply)
                    except OSError:
                        return

    def _answer(self, pdu: bytes) -> bytes:
        self.requests += 1
        fn = pdu[0] if pdu else 0

        def error(code: int) -> bytes:
            return bytes([fn | 0x80, code])

        with self._lock:
            if fn in (3, 4) and len(pdu) >= 5:
                start, count = struct.unpack(">HH", pdu[1:5])
                table = self.holding if fn == 3 else self.inputs
                if count < 1 or start + count > len(table):
                    return error(2)
                values = table[start:start + count]
                return bytes([fn, 2 * count]) + struct.pack(
                    f">{count}H", *values)
            if fn == 6 and len(pdu) >= 5:
                at, value = struct.unpack(">HH", pdu[1:5])
                if at >= len(self.holding):
                    return error(2)
                self.holding[at] = value
                return pdu[:5]
            if fn == 16 and len(pdu) >= 6:
                start, count, nbytes = struct.unpack(">HHB", pdu[1:6])
                if start + count > len(self.holding) or nbytes != 2 * count:
                    return error(2)
                values = struct.unpack(f">{count}H", pdu[6:6 + nbytes])
                self.holding[start:start + count] = list(values)
                return pdu[:5]
        return error(1)


