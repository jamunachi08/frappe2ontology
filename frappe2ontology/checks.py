"""The Check Setup checklist: each check says ok / warn / fail and what to do."""

from __future__ import annotations

from typing import Callable

import frappe

from frappe2ontology import aoclient, api

OK, WARN, FAIL, SKIP = "ok", "warn", "fail", "skip"


def _check(results: list, name: str, fn: Callable[[], tuple[str, str]]) -> str:
    try:
        status, detail = fn()
    except Exception as exc:  # a check must never break the checklist
        status, detail = FAIL, str(exc)[:500]
    results.append({"check": name, "status": status, "detail": detail})
    return status


def run_checks() -> list[dict]:
    settings = frappe.get_single(api.SETTINGS)
    results: list[dict] = []
    state: dict = {}

    def export():
        state["export"] = api.build_export(settings)
        return OK, state["export"].summary

    _check(results, "Model builds from this site", export)

    ro_password = settings.get_password("ro_db_password", raise_exception=False)

    def ro_user():
        if not (settings.ro_db_user and ro_password):
            return FAIL, f"Run: bench --site {frappe.local.site} ontology-readonly-user"
        return OK, f"User {settings.ro_db_user}, host for Auto Ontology: {settings.ro_db_host}:{settings.ro_db_port}"

    have_user = _check(results, "Read-only database user configured", ro_user) == OK

    def ro_access():
        if not have_user or "export" not in state:
            return SKIP, "Needs the read-only user and a model."
        import pymysql

        tables = api.exported_tables(state["export"].model)
        conn = pymysql.connect(
            host=frappe.conf.db_host or "127.0.0.1",
            port=int(frappe.conf.db_port or 3306),
            user=settings.ro_db_user,
            password=ro_password,
            database=api.db_name(),
            unix_socket=frappe.conf.db_socket or None,
        )
        problems = []
        try:
            with conn.cursor() as cursor:
                cursor.execute(f"SELECT 1 FROM `{tables[0]}` LIMIT 1")
                for label, sql in (
                    ("write to " + tables[0], f"UPDATE `{tables[0]}` SET modified = modified WHERE 1 = 0"),
                    ("read password hashes (__Auth)", "SELECT 1 FROM `__Auth` LIMIT 1"),
                ):
                    try:
                        cursor.execute(sql)
                        problems.append(label)
                    except pymysql.err.MySQLError:
                        pass  # denied, as it should be
        finally:
            conn.close()
        if problems:
            return FAIL, "The user can " + " and ".join(problems) + ". Re-run the bench command."
        return OK, f"Can read the {len(tables)} exported tables; cannot write or read __Auth."

    _check(results, "Read-only user can read, and only read", ro_access)

    def mariadb_bind():
        bind = frappe.db.sql("SHOW VARIABLES LIKE 'bind_address'")
        value = bind[0][1] if bind else ""
        if (settings.ro_db_host or "") in ("host.docker.internal", "172.17.0.1") and value in (
            "127.0.0.1",
            "localhost",
            "::1",
        ):
            return WARN, (
                f"MariaDB listens on {value} only, so Docker containers cannot reach it. "
                "Set bind-address = 127.0.0.1,172.17.0.1 and restart MariaDB (see the kit README)."
            )
        return OK, f"bind-address = {value or '(all)'}"

    _check(results, "MariaDB reachable from Auto Ontology", mariadb_bind)

    def keys():
        missing = [
            label
            for field, label in (("nvidia_api_key", "NVIDIA API key"), ("admin_email", "admin email"))
            if not (settings.get_password(field, raise_exception=False) if field.endswith("key") else settings.get(field))
        ]
        if missing:
            return WARN, "Needed for the server kit: " + ", ".join(missing)
        return OK, f"Chat model: {settings.llm_model or 'default'} via {settings.llm_provider}"

    _check(results, "Server kit settings", keys)

    token = settings.get_password("api_token", raise_exception=False)
    client = aoclient.Client(settings.auto_ontology_url, token, timeout=20) if settings.auto_ontology_url else None

    def reachable():
        if not client:
            return FAIL, "Set the Auto Ontology URL."
        health = client.health()
        return (OK if health.get("status") == "ok" else WARN), f"{settings.auto_ontology_url}: {health}"

    up = _check(results, "Auto Ontology is reachable", reachable) in (OK, WARN)

    def token_ok():
        if not up:
            return SKIP, "Auto Ontology is not reachable."
        if not token:
            return FAIL, "Create a token in Auto Ontology (user menu > API Tokens) and paste it here."
        state["connections"] = client.list_connections()
        return OK, "Token accepted."

    authed = _check(results, "API token works", token_ok) == OK

    def connection():
        if not authed:
            return SKIP, "Needs a working token."
        names = {c.get("database_name") for c in state["connections"]}
        if api.db_name() in names:
            return OK, f"Connection for {api.db_name()} exists."
        return WARN, "Not added yet; Push to Auto Ontology adds it."

    _check(results, "Database connection in Auto Ontology", connection)

    def imported():
        if not authed or "export" not in state:
            return SKIP, "Needs a working token and a model."
        sample = state["export"].model["semantic_layer"]["terms"][0]["name"]
        if any(t.get("name") == sample for t in client.find_terms(sample)):
            return OK, f"Found term '{sample}'."
        return WARN, "Model not imported yet; click Push to Auto Ontology."

    _check(results, "Model imported", imported)
    return results
