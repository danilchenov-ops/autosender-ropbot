# -*- coding: utf-8 -*-
"""Ждёт пополнения ProxyAPI и, как только оно есть, запускает бенч моделей.

Запуск: nohup .venv/bin/python watch.py > out/watch.log 2>&1 &
"""
import os
import subprocess
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import runner  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PY = os.path.join(HERE, ".venv/bin/python")


def alive():
    try:
        r = requests.post(
            f"{runner.BASE}/google/v1beta/models/gemini-3.5-flash-lite:generateContent",
            headers={"Authorization": f"Bearer {runner.KEY}"},
            json={"contents": [{"parts": [{"text": "ответь одним словом: да"}]}],
                  "generationConfig": {"maxOutputTokens": 8}},
            timeout=30)
        return r.status_code == 200, r.status_code, r.text[:200]
    except Exception as e:
        return False, 0, str(e)[:200]


def main():
    n = 0
    while True:
        ok, code, body = alive()
        n += 1
        print(f"[{time.strftime('%H:%M:%S')}] проверка {n}: код {code} "
              f"{'ЕСТЬ БАЛАНС' if ok else body}", flush=True)
        if ok:
            break
        time.sleep(180)
    print("запускаю бенч", flush=True)
    subprocess.run([PY, os.path.join(HERE, "runner.py"), "bench",
                    "--n", "4", "--models",
                    "gemini-3.7-flash,gemini-3.1-pro-preview,gemini-3.5-flash"],
                   cwd=HERE)
    print("бенч закончен", flush=True)


if __name__ == "__main__":
    main()
