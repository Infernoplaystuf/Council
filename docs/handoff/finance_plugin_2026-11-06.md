# Finance plugin, step 1: paycheck and taxes (2026-11-06)

Council's first plugin: `council_plugins/finance/` (id `council.finance`,
kind `tab`, network `none`). Like every plugin it is off until ticked in
the Settings tab; its tab "🧩 Finance" appears at the next start. The
plugin system it runs on: `docs/handoff/app_plugins_2026-11-05.md`.

## What it does now

For a household (filing status, state, Ohio home city and school district,
children and other dependents) and each earner (salary, bonus, checks a
year, Ohio work city or JEDD, Section 125 insurance and other pre-tax, HSA,
health and dependent care FSA, traditional and Roth 401(k), employer
match, after-tax deductions) it shows each earner's check and year, line
by line, and the household's year when there is more than one earner.

- Federal: 2026 brackets and standard deduction, child tax credit (with the
  refundable part and phase-out) and the other-dependent credit.
- Social Security to the wage base; Medicare plus the Additional Medicare
  Tax on the household (warned about when no employer withholds it).
- State: the state's brackets, standard deduction and exemptions (credit
  exemptions come off the tax); Ohio's exemption by AGI. State footnotes
  are listed as "not modeled".
- Ohio local: work city or JEDD tax on Medicare wages, the home city's tax
  less its credit (credit fraction is a plan field, default 100%), school
  district tax (traditional: rate × Ohio taxable income; earned income:
  rate × wages).
- Warnings: 401(k) over the limit (age 50 and 60–63 catch-ups), HSA over
  the limit (55+ catch-up) or set without coverage, a general health FSA
  beside an HSA, health FSA and household dependent care FSA over the
  limit, employer match left unclaimed, deductions above pay.

A planner, not tax software: no itemizing, AMT, self-employment or
investment income. Household taxes are shared to earners by pay.

## Where things are

- `engine/paycheck.py`: `compute(plan) -> Result`; the plan format is in
  its docstring. Pure Python, no Qt.
- `engine/tables.py`: reads `data/` (nothing is fetched).
- `data/federal_2026.json`: figures from Rev. Proc. 2025-32, 2025-19,
  IR-2025-111 and SSA's contribution base page; `sources.statutory` names
  the few set by law (Additional Medicare thresholds, HSA 55+ catch-up,
  $500 other-dependent credit and the phase-out, dependent care FSA
  $7,500).
- `data/state_income_tax_{2025,2026}.json` (Tax Foundation),
  `ohio_municipal_income_tax.json`, `ohio_school_district_income_tax.json`,
  `ohio_jedd_income_tax.json` (Ohio Finder tables Andy downloaded).
- `tab.py`: the tab; the plan is saved as
  `<app dir>/plugin_data/council.finance/plan.json`.
- Tests: `tests/test_finance_plugin.py` (hand-worked examples and the tab
  offscreen).

## Next steps (agreed order)

2. Projection over years: raises, expenses with inflation, account
   balances. Later years' federal tables indexed by inflation.
3. Scenarios and more warnings; Excel export.
4. Stock tracker: broker CSV holdings; opt-in online price refresh (Andy
   chose it 2026-11-06) needs the plugin system to allow one named host,
   off by default. Quote service not chosen yet.

Open questions for Andy: plan-only or actuals from pay stubs, projection
horizon and retirement age, whether the Council chat may read the plan.
