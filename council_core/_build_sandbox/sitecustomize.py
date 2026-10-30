"""
Run at startup by every python child the Build tab starts (council_core.
build_sandbox puts this folder first on PYTHONPATH): it runs the
sitecustomize it shadows, then installs the sandbox (council_build_sandbox:
offline, one output stream). Never raises: a failing sitecustomize would
break the child before the user's code runs.
"""
try:
    import os
    import council_build_sandbox
except Exception:                                    # noqa: BLE001
    council_build_sandbox = None

if council_build_sandbox is not None:
    try:
        council_build_sandbox.run_next_sitecustomize(
            os.path.dirname(os.path.abspath(__file__)))
    except Exception:                                # noqa: BLE001
        pass
    try:
        council_build_sandbox.install()
    except Exception:                                # noqa: BLE001
        pass
