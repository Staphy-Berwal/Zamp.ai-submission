"""Policy impact simulator. Replays a case under two policies using the stored document readings
(no AI calls) and reports exactly what changed: each control's severity, the outcome, risk and payment."""
from .pipeline import TUNABLE, run_pipeline

SEV_LABEL = {"pass": "Passed", "info": "Note only", "fixable": "Ask the vendor", "review": "Reviewer decides",
             "hold": "High-risk hold", "critical": "Policy breach"}


def replay(form, documents, prior, policy):
    return [e for e in run_pipeline(form, documents, prior, policy)][-1][1]


def diff(before, after):
    key = lambda f: (f["check"], f["title"].split(" - ")[0])
    b = {key(f): f for f in before["findings"]}
    a = {key(f): f for f in after["findings"]}
    changes = []
    for k in sorted(set(b) | set(a)):
        sb, sa = (b.get(k) or {}).get("severity"), (a.get(k) or {}).get("severity")
        if sb != sa:
            f = a.get(k) or b.get(k)
            changes.append({"control": TUNABLE.get(f["check"], (f["title"],))[0], "check": f["check"],
                            "before": SEV_LABEL.get(sb, "—"), "after": SEV_LABEL.get(sa, "—")})
    db_, da = before["decision"], after["decision"]
    return {"outcome_before": f"{db_['status']} - {db_['sub_status']}", "outcome_after": f"{da['status']} - {da['sub_status']}",
            "outcome_changed": (db_["status"], db_["sub_status"]) != (da["status"], da["sub_status"]),
            "risk_before": db_["risk"], "risk_after": da["risk"], "risk_changed": db_["risk"] != da["risk"],
            "payment_before": db_["payment_ready"], "payment_after": da["payment_ready"],
            "payment_changed": db_["payment_ready"] != da["payment_ready"], "findings": changes}
