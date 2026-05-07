# Data's Inferno

**A panel of AI specialists that reviews, charts, and explains your business data — running entirely on your computer.**

Drop in a CSV of purchase orders, an inventory export, or a customer list. Ask questions in plain English. A panel of AI analysts deliberates, ranks the answers, and gives you a verdict you can trust because every step is visible. Your data never leaves the machine.

---

## What it's for

Built for small businesses, solo operators, and analysts who have data but no time (or budget) for a BI consultant.

Common questions Data's Inferno answers well:

- *"Which customers haven't ordered in 90+ days?"*
- *"What products are sitting in inventory longest?"*
- *"Are my repeat customers spending more or less than they did last quarter?"*
- *"Which supplier delivered late most often this year?"*
- *"Show me revenue trend by category for the last 12 months."*
- *"Which employees process the most orders?"*

You drop the file. The AI panel reads it, picks the right chart, runs the math, and explains what it sees in language you'd use in a board meeting — not jargon.

---

## Why it's different

| | Cloud AI tools | Spreadsheets | Data's Inferno |
|---|---|---|---|
| Your data leaves the machine | ✗ | ✓ | ✓ |
| Works offline | ✗ | ✓ | ✓ |
| Plain-English questions | ✓ | ✗ | ✓ |
| Multiple AI perspectives, not one | ✗ | — | ✓ |
| Auto-suggests the right chart | partial | ✗ | ✓ |
| Catches its own mistakes | ✗ | ✗ | ✓ (panel cross-examines) |
| Monthly fee | ✓ | — | ✗ (one-time) |

The "panel of AI specialists" is the core differentiator. A single AI gives you one answer, confidently, even when wrong. Data's Inferno runs three or four AIs against the same question and a Judge ranks them. You see the disagreements. That's how you know what to trust.

---

## Quick start

### 1. Install

Download the latest release for your platform from the website (or run from source — see `requirements.txt`). Run the installer.

On first launch, a setup wizard walks you through:
- Confirming you have ~15 GB free for AI models
- Installing [Ollama](https://ollama.com) (the local AI engine)
- Pulling a starter model

The whole setup takes about three minutes. Models download once, then everything runs offline.

### 2. Drop in your data

Switch to the **📊 Grapher** tab and drag any of these onto it:

- CSV / TSV (most common — what every accounting tool exports)
- Excel `.xlsx` / `.xls`
- JSON

The schema panel auto-detects which columns are dates, numbers, and categories.

### 3. Ask a question

Switch to the **⚖ Council** tab and type something like:

> *"Looking at the orders.csv, which products had the biggest revenue drop between Q1 and Q2?"*

The panel deliberates. Watch each AI draft its answer. The Judge ranks them. You get a verdict with confidence score and the reasoning behind it.

If the answer needs a chart, the panel will route you to the Grapher and the **Analyst** AI will pick an appropriate visualisation.

---

## Sample workflows

### Find your dormant customers
1. Drop your `customers.csv` into Grapher
2. Council tab: *"Which customers haven't placed an order in 90+ days?"*
3. The panel suggests a date-grouped chart and produces the dormant list
4. Click 📚 Librarian → Save → email the list to your sales lead

### Spot inventory dead weight
1. Drop your `inventory.csv` into Grapher
2. Ask: *"Which SKUs have moved fewer than 2 units this quarter but cost the most to hold?"*
3. The Analyst suggests a scatter (units sold × holding cost)
4. Outliers in the top-left of the chart are your dead weight

### Track client retention month-over-month
1. Drop your `orders.csv` (with customer_id and order_date)
2. Ask: *"Calculate the cohort retention rate for customers acquired in each of the last 6 months"*
3. The Coder writes the cohort calculation, the Analyst plots it as a heatmap
4. Save the chart for your monthly review

---

## Personal Specialists — your own AI experts

Out of the box, Data's Inferno ships with three pre-built **Personal Specialists**:

- **💰 Sales Specialist** — revenue trends, customer behaviour, retention, AOV
- **📦 Inventory Specialist** — stock levels, turnover, dead inventory, supplier risk
- **🤝 Customer Specialist** — loyalty, dormancy, churn risk, segmentation

A specialist isn't a separate AI — it's a **named lens** on top of your data. The vault is shared by every specialist. When you ask a question that mentions one of their domain keywords (e.g. *"churn"* triggers the Customer Specialist), they're automatically summoned and their lens is applied to the answer.

Cross-domain questions are answered by **multiple specialists at the same time**:

> *"Based on previous years' sales what should be purchased to ensure enough stock for predicted demand?"*

This question contains "sales" and "stock" → both the Sales and Inventory Specialists are summoned. They each draft an answer from their lens, looking at the same shared vault data. The Judge then synthesises one combined recommendation that reconciles both views.

You can also create your own specialists — name, icon, description, domain keywords, and a system-prompt overlay that tells the underlying AI how to think about questions in that domain. The **🎓 Specialists** tab walks you through it.

---

## How the panel works

When you ask a question, here's what happens:

1. **Judge** decides what kind of question it is (data lookup, calculation, chart, opinion, etc.)
2. **Convenes a panel** — usually three or four specialists from the available AI roles:
   - **Writer** — explains data in clear prose
   - **Coder** — writes Pandas/SQL queries
   - **Sage** — knows your historical data via the vault index
   - **Strategist** — looks at trends and projections
   - **Skeptic** — challenges weak assumptions
   - **Peasant** — asks plain-language follow-up questions
3. **Each drafts an answer** — you see all of them in the transcript
4. **Judge ranks them** — picks the best, or asks for a retry
5. **Verdict** — final answer with a confidence score

The whole process is transparent. No black boxes — you see every model's reasoning.

---

## Privacy guarantee

This product was built privacy-first. Specifically:

- **Your data never leaves this machine.** No telemetry, no analytics, no "model improvement" uploads.
- **Models run locally** via Ollama. We never call OpenAI, Anthropic, or any cloud AI.
- **No accounts.** No login. No "free tier" that secretly logs you.
- **Your vault is yours.** The local database (`vault/`) contains all your conversations and indexed files. Delete it any time.
- **Crash reports stay local.** When something crashes, a redacted log (stack trace, OS info, app version — no message content, no data values) is saved to `vault/logs/crashes/`. Nothing is sent automatically. If you want to email a log to support, you click ✉ Email and your default mail client opens with a draft you review before sending.

If you optionally enable cloud backends (e.g. you point a personality at OpenAI yourself), you'll get a warning every time. The default is fully local.

---

## System requirements

| | Minimum | Recommended |
|---|---|---|
| OS | Windows 10, macOS 12, Ubuntu 22 | Windows 11, macOS 14, Ubuntu 24 |
| RAM | 16 GB | 32 GB |
| Disk free | 20 GB | 40 GB |
| GPU | Optional (CPU works) | NVIDIA 8 GB+ for big models |
| Python | (bundled in installer) | — |

A modern laptop runs the recommended starter model (`qwen2.5:14b`) at usable speed. Bigger models (32B+) want a desktop with a GPU.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| App won't start | Personality config corrupted | Delete `vault/.onboarded` and re-run setup |
| AI panel times out | Ollama not running | Start Ollama from the system tray |
| "Model not found" | First-time model still downloading | Wait, or check Apothecary tab → Pull Model |
| Chart looks empty | Column types wrong | Schema panel → manually set the X column to "date" |
| Slow on a big file | RAM limit | Open the file in chunks, or use a smaller starter model |

---

## What you get for the price

One-time purchase. No subscription, no usage limits, no per-question fees, no "tokens".

- Full Data's Inferno application (Windows / macOS / Linux)
- Free updates within the major version
- Email support
- Right to use on as many of your own machines as you want (one user)

## How licensing works

When you buy a license, you receive a single line of text by email — the **license blob**. Paste it into **Help → Activate License** in the app.

### One-time activation, then fully offline

The first time you activate on a machine, Data's Inferno makes a single HTTPS call to the activation server. The server checks your license is valid, records that this is one of your devices, and returns a signed **activation token** that's saved locally.

From then on, every launch validates the saved token offline — the app never needs the internet again to keep running. **Lose your wifi, work on a plane, sit in a basement — Data's Inferno keeps working.**

### Two devices per license

Your license activates on up to **two devices** at a time (typically a desktop + laptop). The server is the source of truth for the device count.

If you want to move to a third machine:

1. Open **Help → Activate License** on the device you want to retire
2. Click **Deactivate this device** — that frees up a slot
3. Activate on the new machine

If a device is permanently lost or stolen, contact support with your email address and we'll free the slot for you.

### 7-day free trial

The first time you launch the app, a 7-day trial begins automatically. No card required. You get the full feature set during the trial — that's the only honest way to evaluate the product.

When the trial ends, you can either:
- **Activate** with a license blob (continue normally)
- **Continue read-only** (your past sessions remain accessible; no new deliberations)

You don't lose anything either way — the vault is yours.

### Moving to another computer

Open the activation dialog and click **Deactivate this device**. This frees the slot on the server. Then activate on the new machine.

## How updates work

Data's Inferno **must work without internet** — that's a hard requirement. So updates are designed to be optional and non-intrusive:

- On startup, the app makes one quiet HTTPS request to check for a newer version
- If a newer version exists, you see a dialog: *"Update available — open download page / skip this version / remind me later"*
- **Nothing is ever auto-downloaded or auto-replaced.** You click the link, your browser opens, you choose whether to install.
- If you have no internet, the check fails silently and the app launches normally.
- You can disable update checks entirely by setting `DI_UPDATE_MANIFEST_URL=""` before launching.

---

## License

See `LICENSE`. Data's Inferno bundles no third-party model weights — those are downloaded from Ollama's open model library on first run.
