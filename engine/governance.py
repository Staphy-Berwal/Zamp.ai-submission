"""Reviewer governance. Who may do what, depending on the case's risk tier.

  Routine cases (fixable / review)   any reviewer may approve, request info, escalate or reject
  High-risk hold                     only Finance Controls may clear it, and only with a recorded independent
                                     verification; a procurement reviewer may escalate or reject, never approve
  Confirmed policy breach            nobody approves directly: one reviewer requests an exception, a second,
                                     different Finance Controls reviewer approves or declines it

Identities and roles in this build are SELF-DECLARED (no login). Every record says so, and dual approval is never
marked authenticated. Production would bind role and identity to SSO.
"""
import copy, datetime as dt, re

from .pipeline import Run, TUNABLE, DEFAULT_POLICY, payment_gate

ROLES = ["Procurement reviewer", "Finance Controls"]
VERIFY_METHODS = ["Call-back to the contact on file (not the number on the application)",
                  "Written confirmation from the existing vendor on file",
                  "Bank-issued account confirmation obtained directly from the bank",
                  "Group-company / parent relationship documents verified"]

LABELS = {"approve": "Approve", "request_info": "Request info from vendor", "escalate": "Escalate to Finance Controls",
          "reject": "Reject", "clear_hold": "Clear high-risk hold", "request_exception": "Request policy exception",
          "approve_exception": "Approve exception (second approver)", "decline_exception": "Decline exception"}


class GovernanceError(Exception):
    pass


def ident(name):
    return re.sub(r"\s+", " ", (name or "")).strip().casefold()


def tier(result):
    d = result["decision"]
    if (d.get("exception") or {}).get("state") == "requested":
        return "exception_pending"
    sev = {f["severity"] for f in result["findings"]}
    if "critical" in sev:
        return "breach"
    if "hold" in sev:
        return "hold"
    return "routine"


def allowed_actions(result, role):
    t = tier(result)
    if t == "exception_pending":
        return ["approve_exception", "decline_exception"] if role == "Finance Controls" else []
    if t == "breach":
        return ["request_exception", "reject"]
    if t == "hold":
        return ["clear_hold", "request_info", "reject"] if role == "Finance Controls" else ["escalate", "reject"]
    return ["approve", "request_info", "escalate", "reject"]


def _redecide(result, findings):
    """Re-run the decision rules over a modified finding list (same policy snapshot)."""
    pol = result["decision"].get("policy") or {}
    sev = dict(DEFAULT_POLICY["severity"]); sev.update(pol.get("changed") or {})
    r = Run({}, [], policy={"name": pol.get("name", "Default"), "severity": sev,
                            "low_confidence_threshold": pol.get("low_confidence_threshold", 0.75)})
    r.findings, r.lookups = findings, result.get("lookups", {})
    return r.decide()


def apply_review(result, action, role, name, note, verification=None):
    """Returns a NEW result with the reviewer's decision applied, or raises GovernanceError."""
    if not ident(name) or not (note or "").strip():
        raise GovernanceError("A reviewer name and a note are required.")
    if role not in ROLES:
        raise GovernanceError(f"Unknown role {role!r}.")
    if action not in allowed_actions(result, role):
        raise GovernanceError(f"'{LABELS.get(action, action)}' is not permitted for a {role} on a "
                              f"{tier(result).replace('_', ' ')} case.")
    r = copy.deepcopy(result)
    d0 = r["decision"]
    who = {"name": name.strip(), "role": role, "authenticated": False,
           "at": dt.datetime.now().isoformat(timespec="seconds")}
    findings = r["findings"]

    if action == "clear_hold":
        if not verification or verification.get("method") not in VERIFY_METHODS or not verification.get("evidence", "").strip():
            raise GovernanceError("Clearing a high-risk hold requires a recorded independent verification "
                                  "(method and evidence).")
        for f in findings:
            if f["severity"] == "hold":
                f["severity"] = "info"
                f["title"] = f"{f['title']} - cleared by Finance Controls"
                f["evidence"] = f["evidence"] + [f"Independent verification: {verification['method']} - "
                                                 f"{verification['evidence']} ({who['name']}, self-declared)"]
        d = _redecide(r, findings)
        d["sub_status"] = d["sub_status"] + " (hold cleared by Finance Controls)"
    elif action == "approve":
        d = dict(d0, status="Approved", sub_status="Approved by reviewer", owner="Master-data team",
                 next_action="Create the vendor record in the ERP.")
    elif action == "request_info":
        d = dict(d0, status="Pending", sub_status="Awaiting vendor - reviewer request", owner="Vendor",
                 next_action="Waiting for the vendor to respond to the reviewer's request.")
    elif action == "escalate":
        d = dict(d0, status="Pending", sub_status="High-risk hold - escalated by reviewer", owner="Finance Controls",
                 next_action="Finance Controls verifies independently before anything else proceeds.")
    elif action == "reject":
        d = dict(d0, status="Rejected", sub_status="Rejected by reviewer", owner="Finance Controls",
                 next_action="Vendor receives a neutral notice.")
    elif action == "request_exception":
        d = dict(d0, sub_status=f"Policy exception requested by {who['name']} - awaiting second approver",
                 owner="Second approver (Finance Controls)",
                 next_action="A different Finance Controls reviewer approves or declines the exception.",
                 exception={"state": "requested", "requested_by": who, "reason": note.strip()})
    elif action in ("approve_exception", "decline_exception"):
        exc = dict(d0["exception"])
        if ident(exc["requested_by"]["name"]) == ident(name):
            raise GovernanceError("The second approver must be a different person from the requester.")
        if action == "approve_exception":
            exc.update(state="approved", approved_by=who, dual_approval_authenticated=False)
            d = dict(d0, status="Approved", owner="Master-data team", exception=exc,
                     sub_status=f"Approved by policy exception ({exc['requested_by']['name']} + {who['name']})",
                     next_action="Create the vendor record; the exception is logged for audit.")
        else:
            exc.update(state="declined", declined_by=who)
            d = dict(d0, exception=exc, sub_status=f"Policy exception declined by {who['name']}",
                     owner="Finance Controls", next_action="Vendor receives a neutral notice.")

    # payment is ALWAYS recomputed by the one central gate - a reviewer can never switch it on directly
    gate = payment_gate(findings, d["status"], r.get("lookups", {}))
    d.update(payment_ready=gate["ready"], payment_status=gate["status_text"], payment_gate=gate["conditions"],
             demo_bank_checks_passed=gate["demo_checks_passed"], decided_by="Reviewer",
             system_status=d0.get("system_status", d0["status"]), system_sub_status=d0.get("system_sub_status", d0["sub_status"]))
    d["reviewers"] = " + ".join(x["name"] for x in filter(None, [
        (d.get("exception") or {}).get("requested_by") if action in ("approve_exception", "decline_exception") else None, who]))
    d["review_log"] = d0.get("review_log", []) + [dict(who, action=LABELS[action], note=note.strip(),
                                                        verification=verification if action == "clear_hold" else None)]
    d["reasons"] = [{"title": f"{role} {who['name']}: {LABELS[action].lower()}", "severity": "info",
                     "detail": note.strip()}] + d.get("reasons", [])
    r["decision"] = d
    r["seconds"] = 0
    return r


VENDOR_VIEW = {"approve": "approved", "request_info": "action_needed", "escalate": "under_review", "reject": "not_proceeding",
               "request_exception": "under_review", "decline_exception": "not_proceeding", "approve_exception": "approved"}
