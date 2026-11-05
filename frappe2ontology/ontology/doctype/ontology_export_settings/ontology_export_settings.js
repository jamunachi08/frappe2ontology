// Copyright (c) 2026, Neotec and contributors
// For license information, please see license.txt

frappe.ui.form.on("Ontology Export Settings", {
	refresh(frm) {
		frm.add_custom_button(__("Preview"), () => {
			if (frm.is_dirty()) return frappe.msgprint(__("Save the settings first."));
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
		});

		frm.add_custom_button(__("Download Model"), () => {
			if (frm.is_dirty()) return frappe.msgprint(__("Save the settings first."));
			window.open(
				"/api/method/frappe2ontology.api.download_model",
				"_blank"
			);
		});

		frm.add_custom_button(__("Push to Auto Ontology"), () => {
			if (frm.is_dirty()) return frappe.msgprint(__("Save the settings first."));
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
						freeze: true,
						freeze_message: __("Uploading to Auto Ontology... this can take a few minutes."),
						callback: (r) => {
							frappe.msgprint({
								title: __("Sent to Auto Ontology"),
								message: r.message.summary,
								indicator: "green",
							});
							frm.reload_doc();
						},
					});
				}
			);
		});
	},
});
