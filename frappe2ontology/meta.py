"""Load DocType metadata, either from app source JSON or from a live site.

Both sources produce the same plain structures (:class:`DocType`,
:class:`Field`) so the model builder doesn't care where metadata came from.

* **JSON mode** reads ``<app>/**/doctype/*/*.json`` from app source trees.
  It needs no database, but it cannot see Custom Fields or Property Setters
  added on a particular site.
* **Live mode** reads ``tabDocType``, ``tabDocField``, ``tabCustom Field`` and
  ``tabProperty Setter`` from the site's MariaDB database, and checks every
  column against ``information_schema`` so only columns that really exist are
  emitted. Use this from outside a bench.
* **In-bench mode** (:func:`load_from_frappe`) asks Frappe itself through
  ``frappe.get_meta``, which already merges Custom Fields and Property
  Setters exactly as the forms show them. This is what the app's Settings
  page and API use.
"""

from __future__ import annotations

import glob
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Iterable

logger = logging.getLogger(__name__)

# Field types that have no database column.
NO_VALUE_TYPES = frozenset(
    {
        "Section Break",
        "Column Break",
        "Tab Break",
        "HTML",
        "Button",
        "Image",
        "Fold",
        "Heading",
        "Table",
        "Table MultiSelect",
        "Vue Component",
    }
)
# Field types whose rows live in a child table named by ``options``.
CHILD_TABLE_TYPES = frozenset({"Table", "Table MultiSelect"})


@dataclass
class Field:
    fieldname: str
    fieldtype: str
    label: str = ""
    options: str = ""
    description: str = ""
    fetch_from: str = ""
    is_virtual: bool = False
    custom: bool = False
    # Real column type, only known in live mode.
    db_type: str = ""

    @property
    def has_column(self) -> bool:
        return self.fieldtype not in NO_VALUE_TYPES and not self.is_virtual


@dataclass
class DocType:
    name: str
    module: str = ""
    app: str = ""
    description: str = ""
    istable: bool = False
    issingle: bool = False
    is_virtual: bool = False
    is_submittable: bool = False
    autoname: str = ""
    fields: list[Field] = field(default_factory=list)
    # Real column name -> type, only known in live mode.
    db_columns: dict[str, str] | None = None

    @property
    def table_name(self) -> str:
        return f"tab{self.name}"

    @property
    def has_table(self) -> bool:
        return not (self.issingle or self.is_virtual)


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip() not in ("", "0")
    return bool(value)


def _field_from_dict(raw: dict[str, Any], *, custom: bool = False) -> Field:
    return Field(
        fieldname=str(raw.get("fieldname") or ""),
        fieldtype=str(raw.get("fieldtype") or "Data"),
        label=str(raw.get("label") or ""),
        options=str(raw.get("options") or ""),
        description=str(raw.get("description") or ""),
        fetch_from=str(raw.get("fetch_from") or ""),
        is_virtual=_truthy(raw.get("is_virtual")),
        custom=custom,
    )


def _doctype_from_dict(raw: dict[str, Any], *, app: str = "") -> DocType:
    return DocType(
        name=str(raw["name"]),
        module=str(raw.get("module") or ""),
        app=app,
        description=str(raw.get("description") or ""),
        istable=_truthy(raw.get("istable")),
        issingle=_truthy(raw.get("issingle")),
        is_virtual=_truthy(raw.get("is_virtual")),
        is_submittable=_truthy(raw.get("is_submittable")),
        autoname=str(raw.get("autoname") or ""),
        fields=[
            _field_from_dict(f) for f in raw.get("fields") or [] if f.get("fieldname")
        ],
    )


def load_from_json(app_paths: Iterable[str]) -> dict[str, DocType]:
    """Read every DocType JSON under each app source path."""
    doctypes: dict[str, DocType] = {}
    for app_path in app_paths:
        app = os.path.basename(os.path.normpath(app_path))
        pattern = os.path.join(app_path, "**", "doctype", "*", "*.json")
        for path in sorted(glob.glob(pattern, recursive=True)):
            # A DocType's JSON is named after its folder; other JSON files in
            # the folder (test records, fixtures) are not DocType definitions.
            folder = os.path.basename(os.path.dirname(path))
            if os.path.splitext(os.path.basename(path))[0] != folder:
                continue
            try:
                with open(path, encoding="utf-8") as handle:
                    raw = json.load(handle)
            except (OSError, ValueError) as exc:
                logger.warning("Skipping unreadable %s: %s", path, exc)
                continue
            if not isinstance(raw, dict) or raw.get("doctype") != "DocType":
                continue
            doctypes[raw["name"]] = _doctype_from_dict(raw, app=app)
    return doctypes


# --------------------------------------------------------------------------
# Live site
# --------------------------------------------------------------------------

# DocType/DocField properties a Property Setter may override that we use.
_FIELD_OVERRIDES = ("label", "options", "description", "fieldtype")


def _columns(cursor: Any, table: str) -> set[str]:
    cursor.execute(
        "SELECT COLUMN_NAME FROM information_schema.columns "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
        (table,),
    )
    return {row[0] for row in cursor.fetchall()}


def _select_existing(cursor: Any, table: str, wanted: list[str]) -> list[dict]:
    """``SELECT`` only the *wanted* columns this Frappe version actually has."""
    present = _columns(cursor, table)
    cols = [c for c in wanted if c in present]
    cursor.execute(
        "SELECT " + ", ".join(f"`{c}`" for c in cols) + f" FROM `{table}`"
    )
    names = [d[0] for d in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def _attach_db_columns(doctypes: dict[str, DocType], rows: Iterable[tuple]) -> None:
    """Record each table's real columns and types, from information_schema rows."""
    by_table: dict[str, dict[str, str]] = {}
    for table, column, ctype in rows:
        by_table.setdefault(table, {})[column] = ctype
    for dt in doctypes.values():
        dt.db_columns = by_table.get(dt.table_name)
        if dt.db_columns:
            for fld in dt.fields:
                fld.db_type = dt.db_columns.get(fld.fieldname, "")


_INFO_SCHEMA_COLUMNS = (
    "SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE FROM information_schema.columns "
    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME LIKE 'tab%'"
)


def load_from_frappe() -> dict[str, DocType]:
    """Read metadata through the running Frappe site (call inside a request or
    ``bench execute``). Custom Fields and Property Setters are already applied
    by ``frappe.get_meta``."""
    import frappe  # only available inside a bench

    module_app = {
        m.name: m.app_name or ""
        for m in frappe.get_all("Module Def", fields=["name", "app_name"])
    }
    doctypes: dict[str, DocType] = {}
    for name in frappe.get_all("DocType", pluck="name"):
        try:
            m = frappe.get_meta(name)
        except Exception as exc:  # a DocType whose controller fails to import
            logger.warning("Skipping %s: %s", name, exc)
            continue
        dt = DocType(
            name=m.name,
            module=m.module or "",
            app=module_app.get(m.module, ""),
            description=m.description or "",
            istable=bool(m.istable),
            issingle=bool(m.issingle),
            is_virtual=bool(getattr(m, "is_virtual", 0)),
            is_submittable=bool(m.is_submittable),
            autoname=m.autoname or "",
        )
        for df in m.fields:
            if not df.fieldname:
                continue
            dt.fields.append(
                Field(
                    fieldname=df.fieldname,
                    fieldtype=df.fieldtype or "Data",
                    label=df.label or "",
                    options=str(df.options or ""),
                    description=df.description or "",
                    fetch_from=getattr(df, "fetch_from", "") or "",
                    is_virtual=bool(getattr(df, "is_virtual", 0)),
                    custom=bool(getattr(df, "is_custom_field", 0)),
                )
            )
        doctypes[dt.name] = dt
    _attach_db_columns(doctypes, frappe.db.sql(_INFO_SCHEMA_COLUMNS))
    return doctypes


def load_from_db(connect_kwargs: dict[str, Any]) -> dict[str, DocType]:
    """Read DocType metadata, customisations and real columns from a site DB."""
    import mysql.connector  # imported lazily: JSON mode needs no driver

    connection = mysql.connector.connect(**connect_kwargs)
    try:
        cursor = connection.cursor()
        doctypes: dict[str, DocType] = {}
        module_app = {
            row["name"]: row.get("app_name") or ""
            for row in _select_existing(cursor, "tabModule Def", ["name", "app_name"])
        }
        for row in _select_existing(
            cursor,
            "tabDocType",
            [
                "name",
                "module",
                "description",
                "istable",
                "issingle",
                "is_virtual",
                "is_submittable",
                "autoname",
            ],
        ):
            dt = _doctype_from_dict(row)
            dt.app = module_app.get(dt.module, "")
            doctypes[dt.name] = dt

        field_cols = [
            "parent",
            "fieldname",
            "fieldtype",
            "label",
            "options",
            "description",
            "fetch_from",
            "is_virtual",
            "idx",
        ]
        rows = _select_existing(cursor, "tabDocField", field_cols)
        rows.sort(key=lambda r: (r["parent"], r.get("idx") or 0))
        for row in rows:
            dt = doctypes.get(row["parent"])
            if dt and row.get("fieldname"):
                dt.fields.append(_field_from_dict(row))

        custom_cols = ["dt"] + field_cols[1:]
        for row in _select_existing(cursor, "tabCustom Field", custom_cols):
            dt = doctypes.get(row["dt"])
            if dt and row.get("fieldname"):
                dt.fields.append(_field_from_dict(row, custom=True))

        for row in _select_existing(
            cursor,
            "tabProperty Setter",
            ["doc_type", "field_name", "property", "value", "doctype_or_field"],
        ):
            dt = doctypes.get(row["doc_type"])
            if not dt:
                continue
            prop, value = row["property"], row["value"] or ""
            if row.get("doctype_or_field") == "DocType" or not row.get("field_name"):
                if prop == "description":
                    dt.description = value
                continue
            if prop not in _FIELD_OVERRIDES:
                continue
            for fld in dt.fields:
                if fld.fieldname == row["field_name"]:
                    setattr(fld, prop, value)

        cursor.execute(_INFO_SCHEMA_COLUMNS)
        _attach_db_columns(doctypes, cursor.fetchall())
        return doctypes
    finally:
        connection.close()
