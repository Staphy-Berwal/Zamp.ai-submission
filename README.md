# VendorGuard: vendor onboarding, submission to decision

Zamp ASA case study, PS-2.

**Positioning.** VendorGuard is a configurable vendor-risk decision system with AI-assisted evidence collection, not a
document reader. Trust is earned, not assumed.

1. **Evidence-led.** Every key fact is laid against every source, and each source is classified by who controls it:
   - vendor-supplied documents prove consistency only;
   - derived values (e.g. the PAN inside a GSTIN) are consistency checks;
   - third parties (bank, registry, IFSC directory) can independently verify, and are labelled live or simulated;
   - our own records can reveal conflicts, but "nothing found" is never support.
2. **Governed.** Severities are client policy, with a finding-level impact simulator over past cases. Misconduct is
   never tunable. Reviewer powers depend on the risk tier: high-risk holds need Finance Controls plus a recorded
   independent verification; breaches need a two-person exception. One central gate decides payment.
3. **Proven.** 40 adversarial tests (including governance limits, and a positive control proving the payment gate can
   open) plus 7 end-to-end scenarios.

Honest limits: GST registry and penny drop are simulated (so nothing is ever payment-ready in the demo); reviewer
roles are self-declared; Claude's field confidence is self-reported, not calibrated.

## Run locally
```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...      # without it the app runs in offline mode
streamlit run app.py
```
Test inputs are already in `testcases/`. To regenerate them, run `python make_testdata.py`.
For a quick terminal check of every case, run `python runcase.py TC1 TC2 TC3 TC4 TC5`.

## Deploy (Streamlit Community Cloud, free)
1. Push this folder to a public GitHub repo.
2. On share.streamlit.io, create a new app and point it at `app.py`.
3. Under App settings → Secrets, add `ANTHROPIC_API_KEY = "sk-ant-..."`.
   Optionally add `VG_MODEL` to change the model.
4. Case data is stored in SQLite and resets when the app restarts. Before a demo, open **Test suite**
   and click **Run all scenarios** to repopulate the dashboard.

## Architecture
| Stage | What happens | Who does it |
|---|---|---|
| 1. Intake | Required fields; document requirements set per entity type and category | Rules |
| 2. Document intelligence | Classifies each file and extracts fields with per-field confidence (runs in parallel) | Claude (offline parser fallback) |
| 3. Identifier validation | PAN pattern and holder type; GSTIN checksum, state and embedded PAN; IFSC format | Rules |
| 4. Cross-document | Form vs every document: names (normalised and fuzzy), PAN, GSTIN, CIN, account, IFSC, holder | Rules |
| 5. Verification | GST registry (simulated), IFSC lookup (Razorpay, live), penny drop (simulated) | Rules |
| 6. Duplicate & fraud | Vendor master overlap (PAN, GSTIN, bank, phone, name); employee master; free email domain | Rules |
| 7. Decision | Pure function of finding severities | Rules |
| 8. Communication | Reviewer note and vendor email (neutral on rejections) | Claude (template fallback) |

AI reads and writes; it never decides. Five design rules:
1. No evidence is not a pass: a missing field, unreadable value or unparseable expiry always produces a finding.
2. Never judge what you cannot read: values under 75% confidence trigger a request for a clearer copy, never a mismatch.
3. Suspicion is not proof: risk signals (shared payout account, employee link) put a case on a **high-risk hold**;
   only confirmed policy breaches (bank contradicts the bank proof, GSTIN belongs to another PAN, cancelled GSTIN) reject.
4. The vendor email is drafted only from vendor-safe data, plus a keyword leak guard.
5. Onboarding and payment readiness are separate; a reviewer approval never unblocks an unverified bank account.

## Tests
- `python runcase.py TC1 TC2 TC3 TC4 TC5`: the demo scenarios end to end.
- `python -m engine.edge_tests`: 40 adversarial engine tests (missing fields, unreadable values, overlapping fraud
  signals, registry gaps, message leaks), using a fake extractor and no API calls. Both suites are also in the app's Test suite page.

## Scenarios
| Case | Tests | Expected |
|---|---|---|
| TC1 | Clean company | Approved |
| TC2 | Harmless name variation (`PVT. LTD.`) plus a cheque in a different entity's name | Pending (vendor) |
| TC3 | Perfect-looking documents, but the bank contradicts the cheque (policy breach), and the account already pays another vendor (high-risk signal) | Rejected |
| TC4 → v2 | Missing incorporation certificate and expired FSSAI licence, then resubmission | Pending → Approved |
| TC5 | Proprietorship with a blurry cheque scan; account in the proprietor's own name is fine, unreadable number is not | Pending (vendor) |
| TC6 | Subsidiary honestly paid into its parent's (existing vendor's) account | Pending (high-risk hold); Rejected under a strict client policy |
