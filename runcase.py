import json, os, sys
from engine.pipeline import run_pipeline
def load_case(tc, version=1):
    folder = os.path.join("testcases", tc)
    form = json.load(open(os.path.join(folder, "form.json"))) if os.path.exists(os.path.join(folder,"form.json")) else None
    docs = [{"name": f, "data": open(os.path.join(folder, f), "rb").read(), "version": version}
            for f in sorted(os.listdir(folder)) if f != "form.json"]
    return form, docs
if __name__ == "__main__":
    for tc in sys.argv[1:]:
        form, docs = load_case(tc)
        for ev in run_pipeline(form, docs):
            if ev[0] == "done":
                res = ev[1]; d = res["decision"]
                print(f"\n== {tc}: {d['status']} / {d['sub_status']} / risk {d['risk']} / pay-ready {d['payment_ready']}")
                for f in res["findings"]:
                    if f["severity"] != "pass":
                        print(f"   [{f['severity']:8}] {f['stage']:7} {f['title']}  -- {f['detail'][:110]}")
