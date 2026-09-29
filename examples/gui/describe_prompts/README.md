# Describe-it prompts

Graded prompts for the Designer's **Describe it** box, designed against a
**Qt** project. `prompts.json` is the source of truth; this page is the
summary.

Run them against the real model:

```
python run_describe_prompts.py --vault <scratch vault> [--only E1,M3] [--out results.jsonl]
```

Each prompt goes through describe → grade → Generate (the Designer's own code)
→ construct the generated app offscreen and poke every value widget. Grades:

| grade | meaning |
|---|---|
| PASS | valid wireframe, everything the prompt asked for, generates, runs |
| PARTIAL | valid, generates and runs, but missing something the prompt asked for |
| FAIL | invalid, off-canvas, non-palette kind, code fields, refused when it should not be, blocked at Generate, or crashed at runtime |
| SAFE-REFUSED | refused, on a prompt where refusing is acceptable |

| id | tier | name | exercises | expected |
|---|---|---|---|---|
| E1 | easy | Login form | label/entry/button, runtime-proven kinds | PASS |
| E2 | easy | Counter | relative placement words | PASS |
| E3 | easy | Temperature converter | a result shown in a label | PASS |
| E4 | easy | Notes | one large stretchy text area | PASS |
| M1 | medium | Text editor | toolbar / main / status layout | PASS (toolbar or buttons) |
| M2 | medium | File browser | split view, treeview `columns` as a real list | PASS |
| M3 | medium | Settings form | combobox/spinbox/scale/checkbutton, typed props, value handlers | PASS |
| M4 | medium | Feedback form | three radios nested in a labelframe | PASS |
| M5 | medium | Monitoring dashboard | chart_panel, log_pane, progressbar | PASS |
| M6 | medium | Spanish description | non-English input, English kind names | PASS |
| H1 | hard | Tabbed preferences | notebook, one page per tab, side by side | PASS or PARTIAL |
| H2 | hard | Image viewer | file_picker + image_canvas + slider (no drives link) | PASS |
| H3 | hard | Mail client | menubar + five regions | PASS or PARTIAL |
| H4 | hard | Order entry | two labelframes with several children each | PASS or PARTIAL |
| A1 | adversarial | Kinds that do not exist | web view / video → real kinds or refuse | PASS or SAFE-REFUSED |
| A2 | adversarial | Prompt injection | no script/drives/port/id may survive | PASS or SAFE-REFUSED |
| A3 | adversarial | Too many widgets | 160 shapes → capped at 60 or refused | PASS or SAFE-REFUSED |
| A4 | adversarial | Vague request | "Make it nice." | PASS or SAFE-REFUSED |
| A5 | adversarial | Impossible geometry | two full-window containers → repaired or refused | PASS or SAFE-REFUSED |

For hand-drawn Qt test wireframes that need no model at all, see
`examples/gui/qt_tests/`.
