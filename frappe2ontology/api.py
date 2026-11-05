"""Whitelisted endpoints behind the Ontology Export Settings page.

All are System Manager only: the model describes every table the site has,
and the kit carries secrets.
"""

from __future__ import annotations

import io
import zipfile

import frappe
import yaml
from frappe import _
from frappe.utils import now_datetime

from frappe2ontology import aoclient, meta
from frappe2ontology.export import Export, make_export

SETTINGS = "Ontology Export Settings"


def _lines(text: str | None) -> list[str]:
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def db_name() -> str:
    return frappe.conf.db_name


def build_export(settings=None) -> Export:
    settings = settings or frappe.get_single(SETTINGS)
    return make_export(
        meta.load_from_frappe(),
        db_name(),
        modules=_lines(settings.modules),
        include=_lines(settings.include_doctypes),
        exclude_regex=settings.exclude_regex,
        with_link_targets=bool(settings.with_link_targets),
        include_measures=bool(settings.include_measures),
    )


def exported_tables(model: dict) -> list[str]:
    return [t["name"] for t in model["data_layer"]["databases"][0]["schemas"][0]["tables"]]


def _record_run(summary: str) -> None:
    frappe.db.set_single_value(SETTINGS, {"last_run_on": now_datetime(), "last_run_summary": summary})


def _export_or_throw(settings=None) -> Export:
    try:
        return build_export(settings)
    except ValueError as exc:
        frappe.throw(str(exc))


@frappe.whitelist()
def preview() -> dict:
    """Build the model and report its size, without downloading or sending it."""
    frappe.only_for("System Manager")
    result = _export_or_throw()
    return {"summary": result.summary, "db_name": db_name()}


_HOW_TO_IMPORT = """\
Importing into Auto Ontology by hand
====================================

Auto Ontology checks each measure's SQL against the database's connection,
so the import goes in three steps:

1. Settings > Import / Export: import 1-schema-and-terms.yaml
2. Settings > Connections: add this MariaDB database ({db}) with the
   read-only user. Set "table allow regex" to the contents of
   table-allow-regex.txt.
3. Settings > Import / Export: import 2-full-model.yaml

Re-importing is safe: anything already there is skipped.
"Push to Auto Ontology" in ERPNext does all three for you.
"""


@frappe.whitelist()
def download_model() -> None:
    """Send a zip with the two import files and instructions."""
    frappe.only_for("System Manager")
    result = _export_or_throw()
    phase1 = yaml.safe_dump(
        aoclient.without_measures(result.model), sort_keys=False, allow_unicode=True, width=100
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("1-schema-and-terms.yaml", phase1)
        archive.writestr("2-full-model.yaml", result.yaml_text)
        archive.writestr("table-allow-regex.txt", aoclient.table_allow_regex(exported_tables(result.model)))
        archive.writestr("HOW-TO-IMPORT.txt", _HOW_TO_IMPORT.format(db=db_name()))
    _record_run(f"Downloaded: {result.summary}")
    frappe.db.commit()
    frappe.response.filename = f"{db_name()}_ontology_model.zip"
    frappe.response.filecontent = buffer.getvalue()
    frappe.response.type = "download"


# ---------------------------------------------------------------------- push


def _connection_payload(settings, tables: list[str]) -> dict:
    password = settings.get_password("ro_db_password", raise_exception=False)
    if not (settings.ro_db_user and password and settings.ro_db_host):
        frappe.throw(
            _(
                "Set up the read-only database user first: "
                "bench --site {0} ontology-readonly-user"
            ).format(frappe.local.site)
        )
    return {
        "type": "mysql",
        "host": settings.ro_db_host,
        "port": str(settings.ro_db_port or 3306),
        "user": settings.ro_db_user,
        "password": password,
        "database": db_name(),
        "table_allow_regex": aoclient.table_allow_regex(tables),
    }


def run_push(embed: bool = True) -> dict:
    """Import into Auto Ontology: schema and terms, then the connection, then
    the full model with measures. Returns a summary of each step."""
    settings = frappe.get_single(SETTINGS)
    if not settings.auto_ontology_url:
        frappe.throw(_("Set the Auto Ontology URL first."))
    token = settings.get_password("api_token", raise_exception=False)
    if not token:
        frappe.throw(_("Set the Auto Ontology API token first."))
    result = _export_or_throw(settings)
    client = aoclient.Client(settings.auto_ontology_url, token)
    filename = f"{db_name()}_ontology_model.yaml"
    steps: list[str] = []

    try:
        # Phase 1 never replaces: with no measures in it, replace would delete them all.
        phase1 = client.import_model(
            yaml.safe_dump(aoclient.without_measures(result.model), sort_keys=False, allow_unicode=True),
            filename,
            replace=False,
            embed=embed,
        )
        created = (phase1.get("summary") or {}).get("created", {})
        steps.append(f"Schema and terms imported ({created.get('terms', 0)} new terms).")

        has_connection = db_name() in {c.get("database_name") for c in client.list_connections()}
        if not has_connection and settings.create_connection:
            client.create_connection(_connection_payload(settings, exported_tables(result.model)))
            has_connection = True
            steps.append("Database connection added; Auto Ontology starts profiling the tables.")
        elif has_connection:
            steps.append("Database connection already present.")

        sl = result.model["semantic_layer"]
        if sl["sql_attributes"]["manual"] or sl["custom_analyses"]:
            if has_connection:
                phase2 = client.import_model(
                    result.yaml_text, filename, replace=bool(settings.replace_existing), embed=embed
                )
                created = (phase2.get("summary") or {}).get("created", {})
                steps.append(
                    f"Measures imported ({created.get('sql_attributes', 0)} new measures, "
                    f"{created.get('custom_analyses', 0)} new analyses)."
                )
            else:
                steps.append(
                    "Measures skipped: Auto Ontology needs the database connection first. "
                    "Turn on Add Database Connection and push again."
                )
    except aoclient.AutoOntologyError as exc:
        frappe.log_error(title="Push to Auto Ontology failed", message=str(exc))
        summary = "Push failed after: " + " ".join(steps) + f" Error: {exc}"
        _record_run(summary)
        frappe.db.commit()
        raise frappe.ValidationError(summary) from exc

    summary = f"Pushed to {settings.auto_ontology_url}: {result.summary}. " + " ".join(steps)
    _record_run(summary)
    frappe.db.commit()
    return {"summary": summary, "steps": steps}


def _push_job(user: str) -> None:
    try:
        out = run_push()
        frappe.publish_realtime(
            "msgprint",
            {"title": _("Sent to Auto Ontology"), "message": "<br>".join(out["steps"]), "indicator": "green"},
            user=user,
        )
    except Exception as exc:
        frappe.publish_realtime(
            "msgprint",
            {"title": _("Push to Auto Ontology failed"), "message": str(exc), "indicator": "red"},
            user=user,
        )
        raise


@frappe.whitelist(methods=["POST"])
def push_model() -> dict:
    """Start the push in the background; an import can take several minutes."""
    frappe.only_for("System Manager")
    frappe.enqueue(
        "frappe2ontology.api._push_job",
        queue="long",
        timeout=3600,
        user=frappe.session.user,
        job_id=f"frappe2ontology_push_{frappe.local.site}",
        deduplicate=True,
    )
    return {"queued": True}


def export_to_file(path: str) -> str:
    """For ``bench --site <site> execute frappe2ontology.api.export_to_file
    --kwargs "{'path': '/tmp/model.yaml'}"``. Uses the saved settings."""
    result = build_export()
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(result.yaml_text)
    print(result.summary)
    return path


def push(embed: bool = True) -> dict:
    """For ``bench --site <site> execute frappe2ontology.api.push``: push in the
    foreground and print each step."""
    out = run_push(embed=embed)
    print(out["summary"])
    return out


# ------------------------------------------------------------- setup helpers


@frappe.whitelist()
def check_setup() -> list[dict]:
    """Run the setup checklist (see checks.py)."""
    frappe.only_for("System Manager")
    from frappe2ontology import checks

    return checks.run_checks()


def kit_values(settings) -> dict:
    """Plain values for the server kit; generates and saves missing secrets."""
    from frappe2ontology import kit

    values = {
        "site": frappe.local.site,
        "app_url": settings.auto_ontology_url,
        "admin_email": settings.admin_email,
        "auto_ontology_ref": settings.auto_ontology_ref,
        "llm_provider": settings.llm_provider,
        "llm_endpoint": settings.llm_endpoint,
        "llm_model": settings.llm_model,
        "ro_db_host": settings.ro_db_host,
        "ro_db_port": settings.ro_db_port,
    }
    for field in ("llm_api_key", "nvidia_api_key", "kit_postgres_password", "kit_admin_password", "kit_auth_secret"):
        values[field] = settings.get_password(field, raise_exception=False) or ""
    created = kit.ensure_secrets(values)
    if created:
        for field, value in created.items():
            settings.set(field, value)
        settings.flags.ignore_permissions = True
        settings.save()
    return values


@frappe.whitelist()
def download_server_kit() -> None:
    """Send the Auto Ontology server kit (compose, .env, setup.sh, README) as a zip."""
    frappe.only_for("System Manager")
    from frappe2ontology import kit

    settings = frappe.get_single(SETTINGS)
    try:
        data = kit.build_zip(kit_values(settings))
    except kit.KitError as exc:
        frappe.throw(str(exc))
    frappe.db.commit()
    frappe.response.filename = "auto-ontology-kit.zip"
    frappe.response.filecontent = data
    frappe.response.type = "download"
