"""
generate_post.py — СТАБИЛЬНАЯ ВЕРСИЯ с надёжными моделями
"""

import os, json, time, random, hashlib, urllib.parse, re
import requests, feedparser
from datetime import datetime, timezone

BOT_TOKEN      = os.environ["TELEGRAM_BOT_TOKEN"]
MODERATOR_ID   = os.environ["TELEGRAM_MODERATOR_ID"]
CHANNEL_ID     = os.environ["TELEGRAM_CHANNEL_ID"]
OPENROUTER_KEY = os.environ["OPENROUTER_KEY"]

# Конкретные надёжные модели — не случайный роутер
MODELS = [
    "meta-llama/llama-3.3-70b-instruct:free",
    "deepseek/deepseek-r1:free",
    "mistralai/mistral-small-3.1-24b-instruct:free",
]

RSS_FEEDS = [
    "https://habr.com/ru/rss/hub/artificial_intelligence/all/",
    "https://habr.com/ru/rss/hub/machine_learning/all/",
    "https://techcrunch.com/category/artificial-intelligence/feed/",
    "https://venturebeat.com/category/ai/feed/",
    "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",
    "https://openai.com/blog/rss/",
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
                articles.append({
                    "id":      article_id(url),
                    "title":   entry.get("title", "").strip(),
                    "url":     url,
                    "summary": entry.get("summary", "")[:600].strip(),
                })
        except Exception as e:
            print(f"Ошибка {feed_url}: {e}")
    print(f"Статей из RSS: {len(articles)}")
    return articles


# Системный промпт отдельно от пользовательского — так модели лучше понимают роль
SYSTEM_PROMPT = """Ты — редактор русскоязычного Telegram-канала об искусственном интеллекте «Нейро-новости».
Ты пишешь цепляющие посты на русском языке для широкой аудитории.
Отвечай ТОЛЬКО валидным JSON. Никаких объяснений, никакого текста вне JSON."""

USER_PROMPT = """Напиши Telegram-пост про эту новость об ИИ.

НОВОСТЬ:
{title}

{summary}

Ссылка: {url}

Верни JSON в точно таком формате:
{{
  "post": "текст поста",
  "image_prompt": "image description in english"
}}

Правила для "post":
- Язык: только русский
- Первая строка: короткий крючок (факт или вопрос, который останавливает скролл)
- Затем пустая строка
- 3-4 предложения: суть новости простым языком + почему это важно
- Затем пустая строка  
- Острый вопрос или тезис для обсуждения
- Затем пустая строка
- Хештеги: #ИИ #нейросети и один тематический
- Последняя строка: <a href="{url}">📖 Читать полностью</a>
- Форматирование: <b>жирный</b> для 2-3 ключевых слов
- Эмодзи: 4-6 штук уместно
- Длина: 150-220 слов
- НЕ используй слова: революция, прорыв, невероятный

Правила для "image_prompt":
- Английский язык
- Абстрактная концептуальная иллюстрация к теме (NO humans, NO faces, NO people, NO text)
- Стиль: cinematic concept art, dark background, neon glow, 8k
- Пример: "glowing neural network nodes dark space blue purple light cinematic 8k"
- Максимум 100 символов"""


def call_model(model, prompt):
    response = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {OPENROUTER_KEY}",
            "Content-Type":  "application/json",
        },
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user",   "content": prompt},
            ],
            "max_tokens":  1200,
            "temperature": 0.75,
        },
        timeout=60,
    )
    return response.status_code, response


def call_ai(prompt):
    for model in MODELS:
        print(f"Пробую: {model}")
        for attempt in range(3):
            try:
                status, response = call_model(model, prompt)
                if status == 200:
                    content = response.json()["choices"][0]["message"]["content"].strip()
                    print(f"✓ Ответ получен от {model}")
                    return content
                elif status == 429:
                    wait = 15 * (attempt + 1)
                    print(f"  Rate limit, жду {wait}с...")
                    time.sleep(wait)
                elif status == 404:
                    print(f"  Модель недоступна, перехожу к следующей")
                    break
                else:
                    print(f"  Ошибка {status}, жду 5с...")
                    time.sleep(5)
            except Exception as e:
                print(f"  Исключение: {e}, жду 5с...")
                time.sleep(5)

    raise RuntimeError("Все модели недоступны")


def parse_response(raw):
    """Надёжный парсинг JSON из ответа модели."""
    clean = raw.strip()

    # Убираем markdown-блоки
    if "```" in clean:
        parts = clean.split("```")
        for part in parts:
            if "{" in part:
                clean = part
                if clean.startswith("json"):
                    clean = clean[4:]
                break

    # Пробуем распарсить
    try:
        data = json.loads(clean)
        post_text    = data.get("post", "").strip()
        image_prompt = data.get("image_prompt", "").strip()

        # Проверяем что пост на русском (хотя бы частично)
        russian_chars = sum(1 for c in post_text if '\u0400' <= c <= '\u04FF')
        if russian_chars < 20:
            raise ValueError(f"Пост не на русском (русских символов: {russian_chars})")

        if not image_prompt:
            image_prompt = "AI technology abstract dark neon network 8k cinematic"

        return post_text, image_prompt

    except (json.JSONDecodeError, ValueError) as e:
        raise ValueError(f"Не удалось распарсить ответ: {e}\nОтвет: {raw[:300]}")


def generate_content(article):
    prompt = USER_PROMPT.format(**article)

    # Пробуем до 3 раз (разные модели могут дать плохой ответ)
    last_error = None
    for attempt in range(3):
        raw = call_ai(prompt)
        try:
            return parse_response(raw)
        except ValueError as e:
            print(f"Попытка {attempt+1}: {e}")
            last_error = e
            time.sleep(3)

    raise RuntimeError(f"Не удалось получить корректный пост: {last_error}")


def build_image_url(image_prompt):
    """1080x1080 — квадратный формат, идеален для Telegram"""
    seed    = random.randint(10000, 99999)
    # Добавляем жёсткий запрет на лица
    full    = image_prompt + ", NO humans, NO faces, NO people, NO text, abstract only"
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

    # Чистый текст для превью (без HTML-тегов)
    clean = re.sub(r'<[^>]+>', '', post_text)
    caption = f"📬 Новый пост:\n\n{clean}"

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
    print(f"\n=== Запуск {datetime.now().strftime('%Y-%m-%d %H:%M')} ===")
    try:
        posted_ids   = load_json(POSTED_FILE, [])
        pending      = load_json(PENDING_FILE, {})
        articles     = fetch_articles()
        new_articles = [a for a in articles if a["id"] not in posted_ids]
        print(f"Новых: {len(new_articles)}")

        if not new_articles:
            print("Нет новых статей.")
            return

        article = new_articles[0]
        print(f"Статья: {article['title'][:80]}")

        post_text, image_prompt = generate_content(article)
        print(f"Пост: {len(post_text)} симв. | Image: {image_prompt[:60]}")

        image_url = build_image_url(image_prompt)
        msg_id    = send_for_approval(post_text, image_url, article["id"])
        print(f"Отправлено, msg_id={msg_id}")

        pending[article["id"]] = {
            "post_text":   post_text,
            "image_url":   image_url,
            "title":       article["title"],
            "url":         article["url"],
            "msg_id":      msg_id,
            "created_at":  datetime.now(timezone.utc).isoformat(),
        }
        posted_ids.append(article["id"])
        posted_ids = posted_ids[-500:]
        save_json(PENDING_FILE, pending)
        save_json(POSTED_FILE, posted_ids)
        print("=== Готово ===\n")

    except Exception as e:
        msg = f"❌ Ошибка генерации:\n{type(e).__name__}: {e}"
        print(msg)
        notify_moderator(msg)
        raise

if __name__ == "__main__":
    main()
