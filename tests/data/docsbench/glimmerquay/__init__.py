"""
glimmerquay — a small telemetry toolkit that exists only for the Council's
documentation benchmark.

It is invented: no model can have read about it, so a model that answers a
question about it correctly did so from the documentation it was handed.
Every fact the benchmark asks about is written in a docstring below, and the
hidden tests import this package to check the code a model writes.

Modules:
  glimmerquay.ledger    a bounded, sequence-numbered ledger of entries
  glimmerquay.codec     framing bytes for the wire, with checksums
  glimmerquay.schedule  periodic sampling windows
  glimmerquay.units     voltage units and gain presets
"""
from .codec import CHECKSUMS, FrameError, decode_frame, encode_frame
from .ledger import Ledger, LedgerFullError
from .schedule import Cadence, next_window
from .units import GAIN_PRESETS, apply_gain, to_millivolts

__version__ = "0.7.3"

DEFAULT_TIMEOUT_MS = 2750
"""How long, in milliseconds, a glimmerquay link waits for an acknowledgement
before giving up. The default is 2750 ms."""

__all__ = [
    "CHECKSUMS", "FrameError", "decode_frame", "encode_frame",
    "Ledger", "LedgerFullError", "Cadence", "next_window",
    "GAIN_PRESETS", "apply_gain", "to_millivolts", "DEFAULT_TIMEOUT_MS",
]
