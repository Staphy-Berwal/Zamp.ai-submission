"""Case storage. A case is one vendor application; each (re)submission is a new version,
so the full decision history is preserved for audit."""
import base64, json, os, sqlite3, datetime as dt

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "vendorguard.db")


def conn():
    c = sqlite3.connect(DB, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE IF NOT EXISTS cases(id TEXT PRIMARY KEY, vendor TEXT, entity_type TEXT,
                 category TEXT, created TEXT, updated TEXT, status TEXT, sub_status TEXT, risk TEXT,
                 payment_ready INTEGER, versions INTEGER, test_case TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS versions(case_id TEXT, version INTEGER, at TEXT, trigger TEXT,
                 form TEXT, files TEXT, result TEXT, comms TEXT, seconds REAL,
                 PRIMARY KEY(case_id, version))""")
    return c


def new_case_id():
    """Next number after the highest ever issued - never reuses an ID, even if rows are deleted."""
    with conn() as c:
        c.execute("CREATE TABLE IF NOT EXISTS seq(n INTEGER)")
        row = c.execute("SELECT n FROM seq").fetchone()
        n = (row[0] if row else 0) + 1
        c.execute("DELETE FROM seq"); c.execute("INSERT INTO seq VALUES(?)", (n,))
    return f"VOB-{n:04d}"


def save_version(case_id, version, trigger, form, documents, result, comms, test_case=None):
    now = dt.datetime.now().isoformat(timespec="seconds")
    d = result["decision"]
    files = [{"name": x["name"], "version": x["version"], "data": base64.b64encode(x["data"]).decode()}
             for x in documents]
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO versions VALUES(?,?,?,?,?,?,?,?,?)",
                  (case_id, version, now, trigger, json.dumps(form), json.dumps(files),
                   json.dumps(result), json.dumps(comms), result["seconds"]))
        exists = c.execute("SELECT created FROM cases WHERE id=?", (case_id,)).fetchone()
        c.execute("INSERT OR REPLACE INTO cases VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                  (case_id, form.get("legal_name"), form.get("entity_type"), form.get("category"),
                   exists["created"] if exists else now, now, d["status"], d["sub_status"], d["risk"],
                   int(d["payment_ready"]), version, test_case))


def cases():
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM cases ORDER BY created DESC")]


def versions(case_id):
    with conn() as c:
        rows = c.execute("SELECT * FROM versions WHERE case_id=? ORDER BY version", (case_id,)).fetchall()
    out = []
    for r in rows:
        r = dict(r)
        for k in ("form", "files", "result", "comms"):
            r[k] = json.loads(r[k])
        for f in r["files"]:
            f["data"] = base64.b64decode(f["data"])
        out.append(r)
    return out


def all_results():
    with conn() as c:
        rows = c.execute("""SELECT v.case_id, v.version, v.result, v.seconds FROM versions v""").fetchall()
    return [(r["case_id"], r["version"], json.loads(r["result"]), r["seconds"]) for r in rows]


def reset():
    if os.path.exists(DB):
        os.remove(DB)
