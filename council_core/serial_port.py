"""
council_core.serial_port — a serial port for the PLC trigger plugin's
Modbus RTU link (typhon_plugins/plc; docs/typhon_plc_trigger.md), through
pyserial. Here, not in the plugin's folder, because a built-in plugin
imports only the plugin API: it reaches these through
typhon_plugin_api.builtin.

pyserial is optional: Typhon never installs anything. Without it the
serial link says what to install, and Ethernet (Modbus TCP) still works.
"""
from __future__ import annotations

from typing import Any, List

PARITIES = {"None": "N", "Even": "E", "Odd": "O"}
MISSING = ("the serial link needs pyserial: run `pip install pyserial` in "
           "the Python Typhon runs under (Ethernet needs nothing)")


def _serial() -> Any:
    try:
        import serial  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError(MISSING) from exc
    return serial


def open_port(name: str, baud: int = 115200, parity: str = "None",
              stop_bits: int = 1, timeout_s: float = 0.5) -> Any:
    """The port opened 8 data bits, `parity` (None, Even, Odd) and
    `stop_bits` (1, 2): pyserial's Serial (write, read, reset_input_buffer,
    close)."""
    serial = _serial()
    if not str(name or "").strip():
        raise RuntimeError("no serial port set (COM1, COM3 ...)")
    return serial.Serial(
        port=str(name).strip(), baudrate=int(baud), bytesize=8,
        parity=PARITIES.get(str(parity), "N"),
        stopbits=2 if int(stop_bits) == 2 else 1,
        timeout=float(timeout_s), write_timeout=float(timeout_s))


def list_ports() -> List[str]:
    """The serial ports this PC has now (COM3 ...), or [] (no pyserial)."""
    try:
        from serial.tools import list_ports as lp  # type: ignore
    except ImportError:
        return []
    try:
        return sorted(p.device for p in lp.comports())
    except Exception:                                     # noqa: BLE001
        return []
