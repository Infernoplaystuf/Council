"""Generate a representative Fortune-500 analyst-flavored corpus.

Lands in ~/.council/vault/analyst_eval/ so the Council vault index
picks it up alongside everything else.

Generates:
  orders.csv          - 500 rows, customer_id FK
  customers.json      - 150 records, region/segment
  products.xlsx       - 2 sheets (Products + PriceHistory)
  product_specs.pdf   - 3-page PDF with SKU specifications
  returns.csv         - 60 rows, joins orders + a reason
  q3_summary.md       - narrative report referencing customer C0042
  images/             - 3 synthetic product images (no real content)
"""
from __future__ import annotations

import csv
import json
import random
from pathlib import Path
from datetime import date, timedelta

import openpyxl
from openpyxl import Workbook
from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfWriter
import io

# Reproducible
random.seed(20260603)

VAULT = Path.home() / ".council" / "vault" / "analyst_eval"
VAULT.mkdir(parents=True, exist_ok=True)
(VAULT / "images").mkdir(exist_ok=True)


REGIONS = ["Northeast", "Southeast", "Midwest", "West", "South"]
SEGMENTS = ["Enterprise", "Mid-Market", "SMB"]
PRODUCTS = [
    ("SKU-1001", "Granite Workstation",       3499.0),
    ("SKU-1002", "Granite Rack Server 2U",   12450.0),
    ("SKU-1003", "Slate Edge Gateway",         879.0),
    ("SKU-1004", "Obsidian Storage Array",  24990.0),
    ("SKU-1005", "Quartz Network Switch",    4290.0),
    ("SKU-1006", "Marble Backup Appliance",  6750.0),
]
RETURN_REASONS = ["DOA", "Wrong model", "Customer remorse", "Damaged in transit",
                  "Did not match specs", "Late delivery"]


# ─── customers.json ──────────────────────────────────────────
customers = []
for i in range(1, 151):
    cid = f"C{i:04d}"
    customers.append({
        "customer_id": cid,
        "name": f"Customer {cid}",
        "region": random.choice(REGIONS),
        "segment": random.choice(SEGMENTS),
        "credit_limit_usd": random.choice([25_000, 100_000, 500_000, 2_500_000]),
        "industry": random.choice(["Manufacturing","Healthcare","Financial Services","Retail","Public Sector"]),
        "annual_revenue_usd": random.randint(5_000_000, 5_000_000_000),
    })
# Inject one named, distinctive record for the demo
customers[41] = {
    "customer_id": "C0042",
    "name": "Acme Federal Logistics",
    "region": "Midwest",
    "segment": "Enterprise",
    "credit_limit_usd": 2_500_000,
    "industry": "Public Sector",
    "annual_revenue_usd": 3_400_000_000,
    "notes": "Strategic account — RFP pending Q4 2026",
}
(VAULT / "customers.json").write_text(
    json.dumps(customers, indent=2), encoding="utf-8"
)


# ─── orders.csv ──────────────────────────────────────────────
with (VAULT / "orders.csv").open("w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["order_id","order_date","customer_id","sku","qty","unit_price_usd","total_usd","sales_rep"])
    start = date(2025, 1, 1)
    for i in range(1, 501):
        cust = random.choice(customers)
        sku, name, price = random.choice(PRODUCTS)
        qty = random.randint(1, 25)
        d = start + timedelta(days=random.randint(0, 500))
        w.writerow([f"ORD-{10000+i}", d.isoformat(), cust["customer_id"],
                    sku, qty, price, round(qty*price, 2),
                    random.choice(["Lee","Patel","Garcia","Nguyen","Murphy"])])
# Pin a few high-signal orders against C0042 so cross-file lookup demos land
with (VAULT / "orders.csv").open("a", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["ORD-99001","2026-04-15","C0042","SKU-1004",4,24990.0,99960.0,"Patel"])
    w.writerow(["ORD-99002","2026-05-02","C0042","SKU-1002",6,12450.0,74700.0,"Patel"])
    w.writerow(["ORD-99003","2026-05-19","C0042","SKU-1005",12,4290.0,51480.0,"Patel"])


# ─── returns.csv ─────────────────────────────────────────────
with (VAULT / "returns.csv").open("w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["return_id","order_id","customer_id","sku","qty","reason","return_date"])
    for i in range(1, 61):
        oid = f"ORD-{10000+random.randint(1,500)}"
        sku, _, _ = random.choice(PRODUCTS)
        w.writerow([f"RET-{20000+i}", oid, f"C{random.randint(1,150):04d}",
                    sku, random.randint(1,5), random.choice(RETURN_REASONS),
                    (date(2025,3,1) + timedelta(days=random.randint(0,420))).isoformat()])
# Pin one return against the strategic account
with (VAULT / "returns.csv").open("a", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["RET-99001","ORD-99001","C0042","SKU-1004",1,"Did not match specs","2026-05-10"])


# ─── products.xlsx (multi-sheet) ─────────────────────────────
wb = Workbook()
ws1 = wb.active
ws1.title = "Products"
ws1.append(["sku","name","category","base_price_usd","cogs_usd","gross_margin_pct"])
for sku, name, price in PRODUCTS:
    cogs = round(price * random.uniform(0.45, 0.65), 2)
    margin = round(100 * (price - cogs) / price, 1)
    ws1.append([sku, name, "Compute" if "Server" in name or "Workstation" in name
                          else ("Network" if "Switch" in name or "Gateway" in name
                                else "Storage"),
                price, cogs, margin])
ws2 = wb.create_sheet("PriceHistory")
ws2.append(["sku","effective_date","unit_price_usd","change_reason"])
for sku, _, base in PRODUCTS:
    for k, reason in enumerate(["Launch","Q2 adjustment","Tariff pass-through"]):
        adj = round(base * (0.95 + 0.025*k), 2)
        ws2.append([sku, (date(2025,1,1) + timedelta(days=k*120)).isoformat(),
                    adj, reason])
wb.save(VAULT / "products.xlsx")


# ─── q3_summary.md (narrative referencing C0042) ─────────────
(VAULT / "q3_summary.md").write_text(
    "# Q3 2026 Pipeline Review\n\n"
    "**Strategic account focus**: Acme Federal Logistics (C0042) "
    "showed an outsized signal this quarter. Three orders booked "
    "totalling ~$226K, with a single Obsidian Storage Array unit "
    "(SKU-1004) returned citing spec mismatch (see RET-99001). The "
    "Patel pod owns the account through close. RFP response is "
    "pending; legal review is the only outstanding gate.\n\n"
    "## Risks\n"
    "- Tariff pass-through on PriceHistory may compress margin\n"
    "- Spec-mismatch return is a credibility cost\n",
    encoding="utf-8",
)


# ─── product_specs.pdf (3 pages, real text) ──────────────────
try:
    # Generate via reportlab if available; otherwise fall back to a
    # minimal pypdf path (text-only via canvas).
    try:
        from reportlab.pdfgen.canvas import Canvas
        from reportlab.lib.pagesizes import LETTER
        pdf_path = VAULT / "product_specs.pdf"
        c = Canvas(str(pdf_path), pagesize=LETTER)
        for sku, name, price in PRODUCTS[:3]:
            c.setFont("Helvetica-Bold", 16)
            c.drawString(72, 720, f"{name} ({sku})")
            c.setFont("Helvetica", 11)
            lines = [
                f"List price: ${price:,.2f}",
                "Form factor: 2U rack" if "Rack" in name else "Tower / desk-side",
                "Warranty: 3 years parts + on-site",
                "Power draw: 750W nominal / 1100W peak",
                "Compliance: FIPS 140-3, ENERGY STAR, RoHS",
                "Lead time: 4 weeks; expedited 10 days (+12%)",
                "Notes: certified for Acme Federal Logistics use case",
            ]
            for i, line in enumerate(lines):
                c.drawString(72, 690 - i*18, line)
            c.showPage()
        c.save()
    except ImportError:
        # Minimal alternate path: write text via pypdf if reportlab missing.
        # pypdf can't easily author from scratch; skip with a stub.
        (VAULT / "product_specs.pdf").write_bytes(b"%PDF-1.4\n%no reportlab - stub\n")
except Exception as e:
    print(f"PDF generation failed (non-fatal): {e!r}")


# ─── 3 synthetic product images ──────────────────────────────
for sku, name, _ in PRODUCTS[:3]:
    img = Image.new("RGB", (640, 360), color=(28, 32, 48))
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("arial.ttf", 28)
        font_sm = ImageFont.truetype("arial.ttf", 16)
    except Exception:
        font = ImageFont.load_default()
        font_sm = font
    d.text((20, 20), name, fill=(255, 255, 255), font=font)
    d.text((20, 60), f"SKU {sku}", fill=(200, 200, 220), font=font_sm)
    d.text((20, 320), "PROPRIETARY — INTERNAL USE", fill=(120, 130, 150), font=font_sm)
    # Draw a fake "product silhouette"
    d.rectangle((80, 120, 560, 280), outline=(180, 200, 255), width=3)
    d.line((80, 120, 560, 280), fill=(70, 80, 120), width=1)
    img.save(VAULT / "images" / f"{sku.lower()}.png")


print(f"Corpus written under: {VAULT}")
for p in sorted(VAULT.rglob("*")):
    if p.is_file():
        sz = p.stat().st_size
        rel = p.relative_to(VAULT)
        print(f"  {sz:>10,} B   {rel}")
