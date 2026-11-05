# Copyright (c) 2026, Neotec and contributors
# For license information, please see license.txt
"""Run with: bench --site <site> run-tests --app frappe2ontology

Optional environment, for the tests that touch real systems:
  F2O_DB_ROOT_PASSWORD  MariaDB root password: creates the read-only user
  F2O_AO_URL            a running Auto Ontology (empty catalog) to push to
"""

import io
import json
import os
import threading
import zipfile
from email.parser import BytesParser
from email.policy import default as email_policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import frappe
import yaml

try:  # Frappe v16
    from frappe.tests import IntegrationTestCase as TestCase
except ImportError:  # Frappe v15
    from frappe.tests.utils import FrappeTestCase as TestCase

from frappe2ontology import aoclient, api, build, checks, kit, measures, readonly

HAS_ERPNEXT = "erpnext" in frappe.get_installed_apps()
ROOT_PW = os.environ.get("F2O_DB_ROOT_PASSWORD")
AO_URL = os.environ.get("F2O_AO_URL")


def _tables(model):
    return {t["name"]: t for t in model["data_layer"]["databases"][0]["schemas"][0]["tables"]}


class FakeAutoOntology:
    """Records requests to the few endpoints the app calls."""

    def __init__(self):
        self.requests = []
        self.connections = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def _reply(self, body, status=200):
                out = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def do_GET(self):
                url = urlparse(self.path)
                fake.requests.append(("GET", url.path, parse_qs(url.query), self.headers.get("x-api-key"), None))
                if url.path == "/api/health":
                    return self._reply({"status": "ok"})
                if url.path == "/api/connections":
                    return self._reply({"data": fake.connections})
                if url.path == "/api/terms":
                    return self._reply({"terms": [{"name": parse_qs(url.query)["query"][0]}]})
                self._reply({"detail": "not found"}, 404)

            def do_POST(self):
                url = urlparse(self.path)
                body = self.rfile.read(int(self.headers["Content-Length"]))
                if url.path == "/api/model/import":
                    msg = BytesParser(policy=email_policy).parsebytes(
                        b"Content-Type: " + self.headers["Content-Type"].encode() + b"\r\n\r\n" + body
                    )
                    part = next(msg.iter_parts())
                    payload = {"field": part.get_param("name", header="content-disposition"),
                               "model": yaml.safe_load(part.get_content())}
                    fake.requests.append(("POST", url.path, parse_qs(url.query), self.headers.get("x-api-key"), payload))
                    return self._reply({"success": True, "summary": {"created": {"terms": 1}}})
                if url.path == "/api/connections":
                    conn = json.loads(body)["connection"]
                    fake.requests.append(("POST", url.path, {}, self.headers.get("x-api-key"), conn))
                    fake.connections.append({"database_name": conn["database"], "connection": conn})
                    return self._reply({"data": {"database_name": conn["database"]}}, 201)
                self._reply({"detail": "not found"}, 404)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class TestOntologyExportSettings(TestCase):
    def setUp(self):
        settings = frappe.get_single(api.SETTINGS)
        settings.modules = "Selling\nBuying\nAccounts\nStock\nSetup" if HAS_ERPNEXT else "Core"
        settings.include_doctypes = ""
        settings.exclude_regex = build.DEFAULT_EXCLUDE
        settings.with_link_targets = 0
        settings.include_measures = 1
        settings.create_connection = 1
        settings.replace_existing = 0
        settings.ro_db_host = "host.docker.internal"
        settings.ro_db_user = "f2o_test_ro"
        settings.save()

    # ------------------------------------------------------------ model

    def test_model_is_consistent_and_matches_database(self):
        result = api.build_export()
        self.assertEqual(build.check_references(result.model), [])
        real = set(
            frappe.db.sql(
                "SELECT TABLE_NAME, COLUMN_NAME FROM information_schema.columns WHERE TABLE_SCHEMA = DATABASE()"
            )
        )
        tables = _tables(result.model)
        by_id = {t["id"]: t["name"] for t in tables.values()}
        for t in tables.values():
            for c in t["columns"]:
                self.assertIn((t["name"], c["name"]), real)
        for j in result.model["data_layer"]["joins"]:
            for pair in j["join_columns"]:
                self.assertIn((by_id[j["source_table_id"]], pair["source"]), real)
                self.assertIn((by_id[j["target_table_id"]], pair["target"]), real)
        self.assertEqual(yaml.safe_load(result.yaml_text), result.model)

    def test_every_measure_runs_on_this_site(self):
        if not HAS_ERPNEXT:
            self.skipTest("ERPNext not installed")
        sl = api.build_export().model["semantic_layer"]
        items = sl["sql_attributes"]["manual"] + sl["custom_analyses"]
        self.assertEqual(len(items), len(measures.MEASURES) + len(measures.ANALYSES))
        for item in items:
            frappe.db.sql(item["sql"])

    def test_erpnext_semantics(self):
        if not HAS_ERPNEXT:
            self.skipTest("ERPNext not installed")
        tables = _tables(api.build_export().model)
        si = {c["name"]: c["description"] for c in tables["tabSales Invoice"]["columns"]}
        self.assertIn("docstatus = 1", si["docstatus"])
        self.assertIn("Links to Customer", si["customer"])
        self.assertNotIn("tabEmployee", tables)

    def test_custom_field_and_property_setter_are_picked_up(self):
        if not HAS_ERPNEXT:
            self.skipTest("ERPNext not installed")
        from frappe.custom.doctype.custom_field.custom_field import create_custom_field
        from frappe.custom.doctype.property_setter.property_setter import make_property_setter

        create_custom_field(
            "Sales Invoice",
            {"fieldname": "f2o_branch_code", "fieldtype": "Link", "options": "Branch",
             "label": "F2O Branch Code", "insert_after": "customer"},
        )
        make_property_setter("Sales Invoice", "customer", "label", "Client", "Data")
        frappe.clear_cache(doctype="Sales Invoice")
        try:
            si = {c["name"]: c for c in _tables(api.build_export().model)["tabSales Invoice"]["columns"]}
            self.assertIn("f2o_branch_code", si)
            self.assertIn("Custom field", si["f2o_branch_code"]["description"])
            self.assertTrue(si["customer"]["description"].startswith("Client."))
        finally:
            frappe.delete_doc("Custom Field", "Sales Invoice-f2o_branch_code", force=True)
            for name in frappe.get_all(
                "Property Setter", {"doc_type": "Sales Invoice", "field_name": "customer", "property": "label"},
                pluck="name",
            ):
                frappe.delete_doc("Property Setter", name, force=True)
            frappe.clear_cache(doctype="Sales Invoice")

    def test_download_model_zip(self):
        api.download_model()
        self.assertEqual(frappe.response.type, "download")
        archive = zipfile.ZipFile(io.BytesIO(frappe.response.filecontent))
        self.assertEqual(
            sorted(archive.namelist()),
            ["1-schema-and-terms.yaml", "2-full-model.yaml", "HOW-TO-IMPORT.txt", "table-allow-regex.txt"],
        )
        phase1 = yaml.safe_load(archive.read("1-schema-and-terms.yaml"))
        full = yaml.safe_load(archive.read("2-full-model.yaml"))
        self.assertEqual(phase1["semantic_layer"]["sql_attributes"]["manual"], [])
        self.assertEqual(phase1["data_layer"], full["data_layer"])
        if HAS_ERPNEXT:
            self.assertTrue(full["semantic_layer"]["sql_attributes"]["manual"])

    # ------------------------------------------------------------- push

    def _point_at(self, url, token="auto_ontology_test_token"):
        settings = frappe.get_single(api.SETTINGS)
        settings.auto_ontology_url = url
        settings.api_token = token
        settings.ro_db_password = "secret-ro-pw"
        settings.save()

    def test_push_runs_three_phases(self):
        fake = FakeAutoOntology()
        try:
            self._point_at(fake.url)
            out = api.run_push()
            again = api.run_push()  # connection now exists: not created twice
        finally:
            fake.close()
        calls = [(m, p) for m, p, *_ in fake.requests]
        expected_first = [("POST", "/api/model/import"), ("GET", "/api/connections"),
                          ("POST", "/api/connections"), ("POST", "/api/model/import")]
        self.assertEqual(calls[:4], expected_first)
        self.assertNotIn(("POST", "/api/connections"), calls[4:])
        self.assertTrue(all(r[3] == "auto_ontology_test_token" for r in fake.requests))

        phase1, conn, phase2 = fake.requests[0], fake.requests[2][4], fake.requests[3]
        self.assertEqual(phase1[2]["replace"], ["false"])
        self.assertEqual(phase1[4]["field"], "file")
        self.assertEqual(phase1[4]["model"]["semantic_layer"]["sql_attributes"]["manual"], [])
        self.assertEqual(conn["type"], "mysql")
        self.assertEqual(conn["host"], "host.docker.internal")
        self.assertEqual(conn["user"], "f2o_test_ro")
        self.assertEqual(conn["password"], "secret-ro-pw")
        self.assertEqual(conn["database"], frappe.conf.db_name)
        self.assertRegex("tabSales Invoice" if HAS_ERPNEXT else "tabUser", conn["table_allow_regex"])
        if HAS_ERPNEXT:
            self.assertTrue(phase2[4]["model"]["semantic_layer"]["sql_attributes"]["manual"])
        self.assertEqual(len(out["steps"]), 3)
        self.assertIn("already present", " ".join(again["steps"]))
        self.assertIn("Pushed to", frappe.db.get_single_value(api.SETTINGS, "last_run_summary"))

    def test_push_against_real_auto_ontology(self):
        """Needs F2O_AO_URL, F2O_DB_ROOT_PASSWORD, and F2O_AO_DB_HOST/PORT: how that
        Auto Ontology reaches this MariaDB."""
        if not (AO_URL and ROOT_PW):
            self.skipTest("Set F2O_AO_URL and F2O_DB_ROOT_PASSWORD")
        readonly.setup(root_password=ROOT_PW)
        settings = frappe.get_single(api.SETTINGS)
        settings.auto_ontology_url = AO_URL
        settings.api_token = "unused-direct-backend"
        settings.ro_db_host = os.environ.get("F2O_AO_DB_HOST", "127.0.0.1")
        settings.ro_db_port = int(os.environ.get("F2O_AO_DB_PORT", frappe.conf.db_port or 3306))
        settings.save()
        out = api.run_push(embed=False)
        print("\n" + "\n".join(out["steps"]))
        client = aoclient.Client(AO_URL, None)
        self.assertIn(frappe.conf.db_name, {c["database_name"] for c in client.list_connections()})
        if HAS_ERPNEXT:
            self.assertTrue(any(t["name"] == "Sales Invoice" for t in client.find_terms("Sales Invoice")))
            self.assertIn("17 new measures", " ".join(out["steps"]))
        again = api.run_push(embed=False)
        print("\n".join(again["steps"]))
        self.assertIn("0 new terms", again["steps"][0])
        self.assertIn("already present", again["steps"][1])

    # -------------------------------------------------------------- kit

    def _kit_settings(self, url="https://ontology.example.com"):
        settings = frappe.get_single(api.SETTINGS)
        settings.auto_ontology_url = url
        settings.admin_email = "admin@example.com"
        settings.llm_provider = "Google Gemini"
        settings.llm_endpoint, settings.llm_model = kit.PROVIDERS["Google Gemini"]
        settings.llm_api_key = "gemini-key"
        settings.nvidia_api_key = "nvapi-key"
        for field in ("kit_postgres_password", "kit_admin_password", "kit_auth_secret"):
            settings.set(field, "")
        settings.save()

    def _kit(self):
        api.download_server_kit()
        return zipfile.ZipFile(io.BytesIO(frappe.response.filecontent))

    def test_server_kit(self):
        self._kit_settings()
        archive = self._kit()
        self.assertEqual(
            sorted(archive.namelist()), [".env", "Caddyfile", "README.txt", "docker-compose.yml", "setup.sh"]
        )
        env = dict(
            line.split("=", 1) for line in archive.read(".env").decode().splitlines()
            if line and not line.startswith("#")
        )
        self.assertEqual(env["APP_URL"], "https://ontology.example.com")
        self.assertEqual(env["COMPOSE_PROFILES"], "https")
        self.assertEqual(env["DOMAIN"], "ontology.example.com")
        self.assertEqual(env["AO_BIND"], "127.0.0.1")
        self.assertEqual(env["REASONING_ENDPOINT"], kit.PROVIDERS["Google Gemini"][0])
        self.assertEqual(env["REASONING_API_KEY"], "gemini-key")
        self.assertEqual(env["EMBED_API_KEY"], "nvapi-key")
        self.assertEqual(env["EMBED_ENDPOINT"], kit.NVIDIA_ENDPOINT)
        self.assertEqual(env["CONNECTION_STRINGS"], "")
        self.assertGreaterEqual(len(env["AUTH_SECRET"]), 32)
        self.assertEqual(archive.getinfo("setup.sh").external_attr >> 16, 0o755)
        self.assertEqual(archive.getinfo(".env").external_attr >> 16, 0o600)
        compose = yaml.safe_load(archive.read("docker-compose.yml"))
        published = {name: svc.get("ports") for name, svc in compose["services"].items() if svc.get("ports")}
        self.assertEqual(set(published), {"auto-ontology-frontend", "caddy"})
        self.assertIn("bind-address = 127.0.0.1,172.17.0.1", archive.read("README.txt").decode())

        # Secrets are generated once and reused, so a re-download matches the running server.
        env_again = self._kit().read(".env").decode()
        self.assertIn(f"AUTH_SECRET={env['AUTH_SECRET']}", env_again)
        self.assertIn(f"POSTGRES_PASSWORD={env['POSTGRES_PASSWORD']}", env_again)

    def test_kit_network_plans(self):
        self.assertEqual(kit.network_plan("http://127.0.0.1:3000")["bind"], "127.0.0.1")
        self.assertEqual(kit.network_plan("http://10.0.0.5:3000")["mode"], "plain-http")
        self.assertRaises(kit.KitError, kit.network_plan, "ontology.example.com")

    def test_kit_requires_keys(self):
        self._kit_settings()
        settings = frappe.get_single(api.SETTINGS)
        settings.nvidia_api_key = ""
        settings.save()
        self.assertRaises(frappe.ValidationError, api.download_server_kit)

    # -------------------------------------------------- read-only user

    def test_readonly_sql_is_least_privilege(self):
        sql = readonly.statements("u", "p'w", ["%", "localhost"], "db", ["tabA", "tab B"])
        self.assertIn("CREATE USER IF NOT EXISTS 'u'@'%' IDENTIFIED BY 'p\\'w'", sql)
        grants = [s for s in sql if s.startswith("GRANT")]
        self.assertEqual(len(grants), 4)
        self.assertTrue(all(s.startswith("GRANT SELECT ON `db`.`tab") for s in grants))
        self.assertIn("GRANT SELECT ON `db`.`tab B` TO 'u'@'localhost'", sql)

    def test_readonly_user_and_checks(self):
        if not ROOT_PW:
            self.skipTest("Set F2O_DB_ROOT_PASSWORD to create a real database user")
        out = readonly.setup(root_password=ROOT_PW)
        self.assertGreater(out["tables"], 10)
        results = {c["check"]: c for c in checks.run_checks()}
        access = results["Read-only user can read, and only read"]
        self.assertEqual(access["status"], "ok", access["detail"])
        self.assertEqual(results["Read-only database user configured"]["status"], "ok")

    def test_checks_with_fake_auto_ontology(self):
        fake = FakeAutoOntology()
        try:
            self._point_at(fake.url)
            results = {c["check"]: c["status"] for c in checks.run_checks()}
        finally:
            fake.close()
        self.assertEqual(results["Model builds from this site"], "ok")
        self.assertEqual(results["Auto Ontology is reachable"], "ok")
        self.assertEqual(results["API token works"], "ok")
        self.assertEqual(results["Database connection in Auto Ontology"], "warn")
        self.assertEqual(results["Model imported"], "ok")

    def test_bad_settings_rejected(self):
        settings = frappe.get_single(api.SETTINGS)
        settings.exclude_regex = "([unclosed"
        self.assertRaises(frappe.ValidationError, settings.save)
        settings.reload()
        settings.modules = "No Such Module"
        self.assertRaises(frappe.ValidationError, settings.save)
        settings.reload()
        settings.auto_ontology_url = "https://generativelanguage.googleapis.com/v1beta/openai"
        self.assertRaises(frappe.ValidationError, settings.save)
