# Sample datasets

Six synthetic-but-realistic CSVs that ship with the home build so I can
test the data search / lookup / connection-finding feature without
exposing real data.

| File | Rows | What it contains |
|------|------|------------------|
| `purchase_orders.csv` | 800  | One year of orders, 6 categories, 3 payment methods, refunds + cancels, deliberate winter dip |
| `inventory.csv`       | 117  | Product master with stock, holding cost, last-movement date. ~8% dead stock |
| `customers.csv`       | 120  | Customer master. ~20% of "loyal" customers flagged dormant |
| `employees.csv`       | 14   | Staff with role + region + tenure |
| `returns.csv`         | 33   | ~5% of completed orders, with reason + refund amount + restock flag |
| `suppliers.csv`       | 5    | Suppliers with lead time, on-time rate, credit terms |

## How they're connected

```
customers ─────────┐
                   │ customer_id
                   ▼
         purchase_orders ◀── product_sku ── inventory ── supplier ──▶ suppliers
                   ▲
                   │ order_id
                   │
                returns
```

Specifically:

- `customers.customer_id` ↔ `purchase_orders.customer_id`
- `inventory.sku` ↔ `purchase_orders.product_sku`
- `inventory.supplier` ↔ `suppliers.supplier_name`
- `purchase_orders.order_id` ↔ `returns.order_id`

This shape is what the **🔍 Look Up** feature in the Council uses to
find connections across files.

## Demo questions to try

These are designed to exercise the cross-file lookup:

- *"Show me everything about customer C3001."* — picks `customers` + `purchase_orders` + (returns if any)
- *"Which suppliers are tied to dead-stock SKUs?"* — joins `inventory` + `suppliers`
- *"Find every return for orders placed in March 2026."* — joins `purchase_orders` + `returns`
- *"What's our overall return rate, broken down by category?"* — joins `purchase_orders` + `returns`
- *"Who's our top revenue customer and what did they order most?"* — joins `customers` + `purchase_orders`
- *"Which employees were hired before 2023?"* — single-file lookup in `employees`

The data is generated with `random.seed(42)` so the same questions
produce the same answers every time — useful for screenshots and
tutorials.
