"""Turn Frappe/ERPNext DocType metadata into an NVIDIA Auto Ontology model.

Frappe already knows what every table and column means and how they join
(Link fields, child tables). This package writes that knowledge in Auto
Ontology's native model YAML so it can be imported instead of being guessed
by an LLM.
"""

__version__ = "0.1.0"
