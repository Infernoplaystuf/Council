"""
inferno_local — local-only helper package for Data's Inferno.

A small, intentionally narrow surface that the rest of the app builds on
when it needs to do something that the Odysseus brief flagged as a
potential security regression: spin up a model backend, store/retrieve
persistent memory, survey hardware, or reach an "external" service.

Anything in this package is air-gap-safe by construction:

  - inferno_local.security      egress guard; loopback-only network
  - inferno_local.model_runner  build_runner(config) factory; cloud blocked
  - inferno_local.cookbook      hardware survey + model-fit helpers
  - inferno_local.local_memory  ChromaDB PersistentClient with telemetry off

Anything in here that opens a socket goes through
``security.assert_loopback`` first. Anything that loads a model is
limited to backends that run on this machine (in-process llama-cpp or a
loopback Ollama daemon).
"""

from . import security        # re-export so callers can do `from inferno_local import security`
from . import model_runner
from . import cookbook
from . import local_memory

__all__ = ["security", "model_runner", "cookbook", "local_memory"]
