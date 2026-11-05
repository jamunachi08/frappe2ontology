"""Create the MariaDB user Auto Ontology reads the site with.

Least privilege: ``SELECT`` on exactly the exported tables, nothing else. In
particular it cannot read ``__Auth`` (password hashes), ``tabUser`` (unless
you export it), or anything outside the model, and it cannot write.
Running it again re-syncs the grants with the current export settings.
"""

from __future__ import annotations

import secrets
import string

import frappe

from frappe2ontology import api


def generate_password(length: int = 32) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _q(value: str) -> str:
    """Quote a string literal for MariaDB."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _ident(value: str) -> str:
    return "`" + value.replace("`", "``") + "`"


def statements(user: str, password: str, hosts: list[str], db: str, tables: list[str]) -> list[str]:
    sql: list[str] = []
    for host in hosts:
        account = f"{_q(user)}@{_q(host)}"
        sql += [
            f"CREATE USER IF NOT EXISTS {account} IDENTIFIED BY {_q(password)}",
            f"ALTER USER {account} IDENTIFIED BY {_q(password)}",
            f"REVOKE ALL PRIVILEGES, GRANT OPTION FROM {account}",
        ]
        sql += [f"GRANT SELECT ON {_ident(db)}.{_ident(t)} TO {account}" for t in sorted(tables)]
    sql.append("FLUSH PRIVILEGES")
    return sql


def setup(
    *,
    root_user: str = "root",
    root_password: str | None = None,
    host: str = "%",
    print_only: bool = False,
) -> dict:
    """Create or re-sync the read-only user. Must run inside a site context."""
    settings = frappe.get_single(api.SETTINGS)
    user = settings.ro_db_user or "ontology_ro"
    password = settings.get_password("ro_db_password", raise_exception=False) or generate_password()
    tables = api.exported_tables(api.build_export(settings).model)
    # '%' does not match local logins when the server has anonymous ''@'localhost'
    # users (the MariaDB default on many installs), so add localhost explicitly.
    hosts = [host] + (["localhost"] if host == "%" else [])
    sql = statements(user, password, hosts, api.db_name(), tables)

    if not print_only:
        import pymysql

        conn = pymysql.connect(
            host=frappe.conf.db_host or "127.0.0.1",
            port=int(frappe.conf.db_port or 3306),
            user=root_user,
            password=root_password or frappe.conf.get("root_password") or "",
            unix_socket=frappe.conf.db_socket or None,
        )
        try:
            with conn.cursor() as cursor:
                for statement in sql:
                    cursor.execute(statement)
            conn.commit()
        finally:
            conn.close()

    settings.ro_db_user = user
    settings.ro_db_password = password
    if not settings.ro_db_port:
        settings.ro_db_port = int(frappe.conf.db_port or 3306)
    settings.flags.ignore_permissions = True
    settings.save()
    frappe.db.commit()
    return {"user": user, "hosts": hosts, "tables": len(tables), "sql": sql}
