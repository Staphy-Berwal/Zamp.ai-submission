"""Adversarial engine tests. A fake extractor stands in for Claude so each test isolates one
failure mode: what the engine does when extraction is incomplete, unreadable, contradictory,
or when risk signals overlap. Run: python -m engine.edge_tests"""
import copy, json, os
from contextlib import contextmanager

from . import extract as extract_mod
from . import pipeline
from .comms import _vendor_template, leaks, safe_asks, vendor_view
from .ids import make_gstin

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE_FORM = json.load(open(os.path.join(ROOT, "testcases", "TC1", "form.json")))


def F(v, c=0.97):
    return {"value": v, "confidence": c}


def base_docs():
    fm = BASE_FORM
    return {
        "pan.pdf": {"doc_type": "pan_card", "fields": {"pan": F(fm["pan"]), "name": F(fm["legal_name"].upper())}},
        "gst.pdf": {"doc_type": "gst_certificate", "fields": {"gstin": F(fm["gstin"]), "legal_name": F(fm["legal_name"].upper())}},
        "coi.pdf": {"doc_type": "incorporation_certificate",
                    "fields": {"cin": F(fm["cin"]), "company_name": F(fm["legal_name"].upper()), "pan": F(fm["pan"])}},
        "cheque.pdf": {"doc_type": "bank_proof", "fields": {"account_number": F(fm["account_number"]), "ifsc": F(fm["ifsc"]),
                                                          "account_holder": F(fm["legal_name"].upper()), "bank_name": F("HDFC BANK")}},
    }


@contextmanager
def fake(docs, registry_patch=None):
    real_extract, real_load = extract_mod.extract_one, pipeline.load

    def fake_extract(name, data):
        d = copy.deepcopy(docs[name]); d.setdefault("type_confidence", 0.95); d.setdefault("type_basis", "content")
        d.setdefault("quality_notes", "")
        return d, "Fake extractor (test)", None

    def fake_load(name):
        data = real_load(name)
        if registry_patch and name in registry_patch:
            data = registry_patch[name](copy.deepcopy(data))
        return data
    extract_mod.extract_one, pipeline.load = fake_extract, fake_load
    try:
        yield
    finally:
        extract_mod.extract_one, pipeline.load = real_extract, real_load


def run(form, docs, versions=None, registry_patch=None, policy=None):
    versions = versions or {}
    documents = [{"name": n, "data": (n + str(versions.get(n, 1))).encode(), "version": versions.get(n, 1)} for n in docs]
    with fake(docs, registry_patch):
        return [e for e in pipeline.run_pipeline(form, documents, policy=policy)][-1][1]


def has(res, check, severity):
    return any(f["check"] == check and f["severity"] == severity for f in res["findings"])


# ------------------------------------------------------------------ the tests
def t_baseline():
    r = run(BASE_FORM, base_docs())
    return r, r["decision"]["status"] == "Approved", "Clean fake extraction is approved"


def t_pan_field_missing():
    d = base_docs(); del d["pan.pdf"]["fields"]["pan"]
    r = run(BASE_FORM, d)
    ok = r["decision"]["status"] == "Pending" and has(r, "fields_pan_card", "fixable")
    return r, ok, "PAN card received but PAN not found -> Pending (presence is not verification)"


def t_holder_missing():
    d = base_docs(); del d["cheque.pdf"]["fields"]["account_holder"]
    r = run(BASE_FORM, d)
    ok = r["decision"]["status"] == "Pending" and not r["decision"]["payment_ready"]
    return r, ok, "Bank proof without a holder name -> Pending, payment blocked"


def food_case(valid_upto):
    form = dict(BASE_FORM, category="Food & Beverage Supplies")
    d = base_docs()
    d["fssai.pdf"] = {"doc_type": "fssai_licence", "fields": {"licence_number": F("12417012000345")}}
    if valid_upto is not None:
        d["fssai.pdf"]["fields"]["valid_upto"] = F(valid_upto)
    return form, d


def t_expiry_unparseable():
    form, d = food_case("31st of March, twenty thirty")
    r = run(form, d)
    ok = r["decision"]["status"] == "Pending" and has(r, "expiry_fssai_licence", "fixable")
    return r, ok, "Expiry date present but unparseable -> Pending (never assumed valid)"


def t_expiry_missing():
    form, d = food_case(None)
    r = run(form, d)
    ok = r["decision"]["status"] == "Pending" and has(r, "fields_fssai_licence", "fixable")
    return r, ok, "Licence without an expiry date -> Pending"


def t_expiry_valid_other_format():
    form, d = food_case("31 Mar 2031")
    r = run(form, d)
    return r, r["decision"]["status"] == "Approved", "Valid expiry in another date format is understood -> Approved"


def t_low_conf_account():
    d = base_docs(); d["cheque.pdf"]["fields"]["account_number"] = F("5010023?5678??", 0.35)
    r = run(BASE_FORM, d)
    ok = (r["decision"]["status"] == "Pending" and has(r, "clarity_bank_proof", "fixable")
          and not any("differs" in f["title"] or f["severity"] == "critical" for f in r["findings"]))
    return r, ok, "Low-confidence account number -> asks for clearer copy, never reports a mismatch"


def t_supersede_unreadable():
    d = base_docs()
    d["blurry.jpg"] = {"doc_type": "bank_proof", "fields": {"account_number": F(None, 0), "account_holder": F(None, 0)}}
    d["cheque.pdf"]["fields"]  # clear replacement uploaded in v2
    r = run(BASE_FORM, d, versions={"blurry.jpg": 1, "cheque.pdf": 2})
    return r, r["decision"]["status"] == "Approved", "Unreadable v1 cheque replaced by a clear v2 cheque -> Approved"


def t_same_pan_same_bank():
    """An existing vendor (Orion) registers again: same PAN, GSTIN and bank. Not fraud - a duplicate record."""
    m = pipeline.load("vendor_master.json")[0]
    name = m["legal_name"]
    form = dict(BASE_FORM, legal_name=name, pan=m["pan"], gstin=m["gstin"], account_number=m["account_number"],
                account_holder=name, cin="U51100MH2015PTC262114")
    d = base_docs()
    d["pan.pdf"]["fields"] = {"pan": F(m["pan"]), "name": F(name.upper())}
    d["gst.pdf"]["fields"] = {"gstin": F(m["gstin"]), "legal_name": F(name.upper())}
    d["coi.pdf"]["fields"] = {"cin": F(form["cin"]), "company_name": F(name.upper()), "pan": F(m["pan"])}
    d["cheque.pdf"]["fields"].update(account_number=F(m["account_number"]), account_holder=F(name.upper()))
    r = run(form, d)
    ok = (has(r, "dup_identity", "review") and has(r, "bank_same_entity", "info")
          and r["decision"]["sub_status"] == "Awaiting internal review")
    return r, ok, "Existing vendor re-registers (same PAN + same bank) -> both signals recorded, internal review not rejection"


def t_shared_bank_hold():
    master = pipeline.load("vendor_master.json")[0]
    form = dict(BASE_FORM, account_number=master["account_number"])
    d = base_docs()
    d["cheque.pdf"]["fields"]["account_number"] = F(master["account_number"])
    d["cheque.pdf"]["fields"]["account_holder"] = F("ORION TRADERS PVT LTD")  # honest: cheque shows the real holder
    r = run(form, d)
    ok = r["decision"]["status"] == "Pending" and r["decision"]["sub_status"].startswith("High-risk hold")
    return r, ok, "Shared bank account, honestly declared -> High-risk hold, NOT automatic rejection"


def t_forged_cheque():
    master = pipeline.load("vendor_master.json")[0]
    form = dict(BASE_FORM, account_number=master["account_number"])
    d = base_docs(); d["cheque.pdf"]["fields"]["account_number"] = F(master["account_number"])
    r = run(form, d)
    return r, r["decision"]["status"] == "Rejected", "Shared account + cheque claiming vendor's name (bank disagrees) -> Rejected"


def t_gstin_other_pan():
    other = make_gstin("27", "AAECO4521M")
    form = dict(BASE_FORM, gstin=other)
    d = base_docs(); d["gst.pdf"]["fields"]["gstin"] = F(other)
    r = run(form, d)
    return r, r["decision"]["status"] == "Rejected" and has(r, "gstin_pan", "critical"), \
        "GSTIN belongs to another PAN while PAN card confirms vendor's PAN -> Rejected"


def t_gstin_typo():
    g = BASE_FORM["gstin"]; bad = g[:-1] + ("A" if g[-1] != "A" else "B")
    r = run(dict(BASE_FORM, gstin=bad), base_docs())
    return r, r["decision"]["status"] == "Pending" and has(r, "gstin_format", "fixable"), "One-character GSTIN typo -> checksum catches it"


def t_registry_missing():
    def drop(reg):
        reg.pop(BASE_FORM["gstin"], None); return reg
    r = run(BASE_FORM, base_docs(), registry_patch={"gst_registry.json": drop})
    ok = r["decision"]["sub_status"] == "Awaiting internal review"
    return r, ok, "GSTIN absent from registry -> internal review, not rejection"


def t_registry_cancelled():
    def cancel(reg):
        reg[BASE_FORM["gstin"]]["status"] = "Cancelled"; return reg
    r = run(BASE_FORM, base_docs(), registry_patch={"gst_registry.json": cancel})
    return r, r["decision"]["status"] == "Rejected", "Cancelled GST registration -> Rejected"


def t_employee_link():
    emp = pipeline.load("employee_master.json")[1]
    form = dict(BASE_FORM, contact_phone=emp["phone"])
    r = run(form, base_docs())
    ok = r["decision"]["sub_status"].startswith("High-risk hold") and has(r, "employee_link", "hold")
    return r, ok, "Vendor phone matches an AP employee -> High-risk hold"


def t_pan_entity_mismatch():
    r = run(dict(BASE_FORM, entity_type="Sole Proprietorship", proprietor_name="Anil Deshmukh"), base_docs())
    return r, has(r, "pan_entity", "review"), "Company PAN declared as a proprietorship -> reviewer flag"


def t_bad_form_formats():
    r = run(dict(BASE_FORM, contact_email="accounts@", contact_phone="12345"), base_docs())
    ok = has(r, "format_email", "fixable") and has(r, "format_phone", "fixable")
    return r, ok, "Malformed email and phone -> vendor asked to correct"


def t_leak_guard():
    d = {"status": "Pending", "sub_status": "High-risk hold - Finance Controls"}
    view = vendor_view(d)
    email = _vendor_template(BASE_FORM, view, [])
    bad = {"subject": "x", "body": "Your bank account is shared with another vendor."}
    r = t_shared_bank_hold()[0]
    hold_asks = safe_asks(r["decision"], r["findings"])
    ok = view == "under_review" and not leaks(email) and leaks(bad) and hold_asks == []
    return None, ok, "Hold/rejection emails reveal nothing and ask nothing; guard catches a leaking draft"


def t_low_conf_key_field():
    form, d = food_case("31/03/2031")
    d["fssai.pdf"]["fields"]["licence_number"] = F("1241701200??45", 0.4)
    r = run(form, d)
    return r, has(r, "clarity_fssai_licence", "fixable"), "Low-confidence licence number -> flagged even though nothing compares it"


def t_two_bank_conflict():
    d = base_docs(); d["cheque2.pdf"] = copy.deepcopy(d["cheque.pdf"])
    d["cheque2.pdf"]["fields"]["account_number"] = F("99887766554433")
    r = run(BASE_FORM, d)
    ok = has(r, "conflict_bank_proof", "fixable") and not r["decision"]["payment_ready"] and r["decision"]["status"] == "Pending"
    return r, ok, "Two different bank proofs in one upload -> vendor asked which; neither silently used"


def t_two_identical_copies():
    d = base_docs(); d["cheque_copy.pdf"] = copy.deepcopy(d["cheque.pdf"])
    r = run(BASE_FORM, d)
    return r, r["decision"]["status"] == "Approved" and has(r, "dupcopy_bank_proof", "info"), \
        "Two identical copies of the same bank proof -> harmless, Approved"


def t_all_state_codes():
    from .ids import STATE_CODES, gstin_valid, same_state
    ok = all(gstin_valid(make_gstin(c, "AAKCS7781L"))[0] for c in STATE_CODES)
    ok = ok and same_state("11", "Sikkim") and same_state("38", "Ladakh") and same_state("26", "Daman & Diu")
    ok = ok and same_state("27", "") is None and same_state("27", "Karnataka") is False
    return None, ok, f"All {len(STATE_CODES)} GST state/UT codes accepted; unknown state = 'not verified', never 'match'"


def t_state_unverified():
    r = run(dict(BASE_FORM, state=""), base_docs())
    ok = has(r, "gstin_state_unverified", "info") and not has(r, "gstin_state", "pass")
    return r, ok, "Declared state missing -> state check reported as not verified, not as a match"


def t_ifsc_offline_not_pass():
    real = pipeline.ifsc_lookup
    pipeline.ifsc_lookup = lambda code: {"found": True, "live": False, "bank": "HDFC Bank", "branch": None,
                                         "source": "Offline bank-prefix table (directory unreachable)"}
    try:
        r = run(BASE_FORM, base_docs())
    finally:
        pipeline.ifsc_lookup = real
    ok = not has(r, "ifsc_lookup", "pass") and has(r, "ifsc_lookup", "info")
    return r, ok, "IFSC directory unreachable -> bank prefix noted, branch NOT reported as verified"


def t_payment_simulated():
    r = run(BASE_FORM, base_docs()); d = r["decision"]
    ok = d["status"] == "Approved" and not d["payment_ready"] and d["demo_bank_checks_passed"]
    return r, ok, "Approved with simulated bank checks -> NOT payment-ready (demo checks passed only)"


def t_policy_stricter():
    m = pipeline.load("vendor_master.json")[0]
    form = dict(BASE_FORM, account_number=m["account_number"])
    d = base_docs(); d["cheque.pdf"]["fields"]["account_number"] = F(m["account_number"])
    d["cheque.pdf"]["fields"]["account_holder"] = F("ORION TRADERS PVT LTD")
    strict = json.loads(json.dumps(pipeline.DEFAULT_POLICY)); strict["name"] = "Strict"; strict["severity"]["shared_bank"] = "critical"
    r = run(form, d, policy=strict)
    return r, r["decision"]["status"] == "Rejected", "Client policy 'shared account = breach' -> same case now Rejected"


def t_policy_cannot_soften_breach():
    m = pipeline.load("vendor_master.json")[0]
    form = dict(BASE_FORM, account_number=m["account_number"])
    d = base_docs(); d["cheque.pdf"]["fields"]["account_number"] = F(m["account_number"])
    lenient = json.loads(json.dumps(pipeline.DEFAULT_POLICY)); lenient["name"] = "Lenient"
    lenient["severity"].update({k: v[1][0] for k, v in pipeline.TUNABLE.items()})
    r = run(form, d, policy=lenient)
    return r, r["decision"]["status"] == "Rejected", "Most lenient policy still rejects a forged bank proof (not tunable)"


def t_evidence_conflict():
    r = t_forged_cheque()[0]
    row = next(x for x in r["evidence"] if x["fact"] == "Account holder")
    ok = row["verdict"] == "contradicted" and next(x for x in r["evidence"] if x["fact"] == "Legal name")["verdict"] == "verified_sim"
    return r, ok, "Evidence matrix: legal name confirmed by registry (sim.), account holder contradicted by the bank"


# ------------------------------------------------------------------ iteration 4: evidence integrity + governance
@contextmanager
def live_providers(ifsc_live=True, penny_live=True):
    real_bank, real_fetch = pipeline.verify_bank_account, pipeline._ifsc_fetch
    def bank(acct):
        r = real_bank(acct); r["live"] = penny_live
        r["source"] = "Bank verification - LIVE (test double)" if penny_live else r["source"]; return r
    def fetch(code):
        if not ifsc_live:
            raise ConnectionError("directory down")
        return {"IFSC": code, "BANK": "HDFC Bank", "BRANCH": "Kamala Mills"}
    pipeline.verify_bank_account, pipeline._ifsc_fetch = bank, fetch
    pipeline._ifsc_cache.clear()
    try:
        yield
    finally:
        pipeline.verify_bank_account, pipeline._ifsc_fetch = real_bank, real_fetch
        pipeline._ifsc_cache.clear()


def row(r, fact):
    return next(x for x in r["evidence"] if x["fact"] == fact)


def t_absence_not_corroboration():
    r = run(BASE_FORM, base_docs()); pan = row(r, "PAN")
    vm = next(c for c in pan["cells"] if c["source"] == "Vendor master")
    ok = vm["status"] == "no_conflict" and pan["verdict"] == "consistent" and pan["independent_live"] == pan["independent_sim"] == 0
    return r, ok, "'Not in vendor master' = no known conflict, never corroboration; PAN stays 'consistent', not 'verified'"


def t_pan_in_gstin_derived():
    r = run(BASE_FORM, base_docs())
    cell = next(c for c in row(r, "PAN")["cells"] if c["source"] == "PAN inside GSTIN")
    return r, cell["role"] == "derived" and row(r, "PAN")["vendor_docs_agree"] == 2, \
        "PAN inside the GSTIN is marked derived and not counted as a separate witness"


def t_vendor_docs_alone_not_verified():
    r = t_forged_cheque()[0]
    ok = row(r, "Account holder")["verdict"] == "contradicted" and row(r, "PAN")["verdict"] == "consistent"
    return r, ok, "Forged cheque: vendor documents all agree, only the third party contradicts -> contradicted"


def t_hold_procurement_cannot_approve():
    from .governance import GovernanceError, allowed_actions, apply_review
    r = t_shared_bank_hold()[0]
    try:
        apply_review(r, "approve", "Procurement reviewer", "Asha", "Looks fine to me")
        return r, False, "x"
    except GovernanceError:
        pass
    return r, "approve" not in allowed_actions(r, "Procurement reviewer") and "clear_hold" not in \
        allowed_actions(r, "Procurement reviewer"), "High-risk hold cannot be approved or cleared by a procurement reviewer"


def t_hold_fc_needs_verification():
    from .governance import GovernanceError, VERIFY_METHODS, apply_review
    r = t_shared_bank_hold()[0]
    try:
        apply_review(r, "clear_hold", "Finance Controls", "Vikram", "Cleared", verification=None)
        return r, False, "x"
    except GovernanceError:
        pass
    r2 = apply_review(r, "clear_hold", "Finance Controls", "Vikram", "Parent relationship confirmed",
                      verification={"method": VERIFY_METHODS[0], "evidence": "Called Orion on +91 98200 45512 (on file)"})
    ok = not any(f["severity"] == "hold" for f in r2["findings"]) and "High-risk" not in r2["decision"]["sub_status"]
    return r2, ok, "Finance Controls clears a hold only with a recorded independent verification"


def t_dual_approval_identity():
    from .governance import GovernanceError, apply_review
    r = t_forged_cheque()[0]
    r1 = apply_review(r, "request_exception", "Procurement reviewer", "Asha Rao", "Parent company arrangement")
    try:
        apply_review(r1, "approve_exception", "Finance Controls", "  asha   RAO ", "self-approval attempt")
        return r1, False, "x"
    except GovernanceError:
        pass
    r2 = apply_review(r1, "approve_exception", "Finance Controls", "Vikram", "Second approval")
    exc = r2["decision"]["exception"]
    ok = (exc["dual_approval_authenticated"] is False and r2["decision"]["status"] == "Approved"
          and not r2["decision"]["payment_ready"] and r2["decision"]["system_status"] == "Rejected")
    return r2, ok, "Dual approval: same person (any spelling) blocked; never marked authenticated; payment stays blocked"


def t_reviewer_never_opens_payment():
    from .governance import apply_review
    r = run(BASE_FORM, base_docs())
    r2 = apply_review(r, "approve", "Finance Controls", "Vikram", "Approve")
    return r2, not r2["decision"]["payment_ready"], "Reviewer approval cannot switch payment on - only the central gate can"


def t_live_penny_offline_ifsc():
    with live_providers(ifsc_live=False, penny_live=True):
        r = run(BASE_FORM, base_docs())
    failing = [c["condition"] for c in r["decision"]["payment_gate"] if not c["met"]]
    ok = r["decision"]["status"] == "Approved" and not r["decision"]["payment_ready"] and any("IFSC" in f for f in failing)
    return r, ok, "Live penny drop but IFSC branch unverified -> payment stays blocked"


def t_gate_opens_when_all_met():
    with live_providers(ifsc_live=True, penny_live=True):
        r = run(BASE_FORM, base_docs())
    ok = r["decision"]["payment_ready"] and row(r, "Account holder")["verdict"] == "verified"
    return r, ok, "Positive control: every condition met with live providers -> payment-ready and 'verified'"


def t_ifsc_outage_recovery():
    real = pipeline._ifsc_fetch
    calls = {"n": 0}
    def flaky(code):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("outage")
        return {"IFSC": code, "BANK": "HDFC Bank", "BRANCH": "Kamala Mills"}
    pipeline._ifsc_fetch = flaky; pipeline._ifsc_cache.clear()
    try:
        first, second = pipeline.ifsc_lookup("HDFC0000001"), pipeline.ifsc_lookup("HDFC0000001")
    finally:
        pipeline._ifsc_fetch = real; pipeline._ifsc_cache.clear()
    return None, (not first["live"]) and second["live"], "IFSC outage is not cached: the next lookup retries and gets the live answer"


def t_policy_diff_same_outcome():
    from .policy_sim import diff
    m = pipeline.load("vendor_master.json")[0]
    form = dict(BASE_FORM, account_number=m["account_number"])
    d = base_docs(); d["cheque.pdf"]["fields"]["account_number"] = F(m["account_number"])
    strict = json.loads(json.dumps(pipeline.DEFAULT_POLICY)); strict["name"] = "Strict"; strict["severity"]["shared_bank"] = "critical"
    a, b = run(form, d), run(form, copy.deepcopy(d), policy=strict)
    x = diff(a, b)
    ok = (not x["outcome_changed"]) and any(c["check"] == "shared_bank" and c["after"] == "Policy breach" for c in x["findings"])
    return b, ok, "Policy change that alters a control's severity but not the outcome is still shown in the diff"


TESTS = [t_baseline, t_pan_field_missing, t_holder_missing, t_expiry_unparseable, t_expiry_missing,
         t_expiry_valid_other_format, t_low_conf_account, t_supersede_unreadable, t_same_pan_same_bank,
         t_shared_bank_hold, t_forged_cheque, t_gstin_other_pan, t_gstin_typo, t_registry_missing,
         t_registry_cancelled, t_employee_link, t_pan_entity_mismatch, t_bad_form_formats, t_leak_guard,
         t_low_conf_key_field, t_two_bank_conflict, t_two_identical_copies, t_all_state_codes, t_state_unverified,
         t_ifsc_offline_not_pass, t_payment_simulated, t_policy_stricter, t_policy_cannot_soften_breach, t_evidence_conflict,
         t_absence_not_corroboration, t_pan_in_gstin_derived, t_vendor_docs_alone_not_verified,
         t_hold_procurement_cannot_approve, t_hold_fc_needs_verification, t_dual_approval_identity,
         t_reviewer_never_opens_payment, t_live_penny_offline_ifsc, t_gate_opens_when_all_met, t_ifsc_outage_recovery,
         t_policy_diff_same_outcome]


def run_all():
    out = []
    for t in TESTS:
        try:
            r, ok, desc = t()
            got = f"{r['decision']['status']} - {r['decision']['sub_status']}" if r else "-"
            out.append({"Test": desc, "Outcome": got, "Result": "✅ pass" if ok else "❌ fail"})
        except Exception as e:
            out.append({"Test": t.__name__, "Outcome": f"ERROR {e}", "Result": "❌ fail"})
    return out


if __name__ == "__main__":
    rows = run_all()
    for r in rows:
        print(r["Result"], "|", r["Test"], "|", r["Outcome"])
    print(f"\n{sum(r['Result'].startswith('✅') for r in rows)}/{len(rows)} passed")
