"""Reviewed ERPNext measures, written once so the agent never improvises them.

These encode the rules a generic text-to-SQL agent gets wrong on ERPNext:

* only ``docstatus = 1`` documents are final (0 = draft, 2 = cancelled);
* credit/debit notes are ordinary invoices with ``is_return = 1`` and
  *negative* amounts, so a plain SUM is already net of returns;
* ``base_*`` fields are in company currency, the others in document currency;
* ``outstanding_amount`` is in the party account's currency, not the company's;
* GL and stock ledger rows of cancelled documents stay, flagged
  ``is_cancelled = 1``;
* child rows join to their parent on ``parent`` **and** ``parenttype``.

Each measure lists the DocTypes it reads, and is only emitted when all of them
are in the model. Treat them as a starting point to review with your
accountant, then certify them in Auto Ontology.
"""

from __future__ import annotations

MEASURES: list[dict] = [
    # ---------------------------------------------------------------- Selling
    {
        "term": "Sales Invoice",
        "name": "Net sales (company currency)",
        "description": (
            "Net sales before tax, in company currency, from submitted sales "
            "invoices. Credit notes (is_return = 1) carry negative amounts, so "
            "this is already net of returns. Group by company when there are "
            "several companies."
        ),
        "sql": """
SELECT SUM(si.base_net_total) AS net_sales
FROM `tabSales Invoice` si
WHERE si.docstatus = 1
""",
        "requires": ["Sales Invoice"],
    },
    {
        "term": "Sales Invoice",
        "name": "Gross sales excluding returns (company currency)",
        "description": (
            "Net-of-tax value of submitted sales invoices that are not credit "
            "notes, in company currency."
        ),
        "sql": """
SELECT SUM(si.base_net_total) AS gross_sales
FROM `tabSales Invoice` si
WHERE si.docstatus = 1 AND si.is_return = 0
""",
        "requires": ["Sales Invoice"],
    },
    {
        "term": "Sales Invoice",
        "name": "Sales returns (company currency)",
        "description": (
            "Value of submitted credit notes (is_return = 1), in company "
            "currency, as a positive number."
        ),
        "sql": """
SELECT -SUM(si.base_net_total) AS sales_returns
FROM `tabSales Invoice` si
WHERE si.docstatus = 1 AND si.is_return = 1
""",
        "requires": ["Sales Invoice"],
    },
    {
        "term": "Sales Invoice",
        "name": "Outstanding receivable by currency",
        "description": (
            "Unpaid amount on submitted sales invoices. outstanding_amount is "
            "in the party account currency, so it is only summed per "
            "party_account_currency, never across currencies."
        ),
        "sql": """
SELECT si.party_account_currency, SUM(si.outstanding_amount) AS outstanding
FROM `tabSales Invoice` si
WHERE si.docstatus = 1 AND si.outstanding_amount <> 0
GROUP BY si.party_account_currency
""",
        "requires": ["Sales Invoice"],
    },
    {
        "term": "Sales Invoice Item",
        "name": "Net sales by item (company currency)",
        "description": (
            "Line-level net sales in company currency from submitted sales "
            "invoices, per item. Use for product, item group or warehouse "
            "breakdowns; returns are included as negative lines."
        ),
        "sql": """
SELECT sii.item_code, SUM(sii.base_net_amount) AS net_sales
FROM `tabSales Invoice Item` sii
JOIN `tabSales Invoice` si
  ON sii.parent = si.name AND sii.parenttype = 'Sales Invoice'
WHERE si.docstatus = 1
GROUP BY sii.item_code
""",
        "requires": ["Sales Invoice", "Sales Invoice Item"],
    },
    {
        "term": "Sales Invoice Item",
        "name": "Quantity sold in stock UOM",
        "description": (
            "Quantity invoiced, converted to each item's stock unit of measure "
            "(stock_qty), from submitted sales invoices. Use stock_qty, not "
            "qty, when adding up quantities across different selling units."
        ),
        "sql": """
SELECT sii.item_code, SUM(sii.stock_qty) AS qty_sold
FROM `tabSales Invoice Item` sii
JOIN `tabSales Invoice` si
  ON sii.parent = si.name AND sii.parenttype = 'Sales Invoice'
WHERE si.docstatus = 1
GROUP BY sii.item_code
""",
        "requires": ["Sales Invoice", "Sales Invoice Item"],
    },
    {
        "term": "Sales Order",
        "name": "Booked order value (company currency)",
        "description": (
            "Net value of submitted sales orders, in company currency, "
            "excluding orders whose status is Closed."
        ),
        "sql": """
SELECT SUM(so.base_net_total) AS booked_value
FROM `tabSales Order` so
WHERE so.docstatus = 1 AND so.status <> 'Closed'
""",
        "requires": ["Sales Order"],
    },
    {
        "term": "Sales Order",
        "name": "Unbilled order value (company currency)",
        "description": (
            "Part of submitted, not-closed sales orders that has not been "
            "invoiced yet, estimated from per_billed (percent billed)."
        ),
        "sql": """
SELECT SUM(so.base_net_total * (100 - so.per_billed) / 100) AS unbilled_value
FROM `tabSales Order` so
WHERE so.docstatus = 1 AND so.status NOT IN ('Closed', 'Completed')
  AND so.per_billed < 100
""",
        "requires": ["Sales Order"],
    },
    # ---------------------------------------------------------------- Buying
    {
        "term": "Purchase Invoice",
        "name": "Net purchases (company currency)",
        "description": (
            "Net value before tax of submitted purchase invoices, in company "
            "currency. Debit notes (is_return = 1) are negative, so this is "
            "net of returns."
        ),
        "sql": """
SELECT SUM(pi.base_net_total) AS net_purchases
FROM `tabPurchase Invoice` pi
WHERE pi.docstatus = 1
""",
        "requires": ["Purchase Invoice"],
    },
    {
        "term": "Purchase Invoice",
        "name": "Outstanding payable by currency",
        "description": (
            "Unpaid amount on submitted purchase invoices, per party account "
            "currency (outstanding_amount is not in company currency)."
        ),
        "sql": """
SELECT pi.party_account_currency, SUM(pi.outstanding_amount) AS outstanding
FROM `tabPurchase Invoice` pi
WHERE pi.docstatus = 1 AND pi.outstanding_amount <> 0
GROUP BY pi.party_account_currency
""",
        "requires": ["Purchase Invoice"],
    },
    {
        "term": "Purchase Order",
        "name": "Open purchase order value (company currency)",
        "description": (
            "Net value of submitted purchase orders that are not yet fully "
            "received, excluding closed and completed ones."
        ),
        "sql": """
SELECT SUM(po.base_net_total) AS open_po_value
FROM `tabPurchase Order` po
WHERE po.docstatus = 1 AND po.status NOT IN ('Closed', 'Completed')
  AND po.per_received < 100
""",
        "requires": ["Purchase Order"],
    },
    # -------------------------------------------------------------- Accounts
    {
        "term": "GL Entry",
        "name": "Account balance (debit minus credit)",
        "description": (
            "Balance per account in company currency from the general ledger: "
            "debit minus credit over GL entries that are not cancelled. Add a "
            "posting_date filter for a balance as of a date."
        ),
        "sql": """
SELECT gle.account, SUM(gle.debit - gle.credit) AS balance
FROM `tabGL Entry` gle
WHERE gle.is_cancelled = 0
GROUP BY gle.account
""",
        "requires": ["GL Entry"],
    },
    {
        "term": "Payment Entry",
        "name": "Payments received (company currency)",
        "description": (
            "Money received from parties on submitted payment entries "
            "(payment_type = 'Receive'), in company currency."
        ),
        "sql": """
SELECT SUM(pe.base_received_amount) AS received
FROM `tabPayment Entry` pe
WHERE pe.docstatus = 1 AND pe.payment_type = 'Receive'
""",
        "requires": ["Payment Entry"],
    },
    {
        "term": "Payment Entry",
        "name": "Payments made (company currency)",
        "description": (
            "Money paid to parties on submitted payment entries "
            "(payment_type = 'Pay'), in company currency."
        ),
        "sql": """
SELECT SUM(pe.base_paid_amount) AS paid
FROM `tabPayment Entry` pe
WHERE pe.docstatus = 1 AND pe.payment_type = 'Pay'
""",
        "requires": ["Payment Entry"],
    },
    # ----------------------------------------------------------------- Stock
    {
        "term": "Bin",
        "name": "Stock on hand by item and warehouse",
        "description": (
            "Current physical quantity (actual_qty, in stock UOM) per item and "
            "warehouse, from the Bin summary table that ERPNext keeps up to "
            "date."
        ),
        "sql": """
SELECT b.item_code, b.warehouse, SUM(b.actual_qty) AS qty_on_hand
FROM `tabBin` b
GROUP BY b.item_code, b.warehouse
""",
        "requires": ["Bin"],
    },
    {
        "term": "Bin",
        "name": "Stock value by warehouse",
        "description": (
            "Current stock value in company currency per warehouse, from Bin."
        ),
        "sql": """
SELECT b.warehouse, SUM(b.stock_value) AS stock_value
FROM `tabBin` b
GROUP BY b.warehouse
""",
        "requires": ["Bin"],
    },
    {
        "term": "Stock Ledger Entry",
        "name": "Stock movement quantity",
        "description": (
            "Net quantity change (stock UOM) per item from stock ledger "
            "entries that are not cancelled. Add a posting_date range for "
            "movement in a period."
        ),
        "sql": """
SELECT sle.item_code, SUM(sle.actual_qty) AS qty_change
FROM `tabStock Ledger Entry` sle
WHERE sle.is_cancelled = 0
GROUP BY sle.item_code
""",
        "requires": ["Stock Ledger Entry"],
    },
]

ANALYSES: list[dict] = [
    {
        "name": "Monthly net sales by company",
        "description": (
            "Net sales in company currency per company and calendar month, "
            "from submitted sales invoices (returns netted)."
        ),
        "sql": """
SELECT si.company,
       DATE_FORMAT(si.posting_date, '%Y-%m') AS month,
       SUM(si.base_net_total) AS net_sales
FROM `tabSales Invoice` si
WHERE si.docstatus = 1
GROUP BY si.company, DATE_FORMAT(si.posting_date, '%Y-%m')
ORDER BY si.company, month
""",
        "requires": ["Sales Invoice"],
    },
    {
        "name": "Top customers by net sales",
        "description": (
            "Customers ranked by net sales in company currency from submitted "
            "sales invoices."
        ),
        "sql": """
SELECT si.customer, si.customer_name, SUM(si.base_net_total) AS net_sales
FROM `tabSales Invoice` si
WHERE si.docstatus = 1
GROUP BY si.customer, si.customer_name
ORDER BY net_sales DESC
LIMIT 20
""",
        "requires": ["Sales Invoice"],
    },
]


def applicable(items: list[dict], doctypes: set[str]) -> list[dict]:
    """Items whose required DocTypes are all in the model."""
    return [i for i in items if set(i["requires"]) <= doctypes]
