"""
Run at startup by every python child a test starts: tests/no_tk_guard.py puts
this folder first on PYTHONPATH. It runs the sitecustomize it shadows, if
there is one, and then refuses Tk windows in the child the same way the
pytest process refuses them (see council_no_tk.py) — last, so the refusal is
the hook that wins.

Never raises: a failing sitecustomize would break every child process.
"""
try:
    import os
    import council_no_tk
except Exception:                                    # noqa: BLE001
    council_no_tk = None

if council_no_tk is not None:
    try:
        council_no_tk.run_next_sitecustomize(
            os.path.dirname(os.path.abspath(__file__)))
    except Exception:                                # noqa: BLE001
        pass
    try:
        council_no_tk.install()
    except Exception:                                # noqa: BLE001
        pass
