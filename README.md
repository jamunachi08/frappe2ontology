# frappe2ontology

Gives [NVIDIA Auto Ontology](https://github.com/NVIDIA/auto-ontology) an
accurate picture of an ERPNext database by exporting what Frappe already
knows, instead of letting an LLM guess it.

Out of the box, Auto Ontology infers joins from declared foreign keys and
`*_id` column names. ERPNext has neither: Link fields are plain `varchar`
columns pointing at `name`, and child tables hang off `parent`/`parenttype`.
This tool reads the DocType metadata and writes Auto Ontology's native model
YAML with:

| ERPNext                              | Auto Ontology                                     |
| ------------------------------------ | ------------------------------------------------- |
| DocType (`tabSales Invoice`)         | table + term                                      |
| Field                                | column + column attribute, with a description built from the label, help text, Select options, Link target and currency |
| Link field                           | foreign key, join and semantic FK to `target.name` |
| Child table (`Table` field)          | join `child.parent → parent.name`; shared child tables are flagged "filter parenttype" |
| Dynamic Link                         | described (which column holds the DocType), not joined |
| Custom Field / Property Setter       | included in live mode                             |
| Business rules                       | 17 reviewed measures and 2 analyses (see below)   |

Every id is deterministic, so regenerating and re-importing creates no
duplicates.

## Measures included

`frappe2ontology/measures.py` holds SQL attributes that encode the rules a
generic agent gets wrong: only `docstatus = 1`, credit notes are negative
`is_return = 1` invoices, `base_*` is company currency, `outstanding_amount` is
in the party account currency, cancelled ledger rows have `is_cancelled = 1`,
and child joins need `parenttype`. They cover net/gross sales, returns,
receivables, sales by item, quantity in stock UOM, booked and unbilled orders,
purchases, payables, open POs, GL balances, payments in/out, stock on hand,
stock value and stock movement. **Review them with your accountant before
certifying them in Auto Ontology.**

## Usage

1. **Create a read-only database user.** Auto Ontology runs AI-written SQL and
   doesn't check that it's read-only on its main path, so this is the safety
   boundary:

   ```sql
   CREATE USER 'ontology_ro'@'%' IDENTIFIED BY '<strong password>';
   GRANT SELECT ON `_your_site_db`.* TO 'ontology_ro'@'%';
   ```

   The site database name is `db_name` in `sites/<site>/site_config.json`.

2. **Generate the model** from the live site (recommended, because it sees
   your custom fields):

   ```bash
   pip install .
   frappe2ontology \
     --site-db "mysql://ontology_ro:<password>@<db-host>:3306/_your_site_db" \
     --modules Selling Accounts \
     -o erpnext_model.yaml
   ```

   Or from app source, without a database:

   ```bash
   frappe2ontology --apps ~/frappe-bench/apps/frappe ~/frappe-bench/apps/erpnext \
     --db-name _your_site_db --modules Selling Accounts -o erpnext_model.yaml
   ```

   Options:
   * `--modules`: the default is Selling, Buying, Accounts, Stock, Setup.
     Child tables of included DocTypes come along automatically.
   * `--include`: add DocTypes by name, e.g. `--include Employee`. Employee is
     excluded by default because it holds personal data.
   * `--exclude-regex`: by default drops `* Log`, `* Settings`, `* Tool`,
     `Repost *`, `Process *` and similar.
   * `--with-link-targets`: also pull in tables that included DocTypes link to
     (Currency, UOM, Territory, ...).
   * `--no-measures`: export the schema only.

3. **Import into Auto Ontology.** Do this on a fresh deployment, before
   adding the ERPNext connection, so the import creates the catalog and later
   ingestion adopts it by table and column name. Use **Settings → Import /
   Export**, or:

   ```bash
   curl -X POST "http://localhost:3000/api/model/import" \
     -H "x-api-key: $AUTO_ONTOLOGY_API_TOKEN" -F "file=@erpnext_model.yaml"
   ```

   Then add the connection with the same read-only user and database name.
   Set `table_allow_regex` to the tables you exported, so ingestion doesn't
   pull in the other ~600 tables.

## Tests

`tests/test_end_to_end.py` builds a throwaway ERPNext-shaped site in MariaDB
from the app source, including a custom field, a property setter and known
transactions. It checks that:

* the output validates against Auto Ontology's own import schema, and every
  id it references exists;
* every column and join exists in the database;
* every measure parses as MySQL and runs under a SELECT-only user;
* the measures return the correct totals on seeded data, including drafts,
  cancellations, a credit note, mixed UOMs, and a decoy child row with the
  wrong `parenttype`;
* naive SQL on the same data gets it wrong.

```bash
F2O_APPS="apps/frappe apps/erpnext" \
F2O_MYSQL_ROOT="mysql://root:pw@127.0.0.1:3306/" \
F2O_MYSQL_RO="mysql://ro:ro@127.0.0.1:3306/" \
F2O_AUTO_ONTOLOGY=../auto-ontology \
pytest -q tests
```

## Not yet verified

* An actual import into a running Auto Ontology. The output passes Auto
  Ontology's import schema and reference rules, but it hasn't been loaded
  into the full stack yet.
* How Auto Ontology's **Semantic Compilation** treats an imported model.
  Keep it off for the first pilot.
* Only ERPNext's current `develop` DocTypes have been tested. Live mode reads
  your own site, so the version you run is what gets exported.
