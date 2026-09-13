# Видит ли шлюз наш ttl: смотрим свежее отправленное сообщение целиком.
import json, warnings
warnings.filterwarnings("ignore")
import requests
from common import db

with db() as conn:
    row = conn.execute("""
        SELECT o.external_id, d.login, d.password,
               to_char(o.sent_at AT TIME ZONE 'Asia/Vladivostok','DD HH24:MI')
          FROM sms_outbox o JOIN sms_devices d USING (portal_user_id)
         WHERE o.external_id IS NOT NULL AND o.sent_at IS NOT NULL
         ORDER BY o.sent_at DESC LIMIT 1""").fetchone()

ext, login, pw, when = row
r = requests.get("https://api.sms-gate.app/3rdparty/v1/messages/" + ext,
                 auth=(login, pw), timeout=20)
print("последнее отправленное:", when, ext, "HTTP", r.status_code)
print(json.dumps(r.json(), ensure_ascii=False, indent=1))
