"""Command line: ``frappe2ontology --db-name <site_db> ... -o model.yaml``."""

from __future__ import annotations

import argparse
import logging
import sys
from urllib.parse import unquote, urlparse

import yaml

from frappe2ontology import build, measures, meta


def _connect_kwargs(url: str) -> dict:
    parsed = urlparse(url)
    if parsed.scheme.split("+")[0] != "mysql" or not parsed.path.strip("/"):
        raise SystemExit("--site-db must look like mysql://user:pass@host:3306/<site_db>")
    return {
        "host": parsed.hostname or "127.0.0.1",
        "port": parsed.port or 3306,
        "user": unquote(parsed.username or ""),
        "password": unquote(parsed.password or ""),
        "database": unquote(parsed.path.strip("/")),
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="frappe2ontology",
        description="Export Frappe/ERPNext DocType metadata as an Auto Ontology model.",
    )
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument(
        "--site-db",
        metavar="MYSQL_URL",
        help="Read metadata from a live site: mysql://user:pass@host:3306/<site_db>. "
        "Includes Custom Fields and Property Setters (recommended).",
    )
    src.add_argument(
        "--apps",
        nargs="+",
        metavar="APP_DIR",
        help="Read DocType JSON from app source folders, e.g. apps/frappe apps/erpnext.",
    )
    p.add_argument(
        "--db-name",
        help="Site database name, as in Auto Ontology's connection string. "
        "Defaults to the database in --site-db.",
    )
    p.add_argument(
        "--modules",
        nargs="+",
        default=list(build.DEFAULT_MODULES),
        help="Modules whose DocTypes to include (default: %(default)s).",
    )
    p.add_argument("--include", nargs="*", default=[], help="Extra DocTypes to include.")
    p.add_argument(
        "--exclude-regex",
        default=build.DEFAULT_EXCLUDE,
        help="Drop DocTypes whose name matches (default: %(default)r). '' to keep all.",
    )
    p.add_argument(
        "--with-link-targets",
        action="store_true",
        help="Also include tables that included DocTypes link to (one hop).",
    )
    p.add_argument("--no-measures", action="store_true", help="Skip the built-in measures.")
    p.add_argument("-o", "--output", default="-", help="Output YAML file (default: stdout).")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)

    if args.site_db:
        kwargs = _connect_kwargs(args.site_db)
        doctypes = meta.load_from_db(kwargs)
        db_name = args.db_name or kwargs["database"]
    else:
        if not args.db_name:
            raise SystemExit("--db-name is required with --apps")
        doctypes = meta.load_from_json(args.apps)
        db_name = args.db_name
    if not doctypes:
        raise SystemExit("No DocTypes found.")

    sel = build.select_doctypes(
        doctypes,
        modules=args.modules,
        include=args.include,
        exclude_regex=args.exclude_regex or None,
        with_link_targets=args.with_link_targets,
    )
    names = set(sel.doctypes)
    model = build.build_model(
        sel,
        db_name,
        measures=[] if args.no_measures else measures.applicable(measures.MEASURES, names),
        analyses=[] if args.no_measures else measures.applicable(measures.ANALYSES, names),
    )
    problems = build.check_references(model)
    if problems:
        for prob in problems[:20]:
            print(f"dangling reference: {prob}", file=sys.stderr)
        return 1

    text = yaml.safe_dump(model, sort_keys=False, allow_unicode=True, width=100)
    if args.output == "-":
        sys.stdout.write(text)
    else:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(text)
    sl, dl = model["semantic_layer"], model["data_layer"]
    print(
        f"{len(sl['terms'])} terms, "
        f"{sum(len(t['columns_attributes']) for t in sl['terms'])} columns, "
        f"{len(dl['foreign_keys'])} foreign keys, {len(dl['joins'])} joins, "
        f"{len(sl['sql_attributes']['manual'])} measures, "
        f"{len(sl['custom_analyses'])} analyses",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
