"""The onboarding pipeline. Every check produces a Finding with a severity, evidence and
its source. The decision is a pure function of the findings - no model decides approval.

Severity ladder (lowest to highest):
  pass      check succeeded
  info      worth knowing, no action (weak signals live here)
  fixable   the vendor can fix it                      -> Pending: awaiting vendor
  review    a reviewer must make a judgement call      -> Pending: awaiting internal review
  hold      high-risk signal, not proven misconduct    -> Pending: high-risk hold (Finance Controls)
  critical  confirmed policy breach                    -> Rejected

Core principle: the absence of a detected problem is NOT a pass. A check passes only when
its inputs were actually present and readable.
"""
import datetime as dt
import hashlib, json, os, re, time
from concurrent.futures import ThreadPoolExecutor, as_completed

from rapidfuzz import fuzz

from . import ids
from . import extract as extract_mod
from .extract import DOC_LABELS

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
LOW_CONF = 0.75
FREE_MAIL = {"gmail.com", "yahoo.com", "yahoo.co.in", "hotmail.com", "outlook.com", "rediffmail.com"}
ATTENTION = ("fixable", "review", "hold", "critical")

STAGES = [
    ("intake", "Intake & form validation"),
    ("docs", "Document intelligence"),
    ("ids", "Identifier validation"),
    ("cross", "Cross-document consistency"),
    ("verify", "Registry & bank verification"),
    ("dupes", "Duplicate & fraud screen"),
    ("decision", "Decision"),
]

COMPANY_TYPES = {"Private Limited Company", "Public Limited Company"}

# ---------------------------------------------------------------- client policy
# Controls whose severity a client may tune, with the allowed range. Everything else is fixed:
# e.g. a forged bank proof or a cancelled GSTIN is always a policy breach.
TUNABLE = {
    "shared_bank": ("Payout account shared with another legal entity", ["hold", "critical"], "hold"),
    "employee_link": ("Vendor bank account / phone matches an employee", ["hold", "critical"], "hold"),
    "dup_identity": ("Same PAN / GSTIN as an existing vendor", ["review", "hold"], "review"),
    "gst_not_found": ("GSTIN not found in the GST registry", ["review", "fixable"], "review"),
    "pan_entity": ("PAN holder type contradicts business type", ["info", "review"], "review"),
    "gstin_state": ("GSTIN state differs from principal place of business", ["info", "review"], "review"),
    "shared_phone": ("Phone number shared with an existing vendor", ["info", "review"], "review"),
    "dup_name": ("Name similar to an existing vendor", ["info", "review"], "review"),
    "free_email": ("Registered company using a free email domain", ["info", "review"], "info"),
}
DEFAULT_POLICY = {"name": "Default", "low_confidence_threshold": 0.75,
                  "severity": {k: v[2] for k, v in TUNABLE.items()}}
POLICY_FILE = os.path.join(DATA, "policy.json")


def load_policy():
    try:
        p = json.load(open(POLICY_FILE))
        sev = dict(DEFAULT_POLICY["severity"]); sev.update(p.get("severity", {}))
        return {"name": p.get("name", "Custom"), "low_confidence_threshold": float(p.get("low_confidence_threshold", 0.75)),
                "severity": sev}
    except Exception:
        return json.loads(json.dumps(DEFAULT_POLICY))


def save_policy(policy):
    json.dump(policy, open(POLICY_FILE, "w"), indent=2)

# ---------------------------------------------------------------- policy configuration
# Which documents are needed, which fields on them must be readable, and which are time-bound.
DOC_POLICY = {
    "pan_card": {"key_fields": ["pan", "name"],
                 "why": "identifies the taxpayer and drives TDS"},
    "gst_certificate": {"key_fields": ["gstin", "legal_name"],
                        "why": "confirms GST registration for input tax credit"},
    "incorporation_certificate": {"key_fields": ["cin", "company_name"],
                                  "why": "proves the company legally exists (MCA record)"},
    "bank_proof": {"key_fields": ["account_number", "ifsc", "account_holder"],
                   "why": "is documentary proof that the payout account belongs to the vendor"},
    "fssai_licence": {"key_fields": ["licence_number", "valid_upto"], "expiry_field": "valid_upto",
                      "why": "a valid FSSAI licence is mandatory for food suppliers"},
}
FIELD_LABEL = {"pan": "PAN", "name": "name", "gstin": "GSTIN", "legal_name": "legal name", "cin": "CIN",
               "company_name": "company name", "account_number": "account number", "ifsc": "IFSC",
               "account_holder": "account holder name", "licence_number": "licence number",
               "valid_upto": "expiry date"}


def required_docs(form):
    docs = ["pan_card", "gst_certificate", "bank_proof"]
    if form.get("entity_type") in COMPANY_TYPES:
        docs.append("incorporation_certificate")
    if form.get("category") == "Food & Beverage Supplies":
        docs.append("fssai_licence")
    return docs


def required_fields(form):
    f = ["legal_name", "entity_type", "category", "address", "state", "pan", "gstin", "contact_name",
         "contact_email", "contact_phone", "account_holder", "account_number", "ifsc", "bank_name"]
    if form.get("entity_type") == "Sole Proprietorship":
        f.append("proprietor_name")
    if form.get("entity_type") in COMPANY_TYPES:
        f.append("cin")
    return f


def load(name):
    with open(os.path.join(DATA, name)) as f:
        return json.load(f)


# ---------------------------------------------------------------- name matching
SUFFIX = [(r"\bprivate limited\b", "pvt ltd"), (r"\bprivate ltd\b", "pvt ltd"), (r"\bpvt limited\b", "pvt ltd"),
          (r"\blimited\b", "ltd"), (r"\bm/s\b", ""), (r"&", " and ")]


def norm(name):
    s = (name or "").lower()
    s = re.sub(r"[.,()]", " ", s)
    for pat, rep in SUFFIX:
        s = re.sub(pat, rep, s)
    return re.sub(r"\s+", " ", s).strip()


def name_match(a, b):
    """Returns (level, score). level: exact | normalised | close | different."""
    if not a or not b:
        return "different", 0
    if a.strip().lower() == b.strip().lower():
        return "exact", 100
    if norm(a) == norm(b):
        return "normalised", 100
    score = fuzz.token_sort_ratio(norm(a), norm(b))
    return ("close" if score >= 92 else "different"), score


def bank_match(a, b):
    return fuzz.token_set_ratio(norm(a), norm(b)) >= 90


def parse_date(s):
    s = (s or "").strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d.%m.%Y", "%d %b %Y", "%d %B %Y", "%d-%b-%Y"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


# ---------------------------------------------------------------- external lookups
IFSC_OFFLINE = {"HDFC": "HDFC Bank", "SBIN": "State Bank of India", "KKBK": "Kotak Mahindra Bank",
                "KARB": "Karnataka Bank", "ICIC": "ICICI Bank", "UTIB": "Axis Bank", "PUNB": "Punjab National Bank"}


IFSC_TTL = 6 * 3600
_ifsc_cache = {}   # code -> (timestamp, result). Only LIVE answers are cached; fallbacks always retry.


def _ifsc_fetch(code):
    """The live call. Separate so tests can simulate outages and recovery."""
    import requests
    r = requests.get(f"https://ifsc.razorpay.com/{code}", timeout=4)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    j = r.json()
    if str(j.get("IFSC", "")).upper() != code or not j.get("BANK"):
        raise ValueError("unexpected response shape")
    return j


def ifsc_lookup(code):
    """Razorpay's public IFSC directory; offline bank-prefix table as fallback.
    `live` is True only when the directory itself answered for this exact code."""
    hit = _ifsc_cache.get(code)
    if hit and time.time() - hit[0] < IFSC_TTL:
        return hit[1]
    try:
        j = _ifsc_fetch(code)
        res = ({"found": False, "live": True, "source": "Razorpay IFSC directory (live)"} if j is None else
               {"found": True, "live": True, "bank": j.get("BANK"), "branch": j.get("BRANCH"), "city": j.get("CITY"),
                "state": j.get("STATE"), "source": "Razorpay IFSC directory (live)"})
        _ifsc_cache[code] = (time.time(), res)
        return res
    except Exception:
        bank = IFSC_OFFLINE.get(code[:4])
        return {"found": bool(bank), "live": False, "bank": bank, "branch": None,
                "source": "Offline bank-prefix table (directory unreachable)"}


def verify_bank_account(account_number):
    """Bank-account verification provider (penny drop). This build ships a SIMULATED provider backed by a local
    fixture; a production build swaps in a live KYB API behind the same return shape."""
    name = load("bank_registry.json").get(account_number)
    return {"found": bool(name), "holder": name, "live": False, "source": "Penny-drop bank verification - SIMULATED"}


def payment_gate(findings, status, lookups):
    """The ONE place that decides whether a vendor may be paid. Every condition is mandatory."""
    def passed(check):
        return any(f["check"] == check and f["severity"] == "pass" for f in findings)
    open_bank = [f for f in findings if f["payment"] and f["severity"] in ATTENTION]
    pd = lookups.get("penny_drop") or {}
    conditions = [
        ("Onboarding approved", status == "Approved", status),
        ("Account ownership confirmed by a LIVE provider", passed("penny_drop") and bool(pd.get("live")),
         "simulated provider - not valid for payments" if pd and not pd.get("live") else
         "confirmed" if passed("penny_drop") else "not confirmed"),
        ("Account holder is an allowed vendor identity", passed("penny_drop"), "bank name matches the vendor"
         if passed("penny_drop") else "no match established"),
        ("Account number on bank proof matches the form", passed("acct_match"),
         "matches" if passed("acct_match") else "not confirmed from a readable bank proof"),
        ("Full IFSC branch code validated in the live directory", passed("ifsc_lookup"),
         "validated" if passed("ifsc_lookup") else "not validated (only the bank prefix, or nothing, is known)"),
        ("No open bank-related findings", not open_bank, "none" if not open_bank else open_bank[0]["title"]),
    ]
    ready = all(ok for _, ok, _ in conditions)
    first_fail = next((n for n, ok, _ in conditions if not ok), None)
    sim_only = bool(pd) and not pd.get("live") and passed("penny_drop") and not open_bank
    if ready:
        text = "Ready - all payment conditions met"
    elif sim_only and status == "Approved" and passed("acct_match"):
        text = "Not authorised - bank checks passed in SIMULATION only; a live verification is required before real payments"
    else:
        text = f"Blocked - {first_fail}" + ("" if first_fail != "Onboarding approved" else f" (currently {status})")
    return {"ready": ready, "status_text": text, "demo_checks_passed": sim_only,
            "conditions": [{"condition": n, "met": ok, "detail": d} for n, ok, d in conditions]}


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", re.I)
CIN_RE = re.compile(r"^[LU][0-9]{5}[A-Z]{2}[0-9]{4}[A-Z]{3}[0-9]{6}$")


# ---------------------------------------------------------------- the run
class Run:
    def __init__(self, form, documents, prior_extractions=None, policy=None):
        self.policy = policy or load_policy()
        self.low = self.policy["low_confidence_threshold"]
        self.form = {k: (v.strip() if isinstance(v, str) else v) for k, v in form.items()}
        self.documents = documents          # list of dicts: name, data(bytes), version
        self.prior = prior_extractions or {}
        self.findings = []
        self.extractions = []
        self.active = {}                    # doc_type -> extraction actually used for checks
        self.lookups = {}

    def add(self, stage, check, title, severity, detail, evidence=None, source="Rule",
            ask=None, payment=False):
        if check in TUNABLE and severity != "pass":
            tuned = self.policy["severity"].get(check, severity)
            if tuned != TUNABLE[check][2]:
                evidence = (evidence or []) + [f"Severity set by client policy '{self.policy['name']}'"]
            severity = tuned
        f = {"stage": stage, "check": check, "title": title, "severity": severity, "detail": detail,
             "evidence": evidence or [], "source": source, "vendor_ask": ask, "payment": payment}
        self.findings.append(f)
        return f

    def doc(self, doc_type):
        return self.active.get(doc_type)

    def field(self, doc_type, field):
        """Field-level state: None if the document is absent; otherwise value + confidence
        (value None means the document is present but the field was not found)."""
        d = self.doc(doc_type)
        if not d or d.get("ambiguous"):
            return None
        f = d["fields"].get(field) or {}
        return {"value": f.get("value"), "conf": float(f.get("confidence") or 0), "file": d["file"]}

    # ------------------------------------------------------------------ stage 1
    def s_intake(self):
        fm = self.form
        missing = [k for k in required_fields(fm) if not fm.get(k)]
        if missing:
            self.add("intake", "form_complete", "Form has missing fields", "fixable",
                     "Required for this business type: " + ", ".join(missing),
                     ask="Please complete these fields on the registration form: " + ", ".join(missing))
        else:
            self.add("intake", "form_complete", "All required form fields present", "pass",
                     f"{len(required_fields(fm))} fields required for a {fm.get('entity_type')}.")
        bad = []
        if fm.get("contact_email") and not EMAIL_RE.match(fm["contact_email"]):
            bad.append(("email", f"'{fm['contact_email']}' is not a valid email address"))
        phone = re.sub(r"[\s\-]", "", fm.get("contact_phone") or "")
        phone = phone[3:] if phone.startswith("+91") else phone
        if phone and not re.fullmatch(r"[6-9][0-9]{9}", phone):
            bad.append(("phone", f"'{fm['contact_phone']}' is not a valid 10-digit Indian mobile number"))
        acct = ids.clean(fm.get("account_number"))
        if acct and not re.fullmatch(r"[0-9]{9,18}", acct):
            bad.append(("account number", f"'{fm['account_number']}' must be 9-18 digits"))
        if fm.get("entity_type") in COMPANY_TYPES and fm.get("cin") and not CIN_RE.match(ids.clean(fm["cin"])):
            bad.append(("CIN", f"'{fm['cin']}' is not a valid 21-character CIN"))
        for what, why in bad:
            self.add("intake", f"format_{what}", f"Invalid {what} on form", "fixable", why + ".",
                     ask=f"Please correct the {what} on your form: {why}.", payment=(what == "account number"))
        if not bad and not missing:
            self.add("intake", "form_formats", "Form values well-formed", "pass", "Email, phone, account number and CIN formats valid.")
        self.add("intake", "doc_policy", "Document requirements set", "info",
                 f"{fm.get('entity_type')} in {fm.get('category')} needs: " +
                 ", ".join(DOC_LABELS[d] for d in required_docs(fm)), source="Policy config")

    # ------------------------------------------------------------------ stage 2
    def extract_doc(self, d):
        h = hashlib.sha256(d["data"]).hexdigest()
        if h in self.prior:
            res = json.loads(json.dumps(self.prior[h]))
            res["engine"] = "Reused - unchanged since an earlier version"; res["error"] = None
        else:
            r, engine, err = extract_mod.extract_one(d["name"], d["data"])
            res = {"doc_type": r["doc_type"], "type_confidence": float(r.get("type_confidence", 0.9) or 0),
                   "type_basis": r.get("type_basis", "content"), "fields": r["fields"],
                   "quality_notes": r.get("quality_notes", ""), "engine": engine, "error": err}
        res.update({"file": d["name"], "version": d["version"], "hash": h})
        res.pop("superseded_by", None); res.pop("unreadable", None)
        return res

    def s_docs(self):
        # supersession: per document type, the newest version wins; older ones are kept for audit
        by_type = {}
        for e in self.extractions:
            if e["doc_type"] != "unknown":
                by_type.setdefault(e["doc_type"], []).append(e)
        for dtype, docs in by_type.items():
            top_v = max(d["version"] for d in docs)
            current = sorted([d for d in docs if d["version"] == top_v], key=lambda d: d["file"])
            for d in docs:
                if d["version"] < top_v:
                    d["superseded_by"] = f"{current[0]['file']} (v{top_v})"
                    self.add("docs", "superseded", f"{d['file']} superseded", "info",
                             f"Replaced by a newer {DOC_LABELS[dtype]} in v{top_v}; kept for the audit trail, not used in checks.")
            self.active[dtype] = current[0]
            if len(current) > 1:
                keys = DOC_POLICY.get(dtype, {}).get("key_fields", [])
                sig = lambda d: tuple(ids.clean(str((d["fields"].get(k) or {}).get("value") or "")) for k in keys)
                names = ", ".join(d["file"] for d in current)
                if len({sig(d) for d in current}) == 1:
                    self.add("docs", f"dupcopy_{dtype}", f"Duplicate copies of the {DOC_LABELS[dtype]}", "info",
                             f"{names} carry identical key details; using {current[0]['file']}.")
                else:
                    current[0]["ambiguous"] = True
                    self.add("docs", f"conflict_{dtype}", f"Conflicting {DOC_LABELS[dtype]}s uploaded together", "fixable",
                             f"{names} were submitted in the same version with different details. The system will not "
                             "guess which one applies, so none of them is used.",
                             ask=f"You sent more than one {DOC_LABELS[dtype]} ({names}) with different details. "
                                 "Please confirm which one is correct.", payment=(dtype == "bank_proof"))

        for e in self.extractions:
            if e.get("error"):
                self.add("docs", "engine", f"AI extraction failed for {e['file']}", "info",
                         f"Fell back to the offline parser. Error: {e['error']}", source="System")
            if e["doc_type"] == "unknown":
                self.add("docs", "unrecognised", f"Unrecognised document: {e['file']}", "info",
                         "Not one of the expected document types, so it was ignored. Possibly the wrong file was attached.",
                         source="AI classification")

        for dtype in required_docs(self.form):
            e = self.active.get(dtype)
            label = DOC_LABELS[dtype]; why = DOC_POLICY[dtype]["why"]
            if not e:
                self.add("docs", f"has_{dtype}", f"{label} missing", "fixable", f"Required because it {why}.",
                         ask=f"Upload your {label} - we need it because it {why}.", payment=(dtype == "bank_proof"))
                continue
            if e.get("ambiguous"):
                continue
            # every key field ends in exactly one state: readable / unclear / not found
            states = {}
            for k in DOC_POLICY[dtype]["key_fields"]:
                f = e["fields"].get(k) or {}
                v, c = f.get("value"), float(f.get("confidence") or 0)
                states[k] = "missing" if not v else ("unclear" if c < self.low or "?" in str(v) else "readable")
            if all(st == "missing" for st in states.values()) and not any((f or {}).get("value") for f in e["fields"].values()):
                e["unreadable"] = True
                basis = " Document type was guessed from the file name only." if e.get("type_basis") == "filename" else ""
                self.add("docs", f"has_{dtype}", f"{label} is unreadable", "fixable",
                         f"Nothing could be read from {e['file']}.{basis} {e.get('quality_notes') or ''}".strip(),
                         ask=f"We could not read {e['file']}. Please upload a clear scan or the original PDF.",
                         payment=(dtype == "bank_proof"))
                continue
            if e.get("type_confidence", 1) < 0.7:
                self.add("docs", f"type_{dtype}", f"Unsure {e['file']} is a {label}", "review",
                         f"Classification confidence {e['type_confidence']:.0%}. A reviewer should confirm the document type.",
                         source="AI classification")
            missing = [k for k, st in states.items() if st == "missing"]
            unclear = [k for k, st in states.items() if st == "unclear"]
            if missing:
                names = ", ".join(FIELD_LABEL[k] for k in missing)
                self.add("docs", f"fields_{dtype}", f"{label}: {names} not found", "fixable",
                         f"{e['file']} was received but its {names} could not be found, so it cannot be checked. "
                         "A document being present does not mean its contents were verified.",
                         ask=f"Your {label} ({e['file']}) does not show the {names}. Please upload a complete copy.",
                         payment=(dtype == "bank_proof"))
            if unclear:
                parts = [f"{FIELD_LABEL[k]} read as '{e['fields'][k]['value']}' ({float(e['fields'][k].get('confidence') or 0):.0%})"
                         for k in unclear]
                names = ", ".join(FIELD_LABEL[k] for k in unclear)
                self.add("docs", f"clarity_{dtype}", f"{label}: {names} not clearly readable", "fixable",
                         "; ".join(parts) + f". Below the {self.low:.0%} confidence threshold, so it is neither "
                         "accepted nor treated as a mismatch.", [e["file"]],
                         ask=f"The {names} on your {label} ({e['file']}) is not clearly readable. Please upload a clear, "
                             "full-page scan or the original PDF.", payment=(dtype == "bank_proof"))
            if not missing and not unclear:
                self.add("docs", f"has_{dtype}", f"{label} received and readable", "pass",
                         f"{e['file']}: all key fields read with confidence ≥ {self.low:.0%}.",
                         source="AI classification" if e["engine"] == "Claude" else e["engine"])

            exp_field = DOC_POLICY[dtype].get("expiry_field")
            if exp_field and states.get(exp_field) == "readable":
                raw = e["fields"][exp_field]["value"]
                exp = parse_date(raw)
                ev = [f"{e['file']}: {raw}"]
                if exp is None:
                    self.add("docs", f"expiry_{dtype}", f"{label}: expiry date cannot be confirmed", "fixable",
                             f"Read '{raw}' but could not interpret it as a date. An unconfirmed expiry is not treated as valid.",
                             ev, ask=f"We could not interpret the validity date on your {label}. Please upload a clear copy.")
                elif exp < dt.date.today():
                    self.add("docs", f"expiry_{dtype}", f"{label} has expired", "fixable", f"Expired on {exp:%d %b %Y}.", ev,
                             ask=f"Your {label} expired on {exp:%d %b %Y}. Please upload the renewed licence.")
                elif (exp - dt.date.today()).days < 60:
                    self.add("docs", f"expiry_{dtype}", f"{label} expires soon", "info",
                             f"Valid until {exp:%d %b %Y} - renewal reminder needed.", ev)
                else:
                    self.add("docs", f"expiry_{dtype}", f"{label} is valid", "pass", f"Valid until {exp:%d %b %Y}.", ev)

    # ------------------------------------------------------------------ stage 3
    def s_ids(self):
        fm = self.form
        pan, gstin = ids.clean(fm.get("pan")), ids.clean(fm.get("gstin"))
        if not ids.pan_valid(pan):
            self.add("ids", "pan_format", "PAN format invalid", "fixable", f"'{pan}' is not a valid PAN (AAAAA9999A).",
                     ask="The PAN on your form is not in a valid format. Please re-check and correct it.")
        else:
            self.add("ids", "pan_format", "PAN format valid", "pass", f"{pan} follows the AAAAA9999A pattern. "
                     "(Format only - not proof the PAN is registered.)")
            t = pan[3]; exp = ids.ENTITY_EXPECTED_PAN.get(fm.get("entity_type"))
            if exp and t != exp:
                self.add("ids", "pan_entity", "PAN holder type contradicts business type", "review",
                         f"4th PAN character '{t}' means {ids.PAN_ENTITY.get(t, 'unknown')}, but the vendor declared "
                         f"'{fm.get('entity_type')}' (expects '{exp}'). Could be a wrong business type on the form, or "
                         "a PAN belonging to someone else.", [f"PAN {pan}"])
            elif exp:
                self.add("ids", "pan_entity", "PAN holder type matches business type", "pass",
                         f"4th character '{t}' = {ids.PAN_ENTITY[t]}, consistent with {fm.get('entity_type')}.")
            basis = (fm.get("proprietor_name") or "").split()[-1:] if fm.get("entity_type") == "Sole Proprietorship" \
                else (fm.get("legal_name") or "").split()[:1]
            if basis and basis[0][0].upper() != pan[4]:
                self.add("ids", "pan_name_letter", "PAN initial does not match the name (weak signal)", "info",
                         f"5th PAN character '{pan[4]}' is usually the first letter of '{basis[0]}'. Names change, so "
                         "this is recorded but does not affect the decision.")

        ok, why = ids.gstin_valid(gstin)
        if not ok:
            self.add("ids", "gstin_format", "GSTIN invalid", "fixable", f"{gstin}: {why}.",
                     ask="The GSTIN on your form is not valid. Please copy it exactly from your GST certificate.")
        else:
            self.add("ids", "gstin_format", "GSTIN structurally valid", "pass",
                     f"{gstin}: {why}. (Structure only - registration status is checked in the verification stage.)")
            parts = ids.gstin_parts(gstin)
            card = self.field("pan_card", "pan")
            card_pan = ids.clean(card["value"]) if card and card["value"] and card["conf"] >= self.low else None
            if parts["pan"] == pan:
                self.add("ids", "gstin_pan", "GSTIN is registered to this PAN", "pass",
                         f"Characters 3-12 of the GSTIN ({parts['pan']}) equal the PAN.")
            elif card_pan and card_pan == parts["pan"]:
                self.add("ids", "gstin_pan", "PAN on form looks like a typo", "fixable",
                         f"GSTIN and PAN card both say {card_pan}; the form says {pan}.",
                         [f"GSTIN-embedded PAN {parts['pan']}", f"{card['file']}: {card_pan}"],
                         ask=f"The PAN on your form ({pan}) does not match your PAN card ({card_pan}). Please correct the form.")
            elif card_pan and card_pan == pan:
                self.add("ids", "gstin_pan", "GSTIN belongs to a different PAN", "critical",
                         f"The GSTIN is registered to PAN {parts['pan']}, but the vendor's PAN card is {pan}. "
                         "This GST registration belongs to another legal entity.",
                         [f"GSTIN {gstin}", f"{card['file']}: {card_pan}"])
            else:
                self.add("ids", "gstin_pan", "GSTIN and PAN disagree", "review",
                         f"GSTIN embeds {parts['pan']} but the form PAN is {pan}, and no readable PAN card settles which is right.")
            same = ids.same_state(parts["state_code"], fm.get("state"))
            if same is False:
                self.add("ids", "gstin_state", "GSTIN state differs from declared address", "review",
                         f"GSTIN state code {parts['state_code']} = {parts['state']}, but the declared principal place of "
                         f"business is in {fm['state']}. Legitimate if the vendor registered in another state - confirm.")
            elif same is None:
                self.add("ids", "gstin_state_unverified", "GSTIN state not verified", "info",
                         f"State code {parts['state_code']} ({parts['state'] or 'unmapped'}) could not be compared with "
                         f"the declared state ('{fm.get('state') or 'not given'}'). Not counted as a pass.")
            else:
                legacy = " (legacy code)" if parts["state_code"] in ids.LEGACY_CODES else ""
                self.add("ids", "gstin_state", "GSTIN state matches address", "pass",
                         f"State code {parts['state_code']}{legacy} = {parts['state']}, same as the principal place of business.")

        if fm.get("ifsc") and not ids.ifsc_valid(fm.get("ifsc")):
            self.add("ids", "ifsc_format", "IFSC format invalid", "fixable", f"'{fm.get('ifsc')}' is not an IFSC.",
                     ask="The IFSC code on your form is invalid. Please check your cheque book or bank letter.", payment=True)

    # ------------------------------------------------------------------ stage 4
    def identities(self):
        fm = self.form
        out = [fm.get("legal_name"), fm.get("trade_name")]
        if fm.get("entity_type") == "Sole Proprietorship":
            out.append(fm.get("proprietor_name"))
        return [i for i in out if i]

    def s_cross(self):
        fm = self.form
        idents = self.identities()

        def compare_name(check, label, docf, payment=False):
            # absent document / absent field are already reported in stage 2 - never silently passed
            if not docf or not docf["value"]:
                return None
            if docf["conf"] < self.low or "?" in str(docf["value"]):
                return None   # unclear: already reported in stage 2, never judged
            level, score, who = max((name_match(docf["value"], i) + (i,) for i in idents), key=lambda x: x[1])
            ev = [f"Form: {who}", f"{docf['file']}: {docf['value']}"]
            if level in ("exact", "normalised"):
                return self.add("cross", check, f"{label} matches", "pass",
                                "Identical." if level == "exact" else "Same name once legal suffixes and punctuation are "
                                "normalised (e.g. 'Pvt. Ltd.' = 'Private Limited').", ev, payment=payment)
            if level == "close":
                return self.add("cross", check, f"{label}: minor variation", "info",
                                f"Similarity {score:.0f}% - treated as the same entity.", ev, payment=payment)
            return self.add("cross", check, f"{label} does not match", "fixable",
                            f"Name similarity only {score:.0f}%. Could be a trade name, a former name or a different entity.",
                            ev, payment=payment)

        def compare_id(check, label, form_val, docf, payment=False):
            if not docf or not docf["value"] or not form_val:
                return
            fv, dv = ids.clean(form_val), ids.clean(docf["value"])
            ev = [f"Form: {form_val}", f"{docf['file']}: {docf['value']} ({docf['conf']:.0%})"]
            if docf["conf"] < self.low or "?" in dv:
                return   # unclear: already reported in stage 2, never judged
            if fv == dv:
                self.add("cross", check, f"{label} matches document", "pass", f"{fv} on both.", ev, payment=payment)
            else:
                self.add("cross", check, f"{label} differs from document", "fixable",
                         f"Form says {fv}, {docf['file']} says {dv}.", ev,
                         ask=f"The {label.lower()} on your form ({fv}) does not match {docf['file']} ({dv}). "
                             "Please confirm which is correct.", payment=payment)

        compare_name("name_gst", "Name on GST certificate", self.field("gst_certificate", "legal_name"))
        compare_name("name_coi", "Name on Certificate of Incorporation", self.field("incorporation_certificate", "company_name"))
        compare_name("name_pan", "Name on PAN card", self.field("pan_card", "name"))
        compare_id("pan_card_match", "PAN", fm.get("pan"), self.field("pan_card", "pan"))
        compare_id("coi_pan_match", "PAN on incorporation certificate", fm.get("pan"), self.field("incorporation_certificate", "pan"))
        compare_id("gstin_doc_match", "GSTIN", fm.get("gstin"), self.field("gst_certificate", "gstin"))
        compare_id("cin_match", "CIN", fm.get("cin"), self.field("incorporation_certificate", "cin"))
        compare_id("acct_match", "Bank account number", fm.get("account_number"), self.field("bank_proof", "account_number"), True)
        compare_id("ifsc_match", "IFSC", fm.get("ifsc"), self.field("bank_proof", "ifsc"), True)
        holder = self.field("bank_proof", "account_holder")
        f = compare_name("holder_doc", "Account holder on bank proof", holder, payment=True)
        if f and f["severity"] == "fixable":
            f["vendor_ask"] = (f"Your bank proof is in the name '{holder['value']}', not '{fm.get('legal_name')}'. "
                               "Please provide a cheque or bank letter in the registered business name, or explain how "
                               f"'{holder['value']}' relates to {fm.get('legal_name')}.")

    # ------------------------------------------------------------------ stage 5
    def s_verify(self):
        fm = self.form
        src = "GST registry - SIMULATED"
        gst_ok = ids.gstin_valid(fm.get("gstin"))[0]
        try:
            if not gst_ok:
                raise LookupError("skip")
            gst = load("gst_registry.json").get(ids.clean(fm.get("gstin")))
        except LookupError:
            gst = False   # GSTIN already failed its checksum - a registry lookup would be meaningless
        except Exception:
            self.add("verify", "gst_unavailable", "GST registry unavailable", "review",
                     "Could not reach the registry. This is not a failure by the vendor - retry or verify manually.", source=src)
            gst = False
        self.lookups["gst"] = gst
        if gst is None:
            self.add("verify", "gst_not_found", "GSTIN not found in GST registry", "review",
                     "No registry record. Could be a new registration not yet propagated, or a fabricated number.", source=src)
        elif gst:
            if gst["status"] != "Active":
                self.add("verify", "gst_registry", f"GST registration is {gst['status']}", "critical",
                         "A cancelled or suspended GSTIN cannot be onboarded.", [f"Registry: {gst['legal_name']}, {gst['status']}"], source=src)
            else:
                level, _ = name_match(gst["legal_name"], fm.get("legal_name"))
                if level == "different":
                    self.add("verify", "gst_registry", "GST registry name differs from vendor", "review",
                             f"Registry legal name is '{gst['legal_name']}'.", source=src)
                else:
                    self.add("verify", "gst_registry", "GST registration active, name matches", "pass",
                             f"Registry legal name '{gst['legal_name']}', status Active.", source=src)

        code = ids.clean(fm.get("ifsc"))
        if ids.ifsc_valid(code):
            info = ifsc_lookup(code); self.lookups["ifsc"] = info
            if not info["found"]:
                self.add("verify", "ifsc_lookup", "IFSC does not exist", "fixable", f"{code} is not a real branch code.",
                         source=info["source"], ask="The IFSC code you entered does not exist. Please re-check it.", payment=True)
            elif fm.get("bank_name") and not bank_match(info["bank"], fm["bank_name"]):
                self.add("verify", "ifsc_lookup", "IFSC belongs to a different bank", "fixable",
                         f"{code} is {info['bank']}, but the form says {fm['bank_name']}.", source=info["source"],
                         ask=f"IFSC {code} belongs to {info['bank']}, not {fm['bank_name']}. Please correct it.", payment=True)
            elif not info["live"]:
                self.add("verify", "ifsc_lookup", "IFSC bank prefix recognised; branch not verified", "info",
                         f"'{code[:4]}' = {info['bank']}, but the live IFSC directory was unreachable, so the full branch "
                         "code is unverified. Not counted as a pass.", source=info["source"])
            else:
                where = f", {info['branch']}" if info.get("branch") else ""
                self.add("verify", "ifsc_lookup", "IFSC verified: branch exists at the declared bank", "pass",
                         f"{code} = {info['bank']}{where}.", source=info["source"], payment=True)

        pd = verify_bank_account(ids.clean(fm.get("account_number")))
        src = pd["source"]
        self.lookups["penny_drop"] = pd
        if not pd["found"]:
            self.add("verify", "penny_drop", "Bank could not confirm the account", "fixable",
                     "Penny drop failed - the account may be closed or the number mistyped.", source=src,
                     ask="We could not verify your bank account. Please re-check the account number.", payment=True)
            return
        bank_name = pd["holder"]
        roles = {"registered name": fm.get("legal_name"), "trade name": fm.get("trade_name")}
        if fm.get("entity_type") == "Sole Proprietorship":
            roles["proprietor"] = fm.get("proprietor_name")
        hit = next((k for k, v in roles.items() if v and name_match(bank_name, v)[0] != "different"), None)
        holder = self.field("bank_proof", "account_holder")
        doc_holder = holder["value"] if holder and holder["value"] and holder["conf"] >= self.low else None
        doc_is_vendor = doc_holder and any(name_match(doc_holder, i)[0] != "different" for i in self.identities())
        ev = [f"Bank says: {bank_name}"] + ([f"{holder['file']} says: {doc_holder}"] if doc_holder else [])
        if hit:
            note = " A proprietorship's account may legitimately be held in the proprietor's personal name." if hit == "proprietor" else ""
            self.add("verify", "penny_drop", f"Bank confirms the account belongs to the vendor ({hit})", "pass",
                     f"Name at bank matches the {hit}.{note}", ev, source=src, payment=True)
        elif doc_holder and name_match(doc_holder, bank_name)[0] != "different":
            self.add("verify", "penny_drop", "Account is held by a differently named party", "fixable",
                     "The bank and the bank proof agree with each other, but neither matches the vendor. Payments would "
                     "go to an account that is not provably the vendor's.", ev, source=src, payment=True)
        elif doc_is_vendor:
            self.add("verify", "penny_drop", "Bank proof contradicts the bank's own records", "critical",
                     f"The submitted document names the vendor ('{doc_holder}'), but the bank reports the account belongs "
                     f"to '{bank_name}'. The bank proof has been altered or belongs to another account.",
                     ev, source=src, payment=True)
        else:
            self.add("verify", "penny_drop", "Account holder does not match vendor", "review",
                     f"Bank reports '{bank_name}', which matches none of the vendor's names.", ev, source=src, payment=True)

    # ------------------------------------------------------------------ stage 6
    def s_dupes(self):
        fm = self.form
        pan, gstin, acct = ids.clean(fm.get("pan")), ids.clean(fm.get("gstin")), ids.clean(fm.get("account_number"))
        phone = re.sub(r"\D", "", fm.get("contact_phone") or "")[-10:]
        master, emps = load("vendor_master.json"), load("employee_master.json")
        hits = 0
        mh = self.lookups.setdefault("master", {})
        # every attribute is evaluated independently - one match never hides another
        for v in master:
            ref = f"{v['vendor_id']} {v['legal_name']} (onboarded {v['onboarded']})"
            same_entity = v["pan"] == pan or v["gstin"] == gstin
            if same_entity:
                mh["identity"] = f"On file as {v['vendor_id']}"
                hits += 1
                which = " and ".join(x for x, y in (("PAN", v["pan"] == pan), ("GSTIN", v["gstin"] == gstin)) if y)
                self.add("dupes", "dup_identity", f"Vendor may already exist as {v['vendor_id']}", "review",
                         f"Same {which} as {ref}. Likely a duplicate registration or a new branch - link or update the "
                         "existing record instead of creating a second one.", [ref])
            if acct and v["account_number"] == acct:
                mh["account"] = (f"Same entity's account ({v['vendor_id']})" if same_entity
                                 else f"Pays {v['vendor_id']} {v['legal_name']}", same_entity)
                hits += 1
                if same_entity:
                    self.add("dupes", "bank_same_entity", f"Bank account already on file for {v['vendor_id']}", "info",
                             "Same legal entity, same account - consistent with a re-registration.", [ref], payment=True)
                else:
                    self.add("dupes", "shared_bank", f"Payout account shared with {v['vendor_id']}", "hold",
                             f"This account already receives payments for {ref}, a different legal entity (PAN {v['pan']} vs "
                             f"{pan}). Can be legitimate (group treasury, factoring) but is also the classic payment-diversion "
                             "pattern. Hold until Finance Controls confirms with the existing vendor through contact details on file.",
                             [ref, f"Account {acct}"], payment=True)
            if phone and re.sub(r"\D", "", v.get("contact_phone", ""))[-10:] == phone and not same_entity:
                hits += 1
                self.add("dupes", "shared_phone", f"Phone number shared with {v['vendor_id']}", "review",
                         f"Same contact number as {ref}.", [ref])
            if not same_entity and name_match(v["legal_name"], fm.get("legal_name"))[0] != "different":
                hits += 1
                self.add("dupes", "dup_name", f"Similar name to {v['vendor_id']}", "review",
                         f"Name closely matches {ref} but the identifiers differ - possibly a related entity.", [ref])
        for e in emps:
            matched = [w for w, ok in (("bank account", e["account_number"] == acct), ("phone", e["phone"] == phone)) if ok]
            if matched:
                mh["employee"] = f"Matches employee {e['employee_id']} ({', '.join(matched)})"
                hits += 1
                self.add("dupes", "employee_link", "Vendor details match an employee", "hold",
                         f"{' and '.join(matched).capitalize()} match employee {e['employee_id']} ({e['department']}). "
                         "Possible conflict of interest or ghost vendor - investigate before onboarding.", [e["employee_id"]],
                         payment=True)
        if not hits:
            self.add("dupes", "dup_clear", "No overlap with vendor or employee master", "pass",
                     f"PAN, GSTIN, bank account, phone and name checked independently against {len(master)} vendors and "
                     f"{len(emps)} employees.")
        dom = (fm.get("contact_email") or "").split("@")[-1].lower()
        if dom in FREE_MAIL and fm.get("entity_type") in COMPANY_TYPES:
            self.add("dupes", "free_email", "Company uses a free email address", "info",
                     f"Registered company contacting via {dom}. Weak signal on its own; adds weight to others.")


    # ------------------------------------------------------------------ evidence matrix
    def build_evidence(self):
        """Every key fact against every source - and, crucially, WHO CONTROLS each source.

        Source roles:
          vendor_doc       supplied by the vendor (they could forge all of them together)
          derived          computed from another value (e.g. the PAN inside a GSTIN) - a consistency check, not a witness
          third_party      an independent party answered live
          third_party_sim  an independent party, simulated in this build
          screening        a search of our own records - can find a conflict, can never confirm identity
        Cell status: corroborates | contradicts | risk | no_conflict | flag | unverified | unreadable | absent.
        Only a corroborating THIRD PARTY makes a fact independently verified. Vendor documents agreeing with each
        other is 'consistent', not 'verified'. A screening search finding nothing is 'no known conflict', never support.
        """
        fm, idents, low = self.form, self.identities(), self.low
        same_id = lambda target: (lambda v: ids.clean(v) == ids.clean(target))
        same_name = lambda v: bool(v) and any(name_match(v, i)[0] != "different" for i in idents)

        def doc(source, docf, compare, role="vendor_doc"):
            if docf is None:
                return {"source": source, "role": role, "value": "—", "status": "absent"}
            v = docf["value"]
            if not v:
                return {"source": source, "role": role, "value": "not found", "status": "unreadable"}
            if docf["conf"] < low or "?" in str(v):
                return {"source": source, "role": role, "value": f"{v} ({docf['conf']:.0%})", "status": "unreadable"}
            return {"source": source, "role": role, "value": v, "status": "corroborates" if compare(v) else "contradicts"}

        def third(source, live, value, ok):
            role = "third_party" if live else "third_party_sim"
            if ok is None:
                return {"source": source, "role": role, "value": value, "status": "unverified"}
            return {"source": source, "role": role, "value": value, "status": "corroborates" if ok else "contradicts"}

        mh = self.lookups.get("master", {})
        gst = self.lookups.get("gst") or None
        pd = self.lookups.get("penny_drop") or {}
        info = self.lookups.get("ifsc") or {}
        rows = []

        r = [doc("PAN card", self.field("pan_card", "name"), same_name),
             doc("GST certificate", self.field("gst_certificate", "legal_name"), same_name)]
        if fm.get("entity_type") in COMPANY_TYPES:
            r.append(doc("Incorporation cert.", self.field("incorporation_certificate", "company_name"), same_name))
        r.append(third("GST registry", False, gst["legal_name"] if gst else "no record",
                       same_name(gst["legal_name"]) if gst else None))
        rows.append({"fact": "Legal name", "form": fm.get("legal_name"), "cells": r})

        g = ids.clean(fm.get("gstin"))
        embedded = {"value": g[2:12], "conf": 1.0, "file": "GSTIN"} if ids.gstin_valid(g)[0] else None
        r = [doc("PAN card", self.field("pan_card", "pan"), same_id(fm.get("pan"))),
             doc("PAN inside GSTIN", embedded, same_id(fm.get("pan")), role="derived")]
        if fm.get("entity_type") in COMPANY_TYPES:
            r.append(doc("Incorporation cert.", self.field("incorporation_certificate", "pan"), same_id(fm.get("pan"))))
        r.append({"source": "Vendor master", "role": "screening", "value": mh.get("identity", "no record of this PAN"),
                  "status": "flag" if mh.get("identity") else "no_conflict"})
        rows.append({"fact": "PAN", "form": fm.get("pan"), "cells": r,
                     "gap": "No PAN-verification source is connected, so PAN can be consistent but not independently verified."})

        r = [doc("GST certificate", self.field("gst_certificate", "gstin"), same_id(fm.get("gstin"))),
             third("GST registry", False, gst["status"] if gst else "no record", (gst["status"] == "Active") if gst else None)]
        rows.append({"fact": "GSTIN", "form": fm.get("gstin"), "cells": r})

        acct = mh.get("account")
        r = [doc("Bank proof", self.field("bank_proof", "account_number"), same_id(fm.get("account_number"))),
             third("Bank (penny drop)", pd.get("live"), "account exists" if pd.get("found") else "not confirmed",
                   True if pd.get("found") else None),
             {"source": "Vendor master", "role": "screening",
              "value": acct[0] if acct else "not used by another vendor",
              "status": ("flag" if acct[1] else "risk") if acct else "no_conflict"},
             {"source": "Employee master", "role": "screening", "value": mh.get("employee", "no employee match"),
              "status": "risk" if mh.get("employee") else "no_conflict"}]
        rows.append({"fact": "Bank account", "form": fm.get("account_number"), "cells": r})

        r = [doc("Bank proof", self.field("bank_proof", "account_holder"), same_name),
             third("Bank (penny drop)", pd.get("live"), pd.get("holder") or "not confirmed",
                   same_name(pd.get("holder")) if pd.get("found") else None)]
        rows.append({"fact": "Account holder", "form": fm.get("account_holder"), "cells": r})

        live = bool(info.get("live"))
        r = [doc("Bank proof", self.field("bank_proof", "ifsc"), same_id(fm.get("ifsc"))),
             third("IFSC directory", True,
                   (f"{info.get('bank')}, {info.get('branch') or 'branch exists'}" if info.get("found") else "code does not exist")
                   if live else (f"{info.get('bank')} prefix only - directory unreachable" if info else "not checked"),
                   (info.get("found") and bank_match(info["bank"], fm.get("bank_name", ""))) if live else None)]
        rows.append({"fact": "IFSC / bank", "form": f"{fm.get('ifsc')} · {fm.get('bank_name')}", "cells": r})

        for row in rows:
            cells = row["cells"]; st_ = [c["status"] for c in cells]
            vendor_ok = sum(1 for c in cells if c["role"] == "vendor_doc" and c["status"] == "corroborates")
            live_ok = sum(1 for c in cells if c["role"] == "third_party" and c["status"] == "corroborates")
            sim_ok = sum(1 for c in cells if c["role"] == "third_party_sim" and c["status"] == "corroborates")
            row.update(vendor_docs_agree=vendor_ok, independent_live=live_ok, independent_sim=sim_ok)
            if "contradicts" in st_:
                row["verdict"] = "contradicted"
            elif "risk" in st_:
                row["verdict"] = "risk"
            elif "unreadable" in st_ or "flag" in st_:
                row["verdict"] = "attention"
            elif live_ok:
                row["verdict"] = "verified"
            elif sim_ok:
                row["verdict"] = "verified_sim"
            elif vendor_ok:
                row["verdict"] = "consistent"
            else:
                row["verdict"] = "unverified"
        return rows

    # ------------------------------------------------------------------ decision
    def decide(self):
        by = {s: [f for f in self.findings if f["severity"] == s] for s in ("pass", "info") + ATTENTION}
        crit, hold, fix, rev = by["critical"], by["hold"], by["fixable"], by["review"]
        if crit:
            status, sub, risk = "Rejected", "Policy breach - escalated to Finance Controls", "High"
            nxt = "Finance Controls reviews the evidence. The vendor receives a neutral notice that reveals no checks."
            reasons = crit + hold
        elif hold:
            status, sub, risk = "Pending", "High-risk hold - Finance Controls", "High"
            nxt = ("Finance Controls verifies independently (e.g. calls the existing vendor on its number on file). "
                   "Nothing else proceeds until then; the vendor is told only that review is ongoing.")
            reasons = hold + fix + rev
        elif fix:
            status = "Pending"
            sub = "Awaiting vendor" + (" and internal review" if rev else "")
            risk = "Medium" if rev or any(f["payment"] for f in fix) else "Low"
            nxt = f"Send the drafted request ({len([f for f in fix if f['vendor_ask']])} item(s)). The case resumes automatically on resubmission."
            reasons = fix + rev
        elif rev:
            status, sub, risk = "Pending", "Awaiting internal review", "Medium"
            nxt = f"Reviewer resolves: {rev[0]['title']}."
            reasons = rev
        else:
            status, sub, risk, reasons = "Approved", "Ready to create in vendor master", "Low", []
            nxt = "Create the vendor record in the ERP."

        owner = {"Rejected": "Finance Controls", "Approved": "Master-data team"}.get(status)
        if not owner:
            owner = ("Finance Controls" if hold else "Vendor" if fix else "Procurement reviewer")
        gate = payment_gate(self.findings, status, self.lookups)
        return {"status": status, "sub_status": sub, "risk": risk, "next_action": nxt,
                "owner": owner,
                "payment_ready": gate["ready"], "demo_bank_checks_passed": gate["demo_checks_passed"],
                "payment_status": gate["status_text"], "payment_gate": gate["conditions"],
                "policy": {"name": self.policy["name"], "low_confidence_threshold": self.low,
                           "changed": {k: v for k, v in self.policy["severity"].items() if v != TUNABLE[k][2]}},
                "decided_by": "System", "system_status": status, "system_sub_status": sub,
                "counts": {s: len(v) for s, v in by.items()},
                "simulated_sources": sorted({f["source"] for f in self.findings if "SIMULATED" in f["source"]}),
                "reasons": [{"title": r["title"], "severity": r["severity"], "detail": r["detail"]} for r in reasons]}


def run_pipeline(form, documents, prior_extractions=None, policy=None):
    """Generator yielding live events for the UI. Final event: ('done', result)."""
    r = Run(form, documents, prior_extractions, policy)
    t0 = time.time()
    decision = None
    for key, label in STAGES:
        yield ("stage_start", key, label)
        n = len(r.findings); ts = time.time()
        if key == "intake": r.s_intake()
        elif key == "docs":
            if documents:
                with ThreadPoolExecutor(max_workers=6) as ex:
                    futs = [ex.submit(r.extract_doc, d) for d in documents]
                    for fu in as_completed(futs):
                        res = fu.result(); r.extractions.append(res)
                        yield ("document", res)
            r.s_docs()
        elif key == "ids": r.s_ids()
        elif key == "cross": r.s_cross()
        elif key == "verify": r.s_verify()
        elif key == "dupes": r.s_dupes()
        elif key == "decision":
            evidence = r.build_evidence()
            decision = r.decide()
        yield ("stage_end", key, label, r.findings[n:], time.time() - ts)
    yield ("done", {"decision": decision, "findings": r.findings, "extractions": r.extractions, "evidence": evidence,
                    "lookups": r.lookups, "seconds": round(time.time() - t0, 1)})
