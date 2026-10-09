"""
Run at startup by every python child a test starts: tests/no_tk_guard.py puts
this folder first on PYTHONPATH. It refuses Tk windows in the child the same
way the pytest process refuses them (see council_no_tk.py), then runs the
sitecustomize it shadows, if there is one.

Never raises: a failing sitecustomize would break every child process.
"""
try:
    import council_no_tk
    council_no_tk.install()
except Exception:                                    # noqa: BLE001
    pass
else:
    try:
        import os
        council_no_tk.run_next_sitecustomize(
            os.path.dirname(os.path.abspath(__file__)))
    except Exception:                                # noqa: BLE001
        pass
