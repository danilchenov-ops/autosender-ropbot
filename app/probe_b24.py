import os, requests, json
b = os.getenv("B24_WEBHOOK", "").rstrip("/") + "/"
for uid in [8829, 8831, 11357, 11807, 15077, 15079]:
    r = requests.post(b + "timeman.status", json={"USER_ID": uid}, timeout=25).json()
    print(uid, "->", json.dumps(r.get("result"), ensure_ascii=False)[:400])
print("--- timecontrol ---")
for meth, p in [
    ("timeman.timecontrol.reports.settings.get", {}),
    ("timeman.timecontrol.reports.users.get", {}),
    ("timeman.timecontrol.reports.get", {"USER_ID": 15077}),
    ("timeman.timecontrol.settings.get", {}),
]:
    try:
        r = requests.post(b + meth, json=p, timeout=25).json()
        print(meth, "->", json.dumps(r, ensure_ascii=False)[:700])
    except Exception as e:
        print(meth, "err", e)
