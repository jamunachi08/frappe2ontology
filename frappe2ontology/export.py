"""One entry point shared by the CLI, the Settings page and the API."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

import yaml

from frappe2ontology import build, measures
from frappe2ontology.meta import DocType


@dataclass
class Export:
    model: dict[str, Any]
    yaml_text: str
    summary: str


def make_export(
    doctypes: dict[str, DocType],
    db_name: str,
    *,
    modules: Iterable[str] = build.DEFAULT_MODULES,
    include: Iterable[str] = (),
    exclude_regex: str | None = build.DEFAULT_EXCLUDE,
    with_link_targets: bool = False,
    include_measures: bool = True,
) -> Export:
    sel = build.select_doctypes(
        doctypes,
        modules=modules,
        include=include,
        exclude_regex=exclude_regex or None,
        with_link_targets=with_link_targets,
    )
    if not sel.doctypes:
        raise ValueError("No DocTypes matched the chosen modules and filters.")
    names = set(sel.doctypes)
    model = build.build_model(
        sel,
        db_name,
        measures=measures.applicable(measures.MEASURES, names) if include_measures else [],
        analyses=measures.applicable(measures.ANALYSES, names) if include_measures else [],
    )
    problems = build.check_references(model)
    if problems:
        raise ValueError("Model has dangling references: " + "; ".join(problems[:10]))

    sl, dl = model["semantic_layer"], model["data_layer"]
    summary = (
        f"{len(sl['terms'])} terms, "
        f"{sum(len(t['columns_attributes']) for t in sl['terms'])} columns, "
        f"{len(dl['foreign_keys'])} foreign keys, {len(dl['joins'])} joins, "
        f"{len(sl['sql_attributes']['manual'])} measures, "
        f"{len(sl['custom_analyses'])} analyses"
    )
    text = yaml.safe_dump(model, sort_keys=False, allow_unicode=True, width=100)
    return Export(model=model, yaml_text=text, summary=summary)
