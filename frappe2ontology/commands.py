"""Bench commands added by Frappe2Ontology."""

import click
from frappe.commands import get_site, pass_context


@click.command("ontology-readonly-user")
@click.option("--db-root-username", default="root", help="MariaDB admin user (default: root).")
@click.option("--db-root-password", help="MariaDB admin password. Prompted if not given.")
@click.option(
    "--host",
    default="%",
    help="Host the user may connect from. '%' (default) allows any host, "
    "e.g. Docker containers; narrow it, e.g. 172.%, if you can.",
)
@click.option(
    "--print-sql",
    is_flag=True,
    help="Only print the SQL (for a DBA, or Frappe Cloud support); change nothing.",
)
@pass_context
def ontology_readonly_user(context, db_root_username, db_root_password, host, print_sql):
    """Create or re-sync the read-only MariaDB user for Auto Ontology.

    Grants SELECT on exactly the tables in the current export, saves the
    password in Ontology Export Settings, and can be re-run after changing
    the export modules.
    """
    import frappe

    from frappe2ontology import readonly

    site = get_site(context)
    frappe.init(site=site)
    frappe.connect()
    try:
        if not print_sql and not db_root_password and not frappe.conf.get("root_password"):
            db_root_password = click.prompt(
                f"MariaDB password for {db_root_username}", hide_input=True
            )
        out = readonly.setup(
            root_user=db_root_username,
            root_password=db_root_password,
            host=host,
            print_only=print_sql,
        )
        if print_sql:
            click.echo(";\n".join(out["sql"]) + ";")
            click.secho(
                "\nNothing was changed. After running this SQL, the password above is "
                "already saved in Ontology Export Settings.",
                fg="yellow",
                err=True,
            )
        else:
            click.secho(
                f"Read-only user {out['user']} ({', '.join(out['hosts'])}) can now SELECT "
                f"{out['tables']} tables. Password saved in Ontology Export Settings.",
                fg="green",
            )
    finally:
        frappe.destroy()


commands = [ontology_readonly_user]
