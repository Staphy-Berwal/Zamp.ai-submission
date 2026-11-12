"""Stage 8 - Communication. Claude writes; it never decides.

Two separate drafts with separate inputs:
- the reviewer note sees every finding;
- the vendor email sees ONLY vendor-safe data (status category + the specific asks).
So the vendor email cannot leak internal fraud signals - it never receives them.
A keyword guard then double-checks hold/rejection emails and falls back to a template."""
import json, re
from concurrent.futures import ThreadPoolExecutor

from .extract import MODEL, api_key

SENSITIVE = ["another vendor", "existing vendor", "v-10", "employee", "penny", "bank reports", "bank's records",
             "shared", "diversion", "fraud", "contradict", "finance controls", "registry", "altered", "forged"]

VENDOR_PROMPT = """Write an email from a company's Vendor Onboarding Team to a vendor about their registration.
Vendor company: {vendor}. Contact person: {contact}.
Situation: {situation}
Items the vendor must provide or correct (may be empty): {asks}

Rules: polite, plain language, concise. If there are items, list them numbered and say they can reply to
this email to continue the same application, and that anything already accepted need not be resent.
Do not invent reasons, checks or details beyond what is given. No placeholders in square brackets.
Sign off as "Vendor Onboarding Team".
Respond with ONLY JSON: {{"subject": "...", "body": "..."}}"""

REVIEWER_PROMPT = """You assist a procurement reviewer. A rules engine has already decided this vendor case;
do not change or question the decision.
Vendor: {vendor}. Decision: {status} - {sub_status}. Next action: {next_action}
Findings needing attention: {findings}
Write 2-4 plain sentences: what was found, why it led to this decision, and what the reviewer should look at first.
Respond with ONLY JSON: {{"reviewer_summary": "..."}}"""

SITUATION = {
    "approved": "Their onboarding is complete and approved. Welcome them briefly.",
    "action_needed": "Their application is on hold until they provide the items below.",
    "under_review": "Their application is under internal review. Say a team member will contact them if anything "
                    "else is needed. Give no reason.",
    "not_proceeding": "The company is unable to proceed with their registration at this time. Say the procurement "
                      "team will contact them. Give no reason at all.",
}


def vendor_view(decision):
    s, sub = decision["status"], decision["sub_status"]
    if s == "Approved":
        return "approved"
    if s == "Rejected":
        return "not_proceeding"
    if sub.startswith("Awaiting vendor"):
        return "action_needed"
    return "under_review"   # internal review / high-risk hold


def safe_asks(decision, findings):
    # Only when the vendor is the one who must act. On a hold or internal review the vendor hears nothing
    # specific: Finance Controls decides what to ask, so a fraudster cannot probe the controls.
    if vendor_view(decision) != "action_needed":
        return []
    seen, out = set(), []
    for f in findings:
        a = f.get("vendor_ask")
        if a and f["severity"] == "fixable" and a not in seen:
            seen.add(a); out.append(a)
    return out


def _vendor_template(form, view, asks):
    name, contact = form.get("legal_name", "Vendor"), form.get("contact_name") or "Sir/Madam"
    sign = "\n\nRegards,\nVendor Onboarding Team"
    if view == "approved":
        return {"subject": f"Onboarding complete - {name}",
                "body": f"Dear {contact},\n\nThank you for completing your registration. {name} is now an approved vendor.{sign}"}
    if view == "not_proceeding":
        return {"subject": f"Update on your vendor registration - {name}",
                "body": f"Dear {contact},\n\nThank you for your interest in working with us. We are unable to proceed with "
                        f"your registration at this time. A member of our procurement team will contact you.{sign}"}
    if asks:
        items = "\n".join(f"{i}. {a}" for i, a in enumerate(asks, 1))
        return {"subject": f"Action needed to complete your onboarding - {name}",
                "body": f"Dear {contact},\n\nThank you for your submission. To complete onboarding we need the following:\n\n"
                        f"{items}\n\nPlease reply to this email with the documents. Your application stays open and nothing "
                        f"already accepted needs to be resent.{sign}"}
    return {"subject": f"Your vendor registration is under review - {name}",
            "body": f"Dear {contact},\n\nThank you for your submission. Your registration is under review and a member of "
                    f"our team will contact you if anything else is needed.{sign}"}


def _call(prompt):
    import anthropic
    client = anthropic.Anthropic(api_key=api_key(), timeout=45)
    resp = client.messages.create(model=MODEL, max_tokens=900, messages=[{"role": "user", "content": prompt}])
    text = re.sub(r"```(json)?", "", "".join(b.text for b in resp.content if getattr(b, "type", "") == "text"))
    return json.loads(text[text.find("{"): text.rfind("}") + 1])


def leaks(email):
    blob = (email.get("subject", "") + " " + email.get("body", "")).lower()
    return [w for w in SENSITIVE if w in blob]


def draft(form, decision, findings):
    view = vendor_view(decision)
    asks = safe_asks(decision, findings)
    attention = [{"title": f["title"], "severity": f["severity"], "detail": f["detail"]}
                 for f in findings if f["severity"] in ("fixable", "review", "hold", "critical")]
    top = "; ".join(r["title"] for r in decision["reasons"][:3]) or "all checks passed"
    fallback_note = f"{decision['status']} ({decision['sub_status']}). Key findings: {top}. Next: {decision['next_action']}"
    if not api_key():
        return {"reviewer_summary": fallback_note, "vendor_email": _vendor_template(form, view, asks),
                "engine": "Template", "vendor_view": view}
    vp = VENDOR_PROMPT.format(vendor=form.get("legal_name"), contact=form.get("contact_name") or "the vendor",
                              situation=SITUATION[view], asks=json.dumps(asks))
    rp = REVIEWER_PROMPT.format(vendor=form.get("legal_name"), status=decision["status"], sub_status=decision["sub_status"],
                                next_action=decision["next_action"], findings=json.dumps(attention))
    engine = "Claude"
    with ThreadPoolExecutor(max_workers=2) as ex:
        fv, fr = ex.submit(_call, vp), ex.submit(_call, rp)
        try:
            email = fv.result()
            assert email.get("subject") and email.get("body")
            if view in ("under_review", "not_proceeding") and leaks(email):
                email = _vendor_template(form, view, asks); engine = "Claude (vendor email replaced by template: leak guard)"
        except Exception as e:
            email = _vendor_template(form, view, asks); engine = f"Template (API error: {str(e)[:60]})"
        try:
            note = fr.result()["reviewer_summary"]
        except Exception:
            note = fallback_note
    return {"reviewer_summary": note, "vendor_email": email, "engine": engine, "vendor_view": view}
