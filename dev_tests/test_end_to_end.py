"""End-to-end checks against real DocType JSON and a real MariaDB.

Environment:
  F2O_APPS            space-separated app source dirs (frappe, erpnext)
  F2O_MYSQL_ROOT      mysql://root@127.0.0.1:3307/ (admin, to create the site)
  F2O_MYSQL_RO        mysql://ro:ro@127.0.0.1:3307/ (SELECT-only user)
  F2O_AUTO_ONTOLOGY   path to an auto-ontology checkout (for its import schema)
"""

from __future__ import annotations

import importlib.util
import os
import sys
from decimal import Decimal

import mysql.connector
import pytest
import sqlglot
import yaml
from sqlglot import exp

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from frappe2ontology import build, cli, measures, meta  # noqa: E402
import fake_site  # noqa: E402

APPS = os.environ.get("F2O_APPS", "").split()
ROOT = os.environ.get("F2O_MYSQL_ROOT")
RO = os.environ.get("F2O_MYSQL_RO")
AO = os.environ.get("F2O_AUTO_ONTOLOGY")
DB = "_f2o_test_site"

pytestmark = pytest.mark.skipif(not (APPS and ROOT and RO and AO), reason="needs env, see docstring")


def _kwargs(url: str, db: str | None = None) -> dict:
    k = cli._connect_kwargs(url.rstrip("/") + "/" + (db or "mysql"))
    return k


@pytest.fixture(scope="module")
def site():
    fake_site.create(_kwargs(ROOT), DB, APPS)
    root = mysql.connector.connect(**_kwargs(ROOT))
    cur = root.cursor()
    user = cli._connect_kwargs(RO.rstrip("/") + "/x")
    cur.execute(f"GRANT SELECT ON `{DB}`.* TO '{user['user']}'@'{user['host']}'")
    root.commit()
    root.close()
    return _kwargs(RO, DB)


@pytest.fixture(scope="module")
def schema_cls():
    path = os.path.join(AO, "auto_ontology/server/model_interchange/schemas.py")
    spec = importlib.util.spec_from_file_location("ao_schemas", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ao_schemas"] = mod
    spec.loader.exec_module(mod)
    return mod.AutoOntologyModelDocument


def _build(doctypes):
    sel = build.select_doctypes(doctypes)
    names = set(sel.doctypes)
    return sel, build.build_model(
        sel,
        DB,
        measures=measures.applicable(measures.MEASURES, names),
        analyses=measures.applicable(measures.ANALYSES, names),
    )


@pytest.fixture(scope="module")
def json_model():
    return _build(meta.load_from_json(APPS))


@pytest.fixture(scope="module")
def live_model(site):
    return _build(meta.load_from_db(site))


def _table(model, name):
    for t in model["data_layer"]["databases"][0]["schemas"][0]["tables"]:
        if t["name"] == name:
            return t
    raise KeyError(name)


def _term(model, name):
    return next(t for t in model["semantic_layer"]["terms"] if t["name"] == name)


# ------------------------------------------------------------------ shape


@pytest.mark.parametrize("which", ["json_model", "live_model"])
def test_validates_against_auto_ontology_schema(which, request, schema_cls):
    _sel, model = request.getfixturevalue(which)
    text = yaml.safe_dump(model, sort_keys=False)
    doc = schema_cls.model_validate(yaml.safe_load(text))
    assert len(doc.semantic_layer.terms) > 200
    assert build.check_references(model) == []


def test_ids_are_unique_and_deterministic(json_model):
    _sel, model = json_model
    _sel2, again = _build(meta.load_from_json(APPS))
    assert model == again
    ids = []
    for t in model["data_layer"]["databases"][0]["schemas"][0]["tables"]:
        ids.append(t["id"])
        ids += [c["id"] for c in t["columns"]]
    for t in model["semantic_layer"]["terms"]:
        ids.append(t["id"])
        ids += [a["id"] for a in t["columns_attributes"]]
    assert len(ids) == len(set(ids))


def test_attribute_names_unique_per_term(json_model):
    _sel, model = json_model
    for term in model["semantic_layer"]["terms"]:
        names = [a["name"].lower() for a in term["columns_attributes"]]
        assert len(names) == len(set(names)), term["name"]


def test_sensitive_and_noise_doctypes_excluded_by_default(json_model):
    sel, _model = json_model
    assert not [n for n in sel.doctypes if n.startswith("Employee")]
    assert not [n for n in sel.doctypes if n.endswith((" Settings", " Log"))]
    assert "Sales Invoice" in sel.doctypes and "Sales Invoice Item" in sel.doctypes


def test_links_and_child_tables_become_joins(json_model):
    _sel, model = json_model
    si, sii, cust = (_table(model, n) for n in ("tabSales Invoice", "tabSales Invoice Item", "tabCustomer"))
    joins = {(j["source_table_id"], j["target_table_id"]): j["join_columns"] for j in model["data_layer"]["joins"]}
    assert {"source": "customer", "target": "name"} in joins[(si["id"], cust["id"])]
    assert joins[(sii["id"], si["id"])] == [{"source": "parent", "target": "name"}]
    # Self-links (return_against -> Sales Invoice) are described, not joined.
    assert (si["id"], si["id"]) not in joins
    ret = next(c for c in si["columns"] if c["name"] == "return_against")
    assert "another Sales Invoice" in ret["description"]


def test_descriptions_carry_erpnext_rules(json_model):
    _sel, model = json_model
    si = {c["name"]: c["description"] for c in _table(model, "tabSales Invoice")["columns"]}
    assert "docstatus = 1" in si["docstatus"]
    assert "company's default currency" in si["base_net_total"]
    assert "party_account_currency" in si["outstanding_amount"]
    assert "One of:" in si["status"] and "Overdue" in si["status"]
    assert "1 = Yes" in si["is_return"]
    gle = {c["name"]: c["description"] for c in _table(model, "tabGL Entry")["columns"]}
    assert "`party_type`" in gle["party"]  # Dynamic Link explained
    stc = _term(model, "Sales Taxes and Charges")
    assert "filter parenttype" in stc["description"]  # shared child table


# -------------------------------------------------------------- live site


def test_live_mode_sees_site_customisations(live_model):
    _sel, model = live_model
    si = {c["name"]: c for c in _table(model, "tabSales Invoice")["columns"]}
    assert "custom_branch_code" in si
    assert "Custom field" in si["custom_branch_code"]["description"]
    assert si["customer"]["description"].startswith("Client.")  # Property Setter label
    branch = _table(model, "tabBranch")
    fks = model["data_layer"]["foreign_keys"]
    assert {"source_column_id": si["custom_branch_code"]["id"],
            "target_column_id": next(c["id"] for c in branch["columns"] if c["name"] == "name")} in fks


def test_every_column_and_join_exists_in_database(site, live_model):
    _sel, model = live_model
    conn = mysql.connector.connect(**site)
    cur = conn.cursor()
    cur.execute("SELECT TABLE_NAME, COLUMN_NAME FROM information_schema.columns WHERE TABLE_SCHEMA = DATABASE()")
    real = set(cur.fetchall())
    conn.close()
    tables = {t["id"]: t for t in model["data_layer"]["databases"][0]["schemas"][0]["tables"]}
    for t in tables.values():
        for c in t["columns"]:
            assert (t["name"], c["name"]) in real
    for j in model["data_layer"]["joins"]:
        for pair in j["join_columns"]:
            assert (tables[j["source_table_id"]]["name"], pair["source"]) in real
            assert (tables[j["target_table_id"]]["name"], pair["target"]) in real


# ---------------------------------------------------------------- measures


def _run(site, sql):
    conn = mysql.connector.connect(**site)
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(sql)
        return cur.fetchall()
    finally:
        conn.close()


def test_every_measure_parses_and_runs_read_only(site, live_model):
    _sel, model = live_model
    items = model["semantic_layer"]["sql_attributes"]["manual"] + model["semantic_layer"]["custom_analyses"]
    assert len(items) == len(measures.MEASURES) + len(measures.ANALYSES)
    for item in items:
        tree = sqlglot.parse_one(item["sql"], read="mysql")
        assert isinstance(tree, exp.Select), item["name"]
        _run(site, item["sql"])  # raises on unknown table/column


EXPECTED = {
    "Net sales (company currency)": 1100,  # 1000 - 200 + 300; draft and cancelled excluded
    "Gross sales excluding returns (company currency)": 1300,
    "Sales returns (company currency)": 200,
    "Payments received (company currency)": 600,  # cancelled 999 excluded
    "Payments made (company currency)": 250,
}


def test_measures_give_correct_answers(site, live_model):
    _sel, model = live_model
    by_name = {m["name"]: m["sql"] for m in model["semantic_layer"]["sql_attributes"]["manual"]}
    for name, want in EXPECTED.items():
        (row,) = _run(site, by_name[name])
        assert Decimal(list(row.values())[0]) == want, name

    rows = _run(site, by_name["Net sales by item (company currency)"])
    got = {r["item_code"]: Decimal(r["net_sales"]) for r in rows}
    assert got == {"ITEM-1": 400, "ITEM-2": 700}  # the POS-parented decoy is ignored

    rows = _run(site, by_name["Quantity sold in stock UOM"])
    assert {r["item_code"]: Decimal(r["qty_sold"]) for r in rows} == {"ITEM-1": 8, "ITEM-2": 36}

    rows = _run(site, by_name["Account balance (debit minus credit)"])
    assert {r["account"]: Decimal(r["balance"]) for r in rows} == {"Debtors - DC": 1300, "Sales - DC": -1300}

    (row,) = _run(site, by_name["Outstanding receivable by currency"])
    assert (row["party_account_currency"], Decimal(row["outstanding"])) == ("SAR", 700)


def test_naive_sql_would_be_wrong(site):
    """What an agent without these rules writes, and why the measures matter."""
    (row,) = _run(site, "SELECT SUM(base_net_total) AS s FROM `tabSales Invoice`")
    assert Decimal(row["s"]) == 2300  # counts the draft and the cancelled invoice
    (row,) = _run(
        site,
        "SELECT SUM(sii.base_net_amount) AS s FROM `tabSales Invoice Item` sii "
        "JOIN `tabSales Invoice` si ON sii.parent = si.name WHERE si.docstatus = 1",
    )
    assert Decimal(row["s"]) != 1100  # forgot parenttype


def test_read_only_user_cannot_write(site):
    conn = mysql.connector.connect(**site)
    try:
        with pytest.raises(mysql.connector.Error):
            conn.cursor().execute("DELETE FROM `tabSales Invoice`")
    finally:
        conn.close()


def test_cli_writes_yaml(site, tmp_path):
    out = tmp_path / "model.yaml"
    url = RO.rstrip("/") + "/" + DB
    assert cli.main(["--site-db", url, "--modules", "Selling", "Accounts", "-o", str(out)]) == 0
    doc = yaml.safe_load(out.read_text())
    assert any(t["name"] == "Sales Invoice" for t in doc["semantic_layer"]["terms"])
