# Sample datasets

Three synthetic-but-realistic CSVs that ship with Data's Inferno so new users
can try the tool without exposing their own data.

| File | Rows | What it contains |
|------|------|------------------|
| `purchase_orders.csv` | 800  | One year of orders across 120 customers, 6 categories, 3 payment methods. Includes some refunds and cancellations, with a deliberate seasonality dip in winter. |
| `inventory.csv`       | 117  | Product master with stock levels, holding costs, last-movement dates, suppliers. ~8% are "dead stock" (no sales in 6+ months). |
| `customers.csv`       | 120  | Customer master with segment (one-time / occasional / loyal), first/last order dates, lifetime spend. ~20% of "loyal" customers are flagged dormant. |

## Demo questions to try

After loading these files into the **🗄 Vault** tab, ask the **⚖ Council**:

- *"Which suppliers have the most dead-stock SKUs?"*
- *"Show me revenue by category, month over month."*
- *"Identify loyal customers who haven't ordered in 60+ days."*
- *"What's the average order value by payment method?"*
- *"Which products generate the most revenue per dollar of holding cost?"*

The data is generated with `random.seed(42)` so the same questions produce
the same answers every time — useful for screenshots and tutorials.
