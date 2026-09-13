# В каком состоянии у шлюза висят наши 'sent'.
import json, warnings, collections
warnings.filterwarnings("ignore")
import requests
from common import db

SQL = """
  SELECT o.id, o.external_id, d.login, d.password,
         (extract(epoch FROM now() - o.sent_at) / 3600)::numeric(5,1)
    FROM sms_outbox o JOIN sms_devices d USING (portal_user_id)
   WHERE o.status = 'sent' AND o.external_id IS NOT NULL
   ORDER BY o.sent_at
"""

cnt = collections.Counter()
with db() as conn:
    rows = conn.execute(SQL).fetchall()

for oid, ext, login, pw, hrs in rows:
    try:
        r = requests.get("https://api.sms-gate.app/3rdparty/v1/messages/" + ext,
                         auth=(login, pw), timeout=20)
        b = r.json()
    except Exception as e:
        print(oid, hrs, "ошибка:", e)
        continue
    st = b.get("state")
    cnt[st] += 1
    rec = (b.get("recipients") or [{}])[0]
    print("%5s | %6s ч | шлюз: %-10s | получатель: %-10s | %s"
          % (oid, hrs, st, rec.get("state"), (rec.get("error") or "-")[:60]))

print()
print("итого по состояниям:", dict(cnt))
