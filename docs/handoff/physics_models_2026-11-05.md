# Physics models in the Build tab (2026-11-05)

Andy asked for a way to make physics models in Council by giving it a goal. The examples were a PVC pipe organ (the notes struck objects make), radio antennas, eddy currents and water erosion on materials. Andy then chose to make it a Build tab feature, and asked that models can connect to a folder, Connections knowledge or a Ledger.

## What is where

| Piece | File |
|---|---|
| Tested physics blocks, copied into each model folder | `physics_kit/` (acoustics, antennas, eddy, erosion, materials, units, run) |
| The four worked examples (model, reference tests, sample data) | `examples/physics_models/` |
| Making a model, data links, running it in the sandbox, calibration | `council_core/physics_project.py` |
| Fitting an equation to measured points (safe ast parser, 95% ranges) | `council_core/physics_fit.py` |
| The Model page and the New physics model window | `council_qt/widgets/physics_panel.py` |
| The agent that builds a model from a goal | "Physics modeller" in `council_core/code_profiles.py` |

## How it fits together

* **New physics model…** in the Build tab copies an example, or writes a blank model for a goal. Either way it copies `physics_kit/` in, writes `physics.json` and runs `git init` with one commit. For a goal it switches the chat to Agent mode, picks the Physics modeller and sends the goal, so the plan card comes up for approval.
* **The model's code never runs inside Council.** `physics_project.run` writes `inputs.json` and `data.json` and runs `python -m physics_kit.run model.py` with the Run with Python in `build_sandbox`, then reads JSON back.
* **Data links are read by Council, not by the model.**
  * A folder gives CSV, TSV and Excel tables, plus `.txt` and `.md` texts.
  * A Ledger gives its parent CSV rows, with numbers converted and units taken from the schema.
  * Connections gives `kg_query.gather` text for the link's question.
* **Calibrate.** A model declares `Fit`s: an equation, the measured input, and which model input each fitted parameter sets. The run's `Result.fit_constants` supply the equation's fixed values. "Use these values" puts the fit into the inputs and runs the model again.

## Checks

* `tests/test_physics_kit.py` covers textbook values. The dipole comes out at about 72 Ω and 2.14 dBi, skin depths match, and Shields and Finnie behave as published.
* `tests/test_physics_project.py` creates each example, runs its own reference tests, runs it in the sandbox and calibrates it from its sample data.
* `tests/test_physics_panel.py` checks the Qt page and the Build tab flow offscreen.

## Not done yet (proposal steps 2 to 4)

* A microphone pitch check.
* NEC2 and scikit-fem engines for ground effects and real 3D shapes.
* Saved models in the Connections graph.
* A data-fitting page in Ledger Analyze using `physics_fit` (its built-in laws and AICc ranking are ready for that).
