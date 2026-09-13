# Когда телефон реально обработал сообщения: смотрим states у доставленных.
import json, warnings
warnings.filterwarnings("ignore")
import requests
from common import db

IDS = [
    ("30 23:11 delivered", "g26DLx4kMEjxpJb4NCC-T"),
    ("31 00:30 delivered", "GOFXLjrS6ERujxuFJ8aFO"),
    ("31 05:55 delivered", "WpZmNv6Z90uz9s1q0Hk6c"),
    ("31 09:26 FAILED",    "b2J3_5TzcteW735rnOxR8"),
    ("31 11:18 delivered", "d2Xb6yPo-iq0e9h6FiJX5"),
    ("31 15:05 delivered", "jVrYJ8wvHBqg3fzRWrLC2"),
]

with db() as conn:
    login, pw = conn.execute(
        "SELECT login, password FROM sms_devices WHERE portal_user_id=8831"
    ).fetchone()

for label, ext in IDS:
    try:
        r = requests.get("https://api.sms-gate.app/3rdparty/v1/messages/" + ext,
                         auth=(login, pw), timeout=20)
        b = r.json()
    except Exception as e:
        print(label, ext, "ошибка запроса:", e)
        continue
    rec = (b.get("recipients") or [{}])[0]
    print(label, "|", b.get("state"), "|", rec.get("error") or "-")
    print("   states:", json.dumps(b.get("states"), ensure_ascii=False))
