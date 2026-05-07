# Data's Inferno — operator tools

These scripts live outside the customer-facing application and are used
by **you** (the operator) to manage the business side of the product.

| Tool | What it does |
|------|--------------|
| `generate_license.py` | Mint a license blob to email a customer. |
| `license_server.py`   | Activation server the desktop app talks to once per device. |

---

## `generate_license.py`

Mint a license blob:

```bash
# Lifetime license
python tools/generate_license.py --email customer@example.com

# 1-year subscription
python tools/generate_license.py --email customer@example.com --plan subscription

# Subscription with explicit expiry
python tools/generate_license.py --email c@x.com --plan subscription --expires 2027-12-31
```

Output is a single base64 string. Email it to the customer — they paste
it into **Help → Activate License**.

**Validate an existing blob** (useful for support tickets):

```bash
python tools/generate_license.py --check "<the blob the customer sent>"
```

---

## `license_server.py` — activation server

A small Flask app that handles `/activate` and `/deactivate` calls.
Single SQLite file for storage. Stateless container-friendly.

### What it does

- Validates incoming license blobs (HMAC signature)
- Tracks `(license_email, fingerprint) → device_index` rows
- Enforces the device limit (default: 2 per license)
- Returns signed activation tokens the app stores locally

### Local development

```bash
pip install flask
python tools/license_server.py
# Listens on http://127.0.0.1:8080
```

Then in another terminal:

```bash
# Generate a test license
python tools/generate_license.py --email test@example.com

# Activate
curl -X POST http://127.0.0.1:8080/activate \
     -H "Content-Type: application/json" \
     -d '{"license":"<blob>","fingerprint":"abc123..."}'
```

### Production deployment

The server is stateless apart from one SQLite file. Any of these work:

#### Option A — $5 VPS (DigitalOcean / Hetzner / Linode)

```bash
# On the VPS
git clone https://github.com/Infernoplaystuf/Council.git
cd Council
pip install flask gunicorn

# Run via systemd (recommended) or screen
gunicorn --bind 0.0.0.0:8080 \
         --workers 2 \
         --access-logfile /var/log/license-access.log \
         --chdir tools \
         license_server:make_app\(\)
```

Front it with Caddy or Nginx for HTTPS. Bind your DNS:
```
activate.datas-inferno.app  →  your-vps-ip
```

#### Option B — Docker

```dockerfile
FROM python:3.11-slim
COPY licensing.py /app/
COPY tools/license_server.py /app/server.py
WORKDIR /app
RUN pip install flask gunicorn
ENV DI_LICENSE_SECRET="set-via-orchestrator"
ENV DI_MAX_DEVICES="2"
ENV DI_SERVER_DB="/data/licenses.db"
VOLUME /data
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "server:make_app()"]
```

#### Option C — Heroku / Render / Fly.io

`Procfile`:
```
web: gunicorn --chdir tools license_server:make_app\(\)
```

Set the env vars in the platform dashboard:
- `DI_LICENSE_SECRET` — match the value baked into your desktop builds
- `DI_MAX_DEVICES` — default 2
- `DI_SERVER_DB` — persistent path; on Heroku use a managed Postgres
  add-on instead of SQLite (you'll need to swap the storage layer)

#### Option D — AWS Lambda / Cloudflare Workers

Both require porting the SQLite layer to DynamoDB / KV-store. Cleanest
target if you want zero-ops scaling.

### Required env vars

| Variable | Purpose |
|---|---|
| `DI_LICENSE_SECRET` | HMAC secret. **Must match** the value compiled into your desktop builds. |
| `DI_MAX_DEVICES`    | Device limit per license. Default 2. |
| `DI_SERVER_DB`      | SQLite file path. Default `tools/licenses.db`. |
| `DI_SERVER_BIND`    | Bind address for the dev server. Default `127.0.0.1`. |
| `PORT`              | Port for the dev server. Default 8080. |

### Update the desktop client to use it

In `branding.py` (or as a build-time env var):

```python
ACTIVATION_SERVER_URL = os.environ.get(
    "DI_ACTIVATION_SERVER_URL",
    "https://activate.datas-inferno.app",   # ← your URL
)
```

Then rebuild. New builds will activate against your server.

### Operations

- **DB backups**: `cp licenses.db licenses-backup-$(date +%F).db`
  daily. SQLite WAL is fine to copy while the server is running.
- **Health check**: `GET /health` returns `{"ok": true, "db": "ok"}`.
- **Free a slot for a customer**: `DELETE FROM activations WHERE
  license_email = '...' AND fingerprint = '...';`
- **List a customer's devices**: `SELECT * FROM activations WHERE
  license_email = '...';`
- **Bulk free a customer's slots** (e.g. they got a new laptop and
  forgot to deactivate first): `DELETE FROM activations WHERE
  license_email = '...';`

### Threat model

| Attack | Defence | Residual |
|---|---|---|
| Forged license blob | HMAC signature check | None — must have `DI_LICENSE_SECRET` to mint |
| Server compromised | Auth-only on /activate; offline tokens limit blast radius | Existing customers keep working; new activations rejected until restored |
| Replay of old token | Tokens are scoped to (email, fingerprint) | Useless on a different device |
| Sharing license blob | Hard limit at MAX_DEVICES | Two-device sharing OK by design (laptop + desktop is the common case) |
| Faking fingerprint | Possible — fingerprint is on the wire in cleartext | If user spoofs different fingerprints, they get one slot per "device" up to limit. Same as legit. |

The activation server is not the strongest form of DRM available — it
exists primarily to enforce a soft device limit and to give customers
a clean experience when they move machines. A determined attacker who
extracts `DI_LICENSE_SECRET` from a binary can mint their own keys and
their own activation tokens. Accepting that trade-off is what lets the
app stay fully offline after first activation.
