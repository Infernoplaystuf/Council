"""
Run at startup by every python child a test starts: tests/offline_guard.py
puts this folder on PYTHONPATH, right after tests/_no_tk (whose
sitecustomize runs the next one on sys.path - this). It runs the
sitecustomize that comes after it, if there is one, and then refuses
egress, and the real Ollama's port, in the child the same way the pytest
process does (see council_offline.py).

Never raises: a failing sitecustomize would break every child process.
"""
try:
    import os
    import council_offline
except Exception:                                    # noqa: BLE001
    council_offline = None

if council_offline is not None:
    try:
        council_offline.run_next_sitecustomize(
            os.path.dirname(os.path.abspath(__file__)))
    except Exception:                                # noqa: BLE001
        pass
    try:
        council_offline.install()
    except Exception:                                # noqa: BLE001
        pass
