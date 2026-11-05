"""Build an Auto Ontology native model document from DocType metadata.

Mapping
-------
* Each DocType with a table becomes a catalog **table** ``tab<DocType>`` and a
  **term** named after the DocType.
* Each field with a database column becomes a **column** and a **column
  attribute** of that term. Descriptions come from the field label, its help
  text, Select options, Link targets and currency rules, so retrieval sees
  what Frappe's form shows a user.
* Each ``Link`` field becomes a **foreign key** to ``tab<Target>.name``, a
  table **join**, and a **semantic FK** to the target term's ID attribute.
* Each child table (``Table`` / ``Table MultiSelect`` field) becomes a join
  from ``child.parent`` to ``parent.name``. Child tables shared by several
  parents say in their descriptions that ``parenttype`` must be filtered.
* ``Dynamic Link`` fields are described (which column holds the DocType) but
  not joined, because the target table changes row by row.

Every id is a UUID5 of the site database name plus a natural key, so building
again from the same metadata yields the same ids and re-importing is a no-op.
"""

from __future__ import annotations

import html
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable

from frappe2ontology.meta import CHILD_TABLE_TYPES, DocType, Field

_NAMESPACE = uuid.UUID("6f1d3c52-6a0e-4b7c-9d2b-2b1f5f0e9a11")

DEFAULT_MODULES = ("Selling", "Buying", "Accounts", "Stock", "Setup")
# Logs, settings and background-job records are noise for business questions;
# Employee* holds personal data and is opt-in (``--include Employee``).
DEFAULT_EXCLUDE = (
    r"( Log| Settings| Tool)$"
    r"|^(Employee|Repost |Process |Transaction Deletion|Bisect |Ledger Health"
    r"|Ledger Merge|Bank Statement Import|Email Digest)"
)

# Columns every Frappe table has, in the order Frappe creates them.
_STANDARD = [
    ("name", "varchar(140)"),
    ("creation", "datetime(6)"),
    ("modified", "datetime(6)"),
    ("modified_by", "varchar(140)"),
    ("owner", "varchar(140)"),
    ("docstatus", "int(1)"),
    ("idx", "int(8)"),
]
_CHILD_STANDARD = [
    ("parent", "varchar(140)"),
    ("parentfield", "varchar(140)"),
    ("parenttype", "varchar(140)"),
]

# Database type Frappe uses for each field type (MariaDB).
_TYPE_MAP = {
    "Currency": "decimal(21,9)",
    "Float": "decimal(21,9)",
    "Percent": "decimal(21,9)",
    "Duration": "decimal(21,9)",
    "Rating": "decimal(3,2)",
    "Int": "int(11)",
    "Long Int": "bigint(20)",
    "Check": "int(1)",
    "Date": "date",
    "Datetime": "datetime(6)",
    "Time": "time(6)",
    "Small Text": "text",
    "Text": "text",
    "Code": "longtext",
    "Long Text": "longtext",
    "Text Editor": "longtext",
    "HTML Editor": "longtext",
    "Markdown Editor": "longtext",
    "JSON": "json",
    "Signature": "longtext",
    "Geolocation": "longtext",
    "Barcode": "longtext",
    "Attach": "text",
    "Attach Image": "text",
}
_DEFAULT_TYPE = "varchar(140)"


def column_type(fld: Field) -> str:
    return fld.db_type or _TYPE_MAP.get(fld.fieldtype, _DEFAULT_TYPE)


def _id(db_name: str, *parts: str) -> str:
    return str(uuid.uuid5(_NAMESPACE, "|".join((db_name,) + parts)))


_TAG_RE = re.compile(r"<[^>]+>")


def _clean(text: str) -> str:
    text = html.unescape(_TAG_RE.sub(" ", text or ""))
    return re.sub(r"\s+", " ", text).strip()


def _humanize(fieldname: str) -> str:
    return " ".join(p.capitalize() for p in fieldname.split("_") if p)


def _sentence(text: str) -> str:
    text = text.strip()
    return text if not text or text.endswith((".", "!", "?")) else text + "."


# --------------------------------------------------------------------------
# Selection
# --------------------------------------------------------------------------


@dataclass
class Selection:
    """The DocTypes that go into the model, and how they relate."""

    doctypes: dict[str, DocType]
    # child DocType -> [(parent DocType, fieldname)]
    parents_of: dict[str, list[tuple[str, str]]] = field(default_factory=dict)


def select_doctypes(
    all_doctypes: dict[str, DocType],
    *,
    modules: Iterable[str] = DEFAULT_MODULES,
    include: Iterable[str] = (),
    exclude_regex: str | None = DEFAULT_EXCLUDE,
    with_link_targets: bool = False,
) -> Selection:
    """Pick top-level DocTypes by module, then pull in their child tables.

    Child tables are included because their parent is, whatever module they
    are filed under. With *with_link_targets*, the tables that included
    DocTypes link to (Currency, UOM, Territory, ...) come along one hop deep.
    """
    modules = set(modules)
    include = set(include)
    exclude = re.compile(exclude_regex) if exclude_regex else None

    def wanted(dt: DocType) -> bool:
        if not dt.has_table or dt.istable:
            return False
        if dt.name in include:
            return True
        if exclude and exclude.search(dt.name):
            return False
        return dt.module in modules

    chosen = {name: dt for name, dt in all_doctypes.items() if wanted(dt)}
    missing = include - set(chosen)
    if missing:
        raise ValueError(f"Unknown or table-less DocTypes requested: {sorted(missing)}")

    if with_link_targets:
        for dt in list(chosen.values()):
            for fld in dt.fields:
                target = all_doctypes.get(fld.options)
                if (
                    fld.fieldtype == "Link"
                    and target
                    and target.has_table
                    and not target.istable
                ):
                    chosen.setdefault(target.name, target)

    parents_of: dict[str, list[tuple[str, str]]] = {}
    for dt in list(chosen.values()):
        for fld in dt.fields:
            if fld.fieldtype not in CHILD_TABLE_TYPES:
                continue
            child = all_doctypes.get(fld.options)
            if child is None or not child.istable:
                continue
            chosen.setdefault(child.name, child)
            parents_of.setdefault(child.name, []).append((dt.name, fld.fieldname))

    # Child tables nested inside child tables are rare but exist; their rows
    # point at the top-level document, so they need no extra handling.
    return Selection(doctypes=dict(sorted(chosen.items())), parents_of=parents_of)


# --------------------------------------------------------------------------
# Descriptions
# --------------------------------------------------------------------------


def _currency_note(options: str) -> str:
    if not options:
        return ""
    if options == "Company:company:default_currency":
        return "Amount in the company's default currency."
    if ":" in options:
        doctype, link_field, currency_field = (options.split(":") + ["", ""])[:3]
        return (
            f"Amount in the currency given by {doctype}.{currency_field} "
            f"of the record in column `{link_field}`."
        )
    return f"Amount in the currency stored in column `{options}` of this row."


def column_description(dt: DocType, fld: Field, sel: Selection) -> str:
    parts = [_sentence(fld.label or _humanize(fld.fieldname))]
    if fld.description:
        parts.append(_sentence(_clean(fld.description)))

    ft, opts = fld.fieldtype, fld.options.strip()
    if ft == "Link" and opts:
        if opts == dt.name:
            parts.append(f"Refers to another {opts} record (`tab{opts}`.name).")
        elif opts in sel.doctypes:
            parts.append(f"Links to {opts}: join on `tab{opts}`.name.")
        else:
            parts.append(f"Links to {opts} (`tab{opts}`.name), not in this model.")
    elif ft == "Dynamic Link" and opts:
        parts.append(
            f"ID of a record whose DocType is stored in column `{opts}`; "
            f"join to `tab<that DocType>`.name and filter on `{opts}`."
        )
    elif ft == "Select" and opts:
        values = [v for v in opts.split("\n") if v.strip()]
        if values:
            shown = ", ".join(values[:30]) + (", ..." if len(values) > 30 else "")
            parts.append(f"One of: {shown}.")
    elif ft == "Check":
        parts.append("1 = Yes, 0 = No.")
    elif ft == "Currency":
        note = _currency_note(opts)
        if note:
            parts.append(note)
    elif ft == "Percent":
        parts.append("Percentage, 0-100.")

    if fld.fetch_from:
        parts.append(f"Copied from {fld.fetch_from} when the document is saved.")
    if fld.custom:
        parts.append("Custom field added on this site.")
    return " ".join(parts)


def _standard_description(dt: DocType, column: str, sel: Selection) -> tuple[str, str]:
    """``(attribute name, description)`` for a Frappe standard column."""
    if column == "name":
        return f"{dt.name} ID", (
            f"Unique ID of the {dt.name} (primary key). Other tables link to "
            f"{dt.name} by storing this value."
        )
    if column == "docstatus":
        if dt.is_submittable:
            return "Document Status", (
                "0 = Draft, 1 = Submitted, 2 = Cancelled. Only docstatus = 1 "
                "rows are final: business totals must filter docstatus = 1."
            )
        if dt.istable:
            return "Document Status", (
                "Copies the parent document's docstatus: 0 = Draft, "
                "1 = Submitted, 2 = Cancelled."
            )
        return "Document Status", "Always 0: this DocType is not submittable."
    if column == "parent":
        parents = [p for p, _ in sel.parents_of.get(dt.name, [])]
        if len(parents) == 1:
            return f"Parent {parents[0]}", (
                f"ID of the {parents[0]} this row belongs to: join on "
                f"`tab{parents[0]}`.name."
            )
        listed = ", ".join(parents) if parents else "its parent document"
        return "Parent Document", (
            f"ID of the parent document this row belongs to ({listed}). "
            "Always also filter parenttype to the parent DocType when joining."
        )
    if column == "parenttype":
        parents = [p for p, _ in sel.parents_of.get(dt.name, [])]
        return "Parent DocType", "DocType of the parent document" + (
            f": one of {', '.join(parents)}." if parents else "."
        )
    labels = {
        "creation": ("Created On", "When the record was created."),
        "modified": ("Last Modified", "When the record was last changed."),
        "modified_by": ("Modified By", "User who last changed the record."),
        "owner": ("Created By", "User who created the record."),
        "idx": ("Row Number", "Position of the row (1-based) in its list or table."),
        "parentfield": (
            "Parent Field",
            "Name of the table field on the parent that holds this row.",
        ),
    }
    return labels[column]


def term_description(dt: DocType, sel: Selection) -> str:
    parts = []
    if dt.description:
        parts.append(_sentence(_clean(dt.description)))
    if dt.istable:
        parents = [p for p, _ in sel.parents_of.get(dt.name, [])]
        parts.append(
            f"Line rows (child table) of {', '.join(parents)}. Each row "
            "belongs to the document whose ID is in column `parent`"
            + (
                "; filter parenttype to the parent DocType when joining."
                if len(parents) > 1
                else "."
            )
        )
    else:
        parts.append(f"One row per {dt.name} record.")
    if dt.is_submittable:
        parts.append("Submittable: only rows with docstatus = 1 are final.")
    module = f", module {dt.module}" if dt.module else ""
    parts.append(f"ERPNext DocType '{dt.name}'{module}; table `{dt.table_name}`.")
    return " ".join(parts)


# --------------------------------------------------------------------------
# Model
# --------------------------------------------------------------------------


@dataclass
class _Column:
    name: str
    type: str
    attr_name: str
    description: str
    fld: Field | None = None


def _columns_for(dt: DocType, sel: Selection) -> list[_Column]:
    standard = list(_STANDARD)
    if dt.istable:
        standard += _CHILD_STANDARD
    if dt.autoname == "autoincrement":
        standard[0] = ("name", "bigint(20)")

    cols: list[_Column] = []
    seen: set[str] = set()
    for name, ctype in standard:
        attr, desc = _standard_description(dt, name, sel)
        real = (dt.db_columns or {}).get(name, ctype)
        cols.append(_Column(name, real, attr, desc))
        seen.add(name)

    for fld in dt.fields:
        if not fld.has_column or fld.fieldname in seen:
            continue
        if dt.db_columns is not None and fld.fieldname not in dt.db_columns:
            continue  # live mode: metadata says so, but the column isn't there
        seen.add(fld.fieldname)
        cols.append(
            _Column(
                fld.fieldname,
                column_type(fld),
                fld.label or _humanize(fld.fieldname),
                column_description(dt, fld, sel),
                fld,
            )
        )

    # Attribute names must be unique within a term; labels repeat sometimes.
    counts: dict[str, int] = {}
    for col in cols:
        counts[col.attr_name.lower()] = counts.get(col.attr_name.lower(), 0) + 1
    for col in cols:
        if counts[col.attr_name.lower()] > 1:
            col.attr_name = f"{col.attr_name} ({col.name})"
    return cols


def build_model(sel: Selection, db_name: str, *, measures: list[dict] | None = None,
                analyses: list[dict] | None = None) -> dict[str, Any]:
    """Return the model document as plain dicts, ready for ``yaml.safe_dump``."""
    db_id = _id(db_name, "database")
    schema_id = _id(db_name, "schema", db_name)

    tables: list[dict] = []
    terms: list[dict] = []
    foreign_keys: list[dict] = []
    joins: dict[tuple[str, str], dict] = {}
    semantic_fks: list[dict] = []

    col_id = lambda dt_name, col: _id(db_name, "column", dt_name, col)  # noqa: E731
    attr_id = lambda dt_name, col: _id(db_name, "attribute", dt_name, col)  # noqa: E731
    table_id = lambda dt_name: _id(db_name, "table", dt_name)  # noqa: E731
    term_id = lambda dt_name: _id(db_name, "term", dt_name)  # noqa: E731

    def add_join(src: str, dst: str, src_col: str) -> None:
        key = (table_id(src), table_id(dst))
        join = joins.setdefault(
            key,
            {"source_table_id": key[0], "target_table_id": key[1], "join_columns": []},
        )
        pair = {"source": src_col, "target": "name"}
        if pair not in join["join_columns"]:
            join["join_columns"].append(pair)

    for dt in sel.doctypes.values():
        cols = _columns_for(dt, sel)
        tables.append(
            {
                "id": table_id(dt.name),
                "name": dt.table_name,
                "description": term_description(dt, sel),
                "pk": ["name"],
                "type": "BASE TABLE",
                "columns": [
                    {
                        "id": col_id(dt.name, c.name),
                        "name": c.name,
                        "description": c.description,
                        "type": c.type,
                        "sample_values": [],
                        "is_nullable": c.name != "name",
                        "is_unique": c.name == "name",
                    }
                    for c in cols
                ],
            }
        )
        terms.append(
            {
                "id": term_id(dt.name),
                "name": dt.name,
                "description": term_description(dt, sel),
                "represents": [table_id(dt.name)],
                "columns_attributes": [
                    {
                        "id": attr_id(dt.name, c.name),
                        "name": c.attr_name,
                        "description": c.description,
                        "column_id": col_id(dt.name, c.name),
                    }
                    for c in cols
                ],
            }
        )

        for c in cols:
            target = None
            if c.fld is not None and c.fld.fieldtype == "Link":
                target = c.fld.options.strip()
            if not target or target == dt.name or target not in sel.doctypes:
                continue  # self-links and links outside the model: described only
            foreign_keys.append(
                {
                    "source_column_id": col_id(dt.name, c.name),
                    "target_column_id": col_id(target, "name"),
                }
            )
            add_join(dt.name, target, c.name)
            semantic_fks.append(
                {
                    "column_attribute_id": attr_id(target, "name"),
                    "column_id": col_id(dt.name, c.name),
                    "use_in_join_paths": True,
                }
            )

        for parent, _fieldname in sel.parents_of.get(dt.name, []):
            add_join(dt.name, parent, "parent")
            fk = {
                "source_column_id": col_id(dt.name, "parent"),
                "target_column_id": col_id(parent, "name"),
            }
            if fk not in foreign_keys:
                foreign_keys.append(fk)
            semantic_fks.append(
                {
                    "column_attribute_id": attr_id(parent, "name"),
                    "column_id": col_id(dt.name, "parent"),
                    "use_in_join_paths": True,
                }
            )

    sql_attributes = []
    for m in measures or []:
        sql_attributes.append(
            {
                "id": _id(db_name, "sql_attribute", m["term"], m["name"]),
                "name": m["name"],
                "description": m["description"],
                "sql": m["sql"].strip(),
                "sql_column_is": [],
                "term_id": term_id(m["term"]),
            }
        )
    custom_analyses = [
        {
            "id": _id(db_name, "analysis", a["name"]),
            "name": a["name"],
            "description": a["description"],
            "sql": a["sql"].strip(),
            "sql_column_is": [],
        }
        for a in analyses or []
    ]

    return {
        "data_layer": {
            "databases": [
                {
                    "id": db_id,
                    "dialect": "mysql",
                    "schemas": [
                        {
                            "id": schema_id,
                            "name": db_name,
                            "database_name": db_name,
                            "tables": tables,
                        }
                    ],
                }
            ],
            "foreign_keys": foreign_keys,
            "joins": list(joins.values()),
        },
        "semantic_layer": {
            "terms": terms,
            "semantic_fks": semantic_fks,
            "sql_attributes": {
                "manual": sql_attributes,
                "table": [],
                "sql": [],
                "bridge_table": [],
            },
            "custom_analyses": custom_analyses,
        },
        "zones": [],
    }


def check_references(model: dict[str, Any]) -> list[str]:
    """Return every id the document references but does not define.

    Auto Ontology's import fails the whole transaction on the first such id,
    so this catches it before upload.
    """
    dl, sl = model["data_layer"], model["semantic_layer"]
    tables = {t["id"] for db in dl["databases"] for s in db["schemas"] for t in s["tables"]}
    columns = {
        c["id"]
        for db in dl["databases"]
        for s in db["schemas"]
        for t in s["tables"]
        for c in t["columns"]
    }
    terms = {t["id"] for t in sl["terms"]}
    attrs = {a["id"] for t in sl["terms"] for a in t["columns_attributes"]}
    problems = []
    for fk in dl["foreign_keys"]:
        for key in ("source_column_id", "target_column_id"):
            if fk[key] not in columns:
                problems.append(f"foreign key {key} {fk[key]}")
    for j in dl["joins"]:
        for key in ("source_table_id", "target_table_id"):
            if j[key] not in tables:
                problems.append(f"join {key} {j[key]}")
    for t in sl["terms"]:
        problems += [f"term {t['name']} represents {r}" for r in t["represents"] if r not in tables]
        problems += [
            f"attribute {a['name']} column {a['column_id']}"
            for a in t["columns_attributes"]
            if a["column_id"] not in columns
        ]
    for s in sl["semantic_fks"]:
        if s["column_id"] not in columns or s["column_attribute_id"] not in attrs:
            problems.append(f"semantic fk {s}")
    for a in sl["sql_attributes"]["manual"]:
        if a["term_id"] not in terms:
            problems.append(f"sql attribute {a['name']} term {a['term_id']}")
    return problems
