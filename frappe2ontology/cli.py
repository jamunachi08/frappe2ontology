"""Command line, for use outside a bench: ``frappe2ontology --site-db ... -o model.yaml``.

Inside a bench, use the **Ontology Export Settings** page instead, or::

    bench --site <site> execute frappe2ontology.api.export_to_file --kwargs "{'path': '/tmp/model.yaml'}"
"""

from __future__ import annotations

import argparse
import logging
import sys
from urllib.parse import unquote, urlparse

from frappe2ontology import build, meta
from frappe2ontology.export import make_export


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
        help="Read metadata from a site database: mysql://user:pass@host:3306/<site_db>. "
        "Includes Custom Fields and Property Setters. Needs mysql-connector-python.",
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
        help="Drop DocTypes whose name matches. '' to keep all.",
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

    try:
        result = make_export(
            doctypes,
            db_name,
            modules=args.modules,
            include=args.include,
            exclude_regex=args.exclude_regex,
            with_link_targets=args.with_link_targets,
            include_measures=not args.no_measures,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if args.output == "-":
        sys.stdout.write(result.yaml_text)
    else:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(result.yaml_text)
    print(result.summary, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
