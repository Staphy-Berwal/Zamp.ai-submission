import json, os, time
import pandas as pd
import pymupdf
import streamlit as st

from engine import db
from engine.comms import _vendor_template, draft, vendor_view
from engine.governance import (LABELS, ROLES, VERIFY_METHODS, GovernanceError, allowed_actions, apply_review, tier)
from engine.policy_sim import diff as policy_diff
from engine.extract import DOC_LABELS, MODEL, api_key
from engine.pipeline import (ATTENTION, COMPANY_TYPES, DEFAULT_POLICY, STAGES, TUNABLE, load_policy, run_pipeline,
                             save_policy)

ROOT = os.path.dirname(os.path.abspath(__file__))
TC_DIR = os.path.join(ROOT, "testcases")
st.set_page_config(page_title="VendorGuard · Vendor onboarding", page_icon="🛡️", layout="wide")

st.markdown("""<style>
.block-container{padding-top:2rem;max-width:1250px}
.vg-card{border-radius:10px;padding:18px 22px;margin:8px 0 12px 0;border:1px solid}
.vg-Approved{background:#EAF5EE;border-color:#2E7D4F}
.vg-Pending{background:#FBF3E4;border-color:#B7791F}
.vg-Rejected{background:#F9E9E9;border-color:#B23A3A}
.vg-big{font-size:1.6rem;font-weight:700;margin:0;color:#1B2A41}
.vg-sub{color:#3D4A5C;margin:2px 0 10px 0;font-weight:600}
.vg-row{display:flex;gap:28px;flex-wrap:wrap;color:#1B2A41}
.vg-row div{min-width:150px}
.vg-k{font-size:0.78rem;color:#5B6577}
.vg-ev{color:#5B6577;font-size:0.85rem;margin:-6px 0 6px 26px}
.vg-mx{width:100%;border-collapse:collapse;font-size:0.86rem}
.vg-mx td{border-top:1px solid #E3E7EE;padding:7px 6px;vertical-align:top}
.vg-fact{font-weight:700;color:#1B2A41;white-space:nowrap}
.vg-chip{display:inline-block;border-radius:6px;padding:3px 8px;margin:2px 4px 2px 0;border:1px solid}
.c-corroborates{background:#EAF5EE;border-color:#9CCBAE}.c-contradicts{background:#F9E9E9;border-color:#E2A3A3}
.c-risk{background:#F3ECFA;border-color:#B9A0D8}.c-unreadable{background:#FBF3E4;border-color:#E3C48C}
.c-flag{background:#EAF0FA;border-color:#9DB5DD}.c-no_conflict{background:#FFFFFF;border-color:#D5DAE3;color:#5B6577}
.c-unverified,.c-absent{background:#F2F4F8;border-color:#D5DAE3;color:#5B6577}
.vg-role{font-size:0.72rem;color:#5B6577;margin-left:4px}
.v-verified{color:#2E7D4F}.v-verified_sim{color:#2E7D4F}.v-consistent{color:#5B6577}.v-contradicted{color:#B23A3A}
.v-risk{color:#7A4FB5}.v-attention{color:#B7791F}.v-unverified{color:#5B6577}
.vg-sim{background:#EEF2F8;border-left:3px solid #2457A6;padding:8px 12px;font-size:0.88rem;color:#1B2A41;margin:6px 0}
</style>""", unsafe_allow_html=True)

SEV = {"pass": (":green[✓]", "Passed"), "info": (":gray[•]", "Info"), "fixable": (":orange[!]", "Vendor can fix"),
       "review": (":blue[?]", "Needs reviewer"), "hold": (":violet[⚑]", "High-risk hold"),
       "critical": (":red[✕]", "Policy breach")}
STATUS_COLOR = {"Approved": "green", "Pending": "orange", "Rejected": "red"}
ENTITY_TYPES = ["Private Limited Company", "Public Limited Company", "LLP / Partnership Firm", "Sole Proprietorship"]
CATEGORIES = ["Packaging Materials", "Logistics & Freight", "Industrial Supplies", "Food & Beverage Supplies",
              "Electrical Components", "IT Services"]
FIELDS = [("legal_name", "Registered legal name"), ("trade_name", "Trade name (if any)"),
          ("proprietor_name", "Proprietor name"), ("address", "Principal place of business"),
          ("state", "State"), ("incorporation_date", "Date of incorporation"), ("cin", "CIN"),
          ("contact_name", "Contact person"), ("contact_email", "Contact email"), ("contact_phone", "Contact phone"),
          ("pan", "PAN"), ("gstin", "GSTIN"), ("account_holder", "Bank account holder"),
          ("account_number", "Account number"), ("ifsc", "IFSC"), ("bank_name", "Bank name")]

if "page" not in st.session_state:
    st.session_state.page = "New application"


def visible_fields(entity_type):
    """Conditional form: proprietor name only for proprietorships, CIN/incorporation only for companies."""
    out = []
    for k, label in FIELDS:
        if k == "proprietor_name" and entity_type != "Sole Proprietorship":
            continue
        if k in ("cin", "incorporation_date") and entity_type not in COMPANY_TYPES:
            continue
        out.append((k, label))
    return out


def scenarios():
    p = os.path.join(TC_DIR, "index.json")
    return json.load(open(p)) if os.path.exists(p) else {}


def folder_docs(tc, version=1):
    f = os.path.join(TC_DIR, tc)
    return [{"name": n, "data": open(os.path.join(f, n), "rb").read(), "version": version}
            for n in sorted(os.listdir(f)) if n != "form.json"]


def finding_md(container, f):
    container.markdown(f"{SEV[f['severity']][0]} **{f['title']}** — {f['detail']}")
    extra = list(f["evidence"]) + ([f"Source: {f['source']}"] if f["source"] != "Rule" else [])
    if extra:
        container.markdown(f"<div class='vg-ev'>{' · '.join(extra)}</div>", unsafe_allow_html=True)


MARK = {"corroborates": "✓", "contradicts": "✕", "risk": "⚠", "no_conflict": "○", "flag": "!", "unverified": "~",
        "unreadable": "?", "absent": "—"}
ROLE = {"vendor_doc": "vendor-supplied", "derived": "derived", "third_party": "third party · live",
        "third_party_sim": "third party · simulated", "screening": "our records"}
VERDICT = {"verified": "Independently verified", "verified_sim": "Verified by a simulated third party",
           "consistent": "Vendor documents consistent - not independently verified", "contradicted": "Contradicted",
           "risk": "Risk signal", "attention": "Needs attention", "unverified": "Unverified"}


def evidence_matrix(rows):
    if not rows:
        return
    html = ["<table class='vg-mx'>"]
    for r in rows:
        chips = "".join(f"<span class='vg-chip c-{c['status']}'>{MARK[c['status']]} <b>{c['source']}</b>: {c['value']}"
                        f"<span class='vg-role'>{ROLE[c['role']]}</span></span>" for c in r["cells"])
        gap = f"<div class='vg-ev' style='margin:4px 0 0 0'>{r['gap']}</div>" if r.get("gap") and r["verdict"] == "consistent" else ""
        html.append(f"<tr><td class='vg-fact'>{r['fact']}<br><span class='v-{r['verdict']}' style='font-size:0.78rem;"
                    f"font-weight:600'>{VERDICT[r['verdict']]}</span></td>"
                    f"<td style='color:#5B6577;max-width:180px'>Vendor says<br><b style='color:#1B2A41'>{r['form'] or '—'}</b></td>"
                    f"<td>{chips}{gap}</td></tr>")
    html.append("</table>")
    st.markdown("".join(html), unsafe_allow_html=True)
    st.caption("✓ corroborates · ✕ contradicts · ⚠ risk signal · ○ no known conflict (a search that found nothing - never "
               "counted as support) · ! needs a look · ? unreadable · ~ not verifiable · — not provided. "
               "A fact is *independently verified* only when a third party confirms it. Vendor-supplied documents "
               "agreeing with each other is consistency, not verification - a fraudster controls all of them.")


def payment_checklist(d):
    if d.get("payment_gate"):
        with st.expander(f"Payment authorisation gate - {'all conditions met' if d['payment_ready'] else 'blocked'}"):
            for c in d["payment_gate"]:
                st.markdown(f"{':green[✓]' if c['met'] else ':red[✕]'} **{c['condition']}** — {c['detail']}")


def decision_card(d, comms=None):
    c = d["counts"]; attention = sum(c.get(s, 0) for s in ATTENTION)
    who = ""
    if d.get("decided_by") == "Reviewer":
        who = (f"<div><div class='vg-k'>System recommended</div>{d['system_status']} — {d['system_sub_status']}</div>"
               f"<div><div class='vg-k'>Final decision by</div>{d.get('reviewers') or 'Reviewer'}</div>")
    pol = d.get("policy") or {}
    pol_txt = f"{pol.get('name', 'Default')}" + (f" ({len(pol.get('changed', {}))} control(s) tuned)" if pol.get("changed") else "")
    card_html = f"""
    <div class='vg-card vg-{d['status']}'>
    <p class='vg-big'>{d['status']}</p>
    <p class='vg-sub'>{d['sub_status']}</p>

    <div class='vg-row'>
    <div><div class='vg-k'>Checks passed</div>{c.get('pass', 0)}</div>
    <div><div class='vg-k'>Need attention</div>{attention}</div>
    <div><div class='vg-k'>Risk</div>{d['risk']}</div>
    <div><div class='vg-k'>Next move owned by</div>{d.get('owner', '—')}</div>
    <div><div class='vg-k'>Policy</div>{pol_txt}</div>
    {who}
    </div>

    <p style='margin:12px 0 0 0'>
    <span class='vg-k'>Next action</span><br>
    {d['next_action']}
    </p>

    <p style='margin:8px 0 0 0'>
    <span class='vg-k'>Payment</span><br>
    {d['payment_status']}
    </p>
    </div>
    """

    st.markdown(card_html, unsafe_allow_html=True)
    if d.get("simulated_sources"):
        st.markdown(f"<div class='vg-sim'>Includes <b>simulated</b> verifications: {', '.join(d['simulated_sources'])}. "
                    "They stand in for paid KYB / bank-verification APIs, so no case here can be authorised for real payment.</div>",
                    unsafe_allow_html=True)
    payment_checklist(d)
    if comms:
        st.markdown(f"**Reviewer note** ({comms.get('engine', '').split(' (')[0]}): {comms['reviewer_summary']}")
    if d["reasons"]:
        st.markdown("**Why — top reasons**")
        for r in d["reasons"][:2]:
            st.markdown(f"{SEV.get(r['severity'], SEV['info'])[0]} **{r['title']}** — {r['detail']}")
        if len(d["reasons"]) > 2:
            with st.expander(f"{len(d['reasons']) - 2} more reason(s)"):
                for r in d["reasons"][2:]:
                    st.markdown(f"{SEV.get(r['severity'], SEV['info'])[0]} **{r['title']}** — {r['detail']}")


def vendor_message(comms, form):
    st.markdown(f"**To:** {form.get('contact_name')} <{form.get('contact_email')}>  \n"
                f"**Subject:** {comms['vendor_email']['subject']}")
    st.text(comms["vendor_email"]["body"])
    st.caption(f"Drafted by {comms['engine']}. Drafted only from vendor-safe information; a human sends it.")


def run_live(form, documents, prior=None):
    """Runs the pipeline and renders each stage as it executes."""
    result, boxes = None, {}
    for ev in run_pipeline(form, documents, prior):
        kind = ev[0]
        if kind == "stage_start":
            _, key, label = ev
            boxes[key] = st.status(label + " …", state="running", expanded=True)
            if key == "docs":
                boxes[key].markdown(f"Reading {len(documents)} document(s) in parallel…")
        elif kind == "document":
            d = ev[1]
            n_fields = sum(1 for v in d["fields"].values() if v and v.get("value"))
            low = [k for k, v in d["fields"].items() if v and v.get("value") and float(v.get("confidence") or 0) < 0.75]
            note = f" · :orange[low confidence: {', '.join(low)}]" if low else ""
            basis = " · :orange[type guessed from file name]" if d.get("type_basis") == "filename" else ""
            boxes["docs"].markdown(f"📄 `{d['file']}` → **{DOC_LABELS.get(d['doc_type'], d['doc_type'])}** · "
                                   f"{n_fields} fields read · {d['engine']}{note}{basis}")
        elif kind == "stage_end":
            _, key, label, findings, secs = ev
            box = boxes[key]
            if key == "decision" and result is None:
                pass
            for f in findings:
                finding_md(box, f)
            worst = "error" if any(f["severity"] in ("critical", "hold") for f in findings) else "complete"
            issues = sum(1 for f in findings if f["severity"] in ATTENTION)
            tag = f"{issues} issue(s)" if issues else "clear"
            box.update(label=f"{label} — {tag} · {secs:.1f}s", state=worst, expanded=bool(issues))
        elif kind == "done":
            result = ev[1]
    boxes["decision"].markdown(f"**{result['decision']['status']}** — {result['decision']['sub_status']}")
    with st.status("Drafting communications …", state="running") as s:
        comms = draft(form, result["decision"], result["findings"])
        s.update(label=f"Communication — {comms['engine'].split(' (')[0]}", state="complete", expanded=False)
    return result, comms


def case_report(case, vers):
    v = vers[-1]; d = v["result"]["decision"]; fm = v["form"]
    L = [f"# Vendor onboarding report — {case['vendor']}", "",
         f"Case {case['id']} · {case['entity_type']} · {case['category']} · generated {time.strftime('%Y-%m-%d %H:%M')}", "",
         "## Decision", f"- Final status: **{d['status']}** — {d['sub_status']} (decided by {d.get('decided_by', 'System')})",
         f"- System recommendation: {d.get('system_status')} — {d.get('system_sub_status')}",
         f"- Risk: {d['risk']}", f"- Payment status: {d['payment_status']}", f"- Next action: {d['next_action']}", ""]
    if d.get("simulated_sources"):
        L += [f"> Simulated verifications used: {', '.join(d['simulated_sources'])}. Not valid for real payments.", ""]
    pol = d.get("policy") or {}
    L += [f"- Policy in force: {pol.get('name', 'Default')} (confidence threshold {pol.get('low_confidence_threshold', 0.75):.0%}; "
          f"tuned controls: {pol.get('changed') or 'none'})", ""]
    if d.get("exception"):
        e = d["exception"]
        L += [f"- Policy exception: {e['state']}; requested by {e['requested_by']['name']}: {e['reason']}"
              + (f"; approved by {e['approved_by']['name']} (dual approval NOT authenticated)" if e.get("approved_by") else ""), ""]
    L += ["## Payment authorisation gate"] + [f"- {'MET' if c['met'] else 'NOT MET'}: {c['condition']} ({c['detail']})"
                                               for c in d.get("payment_gate", [])] + [""]
    L += ["## Evidence matrix", "| Fact | Vendor says | Verdict | Sources (status, role) |", "|---|---|---|---|"]
    for r in v["result"].get("evidence", []):
        L.append(f"| {r['fact']} | {r['form']} | {VERDICT[r['verdict']]} | "
                 + "; ".join(f"{MARK[c['status']]} {c['source']}: {c['value']} ({ROLE[c['role']]})" for c in r["cells"]) + " |")
    if d.get("review_log"):
        L += ["", "## Reviewer log (identities self-declared, not authenticated)"] + [
            f"- {x['at']} {x['role']} {x['name']}: {x['action']} - {x['note']}"
            + (f" [verification: {x['verification']['method']} - {x['verification']['evidence']}]" if x.get("verification") else "")
            for x in d["review_log"]]
    L += ["", "## Vendor details"] + [f"- {k}: {val}" for k, val in fm.items() if val] + ["", "## Checks"]
    for key, label in STAGES[:-1]:
        fs = [f for f in v["result"]["findings"] if f["stage"] == key]
        if fs:
            L.append(f"### {label}")
            for f in fs:
                L.append(f"- [{SEV[f['severity']][1]}] **{f['title']}** — {f['detail']}"
                         + (f" _(evidence: {'; '.join(f['evidence'])})_" if f["evidence"] else "")
                         + (f" _(source: {f['source']})_" if f["source"] != "Rule" else ""))
    L += ["", "## Documents"]
    for e in v["result"]["extractions"]:
        sup = f" — superseded by {e['superseded_by']}" if e.get("superseded_by") else ""
        L.append(f"- {e['file']} (v{e['version']}): {DOC_LABELS.get(e['doc_type'], e['doc_type'])}, read by {e['engine']}{sup}")
    L += ["", "## History"] + [f"- v{x['version']} {x['at']} — {x['trigger']} → {x['result']['decision']['status']} "
                               f"({x['result']['decision']['sub_status']})" for x in vers]
    return "\n".join(L)


# ============================================================ sidebar
with st.sidebar:
    st.markdown("### 🛡️ VendorGuard")
    st.caption("Vendor onboarding desk — submission to decision")
    pages = ["New application", "Pipeline dashboard", "Case file", "Client policy", "Test suite", "How it works"]
    if "goto" in st.session_state:
        st.session_state.page = st.session_state.pop("goto")
    st.radio("Go to", pages, key="page", label_visibility="collapsed")
    st.divider()
    if api_key():
        st.success(f"AI: Claude configured\n\n`{MODEL}`")
        if st.button("Test AI connection"):
            try:
                import anthropic
                t = time.time()
                anthropic.Anthropic(api_key=api_key(), timeout=20).messages.create(
                    model=MODEL, max_tokens=5, messages=[{"role": "user", "content": "ping"}])
                st.success(f"Connected · {time.time() - t:.1f}s")
            except Exception as e:
                st.error(f"Failed: {str(e)[:150]}")
    else:
        st.warning("AI: offline mode\n\nSet ANTHROPIC_API_KEY to enable Claude document reading and drafting.")
    _pol = load_policy()
    st.caption(f"Active client policy: **{_pol['name']}**")
    with st.expander("Admin"):
        if st.button("Reset all case data"):
            db.reset(); st.session_state.pop("open_case", None); st.rerun()

page = st.session_state.page

# ============================================================ new application
if page == "New application":
    st.title("New vendor application")
    sc = scenarios()
    opts = ["— blank form —"] + [f"{k} · {v['title']}" for k, v in sc.items()]
    pick = st.selectbox("Load a prepared scenario (every field stays editable)", opts)
    tc = pick.split(" · ")[0] if pick != opts[0] else None
    if st.session_state.get("loaded_tc") != tc:
        form0 = json.load(open(os.path.join(TC_DIR, tc, "form.json"))) if tc else {}
        for k, _ in FIELDS:
            st.session_state[f"f_{k}"] = form0.get(k, "")
        st.session_state.f_entity_type = form0.get("entity_type") or ENTITY_TYPES[0]
        st.session_state.f_category = form0.get("category") or CATEGORIES[0]
        st.session_state.loaded_tc = tc
    if tc:
        st.info(f"**Scenario:** {sc[tc]['story']}  \n**Expected outcome:** {sc[tc]['expected']}")

    c1, c2 = st.columns([3, 2])
    with c1:
        st.subheader("Registration form")
        a, b = st.columns(2)
        a.selectbox("Business type", ENTITY_TYPES, key="f_entity_type")
        b.selectbox("Supply category", CATEGORIES, key="f_category")
        shown = visible_fields(st.session_state.f_entity_type)
        cols = st.columns(2)
        for i, (k, label) in enumerate(shown):
            cols[i % 2].text_input(label, key=f"f_{k}")
    with c2:
        st.subheader("Documents")
        chosen = []
        if tc:
            for d in folder_docs(tc):
                if st.checkbox(d["name"], value=True, key=f"doc_{tc}_{d['name']}"):
                    chosen.append(d)
        ups = st.file_uploader("Add documents (PDF, JPG, PNG)", type=["pdf", "jpg", "jpeg", "png"], accept_multiple_files=True)
        for u in ups or []:
            chosen.append({"name": u.name, "data": u.getvalue(), "version": 1})
        st.caption(f"{len(chosen)} document(s) attached. Untick one to simulate it being missing.")

    if st.button("Run onboarding checks", type="primary", width="stretch"):
        form = {k: st.session_state.get(f"f_{k}", "") for k, _ in shown}
        form.update(entity_type=st.session_state.f_entity_type, category=st.session_state.f_category)
        case_id = db.new_case_id()
        st.divider()
        st.subheader(f"Live run · {case_id} · {form.get('legal_name') or 'Unnamed vendor'}")
        result, comms = run_live(form, chosen)
        db.save_version(case_id, 1, "Initial submission", form, chosen, result, comms, tc)
        st.subheader("Outcome")
        decision_card(result["decision"], comms)
        st.markdown("#### Evidence matrix")
        evidence_matrix(result.get("evidence"))
        with st.expander("Message to vendor", expanded=result["decision"]["status"] != "Approved"):
            vendor_message(comms, form)
        st.session_state.open_case = case_id
        st.success(f"Processed in {result['seconds']}s and saved as **{case_id}**. Open **Case file** in the sidebar for "
                   "documents, every check, history, resubmission, reviewer actions and the audit report.")

# ============================================================ dashboard
elif page == "Pipeline dashboard":
    st.title("Onboarding pipeline")
    cs = db.cases()
    if not cs:
        st.info("No applications yet. Run one from **New application** or the **Test suite**.")
    else:
        df = pd.DataFrame(cs)
        results = db.all_results()
        latest = {}
        for cid, ver, res, secs in results:
            if cid not in latest or ver > latest[cid][0]:
                latest[cid] = (ver, res, secs)
        df["decided_by"] = df.id.map(lambda i: latest[i][1]["decision"].get("decided_by", "System"))
        df["demo_bank"] = df.id.map(lambda i: latest[i][1]["decision"].get("demo_bank_checks_passed", False)
                                    and latest[i][1]["decision"]["status"] == "Approved")
        m = st.columns(8)
        m[0].metric("Applications", len(df))
        m[1].metric("Approved", int((df.status == "Approved").sum()))
        m[2].metric("Awaiting vendor", int(df.sub_status.str.contains("Awaiting vendor").sum()))
        m[3].metric("Awaiting reviewer", int(df.sub_status.str.contains("internal review").sum()))
        m[4].metric("High-risk hold", int(df.sub_status.str.contains("High-risk").sum()))
        m[5].metric("Rejected", int((df.status == "Rejected").sum()))
        m[6].metric("Payment-ready (live-verified)", int(df.payment_ready.sum()))
        m[7].metric("Approved, demo bank checks only", int(df.demo_bank.sum()),
                    help="Bank ownership passed a SIMULATED penny drop. Not authorised for real payments.")

        st.subheader("Applications")
        show = df[["id", "vendor", "status", "sub_status", "risk", "payment_ready", "decided_by", "versions", "updated"]].copy()
        show["payment_ready"] = show.payment_ready.map({1: "Yes", 0: "No"})
        show.columns = ["Case", "Vendor", "Status", "Stage", "Risk", "Payment-ready", "Decided by", "Versions", "Last update"]
        sel = st.dataframe(show, hide_index=True, width="stretch", on_select="rerun", selection_mode="single-row")
        if sel and sel.selection.rows:
            st.session_state.open_case = show.iloc[sel.selection.rows[0]]["Case"]
            st.session_state.goto = "Case file"; st.rerun()
        st.caption("Click a row to open its case file.")

        c1, c2 = st.columns(2)
        with c1:
            st.subheader("Most common issues")
            counts = {}
            for _, res, _ in latest.values():
                for f in res["findings"]:
                    if f["severity"] in ATTENTION:
                        counts[f["title"]] = counts.get(f["title"], 0) + 1
            if counts:
                st.bar_chart(pd.Series(counts).sort_values(ascending=True).tail(10), horizontal=True, color="#B7791F")
            else:
                st.caption("No issues raised yet.")
        with c2:
            st.subheader("Throughput")
            auto = [s for s in (r[3] for r in results) if s > 0]
            st.metric("Average processing time per submission", f"{sum(auto) / len(auto):.1f} s" if auto else "—")
            st.metric("Submissions processed (incl. resubmissions)", len(results))
            st.metric("Decided without a human", f"{int((df.decided_by == 'System').sum())} of {len(df)}")

# ============================================================ case file
elif page == "Case file":
    cs = db.cases()
    if not cs:
        st.info("No cases yet."); st.stop()
    ids_ = [c["id"] for c in cs]
    default = ids_.index(st.session_state.get("open_case")) if st.session_state.get("open_case") in ids_ else 0
    cid = st.selectbox("Case", ids_, index=default, format_func=lambda i: f"{i} · {next(c['vendor'] for c in cs if c['id'] == i)}")
    st.session_state.open_case = cid
    case = next(c for c in cs if c["id"] == cid)
    vers = db.versions(cid)
    h1, h2 = st.columns([4, 1])
    h1.title(case["vendor"])
    h2.download_button("Download audit report", case_report(case, vers), file_name=f"{cid}_report.md", width="stretch")
    st.badge(case["status"], color=STATUS_COLOR.get(case["status"], "gray"))
    st.caption(f"{case['entity_type']} · {case['category']} · opened {case['created']}")
    vnum = st.radio("Version", [v["version"] for v in vers], index=len(vers) - 1, horizontal=True,
                    format_func=lambda n: f"v{n} — {vers[n - 1]['trigger']}")
    v = vers[vnum - 1]; res = v["result"]
    t1, t2, t3, t4, t5 = st.tabs(["Decision", "All checks", "Documents", "Vendor message", "History"])
    with t1:
        decision_card(res["decision"], v["comms"])
        st.markdown("#### Evidence matrix")
        evidence_matrix(res.get("evidence"))
    with t2:
        for key, label in STAGES[:-1]:
            fs = [f for f in res["findings"] if f["stage"] == key]
            if fs:
                issues = sum(1 for f in fs if f["severity"] in ATTENTION)
                with st.expander(f"{label} — {issues} issue(s), {len(fs)} checks", expanded=bool(issues)):
                    for f in fs:
                        finding_md(st, f)
    with t3:
        files = {(f["name"], f["version"]): f for f in v["files"]}
        for e in res["extractions"]:
            sup = f" · superseded by {e['superseded_by']}" if e.get("superseded_by") else ""
            with st.expander(f"📄 {e['file']} (v{e['version']}) → {DOC_LABELS.get(e['doc_type'], e['doc_type'])} · {e['engine']}{sup}"):
                c1, c2 = st.columns(2)
                rows = [{"Field": k, "Value": (x or {}).get("value") or "— not found —",
                         "Confidence": f"{float((x or {}).get('confidence') or 0):.0%}"} for k, x in e["fields"].items()]
                c1.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
                c1.caption(f"Type confidence {float(e.get('type_confidence', 0)):.0%} (from {e.get('type_basis', 'content')})"
                           + (f" · Legibility: {e['quality_notes']}" if e.get("quality_notes") else ""))
                data = files.get((e["file"], e["version"]), {}).get("data")
                if data:
                    if e["file"].lower().endswith(".pdf"):
                        c2.image(pymupdf.open(stream=data, filetype="pdf")[0].get_pixmap(dpi=80).tobytes("png"), width="stretch")
                    else:
                        c2.image(data, width="stretch")
    with t4:
        vendor_message(v["comms"], v["form"])
    with t5:
        hist = [{"Version": x["version"], "When": x["at"], "Trigger": x["trigger"],
                 "Decided by": x["result"]["decision"].get("decided_by", "System"),
                 "Status": x["result"]["decision"]["status"], "Stage": x["result"]["decision"]["sub_status"],
                 "Open issues": sum(1 for f in x["result"]["findings"] if f["severity"] in ATTENTION)} for x in vers]
        st.dataframe(pd.DataFrame(hist), hide_index=True, width="stretch")
        if len(vers) > 1:
            def issues(x):
                return {f["title"] for f in x["result"]["findings"] if f["severity"] in ATTENTION}
            a, b = issues(vers[-2]), issues(vers[-1])
            st.markdown("**Change in latest version**")
            for t in sorted(a - b): st.markdown(f":green[✓ Resolved] — {t}")
            for t in sorted(b - a): st.markdown(f":red[✕ New] — {t}")
            if a == b: st.caption("No change in open issues.")

    latest = vers[-1]
    st.divider()
    left, right = st.columns(2)
    with left:
        st.subheader("Vendor resubmission")
        st.caption("Adds documents to the same application and re-runs every check. A new document of the same type "
                   "supersedes the old one; unchanged documents are not re-read.")
        prepared = scenarios().get(case.get("test_case") or "", {}).get("resubmission")
        ups = st.file_uploader("Upload corrected or missing documents", type=["pdf", "jpg", "jpeg", "png"],
                               accept_multiple_files=True, key=f"re_{cid}")
        use_prepared = bool(prepared) and st.checkbox(f"Use prepared resubmission ({prepared})", value=True)
        if st.button("Re-run with new documents", type="primary", disabled=not (ups or use_prepared)):
            nv = latest["version"] + 1
            new = [{"name": u.name, "data": u.getvalue(), "version": nv} for u in ups or []]
            if use_prepared:
                new += folder_docs(prepared, nv)
            docs = [{"name": f["name"], "data": f["data"], "version": f["version"]} for f in latest["files"]] + new
            prior = {e["hash"]: e for e in latest["result"]["extractions"]}
            st.subheader(f"Live run · {cid} v{nv}")
            result, comms = run_live(latest["form"], docs, prior)
            db.save_version(cid, nv, f"Vendor resubmission ({len(new)} doc(s))", latest["form"], docs, result, comms,
                            case.get("test_case"))
            decision_card(result["decision"], comms)
            evidence_matrix(result.get("evidence"))
            st.success(f"Saved as v{nv}. Re-select the case above to browse the new version.")
    with right:
        st.subheader("Reviewer action")
        lr = latest["result"]; ld = lr["decision"]; t = tier(lr)
        st.caption({"routine": "Routine case: any reviewer may decide.",
                    "hold": "High-risk hold: only Finance Controls may clear it, with a recorded independent verification.",
                    "breach": "Confirmed policy breach: no direct approval. An exception needs a second, different Finance Controls approver.",
                    "exception_pending": "Exception awaiting a second Finance Controls approver."}[t])
        if t == "exception_pending":
            e = ld["exception"]
            st.warning(f"**Exception requested** by {e['requested_by']['name']} ({e['requested_by']['role']}): {e['reason']}")
        c1, c2 = st.columns(2)
        role = c1.selectbox("Acting as (simulated role)", ROLES, key=f"role_{cid}")
        who = c2.text_input("Your name", key=f"who_{cid}")
        st.caption(":orange[Roles and names are self-declared in this prototype - no login. Production binds them to SSO.]")
        note = st.text_area("Reviewer note (required)", key=f"note_{cid}")
        acts = allowed_actions(lr, role)
        verification = None
        if "clear_hold" in acts:
            with st.container(border=True):
                st.markdown("**Independent verification record** (required to clear a hold)")
                verification = {"method": st.selectbox("Method", VERIFY_METHODS, key=f"vm_{cid}"),
                                "evidence": st.text_input("What was confirmed, with whom, how", key=f"ve_{cid}")}
        if not acts:
            st.info(f"No actions available to a {role} at this stage.")
        cols = st.columns(2)
        for i, act in enumerate(acts):
            if cols[i % 2].button(LABELS[act], key=f"act_{act}_{cid}", width="stretch",
                                  type="primary" if act in ("approve", "clear_hold", "approve_exception") else "secondary"):
                try:
                    r2 = apply_review(lr, act, role, who, note, verification)
                except GovernanceError as e:
                    st.error(str(e))
                else:
                    view = vendor_view(r2["decision"])
                    comms = {"reviewer_summary": f"{LABELS[act]} by {who.strip()} ({role}): {note.strip()}",
                             "vendor_email": _vendor_template(latest["form"], view, [note.strip()] if view == "action_needed" else []),
                             "engine": "Template (reviewer action)"}
                    db.save_version(cid, latest["version"] + 1, f"{role} {who.strip()}: {LABELS[act].lower()}", latest["form"],
                                    [{"name": f["name"], "data": f["data"], "version": f["version"]} for f in latest["files"]],
                                    r2, comms, case.get("test_case"))
                    st.rerun()

# ============================================================ client policy
elif page == "Client policy":
    st.title("Client policy")
    st.markdown("Every client has a different risk appetite. Here the analyst tunes **how severe** each control is for this "
                "client, then **replays every past case** under the draft policy to see exactly what would change, before "
                "switching it on. Controls that prove misconduct (a bank contradicting the bank proof, a GSTIN belonging to "
                "another PAN, a cancelled registration) are not tunable.")
    cur = load_policy()
    draft_p = {"name": st.text_input("Policy name", value=cur["name"]), "severity": {},
               "low_confidence_threshold": st.slider("Minimum AI read confidence to accept a value", 0.5, 0.95,
                                                     float(cur["low_confidence_threshold"]), 0.05)}
    labels = {"info": "Note only", "review": "Reviewer decides", "fixable": "Ask the vendor", "hold": "High-risk hold",
              "critical": "Reject (policy breach)"}
    st.markdown("**Control severities**")
    for k, (desc, allowed, default) in TUNABLE.items():
        c1, c2 = st.columns([3, 2])
        c1.markdown(f"{desc}" + ("" if cur["severity"][k] == default else " · :blue[tuned]"))
        draft_p["severity"][k] = c2.selectbox(desc, allowed, index=allowed.index(cur["severity"][k]),
                                              format_func=lambda x: labels[x], key=f"pol_{k}", label_visibility="collapsed")
    b1, b2, b3 = st.columns(3)
    if b1.button("Preview impact on past cases", type="primary", width="stretch"):
        impacts = []
        with st.spinner("Replaying every case under both policies (stored document readings reused - no AI calls)…"):
            for c in db.cases():
                vs = db.versions(c["id"])
                base = next((x for x in reversed(vs) if x["result"]["decision"].get("decided_by", "System") == "System"), vs[-1])
                docs = [{"name": f["name"], "data": f["data"], "version": f["version"]} for f in base["files"]]
                prior = {e["hash"]: e for e in base["result"]["extractions"]}
                x = policy_diff([e for e in run_pipeline(base["form"], docs, prior, cur)][-1][1],
                                [e for e in run_pipeline(base["form"], docs, prior, draft_p)][-1][1])
                impacts.append((c, x))
        if not impacts:
            st.info("No cases to replay yet - run the Test suite first.")
        else:
            m = st.columns(4)
            m[0].metric("Cases replayed", len(impacts))
            m[1].metric("Outcome changes", sum(x["outcome_changed"] for _, x in impacts))
            m[2].metric("Risk changes", sum(x["risk_changed"] for _, x in impacts))
            m[3].metric("Cases with a control re-graded", sum(bool(x["findings"]) for _, x in impacts))
            st.caption("A replay re-runs every rule with the stored document readings, so it isolates the effect of the policy alone.")
            for c, x in impacts:
                if not (x["findings"] or x["outcome_changed"] or x["risk_changed"]):
                    continue
                tag = "outcome changes" if x["outcome_changed"] else "control re-graded, same outcome"
                with st.expander(f"{c['id']} · {c['vendor']} — {tag}", expanded=x["outcome_changed"]):
                    st.markdown(f"**Outcome:** {x['outcome_before']} → **{x['outcome_after']}**  \n"
                                f"**Risk:** {x['risk_before']} → {x['risk_after']} · "
                                f"**Payment-ready:** {'yes' if x['payment_before'] else 'no'} → {'yes' if x['payment_after'] else 'no'}")
                    st.dataframe(pd.DataFrame([{"Control": f["control"], "Current policy": f["before"],
                                                "Draft policy": f["after"]} for f in x["findings"]]),
                                 hide_index=True, width="stretch")
            if not any(x["findings"] or x["outcome_changed"] or x["risk_changed"] for _, x in impacts):
                st.success("This draft changes nothing for any past case.")
    if b2.button("Save as active policy", width="stretch"):
        save_policy(draft_p); st.success(f"Policy '{draft_p['name']}' is now active for new runs."); st.rerun()
    if b3.button("Restore default", width="stretch"):
        save_policy(json.loads(json.dumps(DEFAULT_POLICY))); st.rerun()

# ============================================================ test suite
elif page == "Test suite":
    st.title("Test suite")
    tab1, tab2 = st.tabs(["Demo scenarios (end to end)", "Adversarial engine tests"])
    with tab1:
        st.markdown("Runs every prepared scenario through the full live pipeline, including document reading, and compares "
                    "the outcome with what a procurement expert would expect.")
        sc = scenarios()
        st.dataframe(pd.DataFrame([{"Case": k, "Scenario": v["title"], "Expected": v["expected"], "What it tests": v["story"]}
                                   for k, v in sc.items()] + [{"Case": "TC4 → v2", "Scenario": "Resubmission of TC4",
                                   "Expected": "Approved", "What it tests": "Missing + expired docs supplied; full re-check."}]),
                     hide_index=True, width="stretch")
        save = st.checkbox("Save runs to the dashboard", value=True)
        if st.button("Run all scenarios", type="primary"):
            rows = []; prog = st.progress(0.0)
            for i, (tc, meta) in enumerate(sc.items()):
                form = json.load(open(os.path.join(TC_DIR, tc, "form.json")))
                docs = folder_docs(tc); t = time.time()
                with st.spinner(f"{tc} · {meta['title']}"):
                    result = [e for e in run_pipeline(form, docs)][-1][1]
                    comms = draft(form, result["decision"], result["findings"])
                got = result["decision"]["status"]
                engines = sorted({e["engine"] for e in result["extractions"]})
                rows.append({"Case": tc, "Expected": meta["expected"], "Actual": got, "Detail": result["decision"]["sub_status"],
                             "Result": "✅ pass" if got == meta["expected"] else "❌ fail",
                             "Read by": ", ".join(engines), "Seconds": round(time.time() - t, 1)})
                cid = None
                if save:
                    cid = db.new_case_id(); db.save_version(cid, 1, "Initial submission", form, docs, result, comms, tc)
                if meta.get("resubmission"):
                    docs2 = docs + folder_docs(meta["resubmission"], 2)
                    prior = {e["hash"]: e for e in result["extractions"]}
                    r2 = [e for e in run_pipeline(form, docs2, prior)][-1][1]
                    c2 = draft(form, r2["decision"], r2["findings"])
                    got2 = r2["decision"]["status"]
                    rows.append({"Case": f"{tc} → v2", "Expected": "Approved", "Actual": got2, "Detail": r2["decision"]["sub_status"],
                                 "Result": "✅ pass" if got2 == "Approved" else "❌ fail",
                                 "Read by": ", ".join(sorted({e["engine"] for e in r2["extractions"]})), "Seconds": r2["seconds"]})
                    if save:
                        db.save_version(cid, 2, "Vendor resubmission (2 doc(s))", form, docs2, r2, c2, tc)
                prog.progress((i + 1) / len(sc))
            passed = sum(r["Result"].startswith("✅") for r in rows)
            st.metric("Scenarios behaving as expected", f"{passed} / {len(rows)}")
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    with tab2:
        st.markdown("Targeted tests of the decision engine and its governance. A fake extractor feeds deliberately broken or contradictory "
                    "document data, so each test isolates one failure mode: missing fields, unreadable values, "
                    "overlapping fraud signals, registry gaps and message leaks. Runs in about a second, no API calls.")
        if st.button("Run adversarial tests", type="primary"):
            from engine.edge_tests import run_all
            rows = run_all()
            st.metric("Tests passing", f"{sum(r['Result'].startswith('✅') for r in rows)} / {len(rows)}")
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")

# ============================================================ how it works
else:
    st.title("How it works")
    st.markdown("""
VendorGuard is a **configurable vendor-risk decision system with AI-assisted evidence collection**. Trust is earned, not
assumed. Every decision answers three questions:
1. What do we know?
2. What supports it, and how independent is that support?
3. What are we still *not* entitled to conclude?

### 1. Evidence-led: every source is classified by who controls it
The evidence matrix lays each key fact against every source and labels its role:

| Role | Examples | What it can prove |
|---|---|---|
| Vendor-supplied | PAN card, GST certificate, cancelled cheque | Consistency only. The vendor controls all of them, so a forger can make them agree |
| Derived | PAN inside the GSTIN | A consistency check, never a second witness |
| Third party | Bank, GST registry, IFSC directory | Independent verification; labelled live or simulated |
| Our records | Vendor master, employee master | Can reveal a conflict. Finding nothing means no known conflict, never confirmation |

A fact counts as **independently verified** only when a third party confirms it. Where no third party exists (for
example, PAN in this build), the matrix says so instead of implying verification. TC3 shows why this matters: every
vendor document agrees, and only the bank disagrees.

### 2. Governed: the client sets risk appetite, inside fixed limits
- **Rules decide.** Claude reads documents and drafts messages; deterministic rules decide.
- **Severities are client policy.** They are tuned per client, and the policy simulator replays every past case to
  show, finding by finding, what a draft policy would change before it goes live.
- **Proof of misconduct is never tunable.**
- **Reviewer powers depend on the risk tier:**
  - routine cases: any reviewer;
  - high-risk holds: Finance Controls only, with a recorded independent verification;
  - confirmed breaches: a written exception approved by a second, different Finance Controls reviewer.
  Roles are simulated and labelled as such.
- **One central gate decides payment.** It requires live account-ownership verification, an allowed account holder,
  a confirmed account number, a live-validated IFSC branch, no open bank findings, and approved onboarding. A
  reviewer can never switch payment on.

### 3. Proven: correctness is tested, not claimed
- **Adversarial tests** feed contradictory data on purpose and check the governance limits themselves.
- **A positive control** proves the payment gate *does* open when every condition is genuinely met.
- **End-to-end scenarios** run through the real document reader.

### Design rules
1. **No evidence is not a pass.** Missing, unclear (below the confidence threshold) and unparseable values always produce a finding.
2. **Never judge what you cannot read.** Unclear values trigger a request for a clearer copy, never a mismatch.
3. **Suspicion is not proof.** Risk signals put a case on hold; only confirmed breaches reject.
4. **The vendor never learns which control fired.** Vendor emails are drafted only from vendor-safe data, behind a leak guard.

### Severity → outcome
| Severity | Example | Outcome |
|---|---|---|
| Vendor can fix | Missing/expired/unclear document, typo, malformed email | Pending · awaiting vendor |
| Needs reviewer | Possible duplicate vendor, PAN type contradicts business type, GSTIN not in registry | Pending · awaiting internal review |
| High-risk hold | Payout account shared with another legal entity; vendor linked to an employee | Pending · high-risk hold |
| Policy breach | Bank contradicts the bank proof; GSTIN belongs to another PAN; cancelled GSTIN | Rejected · escalated |

### Real vs simulated
**Real:**
- PAN and GSTIN structure and checksum rules, including all 40 GST state/UT codes.
- IFSC lookup via Razorpay's public directory. If it is unreachable, only the bank prefix is recognised, and that is
  never shown as verified.

**Simulated, labelled everywhere:**
- GST registry status.
- Penny-drop bank verification.

Because these two are simulated, **no case in this demo is ever marked payment-ready**. In production they would be
paid KYB APIs (e.g. Cashfree, HyperVerge, Decentro) behind the same interface.

### Known limits
- **AI confidence is not a calibrated probability.** Claude's per-field confidence is its own estimate of legibility.
  The 75% threshold is a conservative starting point; calibrating it needs a field-accuracy benchmark against
  ground truth, which is planned next.
- No OCR outside Claude.
- Single-instance SQLite storage.
- Reviewer roles and identities are self-declared, so dual approval is recorded but never treated as authenticated.
""")
