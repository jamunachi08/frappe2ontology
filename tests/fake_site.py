"""Create a throwaway ERPNext-shaped site database in MariaDB for tests.

Builds every ``tab*`` table from the app source DocType JSON, fills the
metadata tables (``tabDocType``, ``tabDocField``, ``tabModule Def``), adds one
Custom Field and one Property Setter like a real site would have, and seeds a
handful of transactions whose correct totals are known.

Not a Frappe installation: there is no Python app, only the tables.
"""

from __future__ import annotations

import uuid

import mysql.connector

from frappe2ontology import build, meta

# Known transactions. Expected totals are asserted in the tests.
COMPANY = "Demo Co"
SALES_INVOICES = [
    # name, customer, docstatus, is_return, base_net_total, outstanding, date
    ("SINV-0001", "CUST-A", 1, 0, 1000, 400, "2026-01-10"),
    ("SINV-0002", "CUST-B", 0, 0, 500, 500, "2026-01-12"),  # draft
    ("SINV-0003", "CUST-A", 2, 0, 700, 0, "2026-02-01"),  # cancelled
    ("SINV-0004", "CUST-A", 1, 1, -200, 0, "2026-02-03"),  # credit note
    ("SINV-0005", "CUST-B", 1, 0, 300, 300, "2026-02-15"),
]
SALES_INVOICE_ITEMS = [
    # parent, item, qty, stock_qty, base_net_amount
    ("SINV-0001", "ITEM-1", 10, 10, 600),
    ("SINV-0001", "ITEM-2", 2, 24, 400),  # sold in boxes of 12
    ("SINV-0002", "ITEM-1", 5, 5, 500),
    ("SINV-0003", "ITEM-1", 7, 7, 700),
    ("SINV-0004", "ITEM-1", -2, -2, -200),
    ("SINV-0005", "ITEM-2", 1, 12, 300),
]
GL_ENTRIES = [
    # account, debit, credit, is_cancelled
    ("Debtors - DC", 1000, 0, 0),
    ("Sales - DC", 0, 1000, 0),
    ("Debtors - DC", 700, 0, 1),  # from the cancelled invoice
    ("Sales - DC", 0, 700, 1),
    ("Debtors - DC", 300, 0, 0),
    ("Sales - DC", 0, 300, 0),
]


def _create_table(cursor, dt: meta.DocType, sel: build.Selection) -> None:
    cols = build._columns_for(dt, sel)
    body = ", ".join(f"`{c.name}` {c.type}" for c in cols)
    ddl = f"CREATE TABLE `{dt.table_name}` ({body}, PRIMARY KEY (`name`)) ROW_FORMAT=DYNAMIC"
    try:
        cursor.execute(ddl)
    except mysql.connector.Error as exc:
        if exc.errno != 1118:  # row size too large: shrink varchars like big doctypes need
            raise
        body = ", ".join(
            f"`{c.name}` {'text' if c.type.startswith('varchar') and c.name != 'name' else c.type}"
            for c in cols
        )
        cursor.execute(
            f"CREATE TABLE `{dt.table_name}` ({body}, PRIMARY KEY (`name`)) ROW_FORMAT=DYNAMIC"
        )


def _insert(cursor, table: str, row: dict) -> None:
    row.setdefault("name", str(uuid.uuid4()))
    cols = ", ".join(f"`{k}`" for k in row)
    marks = ", ".join(["%s"] * len(row))
    cursor.execute(f"INSERT INTO `{table}` ({cols}) VALUES ({marks})", list(row.values()))


def create(root: dict, db_name: str, app_paths: list[str]) -> None:
    doctypes = meta.load_from_json(app_paths)
    modules = {dt.module for dt in doctypes.values()}
    sel = build.select_doctypes(doctypes, modules=modules, exclude_regex=None)

    conn = mysql.connector.connect(**root)
    cursor = conn.cursor()
    cursor.execute(f"DROP DATABASE IF EXISTS `{db_name}`")
    cursor.execute(f"CREATE DATABASE `{db_name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
    cursor.execute(f"USE `{db_name}`")
    for dt in sel.doctypes.values():
        _create_table(cursor, dt, sel)

    for module in sorted(modules):
        app = next(dt.app for dt in doctypes.values() if dt.module == module)
        _insert(cursor, "tabModule Def", {"name": module, "module_name": module, "app_name": app})
    for dt in doctypes.values():
        _insert(
            cursor,
            "tabDocType",
            {
                "name": dt.name,
                "module": dt.module,
                "description": dt.description,
                "istable": int(dt.istable),
                "issingle": int(dt.issingle),
                "is_virtual": int(dt.is_virtual),
                "is_submittable": int(dt.is_submittable),
                "autoname": dt.autoname,
            },
        )
        for idx, f in enumerate(dt.fields, start=1):
            _insert(
                cursor,
                "tabDocField",
                {
                    "parent": dt.name,
                    "parenttype": "DocType",
                    "parentfield": "fields",
                    "idx": idx,
                    "fieldname": f.fieldname,
                    "fieldtype": f.fieldtype,
                    "label": f.label,
                    "options": f.options,
                    "description": f.description,
                    "fetch_from": f.fetch_from,
                    "is_virtual": int(f.is_virtual),
                },
            )

    # Site customisations a JSON export cannot see.
    _insert(
        cursor,
        "tabCustom Field",
        {
            "dt": "Sales Invoice",
            "fieldname": "custom_branch_code",
            "fieldtype": "Link",
            "label": "Branch Code",
            "options": "Branch",
            "description": "Branch that issued the invoice",
        },
    )
    cursor.execute("ALTER TABLE `tabSales Invoice` ADD COLUMN `custom_branch_code` varchar(140)")
    _insert(
        cursor,
        "tabProperty Setter",
        {
            "doc_type": "Sales Invoice",
            "field_name": "customer",
            "doctype_or_field": "DocField",
            "property": "label",
            "value": "Client",
        },
    )

    # Transactions.
    _insert(cursor, "tabCompany", {"name": COMPANY, "company_name": COMPANY, "default_currency": "SAR"})
    for cust in ("CUST-A", "CUST-B"):
        _insert(cursor, "tabCustomer", {"name": cust, "customer_name": f"Customer {cust[-1]}"})
    for name, cust, ds, ret, net, out, date in SALES_INVOICES:
        _insert(
            cursor,
            "tabSales Invoice",
            {
                "name": name,
                "company": COMPANY,
                "customer": cust,
                "customer_name": f"Customer {cust[-1]}",
                "docstatus": ds,
                "is_return": ret,
                "base_net_total": net,
                "outstanding_amount": out,
                "party_account_currency": "SAR",
                "posting_date": date,
                "custom_branch_code": "RUH",
            },
        )
    for idx, (parent, item, qty, sqty, amt) in enumerate(SALES_INVOICE_ITEMS, start=1):
        ds = next(s[2] for s in SALES_INVOICES if s[0] == parent)
        _insert(
            cursor,
            "tabSales Invoice Item",
            {
                "parent": parent,
                "parenttype": "Sales Invoice",
                "parentfield": "items",
                "idx": idx,
                "docstatus": ds,
                "item_code": item,
                "qty": qty,
                "stock_qty": sqty,
                "base_net_amount": amt,
            },
        )
    # A row with the same parent name under another parent DocType: joins
    # that forget parenttype would wrongly pick it up.
    _insert(
        cursor,
        "tabSales Invoice Item",
        {"parent": "SINV-0001", "parenttype": "POS Invoice", "item_code": "ITEM-1",
         "stock_qty": 999, "base_net_amount": 99999},
    )
    for account, dr, cr, canc in GL_ENTRIES:
        _insert(
            cursor,
            "tabGL Entry",
            {"account": account, "debit": dr, "credit": cr, "is_cancelled": canc,
             "company": COMPANY, "docstatus": 1, "posting_date": "2026-01-10"},
        )
    for item, wh, qty, value in (("ITEM-1", "Stores - DC", 40, 4000), ("ITEM-2", "Stores - DC", 12, 1200)):
        _insert(cursor, "tabBin", {"item_code": item, "warehouse": wh, "actual_qty": qty, "stock_value": value})
    for ptype, recv, paid, ds in (("Receive", 600, 600, 1), ("Pay", 250, 250, 1), ("Receive", 999, 999, 2)):
        _insert(
            cursor,
            "tabPayment Entry",
            {"payment_type": ptype, "base_received_amount": recv, "base_paid_amount": paid,
             "docstatus": ds, "posting_date": "2026-02-01"},
        )
    conn.commit()
    conn.close()
