# Copyright (c) 2026, Neotec and contributors
# For license information, please see license.txt
"""Run with: bench --site <site> run-tests --app frappe2ontology"""

import json
import threading
from email.parser import BytesParser
from email.policy import default as email_policy
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import frappe
import yaml

try:  # Frappe v16
    from frappe.tests import IntegrationTestCase as TestCase
except ImportError:  # Frappe v15
    from frappe.tests.utils import FrappeTestCase as TestCase

from frappe2ontology import api, build, measures

HAS_ERPNEXT = "erpnext" in frappe.get_installed_apps()


def _tables(model):
    return {t["name"]: t for t in model["data_layer"]["databases"][0]["schemas"][0]["tables"]}


class TestOntologyExportSettings(TestCase):
    def setUp(self):
        settings = frappe.get_single(api.SETTINGS)
        settings.modules = "Selling\nBuying\nAccounts\nStock\nSetup" if HAS_ERPNEXT else "Core"
        settings.include_doctypes = ""
        settings.exclude_regex = build.DEFAULT_EXCLUDE
        settings.with_link_targets = 0
        settings.include_measures = 1
        settings.save()

    def test_model_is_consistent_and_matches_database(self):
        result = api.build_export()
        self.assertEqual(build.check_references(result.model), [])
        real = set(
            frappe.db.sql(
                "SELECT TABLE_NAME, COLUMN_NAME FROM information_schema.columns "
                "WHERE TABLE_SCHEMA = DATABASE()"
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
        # Round-trips through YAML unchanged.
        self.assertEqual(yaml.safe_load(result.yaml_text), result.model)

    def test_every_measure_runs_on_this_site(self):
        if not HAS_ERPNEXT:
            self.skipTest("ERPNext not installed")
        result = api.build_export()
        sl = result.model["semantic_layer"]
        items = sl["sql_attributes"]["manual"] + sl["custom_analyses"]
        self.assertEqual(len(items), len(measures.MEASURES) + len(measures.ANALYSES))
        for item in items:
            frappe.db.sql(item["sql"])  # raises on any unknown table or column

    def test_erpnext_semantics(self):
        if not HAS_ERPNEXT:
            self.skipTest("ERPNext not installed")
        result = api.build_export()
        tables = _tables(result.model)
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
            tables = _tables(api.build_export().model)
            si = {c["name"]: c for c in tables["tabSales Invoice"]["columns"]}
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

    def test_download(self):
        api.download_model()
        self.assertEqual(frappe.response.type, "download")
        self.assertTrue(frappe.response.filename.endswith("_ontology_model.yaml"))
        self.assertIn("semantic_layer", frappe.response.filecontent)

    def test_push_sends_model_to_auto_ontology(self):
        received = {}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                msg = BytesParser(policy=email_policy).parsebytes(
                    b"Content-Type: " + self.headers["Content-Type"].encode() + b"\r\n\r\n" + body
                )
                part = next(msg.iter_parts())
                received.update(
                    path=urlparse(self.path).path,
                    query=parse_qs(urlparse(self.path).query),
                    token=self.headers.get("x-api-key"),
                    field=part.get_param("name", header="content-disposition"),
                    model=yaml.safe_load(part.get_content()),
                )
                out = json.dumps({"created": {"terms": 1}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.handle_request, daemon=True)
        thread.start()
        try:
            settings = frappe.get_single(api.SETTINGS)
            settings.auto_ontology_url = f"http://127.0.0.1:{server.server_port}/"
            settings.api_token = "auto_ontology_test_token"
            settings.replace_existing = 0
            settings.save()
            out = api.push_model()
        finally:
            thread.join(timeout=30)
            server.server_close()

        self.assertEqual(received["path"], "/api/model/import")
        self.assertEqual(received["query"], {"replace": ["false"], "embed": ["true"]})
        self.assertEqual(received["token"], "auto_ontology_test_token")
        self.assertEqual(received["field"], "file")
        self.assertIn("data_layer", received["model"])
        self.assertEqual(out["auto_ontology"], {"created": {"terms": 1}})

    def test_bad_settings_rejected(self):
        settings = frappe.get_single(api.SETTINGS)
        settings.exclude_regex = "([unclosed"
        self.assertRaises(frappe.ValidationError, settings.save)
        settings.reload()
        settings.modules = "No Such Module"
        self.assertRaises(frappe.ValidationError, settings.save)
