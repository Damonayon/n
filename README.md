# 🤖 Нейро-Новости Бот

Автоматизированная сеть Telegram-каналов с генерацией постов через GPT-4o.

Бот:
1. Парсит свежие новости из RSS-источников
2. Фильтрует мусор (GPT-4o оценивает HIGH/MEDIUM/LOW)
3. Пишет SMM-пост с few-shot обучением на эталонных примерах
4. Создаёт уникальную картинку (Pollinations AI)
5. Отправляет модератору с кнопками ✅ / ❌
6. При одобрении публикует в канал

---

## 📁 Структура

```
.
├── scripts/
│   ├── generate_post.py     # генератор постов
│   └── check_approvals.py   # обработчик кнопок модератора
├── .github/workflows/
│   ├── generate.yml         # cron 3 раза/день
│   └── check_approvals.yml  # cron каждые 10 минут
├── data/                    # БД (пока JSON, мигрируем в SQLite)
├── ROADMAP.md               # план 4 стадий
├── PROGRESS.md              # чеклист задач
└── AUDIT.md                 # аудит кода
```

---

## 🔑 Что нужно настроить в GitHub

**Settings → Secrets and variables → Actions → Secrets:**

| Имя | Что это |
|-----|---------|
| `TELEGRAM_BOT_TOKEN` | токен бота от @BotFather |
| `TELEGRAM_MODERATOR_ID` | твой Telegram ID (получишь в @userinfobot) |
| `TELEGRAM_CHANNEL_ID` | ID или @username канала |
| `GH_MODELS_TOKEN` | Personal Access Token GitHub с правом «Models: Read-only» |

**Settings → Secrets and variables → Actions → Variables** (для конфига канала):

| Имя | Пример |
|-----|--------|
| `CHANNEL_TOPIC` | Нейро-новости |
| `CHANNEL_NICHE` | искусственный интеллект и нейросети |
| `CHANNEL_AUDIENCE` | русскоязычные, 18–45 лет, интересуются технологиями |
| `CHANNEL_LANG` | русский |
| `RSS_FEEDS` | список RSS через запятую (опционально) |

---

## 🛠 Технологии

| Компонент | Сервис | Цена |
|-----------|--------|------|
| Хостинг | GitHub Actions | бесплатно |
| ИИ | GitHub Models (GPT-4o) | бесплатно через PAT |
| Картинки | Pollinations AI (flux) | бесплатно |
| Источники | RSS-ленты | бесплатно |
| Мессенджер | Telegram Bot API | бесплатно |

---

## 📖 Документация

- **[CLAUDE.md](CLAUDE.md)** — полный контекст проекта для Claude / новых разработчиков
- **[ROADMAP.md](ROADMAP.md)** — план 4 стадий доработки
- **[PROGRESS.md](PROGRESS.md)** — чеклист 42 задач
- **[AUDIT.md](AUDIT.md)** — аудит текущего состояния кода

---

## 🚀 Запуск

В обычном режиме всё работает автоматически — GitHub Actions запускает скрипты по расписанию.

**Ручной запуск:** Actions → выбери workflow → «Run workflow».
