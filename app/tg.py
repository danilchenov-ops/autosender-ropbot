"""Отправка в Телеграм через SOCKS-туннель до заграничного сервера."""
import os
import time

import requests

TOKEN = os.getenv("TG_BOT_TOKEN", "")
PROXY = os.getenv("TG_PROXY", "")
API = f"https://api.telegram.org/bot{TOKEN}"

PROXIES = {"http": PROXY, "https": PROXY} if PROXY else None
RETRIES = 3


def call(method, _timeout=45, _retries=RETRIES, **params):
    last = None
    for attempt in range(_retries):
        try:
            r = requests.post(f"{API}/{method}", json=params,
                              proxies=PROXIES, timeout=_timeout)
            data = r.json()
            if not data.get("ok"):
                raise RuntimeError(f"{method}: {data.get('description')}")
            return data.get("result")
        except requests.RequestException as e:
            last = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"{method}: сеть недоступна — {str(last)[:150]}")


def send(chat_id, text, markdown=True, preview=False):
    params = dict(chat_id=chat_id, text=text,
                  disable_web_page_preview=not preview)
    if markdown:            # null в parse_mode Telegram не принимает
        params["parse_mode"] = "Markdown"
    return call("sendMessage", **params)


def poll(offset, seconds=25):
    """Длинный опрос: держим соединение чуть меньше, чем таймаут запроса."""
    return call("getUpdates", _timeout=seconds + 25, _retries=1,
                offset=offset, timeout=seconds,
                allowed_updates=["message", "callback_query",
                                 "chat_member", "my_chat_member", "chat_join_request"])


def me():
    return call("getMe")


def call_file(method, files, _timeout=120, **params):
    """Отправка файла: multipart, параметры уходят полями формы."""
    r = requests.post(f"{API}/{method}", data=params, files=files,
                      proxies=PROXIES, timeout=_timeout)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"{method}: {data.get('description')}")
    return data.get("result")
