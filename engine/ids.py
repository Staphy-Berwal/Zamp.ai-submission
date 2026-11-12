"""Deterministic rules for Indian business identifiers.
These are pure format/structure checks. They prove an ID is well-formed and
internally consistent - NOT that it belongs to the vendor (that needs a registry)."""
import re

GST_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

# Complete GST state / UT code table (CBIC). 25 and 28 are legacy codes still found on older GSTINs
# (Daman & Diu before the 2020 merger; Andhra Pradesh before the 2014 bifurcation).
STATE_CODES = {
    "01": "Jammu and Kashmir", "02": "Himachal Pradesh", "03": "Punjab", "04": "Chandigarh",
    "05": "Uttarakhand", "06": "Haryana", "07": "Delhi", "08": "Rajasthan", "09": "Uttar Pradesh",
    "10": "Bihar", "11": "Sikkim", "12": "Arunachal Pradesh", "13": "Nagaland", "14": "Manipur",
    "15": "Mizoram", "16": "Tripura", "17": "Meghalaya", "18": "Assam", "19": "West Bengal",
    "20": "Jharkhand", "21": "Odisha", "22": "Chhattisgarh", "23": "Madhya Pradesh", "24": "Gujarat",
    "25": "Daman and Diu", "26": "Dadra and Nagar Haveli and Daman and Diu", "27": "Maharashtra",
    "28": "Andhra Pradesh", "29": "Karnataka", "30": "Goa", "31": "Lakshadweep", "32": "Kerala",
    "33": "Tamil Nadu", "34": "Puducherry", "35": "Andaman and Nicobar Islands", "36": "Telangana",
    "37": "Andhra Pradesh", "38": "Ladakh", "97": "Other Territory", "99": "Centre Jurisdiction",
}
LEGACY_CODES = {"25", "28"}


def state_key(name):
    s = (name or "").lower().replace("&", " and ")
    s = re.sub(r"[^a-z ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def same_state(code, declared):
    """True / False, or None when it cannot be determined (unknown code or no declared state)."""
    if code not in STATE_CODES or not (declared or "").strip() or code in ("97", "99"):
        return None
    a, b = state_key(STATE_CODES[code]), state_key(declared)
    if a == b:
        return True
    if code == "26" and b in ("dadra and nagar haveli", "daman and diu"):
        return True
    return False

# 4th character of PAN encodes the holder type
PAN_ENTITY = {
    "P": "Individual / Proprietor", "C": "Company", "F": "Firm / LLP", "H": "HUF",
    "A": "Association of Persons", "T": "Trust", "B": "Body of Individuals",
    "L": "Local Authority", "J": "Artificial Juridical Person", "G": "Government",
}

# which PAN type each declared business type should carry
ENTITY_EXPECTED_PAN = {
    "Private Limited Company": "C",
    "Public Limited Company": "C",
    "LLP / Partnership Firm": "F",
    "Sole Proprietorship": "P",
}

PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
IFSC_RE = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")


def clean(s):
    return re.sub(r"\s+", "", (s or "")).upper()


def pan_valid(pan):
    return bool(PAN_RE.match(clean(pan)))


def gstin_checksum(first14):
    total = 0
    for i, ch in enumerate(first14):
        v = GST_CHARS.index(ch) * (1 if i % 2 == 0 else 2)
        total += v // 36 + v % 36
    return GST_CHARS[(36 - total % 36) % 36]


def gstin_parts(gstin):
    g = clean(gstin)
    return {"state_code": g[:2], "state": STATE_CODES.get(g[:2]), "pan": g[2:12], "check": g[14:]}


def gstin_valid(gstin):
    """Returns (ok, reason)."""
    g = clean(gstin)
    if not GSTIN_RE.match(g):
        return False, "does not follow the 15-character GSTIN pattern"
    if g[:2] not in STATE_CODES:
        return False, f"state code {g[:2]} is not a recognised Indian state code"
    if gstin_checksum(g[:14]) != g[14]:
        return False, "checksum digit is wrong (likely a typo or a fabricated number)"
    return True, "format, state code and checksum are valid"


def make_gstin(state_code, pan, entity_no="1"):
    body = f"{state_code}{pan}{entity_no}Z"
    return body + gstin_checksum(body)


def ifsc_valid(ifsc):
    return bool(IFSC_RE.match(clean(ifsc)))
