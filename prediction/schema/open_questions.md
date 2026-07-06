# Open questions -- delay-prediction <-> forensics alignment

Send this along with feature_list.yaml. These need answers before the
crosswalk (shared event_types.yaml) can be finalized.

1. **Unit of prediction**: project-level, contract-line-item-level, or
   RFI-level? Changes which public data sources are even usable
   (USASpending/FPDS is project/contract-level only; RFI-level data barely
   exists publicly, per earlier research).

2. **Target variable**: probability of delay (classification) vs. predicted
   delay-days (regression) vs. both? Affects how retrieved forensic evidence
   should be phrased in the final output ("72% risk" vs "predicted 14-day
   slip").

3. **Data source decision**: are we training on public data (USASpending/FPDS,
   Kaggle construction datasets) or do we have/expect access to a real
   partner dataset? Several TBD fields in feature_list.yaml (supplier
   reliability, subcontractor default history, site investigation score)
   only exist if we have real project records, not public procurement data.

4. **BoQ category coding**: what taxonomy is the BoQ line-item classification
   using (ICMS, a custom trade code list, raw text)? Needed to fill in
   `boq_category_mix` properly.

5. **Contract-number linkage**: do any of the forensic cases already pulled
   (ASBCA/Court of Federal Claims) have extractable federal contract numbers
   that exist in USASpending/FPDS? If yes, this gives paired
   predictive-feature + causal-narrative data for the same real project --
   worth checking before committing to a fully separate training set.
