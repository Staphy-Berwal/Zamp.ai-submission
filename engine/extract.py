"""Stage 2 - Document intelligence.
Primary: Claude reads each document (PDF or scan) and returns its type + fields with
per-field confidence. Fallback: an offline parser for machine-readable PDFs, used only
when no API key is configured or the API call fails (shown in the UI as such)."""
import base64, json, os, re
from concurrent.futures import ThreadPoolExecutor

import pymupdf

MODEL = os.environ.get("VG_MODEL", "claude-sonnet-5-5")

DOC_TYPES = {
    "pan_card": ["pan", "name", "date"],
    "gst_certificate": ["gstin", "legal_name", "trade_name", "constitution", "address"],
    "incorporation_certificate": ["cin", "company_name", "pan", "incorporation_date"],
    "bank_proof": ["bank_name", "ifsc", "account_number", "account_holder"],
    "fssai_licence": ["licence_number", "licensee_name", "valid_upto"],
}
DOC_LABELS = {"pan_card": "PAN card", "gst_certificate": "GST registration certificate",
              "incorporation_certificate": "Certificate of Incorporation", "bank_proof": "Cancelled cheque / bank proof",
              "fssai_licence": "FSSAI licence", "unknown": "Unrecognised document"}

PROMPT = """You are the document-intake step of a vendor onboarding system for an Indian company.
Classify this document and extract its fields exactly as printed.

Document types and their fields:
- pan_card: pan, name, date
- gst_certificate: gstin, legal_name, trade_name, constitution, address
- incorporation_certificate: cin, company_name, pan, incorporation_date
- bank_proof (cancelled cheque or bank letter): bank_name, ifsc, account_number, account_holder
- fssai_licence: licence_number, licensee_name, valid_upto
- unknown: anything else

Rules:
- Copy values exactly as printed; do not correct, complete or guess them. Dates as DD/MM/YYYY.
- confidence is 0.0-1.0 for how clearly you can READ the value (not whether it is genuine).
- If any character of a value is blurred, cut off or ambiguous, give your best partial reading with
  unreadable characters replaced by '?', and confidence below 0.5.
- If a field is absent, value null and confidence 0.
- type_confidence: 0.0-1.0, how sure you are of doc_type.
- quality_notes: one short sentence on legibility problems, or "" if none.

Respond with ONLY a JSON object, no prose, no code fences:
{"doc_type": "...", "type_confidence": 0.0, "fields": {"<field>": {"value": "...", "confidence": 0.0}}, "quality_notes": ""}"""


def api_key():
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        try:
            import streamlit as st
            key = st.secrets.get("ANTHROPIC_API_KEY")
        except Exception:
            key = None
    return key


def _media_type(name):
    n = name.lower()
    if n.endswith(".pdf"): return "application/pdf"
    if n.endswith(".png"): return "image/png"
    if n.endswith(".webp"): return "image/webp"
    return "image/jpeg"


def _claude_extract(name, data):
    import anthropic
    client = anthropic.Anthropic(api_key=api_key(), timeout=60)
    mt = _media_type(name)
    block_type = "document" if mt == "application/pdf" else "image"
    resp = client.messages.create(
        model=MODEL, max_tokens=1200,
        messages=[{"role": "user", "content": [
            {"type": block_type, "source": {"type": "base64", "media_type": mt,
                                            "data": base64.b64encode(data).decode()}},
            {"type": "text", "text": PROMPT}]}])
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    text = re.sub(r"```(json)?", "", text).strip()
    return normalise(json.loads(text[text.find("{"): text.rfind("}") + 1]))


ALIASES = {"cancelled_cheque": "bank_proof", "cheque": "bank_proof", "bank_letter": "bank_proof", "bank_statement": "bank_proof",
           "pan": "pan_card", "gst": "gst_certificate", "gst_registration_certificate": "gst_certificate",
           "certificate_of_incorporation": "incorporation_certificate", "coi": "incorporation_certificate",
           "fssai": "fssai_licence", "fssai_license": "fssai_licence"}


def _num(x, default):
    try:
        return max(0.0, min(1.0, float(x)))
    except (TypeError, ValueError):
        return default


def normalise(out):
    """Model output is untrusted input: coerce it into the exact shape the engine expects."""
    dt_ = str(out.get("doc_type") or "unknown").strip().lower().replace(" ", "_")
    dt_ = ALIASES.get(dt_, dt_)
    fields = {}
    for k, v in (out.get("fields") or {}).items():
        if not isinstance(v, dict):
            v = {"value": v, "confidence": 0.8}
        val = v.get("value")
        if isinstance(val, str) and val.strip().lower() in ("", "-", "n/a", "na", "null", "none", "not found"):
            val = None
        fields[str(k)] = {"value": None if val is None else str(val).strip(),
                          "confidence": _num(v.get("confidence"), 0.8) if val is not None else 0.0}
    return {"doc_type": dt_ if dt_ in DOC_TYPES else "unknown", "fields": fields,
            "type_confidence": _num(out.get("type_confidence"), 0.9), "type_basis": "content",
            "quality_notes": str(out.get("quality_notes") or "")}


# ------------------------------------------------------------- offline fallback
FALLBACK_LABELS = {
    "Permanent Account Number Card": ("pan_card", None),
    "Registration Certificate": ("gst_certificate", None),
    "Certificate of Incorporation": ("incorporation_certificate", None),
    "CANCELLED": ("bank_proof", None),
    "FOOD SAFETY": ("fssai_licence", None),
}
FIELD_LABELS = {
    "pan_card": {"Permanent Account Number": "pan", "Name": "name", "Date of Incorporation / Birth": "date"},
    "gst_certificate": {"Registration Number": "gstin", "Legal Name": "legal_name", "Trade Name, if any": "trade_name",
                        "Constitution of Business": "constitution", "Address of Principal Place of Business": "address"},
    "incorporation_certificate": {"Corporate Identity Number": "cin", "Name of Company": "company_name",
                                  "Permanent Account Number (PAN)": "pan", "Date of Incorporation": "incorporation_date"},
    "bank_proof": {"IFSC": "ifsc", "A/c No": "account_number", "A/c Holder": "account_holder"},
    "fssai_licence": {"Licence Number": "licence_number", "Name of Licensee": "licensee_name", "Valid Upto": "valid_upto"},
}


def _fallback_extract(name, data):
    text = ""
    if _media_type(name) == "application/pdf":
        doc = pymupdf.open(stream=data, filetype="pdf")
        text = "\n".join(p.get_text() for p in doc)
    if not text.strip():
        # no text layer (scan). Offline mode cannot read images: guess type from filename only
        n = name.lower()
        guess = "bank_proof" if any(k in n for k in ("cheque", "bank")) else "unknown"
        fields = {f: {"value": None, "confidence": 0.0} for f in DOC_TYPES.get(guess, [])}
        return {"doc_type": guess, "fields": fields, "type_confidence": 0.2, "type_basis": "filename",
                "quality_notes": "Scanned image with no text layer - the offline parser cannot read images."}
    doc_type = "unknown"
    for marker, (dt, _) in FALLBACK_LABELS.items():
        if marker in text:
            doc_type = dt; break
    fields = {}
    lines = [l.strip() for l in text.splitlines()]
    for label, field in FIELD_LABELS.get(doc_type, {}).items():
        for i, l in enumerate(lines):
            if l.startswith(label + " :"):
                val = l.split(" :", 1)[1].strip()
                if not val and i + 1 < len(lines):   # value printed as its own text run
                    val = lines[i + 1]
                fields[field] = {"value": None if val in ("-", "") else val, "confidence": 0.95}
                break
    if doc_type == "bank_proof" and lines:
        fields["bank_name"] = {"value": lines[0].title(), "confidence": 0.9}
    return {"doc_type": doc_type, "fields": fields, "quality_notes": "",
            "type_confidence": 0.9 if doc_type != "unknown" else 0.0, "type_basis": "content"}


# ------------------------------------------------------------- public
def extract_one(name, data):
    """Returns (result, engine_used, error)."""
    if api_key():
        try:
            return _claude_extract(name, data), "Claude", None
        except Exception as e:  # never let one API failure kill a live run
            r = _fallback_extract(name, data)
            return r, "Offline parser (API error)", str(e)[:160]
    return _fallback_extract(name, data), "Offline parser", None


def extract_all(docs):
    """docs: list of (name, bytes). Runs extraction in parallel."""
    with ThreadPoolExecutor(max_workers=5) as ex:
        results = list(ex.map(lambda d: extract_one(*d), docs))
    out = []
    for (name, _), (res, engine, err) in zip(docs, results):
        out.append({"file": name, "doc_type": res["doc_type"], "fields": res["fields"],
                    "quality_notes": res.get("quality_notes", ""), "engine": engine, "error": err})
    return out
