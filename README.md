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

### Setup, start to finish

Open **Ontology Export Settings** (search the awesome bar). It is for System
Managers only. The blue note at the top repeats these steps.

**1. Choose what to export.** List the **Modules**, one per line. The default
is Selling, Buying, Accounts, Stock and Setup; child tables come along
automatically. Employee and other personal-data DocTypes stay out unless you
add them under **Also Include DocTypes**. Save, then click
**Model → Preview** to see the size.

**2. Fill in the server details and download the kit.**

* **Auto Ontology URL**: the address people will open, e.g.
  `https://ontology.example.com`. This is your Auto Ontology server, not an AI
  model endpoint. An `https://` domain gets an automatic Let's Encrypt
  certificate.
* **Chat Model Provider**: NVIDIA, Google Gemini, or any OpenAI-compatible
  endpoint, with its **API Key**.
* **NVIDIA API Key**: required for embeddings. It's free at
  build.nvidia.com.
* **Admin Email**.

Then click **Setup → Download Server Kit**. Copy the zip to the server that
will run Auto Ontology (Docker + Compose v2) and run:

```bash
unzip auto-ontology-kit.zip -d auto-ontology-kit && cd auto-ontology-kit
chmod 600 .env && ./setup.sh
```

The kit is NVIDIA's compose stack, hardened:

* only the web app is published;
* the backend, ingestion service and Postgres are internal;
* pgAdmin is removed;
* passwords are generated once and reused when you download again.

`README.txt` in the kit covers DNS, firewall and MariaDB `bind-address`.

**3. Create the read-only database user** on the ERPNext server:

```bash
bench --site <your-site> ontology-readonly-user
```

It grants `SELECT` on exactly the exported tables. It cannot write, and it
cannot read `__Auth` or anything outside the model. It saves the password in
the settings. Re-run it after changing the modules. `--print-sql` prints the
SQL for a DBA instead, for example on Frappe Cloud, where you have no root
access.

Set **Database Host (as seen by Auto Ontology)**:

* `host.docker.internal` when Auto Ontology runs on this same server. MariaDB
  must then also listen on the Docker bridge
  (`bind-address = 127.0.0.1,172.17.0.1`).
* Otherwise, the database server's private IP.

**4. Connect.** Sign in to Auto Ontology and create an API token (user menu →
API Tokens). Paste it into **API Token**, Save, and click
**Setup → Check Setup**. Each line shows ✅, ⚠️ or ❌ with what to fix:

* the model builds;
* the read-only user can read and only read;
* MariaDB is reachable from Docker;
* Auto Ontology is up;
* the token works;
* the connection exists;
* the model is imported.

**5. Push to Auto Ontology.** This runs in the background and sends you a
message when done. It works in three steps, because Auto Ontology checks each
measure's SQL against the database connection:

1. import the schema and terms;
2. add the MariaDB connection, limited to the exported tables;
3. import the measures.

Pushing again is safe: existing items are skipped. Keep **Replace Existing
Definitions** off unless you want edits made in Auto Ontology discarded.
Keep Auto Ontology's *Semantic Compilation* off for the first pilot.

**Without a push:** **Model → Download Model** gives a zip with the two
import files and `HOW-TO-IMPORT.txt`.

From the bench command line:

```bash
bench --site <your-site> execute frappe2ontology.api.export_to_file --kwargs "{'path': '/tmp/model.yaml'}"
bench --site <your-site> execute frappe2ontology.api.push
```

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

* On a site, run `bench --site <site> run-tests --app frappe2ontology`.
  These tests cover:
  * the model against the live schema;
  * every measure running;
  * custom fields and property setters;
  * the download zip;
  * the server kit's contents and that its secrets stay the same across
    downloads;
  * the read-only user's grants;
  * the checklist;
  * the three-step push, against a fake Auto Ontology.

  Optional environment variables enable two more tests:
  * `F2O_DB_ROOT_PASSWORD` creates a real read-only user;
  * `F2O_AO_URL` (plus `F2O_AO_DB_HOST`/`F2O_AO_DB_PORT`) pushes to a real
    Auto Ontology.
* Standalone tests are in `dev_tests/`; see the docstring in
  `dev_tests/test_end_to_end.py`.

Tested on Frappe 15.126 + ERPNext 15.124, MariaDB 10.11. The push was also
tested against a real Auto Ontology backend on Postgres 16 + pgvector, with
embeddings off. It imported 252 terms, added the connection and imported
17 measures, and a second push changed nothing.

### Not yet verified

* The Docker kit on a real server. The compose file validates with
  `docker compose config`, but this test environment could not pull images
  from Docker Hub.
* Embeddings and chat answers, which need real model API keys.
* Gemini as the chat model. Auto Ontology accepts OpenAI-compatible
  endpoints, but it was developed against NVIDIA models.

### License

Apache-2.0. The server kit builds NVIDIA Auto Ontology (Apache-2.0).
