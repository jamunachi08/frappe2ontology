// Copyright (c) 2026, Neotec and contributors
// For license information, please see license.txt

const PROVIDERS = {
	"NVIDIA (build.nvidia.com)": ["https://integrate.api.nvidia.com/v1", "nvidia/nemotron-3.5-lightning-30b-a3b"],
	"Google Gemini": ["https://generativelanguage.googleapis.com/v1beta/openai", "gemini-2.5-flash"],
	"Other OpenAI-compatible": ["", ""],
};

const ICONS = { ok: "✅", warn: "⚠️", fail: "❌", skip: "➖" };

function saved(frm) {
	if (frm.is_dirty()) {
		frappe.msgprint(__("Save the settings first."));
		return false;
	}
	return true;
}

function download(method) {
	window.open(`/api/method/${method}`, "_blank");
}

frappe.ui.form.on("Ontology Export Settings", {
	llm_provider(frm) {
		const [endpoint, model] = PROVIDERS[frm.doc.llm_provider] || ["", ""];
		frm.set_value("llm_endpoint", endpoint);
		frm.set_value("llm_model", model);
	},

	refresh(frm) {
		frm.set_intro(
			__(
				"1. Pick what to export and Save. 2. Download Server Kit and run setup.sh on the Auto Ontology server. " +
					"3. Run <code>bench --site {0} ontology-readonly-user</code> here. " +
					"4. Paste the Auto Ontology API token, Save, Check Setup, then Push to Auto Ontology.",
				[frappe.boot.sitename || "your-site"]
			),
			"blue"
		);

		frm.add_custom_button(
			__("Check Setup"),
			() => {
				if (!saved(frm)) return;
				frappe.call({
					method: "frappe2ontology.api.check_setup",
					freeze: true,
					freeze_message: __("Checking..."),
					callback: (r) => {
						const rows = (r.message || [])
							.map(
								(c) =>
									`<tr><td style="width:2em">${ICONS[c.status] || ""}</td>` +
									`<td><b>${frappe.utils.escape_html(c.check)}</b><br>` +
									`<span class="text-muted">${frappe.utils.escape_html(c.detail || "")}</span></td></tr>`
							)
							.join("");
						frappe.msgprint({
							title: __("Setup Check"),
							message: `<table class="table table-condensed">${rows}</table>`,
							wide: true,
						});
					},
				});
			},
			__("Setup")
		);

		frm.add_custom_button(
			__("Download Server Kit"),
			() => {
				if (!saved(frm)) return;
				frappe.confirm(
					__(
						"The kit contains passwords and API keys for the Auto Ontology server. " +
							"Copy it only to that server. Continue?"
					),
					() => {
						download("frappe2ontology.api.download_server_kit");
						setTimeout(() => frm.reload_doc(), 3000);
					}
				);
			},
			__("Setup")
		);

		frm.add_custom_button(
			__("Preview"),
			() => {
				if (!saved(frm)) return;
				frappe.call({
					method: "frappe2ontology.api.preview",
					freeze: true,
					freeze_message: __("Reading DocType metadata..."),
					callback: (r) => {
						frappe.msgprint({
							title: __("Model for database {0}", [r.message.db_name]),
							message: r.message.summary,
							indicator: "blue",
						});
					},
				});
			},
			__("Model")
		);

		frm.add_custom_button(
			__("Download Model"),
			() => saved(frm) && download("frappe2ontology.api.download_model"),
			__("Model")
		);

		frm.add_custom_button(__("Push to Auto Ontology"), () => {
			if (!saved(frm)) return;
			if (!frm.doc.auto_ontology_url) {
				return frappe.msgprint(__("Set the Auto Ontology URL first."));
			}
			const warning = frm.doc.replace_existing
				? __("Replace Existing Definitions is on: definitions for this database that are not in the model will be deleted in Auto Ontology.")
				: __("Existing definitions in Auto Ontology are kept.");
			frappe.confirm(
				__("Send the model to {0}?", [frm.doc.auto_ontology_url]) + "<br><br>" + warning,
				() => {
					frappe.call({
						method: "frappe2ontology.api.push_model",
						callback: () => {
							frappe.show_alert({
								message: __("Push started. You will get a message when it finishes (usually a few minutes)."),
								indicator: "blue",
							}, 10);
						},
					});
				}
			);
		}).addClass("btn-primary");
	},
});
