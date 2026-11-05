# Copyright (c) 2026, Neotec and contributors
# For license information, please see license.txt

import re

import frappe
from frappe import _
from frappe.model.document import Document


class OntologyExportSettings(Document):
    def validate(self):
        if self.exclude_regex:
            try:
                re.compile(self.exclude_regex)
            except re.error as exc:
                frappe.throw(_("Exclude DocTypes Matching is not a valid regular expression: {0}").format(exc))
        modules = {m.strip() for m in (self.modules or "").splitlines() if m.strip()}
        unknown = sorted(modules - set(frappe.get_all("Module Def", pluck="name")))
        if unknown:
            frappe.throw(_("Unknown modules: {0}").format(", ".join(unknown)))
        for name in (d.strip() for d in (self.include_doctypes or "").splitlines()):
            if name and not frappe.db.exists("DocType", name):
                frappe.throw(_("DocType {0} does not exist").format(frappe.bold(name)))
        if self.auto_ontology_url:
            self.auto_ontology_url = self.auto_ontology_url.strip().rstrip("/")
            if not re.match(r"^https?://[^/\s]+", self.auto_ontology_url):
                frappe.throw(_("Auto Ontology URL must start with https:// or http://"))
            if re.search(r"generativelanguage\.googleapis\.com|api\.openai\.com|integrate\.api\.nvidia\.com",
                         self.auto_ontology_url):
                frappe.throw(
                    _(
                        "Auto Ontology URL is the address of your Auto Ontology server, not of an AI "
                        "model. Put model endpoints under Auto Ontology Server Kit."
                    )
                )
        if not self.admin_email and frappe.session.user not in ("Administrator", "Guest"):
            self.admin_email = frappe.session.user
        if not self.ro_db_port:
            self.ro_db_port = int(frappe.conf.db_port or 3306)
