"""Small client for the Auto Ontology web app's public API (port 3000).

Only the calls this app needs: health, model import, connections, terms.
Authenticates with an Auto Ontology API token in ``x-api-key``.
"""

from __future__ import annotations

import re
from typing import Any

import requests

IMPORT_TIMEOUT_S = 1800  # import embeds every term and column; minutes on a big site


class AutoOntologyError(Exception):
    pass


class Client:
    def __init__(self, base_url: str, token: str | None, *, timeout: int = 60):
        self.base = base_url.rstrip("/")
        self.session = requests.Session()
        if token:
            self.session.headers["x-api-key"] = token
        self.timeout = timeout

    def _call(self, method: str, path: str, **kwargs) -> requests.Response:
        kwargs.setdefault("timeout", self.timeout)
        try:
            response = self.session.request(method, self.base + path, **kwargs)
        except requests.RequestException as exc:
            raise AutoOntologyError(f"Could not reach Auto Ontology at {self.base}: {exc}") from exc
        if response.status_code >= 400:
            raise AutoOntologyError(
                f"{method} {path} failed with HTTP {response.status_code}: {response.text[:500]}"
            )
        return response

    def health(self) -> dict:
        return self._call("GET", "/api/health").json()

    def import_model(self, yaml_text: str, filename: str, *, replace: bool, embed: bool = True) -> dict:
        response = self._call(
            "POST",
            "/api/model/import",
            params={"replace": str(replace).lower(), "embed": str(embed).lower()},
            files={"file": (filename, yaml_text.encode("utf-8"), "application/x-yaml")},
            timeout=IMPORT_TIMEOUT_S,
        )
        try:
            return response.json()
        except ValueError:
            return {"response": response.text[:2000]}

    def list_connections(self) -> list[dict]:
        return self._call("GET", "/api/connections").json().get("data", [])

    def create_connection(self, connection: dict[str, Any]) -> dict:
        return self._call(
            "POST", "/api/connections", json={"connection": connection}, timeout=300
        ).json()

    def find_terms(self, query: str) -> list[dict]:
        body = self._call("GET", "/api/terms", params={"query": query, "limit": 20}).json()
        return body.get("terms", [])


def table_allow_regex(table_names: list[str]) -> str:
    """Anchored regex matching exactly these tables (Auto Ontology uses re.search)."""
    return "^(" + "|".join(re.escape(t) for t in sorted(table_names)) + ")$"


def without_measures(model: dict) -> dict:
    """The model minus SQL attributes and analyses.

    Auto Ontology parses each measure with the SQL dialect of the database's
    *connection*. Before the connection exists it assumes Postgres, rejects
    MySQL's backtick quoting and rolls back the whole import. So the catalog
    and terms go first, then the connection, then the full model.
    """
    stripped = dict(model)
    semantic = dict(model["semantic_layer"])
    semantic["sql_attributes"] = {key: [] for key in model["semantic_layer"]["sql_attributes"]}
    semantic["custom_analyses"] = []
    stripped["semantic_layer"] = semantic
    return stripped
