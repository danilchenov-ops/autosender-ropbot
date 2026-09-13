"""Отправка готового текста в Телеграм. Длинное режется по границам блоков.

    python send_text.py <файл> <chat_id> [<chat_id> ...]
"""
import sys

import tg


def chunks(text, limit=3900):
    if len(text) <= limit:
        return [text]
    parts, cur = [], ""
    for block in text.split("\n\n*"):
        piece = block if not parts and not cur else "\n\n*" + block
        if cur and len(cur) + len(piece) > limit:
            parts.append(cur)
            cur = piece.lstrip("\n")
        else:
            cur += piece
    if cur:
        parts.append(cur)
    return parts


def main():
    path, chats = sys.argv[1], [int(c) for c in sys.argv[2:]]
    text = open(path, encoding="utf-8").read().strip()
    parts = chunks(text)
    for chat in chats:
        for part in parts:
            tg.send(chat, part)
        print(f"отправлено в {chat}: {len(parts)} сообщ., {len(text)} символов")


if __name__ == "__main__":
    main()
