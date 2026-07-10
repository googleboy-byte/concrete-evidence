<!-- VENDORED from origin/delay-prediction — do not edit directly, re-vendor instead -->
# Event type reference (from forensics branch, v0.1 draft)

This is a copy of the draft causal event taxonomy proposed on the forensics side,
kept here so the predictive branch can align feature names against it without
needing to dig through the other branch's chat history. Source of truth is
still whatever the forensics branch actually ships in its own schema file --
update this copy if theirs changes.

| Category               | Event types (examples)                                                          |
|------------------------|----------------------------------------------------------------------------------|
| site_conditions        | differing_site_conditions, geotechnical_issue, utility_conflict, environmental_contamination |
| design_information     | rfi_delay, design_freeze, drawing_discrepancy, incomplete_specifications         |
| weather_seasonal       | seasonal_timing_shift, weather_delay, ground_condition_seasonal                  |
| procurement_material   | material_shortage, submittal_delay, long_lead_item, supplier_default            |
| owner_client_caused    | late_award, scope_change, late_possession_of_site, payment_delay                |
| contractor_caused      | resource_shortage, subcontractor_default, workmanship_defect, sequencing_error   |
| regulatory_thirdparty  | permit_delay, inspection_failure, agency_approval_delay, utility_company_delay   |
| schedule_management    | critical_path_impact, float_consumption, acceleration_directive, concurrent_delay |
| financial              | liquidated_damages_trigger, equitable_adjustment_dispute, change_order_dispute   |

Notes:
- This is explicitly v0.1 / not final on their side either -- expect categories
  to shift as more cases get extracted.
- The actual crosswalk artifact both branches should converge on is a single
  shared file (likely `event_types.yaml` at repo root or in a `shared/` folder)
  containing, per event type: category, description, and
  `predictive_feature_aliases: [...]` pointing back at feature_list.yaml names.
- Until that shared file exists, feature_list.yaml's `maps_to_event_type` fields
  are just this branch's best guess and need sign-off from the forensics owner.
