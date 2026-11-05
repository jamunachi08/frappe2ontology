"""Whitelisted endpoints behind the Ontology Export Settings page.

All of them are System Manager only: the model describes every table the
site has, which is sensitive even without row data.
"""

from __future__ import annotations

import frappe
import requests
from frappe import _
from frappe.utils import now_datetime

from frappe2ontology import meta
from frappe2ontology.export import Export, make_export

SETTINGS = "Ontology Export Settings"
_IMPORT_TIMEOUT_S = 900  # Auto Ontology embeds every term on import


def _lines(text: str | None) -> list[str]:
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def _db_name() -> str:
    return frappe.conf.db_name


def build_export(settings=None) -> Export:
    settings = settings or frappe.get_single(SETTINGS)
    return make_export(
        meta.load_from_frappe(),
        _db_name(),
        modules=_lines(settings.modules),
        include=_lines(settings.include_doctypes),
        exclude_regex=settings.exclude_regex,
        with_link_targets=bool(settings.with_link_targets),
        include_measures=bool(settings.include_measures),
    )


def _record_run(summary: str) -> None:
    frappe.db.set_single_value(
        SETTINGS, {"last_run_on": now_datetime(), "last_run_summary": summary}
    )


@frappe.whitelist()
def preview() -> dict:
    """Build the model and report its size, without downloading or sending it."""
    frappe.only_for("System Manager")
    try:
        result = build_export()
    except ValueError as exc:
        frappe.throw(str(exc))
    return {"summary": result.summary, "db_name": _db_name()}


@frappe.whitelist()
def download_model() -> None:
    """Send the model YAML to the browser as a file."""
    frappe.only_for("System Manager")
    try:
        result = build_export()
    except ValueError as exc:
        frappe.throw(str(exc))
    _record_run(f"Downloaded: {result.summary}")
    frappe.db.commit()
    frappe.response.filename = f"{_db_name()}_ontology_model.yaml"
    frappe.response.filecontent = result.yaml_text
    frappe.response.type = "download"


@frappe.whitelist(methods=["POST"])
def push_model() -> dict:
    """Upload the model to Auto Ontology's ``POST /api/model/import``."""
    frappe.only_for("System Manager")
    settings = frappe.get_single(SETTINGS)
    if not settings.auto_ontology_url:
        frappe.throw(_("Set the Auto Ontology URL first."))
    token = settings.get_password("api_token", raise_exception=False)
    if not token:
        frappe.throw(_("Set the Auto Ontology API token first."))
    try:
        result = build_export(settings)
    except ValueError as exc:
        frappe.throw(str(exc))

    url = settings.auto_ontology_url.rstrip("/") + "/api/model/import"
    try:
        response = requests.post(
            url,
            params={
                "replace": "true" if settings.replace_existing else "false",
                "embed": "true",
            },
            headers={"x-api-key": token},
            files={
                "file": (
                    f"{_db_name()}_ontology_model.yaml",
                    result.yaml_text.encode("utf-8"),
                    "application/x-yaml",
                )
            },
            timeout=_IMPORT_TIMEOUT_S,
        )
    except requests.RequestException as exc:
        frappe.throw(_("Could not reach Auto Ontology at {0}: {1}").format(url, exc))
    if response.status_code >= 400:
        frappe.log_error(
            title="Auto Ontology import failed",
            message=f"{response.status_code}\n{response.text[:5000]}",
        )
        frappe.throw(
            _("Auto Ontology rejected the import (HTTP {0}): {1}").format(
                response.status_code, response.text[:500]
            )
        )
    try:
        body = response.json()
    except ValueError:
        body = {"response": response.text[:2000]}
    _record_run(f"Pushed to {settings.auto_ontology_url}: {result.summary}")
    return {"summary": result.summary, "auto_ontology": body}


def export_to_file(path: str) -> str:
    """For ``bench --site <site> execute frappe2ontology.api.export_to_file
    --kwargs "{'path': '/tmp/model.yaml'}"``. Uses the saved settings."""
    result = build_export()
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(result.yaml_text)
    print(result.summary)
    return path
