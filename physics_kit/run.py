"""
physics_kit.run — how Council runs a physics model (in the Build tab's
sandbox, with the "Run with" Python), and how you can run one yourself:

    python -m physics_kit.run model.py --describe            what it takes
    python -m physics_kit.run model.py --inputs in.json --data data.json --out out.json

``--describe`` prints the model's inputs, fits, assumptions and limits as
JSON. A run writes the Result as JSON to ``--out`` (or prints it). Errors
are written as {"error": "..."} with exit code 1, so Council can show them
and "Fix this" can send them to the chat.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import traceback
from pathlib import Path

from . import Data, Model


def load_model(path) -> Model:
    path = Path(path).resolve()
    if str(path.parent) not in sys.path:
        sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location("physics_model", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Can't load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    model = getattr(mod, "MODEL", None)
    if not isinstance(model, Model):
        raise TypeError(f"{path.name} has no MODEL = physics_kit.Model(...)")
    return model


def _read(path):
    if not path:
        return {}
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="physics_kit.run")
    ap.add_argument("model")
    ap.add_argument("--describe", action="store_true")
    ap.add_argument("--inputs")
    ap.add_argument("--data")
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    try:
        model = load_model(a.model)
        if a.describe:
            payload = model.describe()
        else:
            result = model.run(_read(a.inputs), Data.from_json(_read(a.data)))
            payload = result.to_json()
        ok = 0
    except Exception as exc:                              # noqa: BLE001 - reported to the user
        payload = {"error": f"{type(exc).__name__}: {exc}",
                   "traceback": traceback.format_exc()}
        ok = 1
    text = json.dumps(payload, indent=1, default=str)
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
    else:
        print(text)
    if ok:
        print(payload["traceback"], file=sys.stderr)
    return ok


if __name__ == "__main__":
    sys.exit(main())
