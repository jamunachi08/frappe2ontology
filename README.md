## Frappe2Ontology

A Frappe app that gives [NVIDIA Auto Ontology](https://github.com/NVIDIA/auto-ontology)
an accurate picture of an ERPNext database, by exporting what Frappe already
knows instead of letting an LLM guess it.

Out of the box, Auto Ontology infers joins from declared foreign keys and
`*_id` column names. ERPNext has neither: Link fields are plain `varchar`
columns pointing at `name`, and child tables hang off `parent`/`parenttype`.
This app reads the DocType metadata (Custom Fields and Property Setters
included) and writes Auto Ontology's native model YAML:

| ERPNext                        | Auto Ontology                                      |
| ------------------------------ | -------------------------------------------------- |
| DocType (`tabSales Invoice`)   | table + term                                       |
| Field                          | column + column attribute, described from the label, help text, Select options, Link target and currency |
| Link field                     | foreign key, join and semantic FK to `target.name` |
| Child table (`Table` field)    | join `child.parent → parent.name`; shared child tables are flagged "filter parenttype" |
| Dynamic Link                   | described (which column holds the DocType), not joined |
| Business rules                 | 17 reviewed measures and 2 analyses                |

Every id is deterministic, so exporting again and re-importing creates no
duplicates.

### Installation

```bash
cd ~/frappe-bench
bench get-app https://github.com/jamunachi08/frappe2ontology.git
bench --site <your-site> install-app frappe2ontology
```

Tested on Frappe 15.126 with ERPNext 15.124. The app declares Frappe
`>=15,<17`.

### Use

Open **Ontology Export Settings** (search the awesome bar). System Manager only.

1. Pick the **Modules** to export, one per line. The default is Selling,
   Buying, Accounts, Stock and Setup. Child tables come along automatically.
   Employee and other personal-data DocTypes are excluded unless you add them
   under **Also Include DocTypes**.
2. Save, then click **Preview** to see how many terms, columns and joins the
   model will have.
3. Either:
   * click **Download Model** and import the YAML in Auto Ontology under
     **Settings → Import / Export**; or
   * set **Auto Ontology URL** and **API Token**, then click
     **Push to Auto Ontology**. This calls `POST /api/model/import`. Leave
     **Replace Existing Definitions** off unless you want Auto Ontology edits
     for this database discarded.

From the command line on the bench:

```bash
bench --site <your-site> execute frappe2ontology.api.export_to_file --kwargs "{'path': '/tmp/model.yaml'}"
```

### Connecting Auto Ontology to the site safely

Auto Ontology runs AI-written SQL, and its main path does not check that the
SQL is read-only. Give it a **SELECT-only** database user, never the site's
own user:

```sql
CREATE USER 'ontology_ro'@'%' IDENTIFIED BY '<strong password>';
GRANT SELECT ON `<db_name>`.* TO 'ontology_ro'@'%';
```

`db_name` is in `sites/<site>/site_config.json`, and **Preview** shows it too.
The model's database name must match the one in Auto Ontology's connection
string. Import the model on a fresh Auto Ontology *before* adding the
connection, so ingestion adopts the imported catalog by table and column name.
Set the connection's `table_allow_regex` to the exported tables.

### Measures

`frappe2ontology/measures.py` encodes the rules a generic agent gets wrong:

* only `docstatus = 1` is final;
* credit notes are negative `is_return = 1` invoices;
* `base_*` fields are in company currency;
* `outstanding_amount` is in the party account currency;
* cancelled ledger rows have `is_cancelled = 1`;
* child joins need `parenttype`.

The measures cover net and gross sales, returns, receivables, sales by item,
quantity in stock UOM, booked and unbilled orders, purchases, payables, open
POs, GL balances, payments, stock on hand, stock value and stock movement.
**Review them with your accountant before certifying them in Auto Ontology.**

### Outside a bench

The exporter also runs as a plain CLI against a site database or app source:

```bash
pip install "frappe2ontology[standalone] @ git+https://github.com/jamunachi08/frappe2ontology.git"
frappe2ontology --site-db "mysql://ontology_ro:<pw>@<host>:3306/<db_name>" --modules Selling Accounts -o model.yaml
```

### Tests

* On a site: `bench --site <site> run-tests --app frappe2ontology`. These
  check that the model matches the live schema, that every measure runs,
  that custom fields and property setters are picked up, and that download
  and push work against a mock Auto Ontology.
* Standalone: `dev_tests/`. These build a fake site in MariaDB with known
  transactions and check that the measures return correct totals and that
  the output validates against Auto Ontology's own import schema. The
  environment variables are listed in `dev_tests/test_end_to_end.py`.

### Not yet verified

* A push to a running Auto Ontology. The push was tested against a mock of
  its import endpoint, and the YAML validates against its import schema.
* How Auto Ontology's Semantic Compilation treats an imported model. Keep it
  off for the first pilot.

### License

Apache-2.0
