"""Общие утилиты: конфиг, доступ к Postgres, клиент Битрикс24."""
import json
import logging
import os
import time

import psycopg
import requests

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)


class Cfg:
    B24_WEBHOOK = os.getenv("B24_WEBHOOK", "").rstrip("/")
    PG_DSN = os.getenv("PG_DSN", "postgresql://rop:rop@postgres:5432/rop")

    BACKFILL_DAYS = int(os.getenv("BACKFILL_DAYS", "90"))
    POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "300"))
    B24_RATE_SLEEP = float(os.getenv("B24_RATE_SLEEP", "0.5"))  # 2 запроса/сек

    SYNC_CRM = os.getenv("SYNC_CRM", "1") not in ("0", "false", "no")
    CRM_SINCE_DAYS = int(os.getenv("CRM_SINCE_DAYS", "90"))

    MIN_CALL_SEC = int(os.getenv("MIN_CALL_SEC", "20"))
    WHISPER_MODEL = os.getenv("WHISPER_MODEL", "small")
    WHISPER_THREADS = int(os.getenv("WHISPER_THREADS", "4"))
    WHISPER_COMPUTE = os.getenv("WHISPER_COMPUTE", "int8")
    ASR_BEAM_SIZE = int(os.getenv("ASR_BEAM_SIZE", "1"))
    # подсказка модели с лексикой предметной области — заметно повышает точность
    WHISPER_PROMPT = os.getenv(
        "WHISPER_PROMPT",
        "Разговор менеджера автосалона с клиентом. Покупка автомобиля и мотоцикла "
        "из Японии, Кореи, Китая. Аукцион, растаможка, правый руль, левый руль, "
        "пробег, комплектация, бюджет, доставка, Владивосток, предоплата, договор.",
    )
    ASR_IDLE_SLEEP = int(os.getenv("ASR_IDLE_SLEEP", "60"))
    KEEP_AUDIO_DAYS = int(os.getenv("KEEP_AUDIO_DAYS", "0"))  # 0 = удалять сразу
    AUDIO_DIR = os.getenv("AUDIO_DIR", "/data/audio")


cfg = Cfg()
log = logging.getLogger("rop")


def db():
    """Новое соединение с автокоммитом."""
    return psycopg.connect(cfg.PG_DSN, autocommit=True)


def get_state(conn, key, default=None):
    row = conn.execute("SELECT value FROM sync_state WHERE key = %s", (key,)).fetchone()
    return row[0] if row else default


def set_state(conn, key, value):
    conn.execute(
        """INSERT INTO sync_state (key, value, updated_at) VALUES (%s, %s, now())
           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()""",
        (key, str(value)),
    )


class BitrixError(RuntimeError):
    pass


class Bitrix:
    """Минимальный клиент REST Битрикс24 через входящий вебхук.

    Держит темп 2 запроса/сек и переживает временные ошибки.
    """

    def __init__(self, webhook: str = None):
        self.base = (webhook or cfg.B24_WEBHOOK).rstrip("/")
        if not self.base:
            raise BitrixError("Не задан B24_WEBHOOK")
        self.s = requests.Session()

    def call(self, method: str, params: dict = None, retries: int = 5):
        url = f"{self.base}/{method}.json"
        params = params or {}
        for attempt in range(retries):
            time.sleep(cfg.B24_RATE_SLEEP)
            try:
                r = self.s.post(url, json=params, timeout=90)
            except requests.RequestException as e:
                log.warning("Сеть: %s (попытка %s)", e, attempt + 1)
                time.sleep(5 * (attempt + 1))
                continue

            if r.status_code == 503 or r.status_code == 429:
                log.warning("Битрикс просит подождать (%s)", r.status_code)
                time.sleep(10 * (attempt + 1))
                continue

            try:
                data = r.json()
            except json.JSONDecodeError:
                raise BitrixError(f"{method}: не JSON, код {r.status_code}: {r.text[:300]}")

            if "error" in data:
                err = data.get("error")
                if err in ("QUERY_LIMIT_EXCEEDED", "OPERATION_TIME_LIMIT"):
                    log.warning("Лимит Битрикса (%s), пауза", err)
                    time.sleep(15 * (attempt + 1))
                    continue
                raise BitrixError(f"{method}: {err} — {data.get('error_description')}")
            return data
        raise BitrixError(f"{method}: не удалось за {retries} попыток")

    def list_all(self, method: str, params: dict = None, page_size: int = 50):
        """Постраничный обход списочного метода."""
        params = dict(params or {})
        start = 0
        while True:
            params["start"] = start
            data = self.call(method, params)
            result = data.get("result") or []
            if isinstance(result, dict):  # некоторые методы отдают dict
                result = list(result.values())
            for item in result:
                yield item
            nxt = data.get("next")
            if nxt is None or not result:
                break
            start = nxt
