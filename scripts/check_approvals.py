"""
check_approvals.py — ФИНАЛЬНАЯ ВЕРСИЯ
"""

import os, json, requests
from datetime import datetime

BOT_TOKEN    = os.environ["TELEGRAM_BOT_TOKEN"]
MODERATOR_ID = os.environ["TELEGRAM_MODERATOR_ID"]
CHANNEL_ID   = os.environ["TELEGRAM_CHANNEL_ID"]

DATA_DIR     = "data"
PENDING_FILE = f"{DATA_DIR}/pending.json"
OFFSET_FILE  = f"{DATA_DIR}/tg_offset.json"


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default

def save_json(path, data):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def tg(method, payload):
    resp = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
        json=payload, timeout=15,
    )
    return resp.json()

def notify_moderator(text):
    try:
        tg("sendMessage", {"chat_id": MODERATOR_ID, "text": text})
    except Exception:
        pass

def get_updates(offset):
    result = tg("getUpdates", {"offset": offset, "limit": 100, "timeout": 0})
    return result.get("result", []) if result.get("ok") else []

def answer_callback(cq_id, text):
    tg("answerCallbackQuery", {
        "callback_query_id": cq_id,
        "text": text,
        "show_alert": False,
    })

def publish_to_channel(post_text, image_url):
    if image_url:
        result = tg("sendPhoto", {
            "chat_id":    CHANNEL_ID,
            "photo":      image_url,
            "caption":    post_text[:1024],
            "parse_mode": "HTML",
        })
        if result.get("ok"):
            return True
        print(f"Фото не отправилось: {result.get('description')}")

    result = tg("sendMessage", {
        "chat_id":    CHANNEL_ID,
        "text":       post_text[:4096],
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    })
    return result.get("ok", False)

def remove_buttons(msg_id, label):
    """Убирает кнопки и ставит статус."""
    for method in ("editMessageCaption", "editMessageText"):
        field = "caption" if "Caption" in method else "text"
        r = tg(method, {
            "chat_id":      MODERATOR_ID,
            "message_id":   msg_id,
            field:          label,
            "reply_markup": {"inline_keyboard": []},
        })
        if r.get("ok"):
            break


def main():
    print(f"\nПроверка одобрений — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    try:
        pending     = load_json(PENDING_FILE, {})
        offset_data = load_json(OFFSET_FILE, {"offset": None})
        offset      = offset_data.get("offset")

        updates = get_updates(offset)
        print(f"Обновлений: {len(updates)}")

        new_offset = offset
        changed    = False

        for update in updates:
            new_offset = update["update_id"] + 1

            if "callback_query" not in update:
                continue

            cq     = update["callback_query"]
            data   = cq.get("data", "")
            cq_id  = cq["id"]
            msg_id = cq["message"]["message_id"]

            if data.startswith("approve_"):
                art_id = data.removeprefix("approve_")
                if art_id not in pending:
                    answer_callback(cq_id, "⚠️ Уже обработан")
                    continue

                item    = pending[art_id]
                success = publish_to_channel(item["post_text"], item.get("image_url", ""))

                if success:
                    answer_callback(cq_id, "✅ Пост опубликован!")
                    remove_buttons(msg_id, "✅ ОПУБЛИКОВАНО")
                    del pending[art_id]
                    changed = True
                    print(f"✅ Опубликован: {art_id}")
                else:
                    answer_callback(cq_id, "❌ Ошибка публикации")
                    notify_moderator("❌ Не удалось опубликовать пост")

            elif data.startswith("reject_"):
                art_id = data.removeprefix("reject_")
                if art_id not in pending:
                    answer_callback(cq_id, "⚠️ Уже обработан")
                    continue
                answer_callback(cq_id, "❌ Отклонено")
                remove_buttons(msg_id, "❌ ОТКЛОНЕНО")
                del pending[art_id]
                changed = True
                print(f"❌ Отклонён: {art_id}")

        if changed:
            save_json(PENDING_FILE, pending)
        save_json(OFFSET_FILE, {"offset": new_offset})
        print("Готово\n")

    except Exception as e:
        error_msg = f"❌ Ошибка check_approvals:\n{type(e).__name__}: {e}"
        print(error_msg)
        notify_moderator(error_msg)
        raise

if __name__ == "__main__":
    main()
