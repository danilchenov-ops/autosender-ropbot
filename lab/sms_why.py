# Почему у Симаненко Failed: спрашиваем шлюз про конкретные сообщения.
import json, warnings
warnings.filterwarnings("ignore")
import requests
from common import db

IDS_SQL = """
  SELECT o.external_id, o.status,
         to_char(o.created_at AT TIME ZONE 'Asia/Vladivostok','DD HH24:MI') t,
         d.login, d.password
    FROM sms_outbox o JOIN sms_devices d USING (portal_user_id)
   WHERE o.portal_user_id = 8831 AND o.external_id IS NOT NULL
     AND o.status IN ('failed','delivered')
   ORDER BY (o.status='failed') DESC, o.created_at
   LIMIT 6
"""

with db() as conn:
    rows = conn.execute(IDS_SQL).fetchall()

for ext, st, t, login, pw in rows:
    try:
        r = requests.get("https://api.sms-gate.app/3rdparty/v1/messages/" + ext,
                         auth=(login, pw), timeout=20)
        body = r.json()
    except Exception as e:
        print(st, t, ext, "ЗАПРОС НЕ ПРОШЁЛ:", e)
        continue
    print("=== наш статус:", st, "| создано:", t, "|", ext)
    print(json.dumps(body, ensure_ascii=False)[:900])
    print()
