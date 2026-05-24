"""
generate_post.py — GitHub Models (GPT-4o)
"""

import os, json, time, random, hashlib, urllib.parse, re
import requests, feedparser
from datetime import datetime, timezone

BOT_TOKEN      = os.environ["TELEGRAM_BOT_TOKEN"]
MODERATOR_ID   = os.environ["TELEGRAM_MODERATOR_ID"]
CHANNEL_ID     = os.environ["TELEGRAM_CHANNEL_ID"]
GITHUB_TOKEN   = os.environ["GITHUB_TOKEN"]

GITHUB_MODELS_URL = "https://models.inference.ai.azure.com/chat/completions"

# GPT-4o → GPT-4o-mini как запасной
MODELS = ["gpt-4o", "gpt-4o-mini"]

RSS_FEEDS = [
    "https://habr.com/ru/rss/hub/artificial_intelligence/all/",
    "https://habr.com/ru/rss/hub/machine_learning/all/",
    "https://techcrunch.com/category/artificial-intelligence/feed/",
    "https://venturebeat.com/category/ai/feed/",
    "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",
    "https://openai.com/blog/rss/",
    "https://blogs.nvidia.com/feed/",
]

DATA_DIR     = "data"
PENDING_FILE = f"{DATA_DIR}/pending.json"
POSTED_FILE  = f"{DATA_DIR}/posted_ids.json"


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

def article_id(url):
    return hashlib.md5(url.encode()).hexdigest()[:16]

def notify_moderator(text):
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": MODERATOR_ID, "text": text},
            timeout=10,
        )
    except Exception:
        pass

def fetch_articles():
    articles = []
    for feed_url in RSS_FEEDS:
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries[:5]:
                url = entry.get("link", "")
                if not url:
                    continue
                summary = re.sub(r'<[^>]+>', '', entry.get("summary", ""))
                articles.append({
                    "id":      article_id(url),
                    "title":   entry.get("title", "").strip(),
                    "url":     url,
                    "summary": summary[:600].strip(),
                })
        except Exception as e:
            print(f"Ошибка {feed_url}: {e}")
    print(f"Статей из RSS: {len(articles)}")
    return articles


SYSTEM = """Ты — главный редактор топового Telegram-канала «Нейро-новости» об ИИ.
Аудитория: русскоязычные, 18-45 лет, интересуются технологиями и будущим.
Твои посты — вирусные, умные, живые. Отвечай ТОЛЬКО валидным JSON."""

USER_PROMPT = """Напиши Telegram-пост про эту новость об ИИ.

НОВОСТЬ:
Заголовок: {title}
Содержание: {summary}
Ссылка: {url}

Верни ТОЛЬКО JSON:
{{"post": "текст поста", "image_prompt": "english visual prompt"}}

━━━ СТРУКТУРА ПОСТА ━━━

Строка 1 — КРЮЧОК (останавливает скролл):
Примеры стиля:
• «ИИ сделал за 4 секунды то, на что юрист тратит день ⚡»
• «Google скрывал это полгода. Больше не скрывает 🔓»
• «Твоя профессия в этом списке? Проверь 👇»
• «<b>93%</b> менеджеров не знают об этом инструменте. А зря 🎯»

(пустая строка)

2-3 предложения — СУТЬ:
Что произошло, простым языком. Конкретные цифры если есть. Никакого жаргона.

(пустая строка)

2-3 предложения — ПОЧЕМУ ЭТО ВАЖНО ТЕБЕ:
Конкретно для читателя: «Если ты фрилансер...», «Для малого бизнеса это значит...»
Живо, лично, без воды.

(пустая строка)

1 строка — ВОПРОС или ОСТРЫЙ ТЕЗИС:
Провоцирует обсуждение. Заставляет написать комментарий.

(пустая строка)

#ИИ #нейросети #тематический_хештег

(пустая строка)

<a href="{url}">📖 Читать полностью</a>

━━━ ПРАВИЛА ФОРМАТИРОВАНИЯ ━━━
• Язык: ТОЛЬКО русский
• <b>жирный</b> — ровно 2-3 раза для ключевых фактов/цифр
• Эмодзи: 5-7 штук, уместно, не подряд
• Длина: 180-250 слов — строго
• Запрещено: «революция», «прорыв», «невероятный», «уникальный»
• Тон: умный друг с характером — как лучшие российские tech-блогеры

━━━ IMAGE PROMPT ━━━
• Английский, до 120 символов
• Абстрактная концептуальная иллюстрация к теме
• Стиль: cinematic concept art, dark background, neon glow, 8k, ultra detailed
• ЗАПРЕЩЕНО: humans, faces, people, text, letters, words
• Пример: «glowing AI processor dark space electric blue circuits neon 8k cinematic»"""


def call_model(model, messages):
    resp = requests.post(
        GITHUB_MODELS_URL,
        headers={
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Content-Type":  "application/json",
        },
        json={
            "model":       model,
            "messages":    messages,
            "max_tokens":  1500,
            "temperature": 0.82,
        },
        timeout=60,
    )
    return resp.status_code, resp


def call_ai(prompt):
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user",   "content": prompt},
    ]

    for model in MODELS:
        print(f"Пробую: {model}")
        for attempt in range(4):
            try:
                status, resp = call_model(model, messages)

                if status == 200:
                    content = resp.json()["choices"][0]["message"]["content"].strip()
                    print(f"✓ Ответ от {model}")
                    return content

                elif status == 429:
                    wait = 30 * (attempt + 1)
                    print(f"  Rate limit, жду {wait}с...")
                    time.sleep(wait)

                elif status in (404, 400):
                    print(f"  Модель {model} недоступна → следующая")
                    break

                else:
                    print(f"  Ошибка {status}: {resp.text[:150]}")
                    time.sleep(10)

            except Exception as e:
                print(f"  Исключение: {e}")
                time.sleep(10)

    raise RuntimeError("Все модели недоступны")


def parse_response(raw):
    clean = raw.strip()

    if "```" in clean:
        parts = clean.split("```")
        for part in parts:
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            if part.startswith("{"):
                clean = part
                break

    data         = json.loads(clean)
    post_text    = data.get("post", "").strip()
    image_prompt = data.get("image_prompt", "").strip()

    if not post_text:
        raise ValueError("Пустой пост")

    # Проверка на русский язык
    ru = sum(1 for c in post_text if '\u0400' <= c <= '\u04FF')
    if ru < 30:
        raise ValueError(f"Пост не на русском (ru символов: {ru})")

    if not image_prompt:
        image_prompt = "AI neural network dark space neon glow cinematic 8k"

    return post_text, image_prompt


def generate_content(article):
    prompt = USER_PROMPT.format(**article)

    for attempt in range(3):
        try:
            raw = call_ai(prompt)
            return parse_response(raw)
        except (json.JSONDecodeError, ValueError) as e:
            print(f"Попытка {attempt+1}: ошибка парсинга — {e}")
            time.sleep(5)

    raise RuntimeError("Не удалось получить корректный пост")


def build_image_url(prompt):
    """1080x1080 — квадрат, идеал для Telegram. Модель flux — лучшее качество."""
    seed    = random.randint(10000, 99999)
    full    = f"{prompt}, NO humans, NO faces, NO text, NO letters, abstract only"
    encoded = urllib.parse.quote(full)
    return (
        f"https://image.pollinations.ai/prompt/{encoded}"
        f"?width=1080&height=1080&model=flux&nologo=true&enhance=true&seed={seed}"
    )


def send_for_approval(post_text, image_url, art_id):
    keyboard = {
        "inline_keyboard": [[
            {"text": "✅ Опубликовать", "callback_data": f"approve_{art_id}"},
            {"text": "❌ Отклонить",    "callback_data": f"reject_{art_id}"},
        ]]
    }

    preview = re.sub(r'<[^>]+>', '', post_text)
    caption = f"📬 Новый пост на одобрение:\n\n{preview}"

    result = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
        json={
            "chat_id":      MODERATOR_ID,
            "photo":        image_url,
            "caption":      caption[:1024],
            "reply_markup": keyboard,
        },
        timeout=25,
    ).json()

    if not result.get("ok"):
        print(f"Фото не загрузилось, отправляю текстом")
        result = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={
                "chat_id":      MODERATOR_ID,
                "text":         caption[:4096],
                "reply_markup": keyboard,
            },
            timeout=10,
        ).json()

    if not result.get("ok"):
        raise RuntimeError(f"Telegram ошибка: {result}")

    return result["result"]["message_id"]


def main():
    print(f"\n{'='*50}")
    print(f"Запуск — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*50}")

    try:
        posted_ids   = load_json(POSTED_FILE, [])
        pending      = load_json(PENDING_FILE, {})
        articles     = fetch_articles()
        new_articles = [a for a in articles if a["id"] not in posted_ids]
        print(f"Новых статей: {len(new_articles)}")

        if not new_articles:
            print("Нет новых статей.")
            return

        article = new_articles[0]
        print(f"Статья: {article['title'][:80]}")

        post_text, image_prompt = generate_content(article)
        print(f"Пост: {len(post_text)} символов")
        print(f"Image: {image_prompt[:60]}")

        image_url = build_image_url(image_prompt)
        msg_id    = send_for_approval(post_text, image_url, article["id"])
        print(f"✓ Отправлено модератору (msg_id={msg_id})")

        pending[article["id"]] = {
            "post_text":  post_text,
            "image_url":  image_url,
            "title":      article["title"],
            "url":        article["url"],
            "msg_id":     msg_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        posted_ids.append(article["id"])
        posted_ids = posted_ids[-500:]

        save_json(PENDING_FILE, pending)
        save_json(POSTED_FILE,  posted_ids)
        print("✓ Готово\n")

    except Exception as e:
        msg = f"❌ Ошибка:\n{type(e).__name__}: {e}"
        print(msg)
        notify_moderator(msg)
        raise

if __name__ == "__main__":
    main()
