"""
A small Modbus client (standard library only) for the PLC plugin — Modbus
TCP over Ethernet, or Modbus RTU over a serial port — and how a Do-More
address names a Modbus register.

WHY OUR OWN
Modbus TCP is a 7-byte header and a few function codes; the plugin needs
four of them (read holding / input registers, write one / several holding
registers). Writing them here keeps Typhon free of another package to
install (house rule: Typhon never installs anything).

DO-MORE ADDRESSES
Do-More Designer's Modbus TCP server maps its own memory blocks to Modbus
tables (System Configuration, the internal Ethernet port, "Enable
Modbus/TCP Server"):

  MHR1 .. MHR2048   holding registers  40001 ..   function 3 read, 6/16 write
  MIR1 .. MIR2048   input registers    30001 ..   function 4 read
  (MC coils and MI discrete inputs are not needed here.)

`parse_address` takes either spelling ("MHR1", "40001", "MIR5", "30005")
and gives (table, offset): offset 0 is MHR1 / 40001.
"""
from __future__ import annotations

import re
import socket
import struct
import threading
from dataclasses import dataclass
from typing import Any, List, Optional, Sequence

HOLDING = "holding"
INPUT = "input"
DEFAULT_PORT = 502
#: Seconds a request may take before the PLC counts as not answering.
DEFAULT_TIMEOUT_S = 0.5
#: Most registers one read may ask for (Modbus: 125).
MAX_READ = 125

_EXCEPTIONS = {
    1: "illegal function (is the Modbus/TCP server enabled?)",
    2: "illegal data address (that register is not mapped in the PLC)",
    3: "illegal data value",
    4: "server device failure",
    6: "server device busy",
    10: "gateway path unavailable",
    11: "gateway target failed to respond",
}


class ModbusError(Exception):
    """The PLC could not be reached, or refused a request; in words."""


@dataclass(frozen=True)
class Address:
    table: str          # HOLDING or INPUT
    offset: int         # 0-based register number in that table
    text: str           # as the user wrote it, tidied

    @property
    def writable(self) -> bool:
        return self.table == HOLDING


_DOMORE = re.compile(r"^(MHR|MIR)\s*(\d+)$", re.IGNORECASE)


def parse_address(text: str) -> Address:
    """A Do-More name ("MHR1", "MIR3") or a Modbus number ("40001",
    "30003", "400001") as (table, 0-based offset). Raises ValueError, in
    words."""
    raw = " ".join(str(text or "").split()).upper()
    if not raw:
        raise ValueError("no register given")
    m = _DOMORE.match(raw)
    if m:
        number = int(m.group(2))
        if number < 1:
            raise ValueError(f"{raw}: Do-More numbers start at 1")
        table = HOLDING if m.group(1) == "MHR" else INPUT
        return Address(table, number - 1, f"{m.group(1)}{number}")
    if raw.isdigit():
        number = int(raw)
        for base, table in ((400001, HOLDING), (300001, INPUT)):
            if len(raw) == 6 and base <= number <= base + 65535:
                return Address(table, number - base, raw)
        for base, table in ((40001, HOLDING), (30001, INPUT)):
            if len(raw) == 5 and base <= number <= base + 9998:
                return Address(table, number - base, raw)
    raise ValueError(f"{text!r} is not a register: write MHR1 (or 40001) "
                     f"for a holding register, MIR1 (or 30001) for an "
                     f"input register")


class ModbusClient:
    """One TCP connection to a Modbus server (the PLC), opened on first use
    and opened again after a failure. Thread-safe: one request at a time."""

    def __init__(self, host: str, port: int = DEFAULT_PORT, unit: int = 1,
                 timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        self.host = str(host or "").strip()
        self.port = int(port or DEFAULT_PORT)
        self.unit = int(unit) & 0xFF
        self.timeout_s = float(timeout_s)
        self._sock: Optional[socket.socket] = None
        self._tid = 0
        self._lock = threading.Lock()

    # -- the connection ---------------------------------------------------
    def _connect(self) -> socket.socket:
        if self._sock is not None:
            return self._sock
        if not self.host:
            raise ModbusError("no PLC address set")
        try:
            sock = socket.create_connection((self.host, self.port),
                                            timeout=self.timeout_s)
        except OSError as exc:
            raise ModbusError(f"cannot reach the PLC at {self.host}:"
                              f"{self.port} ({exc})") from exc
        sock.settimeout(self.timeout_s)
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        self._sock = sock
        return sock

    def close(self) -> None:
        with self._lock:
            self._drop()

    def _drop(self) -> None:
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def _recv(self, sock: socket.socket, n: int) -> bytes:
        out = b""
        while len(out) < n:
            part = sock.recv(n - len(out))
            if not part:
                raise ModbusError("the PLC closed the connection")
            out += part
        return out

    def request(self, function: int, body: bytes) -> bytes:
        """Send one request, return the response's data (after the function
        code). Raises ModbusError."""
        with self._lock:
            sock = self._connect()
            self._tid = (self._tid + 1) & 0xFFFF
            pdu = bytes([function]) + body
            frame = struct.pack(">HHHB", self._tid, 0, len(pdu) + 1,
                                self.unit) + pdu
            try:
                sock.sendall(frame)
                head = self._recv(sock, 7)
                tid, proto, length, _unit = struct.unpack(">HHHB", head)
                if length < 2 or length > 260:
                    raise ModbusError(f"bad reply length {length}")
                rest = self._recv(sock, length - 1)
            except ModbusError:
                self._drop()
                raise
            except OSError as exc:
                self._drop()
                raise ModbusError(f"lost the PLC at {self.host}:{self.port} "
                                  f"({exc})") from exc
            if tid != self._tid or proto != 0:
                self._drop()
                raise ModbusError("the PLC's reply did not match the request")
            code = rest[0]
            if code == function | 0x80:
                why = _EXCEPTIONS.get(rest[1] if len(rest) > 1 else 0,
                                      "unknown exception")
                raise ModbusError(f"the PLC refused: {why}")
            if code != function:
                raise ModbusError("the PLC answered a different request")
            return rest[1:]

    # -- the requests -----------------------------------------------------
    def read(self, address: Address, count: int = 1) -> List[int]:
        """`count` 16-bit registers from `address` on (unsigned)."""
        count = int(count)
        if not 1 <= count <= MAX_READ:
            raise ValueError(f"read 1..{MAX_READ} registers, not {count}")
        function = 3 if address.table == HOLDING else 4
        data = self.request(function, struct.pack(">HH", address.offset,
                                                  count))
        if not data or data[0] != 2 * count or len(data) < 1 + 2 * count:
            raise ModbusError("the PLC sent a short reply")
        return list(struct.unpack(f">{count}H", data[1:1 + 2 * count]))

    def read_one(self, address: Address) -> int:
        return self.read(address, 1)[0]

    def write(self, address: Address, values: Sequence[int]) -> None:
        """Write holding registers from `address` on (each 0..65535)."""
        if not address.writable:
            raise ModbusError(f"{address.text} is an input register: Modbus "
                              f"cannot write it")
        values = [int(v) & 0xFFFF for v in values]
        if len(values) == 1:
            self.request(6, struct.pack(">HH", address.offset, values[0]))
            return
        self.request(16, struct.pack(">HHB", address.offset, len(values),
                                     2 * len(values))
                     + struct.pack(f">{len(values)}H", *values))



# ======================================================================
# Modbus RTU over a serial port (Andy, 24 November 2026: "a serial
# connection between the GUI and the PLC")
# ======================================================================
def crc16(data: bytes) -> int:
    """Modbus RTU's CRC-16 (polynomial 0xA001, start 0xFFFF)."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def rtu_frame(unit: int, pdu: bytes) -> bytes:
    """A request or reply as it goes down the wire: unit, PDU, CRC (low
    byte first)."""
    body = bytes([unit & 0xFF]) + pdu
    return body + struct.pack("<H", crc16(body))


class ModbusRtuClient(ModbusClient):
    """The same requests as ModbusClient, framed for Modbus RTU on a serial
    port (RS-232 or RS-485). `open_port()` makes the port: an object with
    write(bytes), read(n) -> bytes (fewer on timeout), reset_input_buffer()
    and close() — pyserial's Serial, or the simulator's stand-in. Opened on
    first use and again after a failure; one request at a time."""

    def __init__(self, open_port: Any, unit: int = 1, name: str = "",
                 timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        super().__init__("", unit=unit, timeout_s=timeout_s)
        self._open_port = open_port
        self.name = str(name or "the serial port")
        self._port: Any = None

    def _drop(self) -> None:
        port, self._port = self._port, None
        if port is not None:
            try:
                port.close()
            except Exception:                             # noqa: BLE001
                pass

    def _read(self, n: int) -> bytes:
        data = self._port.read(n)
        if len(data) < n:
            raise ModbusError(f"no answer from the PLC on {self.name} (check "
                              f"the cable, baud rate, parity and station "
                              f"number)")
        return bytes(data)

    def request(self, function: int, body: bytes) -> bytes:
        with self._lock:
            if self._port is None:
                try:
                    self._port = self._open_port()
                except ModbusError:
                    raise
                except Exception as exc:                  # noqa: BLE001
                    raise ModbusError(f"cannot open {self.name} ({exc})") \
                        from exc
            pdu = bytes([function]) + body
            try:
                try:
                    self._port.reset_input_buffer()
                except Exception:                         # noqa: BLE001
                    pass
                self._port.write(rtu_frame(self.unit, pdu))
                head = self._read(2)
                code = head[1]
                if code & 0x80:
                    rest = self._read(3)
                elif code in (3, 4):
                    count = self._read(1)
                    rest = count + self._read(count[0] + 2)
                elif code in (6, 16):
                    rest = self._read(6)
                else:
                    raise ModbusError("the PLC sent a reply Typhon cannot "
                                      "read")
            except ModbusError:
                self._drop()
                raise
            except Exception as exc:                      # noqa: BLE001
                self._drop()
                raise ModbusError(f"lost {self.name} ({exc})") from exc
            frame = head + rest
            if crc16(frame[:-2]) != struct.unpack("<H", frame[-2:])[0]:
                self._drop()
                raise ModbusError(f"a garbled reply on {self.name} (check "
                                  f"the baud rate and parity)")
            if head[0] != self.unit:
                raise ModbusError(f"station {head[0]} answered, not "
                                  f"{self.unit}")
            if code == function | 0x80:
                why = _EXCEPTIONS.get(rest[0], "unknown exception")
                raise ModbusError(f"the PLC refused: {why}")
            if code != function:
                raise ModbusError("the PLC answered a different request")
            return bytes(rest[:-2])
