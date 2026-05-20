"""
check_approvals.py  —  ВЕРСИЯ 2.0
────────────────────────────────────────────────────────────────────────────────
Что делает:
  1. Проверяет нажатые кнопки в боте (каждые 10 минут)
  2. ✅ Одобрен → публикует пост с картинкой в канал
  3. ❌ Отклонён → удаляет из очереди
  4. При ошибке → уведомляет тебя в Telegram
────────────────────────────────────────────────────────────────────────────────
"""

import os, json, requests
from datetime import datetime

# ── Переменные окружения ──────────────────────────────────────────────────────
BOT_TOKEN    = os.environ["TELEGRAM_BOT_TOKEN"]
MODERATOR_ID = os.environ["TELEGRAM_MODERATOR_ID"]
CHANNEL_ID   = os.environ["TELEGRAM_CHANNEL_ID"]

# ── Файлы данных ──────────────────────────────────────────────────────────────
DATA_DIR     = "data"
PENDING_FILE = f"{DATA_DIR}/pending.json"
OFFSET_FILE  = f"{DATA_DIR}/tg_offset.json"


# ─────────────────────────────────────────────────────────────────────────────
# Утилиты
# ─────────────────────────────────────────────────────────────────────────────

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

def tg(method: str, payload: dict) -> dict:
    resp = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
        json=payload,
        timeout=15,
    )
    return resp.json()

def notify_moderator(text: str):
    try:
        tg("sendMessage", {"chat_id": MODERATOR_ID, "text": text})
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Telegram-операции
# ─────────────────────────────────────────────────────────────────────────────

def get_updates(offset) -> list:
    result = tg("getUpdates", {"offset": offset, "limit": 100, "timeout": 0})
    if not result.get("ok"):
        print(f"⚠️  getUpdates вернул ошибку: {result}")
        return []
    return result.get("result", [])

def answer_callback(cq_id: str, text: str):
    tg("answerCallbackQuery", {"callback_query_id": cq_id, "text": text})

def publish_to_channel(post_text: str, image_url: str) -> bool:
    """
    Публикует пост в канал.
    Если есть картинка — отправляет фото с подписью.
    Если картинка не загрузилась — отправляет просто текст.
    """
    if image_url:
        result = tg("sendPhoto", {
            "chat_id": CHANNEL_ID,
            "photo":   image_url,
            "caption": post_text[:1024],
        })
        if result.get("ok"):
            return True
        # Картинка не загрузилась — пробуем текстом
        print(f"⚠️  Фото не отправилось ({result.get('description')}), пробую текстом...")

    # Текстовая публикация (fallback)
    result = tg("sendMessage", {
        "chat_id": CHANNEL_ID,
        "text":    post_text[:4096],
        "disable_web_page_preview": False,
    })
    return result.get("ok", False)

def mark_moderator_msg(msg_id: int, status: str, post_text: str):
    """Обновляет сообщение у модератора — убирает кнопки, добавляет статус."""
    icons = {"approved": "✅ Опубликовано", "rejected": "❌ Отклонено"}
    label = icons.get(status, status)
    # Редактируем caption если это фото, иначе текст
    for method in ("editMessageCaption", "editMessageText"):
        result = tg(method, {
            "chat_id":    MODERATOR_ID,
            "message_id": msg_id,
            "caption" if method == "editMessageCaption" else "text":
                f"{label}\n\n{post_text[:900]}",
        })
        if result.get("ok"):
            break


# ─────────────────────────────────────────────────────────────────────────────
# Главная функция
# ─────────────────────────────────────────────────────────────────────────────

def main():
    print(f"\n🔍 Проверка одобрений — {datetime.now().strftime('%Y-%m-%d %H:%M')}")

    try:
        pending     = load_json(PENDING_FILE, {})
        offset_data = load_json(OFFSET_FILE,  {"offset": None})
        offset      = offset_data.get("offset")

        updates = get_updates(offset)
        print(f"📨 Обновлений: {len(updates)}")

        new_offset    = offset
        changed       = False  # флаг: нужно ли сохранять pending

        for update in updates:
            new_offset = update["update_id"] + 1

            if "callback_query" not in update:
                continue

            cq     = update["callback_query"]
            data   = cq.get("data", "")
            cq_id  = cq["id"]
            msg_id = cq["message"]["message_id"]

            # ── ✅ Одобрение ──────────────────────────────────────────────────
            if data.startswith("approve_"):
                art_id = data.removeprefix("approve_")

                if art_id not in pending:
                    answer_callback(cq_id, "⚠️ Пост уже обработан")
                    continue

                item      = pending[art_id]
                post_text = item["post_text"]
                image_url = item.get("image_url", "")

                success = publish_to_channel(post_text, image_url)

                if success:
                    answer_callback(cq_id, "✅ Пост опубликован в канале!")
                    mark_moderator_msg(msg_id, "approved", post_text)
                    del pending[art_id]
                    changed = True
                    print(f"✅ Опубликован пост {art_id}")
                else:
                    answer_callback(cq_id, "❌ Ошибка публикации — попробуй ещё раз")
                    notify_moderator(f"❌ Не удалось опубликовать пост {art_id}")
                    print(f"❌ Ошибка публикации {art_id}")

            # ── ❌ Отклонение ─────────────────────────────────────────────────
            elif data.startswith("reject_"):
                art_id = data.removeprefix("reject_")

                if art_id not in pending:
                    answer_callback(cq_id, "⚠️ Пост уже обработан")
                    continue

                item = pending[art_id]
                answer_callback(cq_id, "❌ Пост отклонён")
                mark_moderator_msg(msg_id, "rejected", item["post_text"])
                del pending[art_id]
                changed = True
                print(f"❌ Отклонён пост {art_id}")

        # Сохраняем только если что-то изменилось
        if changed:
            save_json(PENDING_FILE, pending)

        save_json(OFFSET_FILE, {"offset": new_offset})
        print("✅ Проверка завершена\n")

    except Exception as e:
        error_msg = f"❌ Ошибка в check_approvals:\n{type(e).__name__}: {e}"
        print(error_msg)
        notify_moderator(error_msg)
        raise


if __name__ == "__main__":
    main()
