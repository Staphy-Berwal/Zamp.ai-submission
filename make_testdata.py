"""Generates every test input: vendor forms, document PDFs/scans, simulated registries
and the existing vendor master. Run once: python make_testdata.py"""
import json, os, random
from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfgen import canvas
from reportlab.lib.colors import HexColor, black
from PIL import Image, ImageDraw, ImageFont, ImageFilter
from engine.ids import make_gstin

ROOT = os.path.dirname(os.path.abspath(__file__))
TC = os.path.join(ROOT, "testcases")
DATA = os.path.join(ROOT, "data")
random.seed(7)

INK = HexColor("#1B2A41")
GREY = HexColor("#5B6577")


# ---------------------------------------------------------------- document renderers
def _header(c, title, subtitle, w, h):
    c.setFillColor(INK); c.setFont("Helvetica-Bold", 15); c.drawString(50, h - 60, title)
    c.setFillColor(GREY); c.setFont("Helvetica", 9); c.drawString(50, h - 76, subtitle)
    c.setStrokeColor(INK); c.line(50, h - 86, w - 50, h - 86)


def _rows(c, rows, y, x=50, gap=22):
    for label, value in rows:
        c.setFillColor(GREY); c.setFont("Helvetica", 9.5); c.drawString(x, y, f"{label} :")
        c.setFillColor(black); c.setFont("Helvetica-Bold", 10.5); c.drawString(x + 210, y, str(value))
        y -= gap
    return y


def pdf_doc(path, title, subtitle, rows, footer=None):
    c = canvas.Canvas(path, pagesize=A4); w, h = A4
    _header(c, title, subtitle, w, h)
    y = _rows(c, rows, h - 120)
    if footer:
        c.setFillColor(GREY); c.setFont("Helvetica-Oblique", 8.5)
        for i, line in enumerate(footer):
            c.drawString(50, y - 20 - i * 12, line)
    c.save()


def pan_card(path, v):
    pdf_doc(path, "INCOME TAX DEPARTMENT - GOVT. OF INDIA", "Permanent Account Number Card (e-PAN)", [
        ("Permanent Account Number", v["doc_pan"]),
        ("Name", v["pan_name"]),
        ("Date of Incorporation / Birth", v["incorporation_date"]),
    ], ["This is a digitally generated e-PAN for test purposes."])


def gst_cert(path, v):
    pdf_doc(path, "Government of India - Form GST REG-06", "Registration Certificate", [
        ("Registration Number", v["doc_gstin"]),
        ("Legal Name", v["gst_legal_name"]),
        ("Trade Name, if any", v.get("trade_name") or "-"),
        ("Constitution of Business", v["entity_type"]),
        ("Address of Principal Place of Business", v["address"]),
        ("Date of Liability", v["incorporation_date"]),
        ("Type of Registration", "Regular"),
    ], ["Certificate generated for test purposes. Status must be verified on the GST portal."])


def coi(path, v):
    pdf_doc(path, "GOVERNMENT OF INDIA - MINISTRY OF CORPORATE AFFAIRS", "Certificate of Incorporation", [
        ("Corporate Identity Number", v["cin"]),
        ("Name of Company", v["coi_name"]),
        ("Permanent Account Number (PAN)", v["doc_pan"]),
        ("Date of Incorporation", v["incorporation_date"]),
        ("Registrar of Companies", v["roc"]),
    ], ["I hereby certify that the above company is incorporated under the Companies Act, 2013."])


def fssai(path, v, valid_upto):
    pdf_doc(path, "FOOD SAFETY AND STANDARDS AUTHORITY OF INDIA", "Licence under Food Safety & Standards Act, 2006", [
        ("Licence Number", v["fssai_no"]),
        ("Name of Licensee", v["legal_name"]),
        ("Kind of Business", "Manufacturer / Supplier - Packaged Foods"),
        ("Address", v["address"]),
        ("Valid Upto", valid_upto),
    ])


def cheque_pdf(path, v):
    c = canvas.Canvas(path, pagesize=landscape((300, 700))); w, h = 700, 300
    c.setFillColor(HexColor("#EEF3F8")); c.rect(20, 20, w - 40, h - 40, fill=1, stroke=0)
    c.setFillColor(INK); c.setFont("Helvetica-Bold", 16); c.drawString(40, h - 60, v["doc_bank_name"].upper())
    c.setFont("Helvetica", 9.5); c.setFillColor(GREY)
    c.drawString(40, h - 78, f"IFSC : {v['doc_ifsc']}")
    c.drawString(40, h - 92, "Branch : Main Branch")
    c.setFillColor(black); c.setFont("Helvetica", 10)
    c.drawString(40, 140, f"A/c No : {v['doc_account']}")
    c.drawString(40, 120, f"A/c Holder : {v['doc_holder']}")
    c.drawString(40, 100, "Cheque Type : Current Account")
    c.setFont("Helvetica", 9); c.drawString(w - 260, 70, f"For {v['doc_holder']}")
    c.drawString(w - 200, 45, "Authorised Signatory")
    c.saveState(); c.translate(w / 2, h / 2); c.rotate(14)
    c.setFillColor(HexColor("#B23A3A")); c.setFont("Helvetica-Bold", 46); c.drawCentredString(0, 0, "CANCELLED")
    c.restoreState(); c.save()


def cheque_scan(path, v):
    """A poor phone scan: tilted, noisy, account-number region badly smudged."""
    W, H = 1400, 600
    img = Image.new("RGB", (W, H), (226, 230, 236)); d = ImageDraw.Draw(img)
    try:
        fb = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 34)
        fr = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 24)
    except OSError:
        fb = fr = ImageFont.load_default()
    d.text((60, 50), v["doc_bank_name"].upper(), fill=(27, 42, 65), font=fb)
    d.text((60, 105), f"IFSC : {v['doc_ifsc']}", fill=(60, 60, 70), font=fr)
    d.text((60, 300), f"A/c No : {v['doc_account']}", fill=(20, 20, 20), font=fr)
    d.text((60, 345), f"A/c Holder : {v['doc_holder']}", fill=(20, 20, 20), font=fr)
    d.text((880, 500), "Authorised Signatory", fill=(40, 40, 40), font=fr)
    # smudge the account number line
    box = (190, 290, 700, 335)
    region = img.crop(box).filter(ImageFilter.GaussianBlur(9))
    img.paste(region, box)
    d = ImageDraw.Draw(img)
    for _ in range(6000):
        x, y = random.randint(0, W - 1), random.randint(0, H - 1)
        g = random.randint(120, 200); d.point((x, y), fill=(g, g, g))
    img = img.rotate(3.5, expand=True, fillcolor=(200, 200, 200)).filter(ImageFilter.GaussianBlur(1.6))
    img = img.resize((img.width // 2, img.height // 2))
    img.save(path, "JPEG", quality=38)


# ---------------------------------------------------------------- vendors
def vendor(**kw):
    v = dict(kw)
    v.setdefault("trade_name", "")
    v["gstin"] = v.get("gstin") or make_gstin(v["state_code"], v["pan"])
    for k in ("pan", "gstin"):
        v.setdefault("doc_" + k, v[k])
    v.setdefault("pan_name", v["legal_name"].upper())
    v.setdefault("gst_legal_name", v["legal_name"].upper())
    v.setdefault("coi_name", v["legal_name"].upper())
    v.setdefault("doc_account", v["account_number"])
    v.setdefault("doc_ifsc", v["ifsc"])
    v.setdefault("doc_bank_name", v["bank_name"])
    v.setdefault("doc_holder", v["account_holder"].upper())
    return v


V = {
 "TC1": vendor(legal_name="Shree Ganesh Packaging Private Limited", entity_type="Private Limited Company",
     category="Packaging Materials", state_code="27", state="Maharashtra",
     address="Plot 14, MIDC Bhosari, Pune, Maharashtra 411026", pan="AAKCS7781L",
     cin="U21012MH2016PTC284512", roc="RoC-Pune", incorporation_date="12/07/2016",
     contact_name="Anil Deshmukh", contact_email="accounts@shreeganeshpack.in", contact_phone="9822014471",
     account_holder="Shree Ganesh Packaging Private Limited", account_number="50100234567812",
     ifsc="HDFC0000001", bank_name="HDFC Bank"),
 "TC2": vendor(legal_name="Nimbus Logistics Private Limited", entity_type="Private Limited Company",
     category="Logistics & Freight", state_code="29", state="Karnataka",
     address="No. 88, Peenya Industrial Area, Bengaluru, Karnataka 560058", pan="AAGCN3349Q",
     cin="U63030KA2019PTC121907", roc="RoC-Bangalore", incorporation_date="03/02/2019",
     contact_name="Priya Raman", contact_email="finance@nimbuslogistics.in", contact_phone="9845011208",
     gst_legal_name="NIMBUS LOGISTICS PVT. LTD.",
     account_holder="Nimbus Logistics Private Limited", account_number="912020045561230",
     ifsc="KARB0000001", bank_name="Karnataka Bank", doc_holder="NIMBUS FREIGHT SERVICES"),
 "TC3": vendor(legal_name="Apex Industrial Supplies Private Limited", entity_type="Private Limited Company",
     category="Industrial Supplies", state_code="24", state="Gujarat",
     address="Shed 7, GIDC Vatva, Ahmedabad, Gujarat 382445", pan="AAICA6612H",
     cin="U51909GJ2024PTC149330", roc="RoC-Ahmedabad", incorporation_date="18/03/2024",
     contact_name="Rohit Shah", contact_email="apexindustrial.supplies@gmail.com", contact_phone="9727700415",
     account_holder="Apex Industrial Supplies Private Limited", account_number="50200011223344",
     ifsc="HDFC0000001", bank_name="HDFC Bank"),
 "TC4": vendor(legal_name="Fresh Fields Foods Private Limited", entity_type="Private Limited Company",
     category="Food & Beverage Supplies", state_code="33", state="Tamil Nadu",
     address="12 SIDCO Estate, Guindy, Chennai, Tamil Nadu 600032", pan="AADCF5520P",
     cin="U15400TN2017PTC118204", roc="RoC-Chennai", incorporation_date="21/09/2017",
     contact_name="Meena Subramanian", contact_email="procure@freshfieldsfoods.in", contact_phone="9840033712",
     account_holder="Fresh Fields Foods Private Limited", account_number="38214455109",
     ifsc="SBIN0000300", bank_name="State Bank of India", fssai_no="12417012000345"),
 "TC5": vendor(legal_name="Bharat Electricals", entity_type="Sole Proprietorship",
     category="Electrical Components", state_code="09", state="Uttar Pradesh",
     address="21 Naya Ganj, Kanpur, Uttar Pradesh 208001", pan="ADMPV4471K",
     proprietor_name="Rakesh Kumar Verma", pan_name="RAKESH KUMAR VERMA",
     incorporation_date="05/06/1984", cin="", roc="",
     contact_name="Rakesh Kumar Verma", contact_email="bharatelectricals.knp@gmail.com", contact_phone="9415122090",
     account_holder="Bharat Electricals", account_number="0261102000017734",
     ifsc="KKBK0000261", bank_name="Kotak Mahindra Bank"),
    "TC6": vendor(legal_name="Orion Industrial Packaging Private Limited", entity_type="Private Limited Company",
     category="Packaging Materials", state_code="27", state="Maharashtra",
     address="Unit 4, TTC Industrial Area, Navi Mumbai, Maharashtra 400705", pan="AAFCO8813R",
     cin="U25209MH2021PTC361245", roc="RoC-Mumbai", incorporation_date="09/08/2021",
     contact_name="Neha Kulkarni", contact_email="finance@orionpack.in", contact_phone="9867021450",
     account_holder="Orion Traders Private Limited", account_number="50200011223344",
     ifsc="HDFC0000001", bank_name="HDFC Bank", doc_holder="ORION TRADERS PVT LTD"),
}

CASES = {
 "TC1": dict(title="Happy path - clean company", expected="Approved",
             story="All documents agree, registries confirm, bank verified.",
             docs=["pan", "gst", "coi", "cheque"]),
 "TC2": dict(title="Inconsistent entity names", expected="Pending",
             story="GST shows 'PVT. LTD.' (harmless); cancelled cheque is in the name 'Nimbus Freight Services' (needs clarification).",
             docs=["pan", "gst", "coi", "cheque"]),
 "TC3": dict(title="Bank account belongs to an existing vendor", expected="Rejected",
             story="Every document looks perfect, but the bank account already pays another vendor and the bank returns that vendor's name.",
             docs=["pan", "gst", "coi", "cheque"]),
 "TC4": dict(title="Missing + expired documents, then resubmission", expected="Pending",
             story="No Certificate of Incorporation and an FSSAI licence that expired in March 2026. Resubmission TC4-R fixes both.",
             docs=["pan", "gst", "cheque", "fssai_expired"], resubmission="TC4R"),
 "TC5": dict(title="Poor-quality scanned bank proof", expected="Pending",
             story="Sole proprietorship; the cheque is a blurry phone scan, account number unreadable. The system must not call it a mismatch.",
             docs=["pan", "gst", "cheque_scan"]),
 "TC6": dict(title="Subsidiary paid into parent's account", expected="Pending",
             story="A new subsidiary honestly declares it is paid into its parent's (an existing vendor's) account. Not fraud, "
                   "but the classic diversion pattern: high-risk hold for Finance Controls. Under a stricter client policy it is rejected.",
             docs=["pan", "gst", "coi", "cheque"]),
}


FORM_FIELDS = ["legal_name", "trade_name", "entity_type", "category", "proprietor_name", "address", "state",
               "incorporation_date", "cin", "contact_name", "contact_email", "contact_phone", "pan", "gstin",
               "account_holder", "account_number", "ifsc", "bank_name"]


def build():
    os.makedirs(DATA, exist_ok=True)
    index = {}
    for tc, meta in CASES.items():
        v = V[tc]; folder = os.path.join(TC, tc); os.makedirs(folder, exist_ok=True)
        for f in os.listdir(folder):
            os.remove(os.path.join(folder, f))
        for d in meta["docs"]:
            if d == "pan": pan_card(os.path.join(folder, "pan_card.pdf"), v)
            if d == "gst": gst_cert(os.path.join(folder, "gst_certificate.pdf"), v)
            if d == "coi": coi(os.path.join(folder, "certificate_of_incorporation.pdf"), v)
            if d == "cheque": cheque_pdf(os.path.join(folder, "cancelled_cheque.pdf"), v)
            if d == "cheque_scan": cheque_scan(os.path.join(folder, "cheque_scan.jpg"), v)
            if d == "fssai_expired": fssai(os.path.join(folder, "fssai_licence.pdf"), v, "31/03/2026")
        form = {k: v.get(k, "") for k in FORM_FIELDS}
        json.dump(form, open(os.path.join(folder, "form.json"), "w"), indent=2)
        index[tc] = {k: meta[k] for k in meta if k != "docs"}

    # resubmission for TC4: the two missing/expired documents
    r = os.path.join(TC, "TC4R"); os.makedirs(r, exist_ok=True)
    coi(os.path.join(r, "certificate_of_incorporation.pdf"), V["TC4"])
    fssai(os.path.join(r, "fssai_licence_renewed.pdf"), V["TC4"], "31/03/2031")
    json.dump(index, open(os.path.join(TC, "index.json"), "w"), indent=2)

    # ---------- simulated GST registry (stands in for the GST portal / a KYB API)
    gst = {V[t]["gstin"]: {"legal_name": V[t]["gst_legal_name"], "status": "Active",
                            "state": V[t]["state"]} for t in V}
    orion_gstin = make_gstin("27", "AAECO4521M")
    gst[orion_gstin] = {"legal_name": "ORION TRADERS PRIVATE LIMITED", "status": "Active", "state": "Maharashtra"}
    json.dump(gst, open(os.path.join(DATA, "gst_registry.json"), "w"), indent=2)

    # ---------- simulated penny drop (stands in for a bank-account verification API)
    bank = {
        V["TC1"]["account_number"]: "SHREE GANESH PACKAGING PVT LTD",
        V["TC2"]["account_number"]: "NIMBUS FREIGHT SERVICES",
        "50200011223344": "ORION TRADERS PVT LTD",  # TC3 reuses Orion's account
        V["TC4"]["account_number"]: "FRESH FIELDS FOODS PRIVATE LIMITED",
        V["TC5"]["account_number"]: "RAKESH KUMAR VERMA",
    }
    json.dump(bank, open(os.path.join(DATA, "bank_registry.json"), "w"), indent=2)

    # ---------- existing vendor master
    master = [
        {"vendor_id": "V-1001", "legal_name": "Orion Traders Private Limited", "pan": "AAECO4521M",
         "gstin": orion_gstin, "account_number": "50200011223344", "ifsc": "HDFC0000001",
         "contact_phone": "9820045512", "onboarded": "2023-08-14", "status": "Active"},
        {"vendor_id": "V-1002", "legal_name": "Kaveri Steel Private Limited", "pan": "AAFCK2210D",
         "gstin": make_gstin("29", "AAFCK2210D"), "account_number": "60112233445", "ifsc": "SBIN0000300",
         "contact_phone": "9880012345", "onboarded": "2022-11-02", "status": "Active"},
        {"vendor_id": "V-1003", "legal_name": "Sunrise Print Solutions", "pan": "AJKPS9921B",
         "gstin": make_gstin("07", "AJKPS9921B"), "account_number": "917010023344556", "ifsc": "KKBK0000261",
         "contact_phone": "9810077123", "onboarded": "2024-04-19", "status": "Active"},
    ]
    json.dump(master, open(os.path.join(DATA, "vendor_master.json"), "w"), indent=2)

    # ---------- employee master (for insider / ghost-vendor checks)
    emps = [{"employee_id": "E-217", "name": "Suresh Pillai", "department": "Procurement",
             "account_number": "33019876543", "phone": "9447011223"},
            {"employee_id": "E-342", "name": "Kavita Joshi", "department": "Accounts Payable",
             "account_number": "50100998877665", "phone": "9822233441"}]
    json.dump(emps, open(os.path.join(DATA, "employee_master.json"), "w"), indent=2)
    print("Test data generated:", ", ".join(index))


if __name__ == "__main__":
    build()
