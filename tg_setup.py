#!/usr/bin/env python3
"""
TG Setup — устанавливает chat_id за 30 секунд, без ручного чтения JSON.

Проблема, которую решает: getUpdates отдаёт апдейты один раз (второй вызов —
пустой result:[]), а в JSON легко промахнуться мимо chat.id.

Использование:
    python3 tg_setup.py --token 123456789:AAH...
Затем отправь своему боту /start (именно боту, не @BotFather!) — скрипт
мгновенно напечатает chat_id, проверит токен и отправит тестовое сообщение.
"""

import argparse
import json
import sys
import time
import urllib.request


def call(token, method, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", required=True)
    args = ap.parse_args()

    try:
        me = call(args.token, "getMe")
    except Exception as e:  # noqa: BLE001
        sys.exit(f"Токен не работает: {e}")
    bot = me["result"].get("username")
    print(f"✓ Токен валиден. Бот: @{bot}")

    print(f"\n→ Открой в Telegram @{bot} и отправь ему /start (или любое сообщение)...")
    print("  (писать нужно боту, не @BotFather)")
    seen = set()
    for _ in range(60):  # ждём до 60 секунд
        try:
            ups = call(args.token, "getUpdates", {"timeout": 0}).get("result", [])
        except Exception:  # noqa: BLE001
            ups = []
        for u in ups:
            msg = u.get("message") or u.get("edited_message") or {}
            chat = msg.get("chat", {})
            cid = chat.get("id")
            if cid and cid not in seen:
                seen.add(cid)
                who = chat.get("username") or chat.get("first_name", "")
                print(f"\n✅ CHAT_ID = {cid}  (от {who})")
                try:
                    call(args.token, "sendMessage",
                         {"chat_id": cid,
                          "text": "✅ Niche Watcher подключён. Жди первых зомби!"})
                    print("✓ Тестовое сообщение отправлено — проверь Telegram")
                except Exception as e:  # noqa: BLE001
                    print(f"! тест не ушёл: {e}")
                print(f"\nДальше:\n  gh secret set TG_BOT_TOKEN   # {args.token[:12]}...\n  gh secret set TG_CHAT_ID      # {cid}")
                return
        time.sleep(2)
    print("\nНе дождался сообщения за 60с. Проверь, что пишешь именно боту, и перезапусти.")


if __name__ == "__main__":
    main()
