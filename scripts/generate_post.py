"""
generate_post.py — ФИНАЛЬНАЯ ВЕРСИЯ на Gemini
"""

import os, json, time, random, hashlib, urllib.parse, re
import requests, feedparser
from datetime import datetime, timezone

BOT_TOKEN      = os.environ["TELEGRAM_BOT_TOKEN"]
MODERATOR_ID   = os.environ["TELEGRAM_MODERATOR_ID"]
CHANNEL_ID     = os.environ["TELEGRAM_CHANNEL_ID"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-1.5-flash:generateContent?key={key}"
)

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
                # Убираем HTML-теги из summary
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


PROMPT = """Ты — главный редактор топового русскоязычного Telegram-канала «Нейро-новости» об искусственном интеллекте. У тебя 500 000 подписчиков. Твои посты — вирусные, умные, живые.

Напиши пост на основе этой новости:

ЗАГОЛОВОК: {title}
СОДЕРЖАНИЕ: {summary}
ССЫЛКА: {url}

Верни ТОЛЬКО JSON (никакого текста до или после):
{{"post": "текст поста", "image_prompt": "english prompt"}}

━━━ ТРЕБОВАНИЯ К ПОСТУ ━━━

Язык: строго русский.

СТРУКТУРА:
1. КРЮЧОК (1 строка) — останавливает скролл. Примеры стиля:
   • «ИИ только что сделал то, что юристы делали годами — за 4 секунды ⚡»
   • «Google это скрывал. Теперь скрывать нечего 🔓»
   • «Твоя профессия в списке. Проверь 👇»

2. (пустая строка)

3. СУТЬ (3-4 предложения) — что произошло простым языком. Конкретные цифры и факты. Никакого жаргона. Как рассказываешь другу.

4. (пустая строка)

5. ПОЧЕМУ ЭТО ВАЖНО ТЕБЕ (2-3 предложения) — конкретно: «Если ты работаешь с текстом...», «Для предпринимателей это значит...». Живо и лично.

6. (пустая строка)

7. ВОПРОС или ТЕЗИС (1 строка) — провоцирует обсуждение в комментариях.

8. (пустая строка)

9. #ИИ #нейросети #тематический_хештег

10. (пустая строка)

11. <a href="{url}">📖 Читать полностью</a>

ФОРМАТИРОВАНИЕ:
• <b>жирный</b> — ровно 2-3 раза, только для самых важных слов
• Эмодзи: 5-7 штук, уместно по тексту, не подряд
• Длина: 180-250 слов — не больше, не меньше
• Запрещено использовать: «революция», «прорыв», «невероятный», «уникальный», «потрясающий»
• Тон: умный, живой, с характером — как лучшие российские tech-медиа

━━━ ТРЕБОВАНИЯ К IMAGE PROMPT ━━━
• Английский язык
• Абстрактная концептуальная иллюстрация к теме новости
• Стиль: cinematic concept art, dark background, neon glow, ultra detailed, 8k
• Строго: NO humans, NO faces, NO people, NO text, NO letters
• Пример хорошего промпта: «glowing AI processor dark space electric blue neon circuits cinematic 8k ultra detailed»
• Длина: до 120 символов

Верни ТОЛЬКО валидный JSON."""


def call_gemini(prompt, attempt=0):
    """Прямой вызов Gemini API через requests."""
    url  = GEMINI_URL.format(key=GEMINI_API_KEY)
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature":     0.8,
            "maxOutputTokens": 1500,
            "topP":            0.95,
        },
    }
    resp = requests.post(url, json=body, timeout=60)

    if resp.status_code == 200:
        data = resp.json()
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    elif resp.status_code == 429:
        wait = 30 * (attempt + 1)
        print(f"Rate limit от Gemini, жду {wait}с...")
        time.sleep(wait)
        if attempt < 3:
            return call_gemini(prompt, attempt + 1)
        raise RuntimeError("Gemini rate limit исчерпан")
    else:
        raise RuntimeError(f"Gemini вернул {resp.status_code}: {resp.text[:300]}")


def parse_json_response(raw):
    """Надёжный парсинг JSON из ответа Gemini."""
    clean = raw.strip()

    # Убираем markdown-блоки если есть
    if "```" in clean:
        parts = clean.split("```")
        for part in parts:
            part = part.strip()
            if part.startswith("json"):
                part = part[4:]
            if part.strip().startswith("{"):
                clean = part.strip()
                break

    data = json.loads(clean)
    post_text    = data.get("post", "").strip()
    image_prompt = data.get("image_prompt", "").strip()

    if not post_text:
        raise ValueError("Пустое поле post")

    # Проверяем что текст на русском
    ru_chars = sum(1 for c in post_text if '\u0400' <= c <= '\u04FF')
    if ru_chars < 30:
        raise ValueError(f"Пост не на русском (русских букв: {ru_chars})")

    if not image_prompt:
        image_prompt = "AI neural network dark space neon glow cinematic 8k"

    return post_text, image_prompt


def generate_content(article):
    prompt = PROMPT.format(**article)

    for attempt in range(3):
        try:
            raw = call_gemini(prompt)
            post_text, image_prompt = parse_json_response(raw)
            print(f"✓ Пост сгенерирован: {len(post_text)} символов")
            return post_text, image_prompt
        except (json.JSONDecodeError, ValueError, KeyError) as e:
            print(f"Попытка {attempt+1}: ошибка парсинга — {e}")
            time.sleep(5)

    raise RuntimeError("Не удалось получить корректный пост от Gemini")


def build_image_url(image_prompt):
    """Квадрат 1080x1080 — стандарт для Telegram."""
    seed    = random.randint(10000, 99999)
    full    = f"{image_prompt}, NO humans, NO faces, NO text, NO letters, abstract only"
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

    # Для превью модератору убираем HTML-теги
    clean_preview = re.sub(r'<[^>]+>', '', post_text)
    caption = f"📬 Новый пост на одобрение:\n\n{clean_preview}"

    # Сначала пробуем с картинкой
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

    # Если картинка не загрузилась — отправляем текстом
    if not result.get("ok"):
        print(f"Фото не загрузилось ({result.get('description')}), отправляю текстом")
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
    print(f"Запуск генерации — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*50}")

    try:
        posted_ids   = load_json(POSTED_FILE, [])
        pending      = load_json(PENDING_FILE, {})
        articles     = fetch_articles()
        new_articles = [a for a in articles if a["id"] not in posted_ids]
        print(f"Новых статей: {len(new_articles)}")

        if not new_articles:
            print("Нет новых статей — выходим.")
            return

        article = new_articles[0]
        print(f"Обрабатываем: {article['title'][:80]}")

        post_text, image_prompt = generate_content(article)
        print(f"Image prompt: {image_prompt}")

        image_url = build_image_url(image_prompt)
        print(f"Image URL готов")

        msg_id = send_for_approval(post_text, image_url, article["id"])
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
        print("✓ Данные сохранены")
        print("✓ ГОТОВО\n")

    except Exception as e:
        msg = f"❌ Ошибка генерации поста:\n{type(e).__name__}: {e}"
        print(msg)
        notify_moderator(msg)
        raise

if __name__ == "__main__":
    main()
