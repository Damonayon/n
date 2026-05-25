# ARCHITECTURE.md — Архитектура «Нейро-Новости Бот»

Документ для нового разработчика / форка: что из чего состоит, как данные текут, где точки отказа.

---

## 🎯 Назначение системы

Автоматизированная сеть Telegram-каналов. На каждый канал — отдельный fork репозитория с теми же скриптами, но разной конфигурацией (`CHANNEL_TOPIC`, `RSS_FEEDS`, …). Бот:

1. Парсит RSS
2. Оценивает каждую статью (`HIGH/MEDIUM/LOW`) через GPT-4o
3. Генерирует SMM-пост с few-shot обучением + картинку
4. Отправляет модератору с кнопками ✅ / ❌
5. После одобрения публикует в канал

---

## 🗺 Высокоуровневая схема

```mermaid
flowchart LR
    subgraph "GitHub Actions (cron)"
        G[📝 generate.yml<br/>3×/день]
        C[✅ check_approvals.yml<br/>каждые 10 мин]
        H[🩺 health.yml<br/>каждые 30 мин]
        B[💾 backup.yml<br/>раз в сутки]
        S[🔒 security.yml<br/>при push]
        I[🧪 ci.yml<br/>при push/PR]
    end

    subgraph "Python"
        GP[generate_post.py]
        CP[check_approvals.py]
        HP[health_check.py]
        BP[backup_db.py]
    end

    subgraph "Хранилище"
        DB[(SQLite<br/>data/bot.db)]
        BR[(orphan-ветка<br/>backups)]
    end

    subgraph "Внешние сервисы"
        RSS[(RSS-фиды)]
        GH[GitHub Models<br/>GPT-4o]
        PA[Pollinations AI]
        TG[Telegram Bot API]
        SE[Sentry<br/>опц.]
    end

    G --> GP
    C --> CP
    H --> HP
    B --> BP

    GP --> RSS
    GP --> GH
    GP --> PA
    GP --> TG
    GP --> DB

    CP --> TG
    CP --> DB

    HP --> TG
    HP --> DB
    HP --> GH
    HP --> RSS

    BP --> DB
    BP --> BR

    GP --> SE
    CP --> SE
    HP --> SE
```

---

## 🧱 Слои кода

```mermaid
flowchart TB
    subgraph "scripts/  (точки входа)"
        s1[generate_post.py]
        s2[check_approvals.py]
        s3[health_check.py]
        s4[backup_db.py]
        s5[migrate_json_to_db.py]
        s6[status.py]
    end

    subgraph "bot/  (переиспользуемая логика)"
        cfg[config.py<br/>pydantic-settings]
        models[models.py<br/>ORM]
        db[db.py<br/>engine + session]
        storage[storage.py<br/>высокоуровневые операции]
        http[http.py<br/>retry + CB + deadline]
        logsetup[logging_setup.py<br/>stdout + DB + Telegram + Sentry]
        utils[utils.py<br/>canonicalize_url, file_id]
    end

    s1 --> http
    s1 --> storage
    s1 --> logsetup
    s2 --> http
    s2 --> storage
    s2 --> logsetup
    s3 --> http
    s3 --> storage
    s3 --> logsetup
    s4 --> logsetup
    s6 --> storage

    storage --> models
    storage --> db
    storage --> utils
    db --> models
    db --> cfg
    logsetup --> cfg
    http --> logsetup
```

**Правило слоёв:**
- `scripts/*.py` — оркестрация. Никакого SQL.
- `bot/*.py` — переиспользуемая логика. Не должна знать о Telegram-специфике.
- Скрипты НЕ ходят в ORM напрямую — только через `bot.storage`.

---

## 🌊 Жизненный цикл поста

```mermaid
sequenceDiagram
    participant Cron as cron 3×/день
    participant GP as generate_post.py
    participant RSS
    participant GPT as GPT-4o
    participant DB
    participant TG as Telegram (модератор)
    participant CP as check_approvals.py
    participant Ch as Telegram (канал)

    Cron->>GP: запуск
    GP->>RSS: GET feeds (retry + UA)
    RSS-->>GP: статьи
    GP->>DB: known_article_hashes (фильтр дублей)
    GP->>GPT: filter_article (HIGH/MEDIUM/LOW)
    GPT-->>GP: оценка
    GP->>DB: save_article(quality=...)
    Note over GP: выбирает первую HIGH или first MEDIUM
    GP->>GPT: generate_post (few-shot)
    GPT-->>GP: post + image_prompt
    GP->>TG: sendPhoto + inline keyboard
    TG-->>GP: msg_id + file_id (!)
    GP->>DB: create_pending_post(file_id=...)

    Note over CP: cron каждые 10 минут
    Cron->>CP: запуск
    CP->>TG: getUpdates(offset)
    TG-->>CP: callback_query ✅/❌
    alt approve
        CP->>DB: get_pending_by_article_hash
        CP->>Ch: sendPhoto(file_id!) + текст
        CP->>DB: mark_published
        CP->>TG: editMessage (убрать кнопки)
    else reject
        CP->>DB: mark_rejected
        CP->>TG: editMessage
    end
```

**Ключевые гарантии:**
- Картинка идёт по **Telegram file_id**, а не URL. Если Pollinations лежит — пост всё равно опубликуется (см. T1.5).
- URL-канонизация: одна статья = один hash, даже с разными UTM-метками (см. `bot.utils.canonicalize_url`).
- Retry + circuit breaker: на любые сетевые сбои; общий дедлайн 5 минут на пайплайн (см. `bot/http.py`).

---

## 🗃 Схема БД

```mermaid
erDiagram
    channels ||--o{ articles : "1:N"
    channels ||--o{ posts : "1:N"
    articles ||--o{ posts : "1:N"
    posts ||--o{ metrics : "1:N"
    prompts ||--o{ posts : "версия промпта"

    channels {
        int id PK
        string slug UK
        string topic
        text niche
        text audience
        string language
        bool is_active
    }
    articles {
        int id PK
        int channel_id FK
        string article_hash
        text url
        text title
        text summary
        string quality
        string rubric
        datetime discovered_at
    }
    posts {
        int id PK
        int article_id FK
        int channel_id FK
        text post_text
        text image_url
        string image_file_id
        bigint moderator_msg_id
        string status "pending|published|rejected|failed"
        datetime created_at
        datetime decided_at
        datetime published_at
    }
    metrics {
        int id PK
        int post_id FK
        int views
        int forwards
        text reactions_json
    }
    prompts {
        int id PK
        string kind "filter|generator|critic"
        string version
        text system_prompt
        text user_template
        bool is_active
    }
    logs {
        int id PK
        string level
        string event
        text message
        text payload_json
        datetime created_at
    }
    system_state {
        string key PK
        text value
    }
```

7 таблиц + `alembic_version` (служебная). Миграции — через Alembic, см. [RECOVERY.md](RECOVERY.md).

---

## 🩺 Точки отказа и компенсации

| Точка | Что может сломаться | Что мы делаем |
|-------|---------------------|---------------|
| GitHub Models | Rate limit / 5xx | `bot.http` ретраит с backoff; fallback на gpt-4o-mini |
| Pollinations AI | Картинка не сгенерилась | Retry; если совсем нет — пост идёт текстом |
| Telegram API | Сеть / лимиты | Retry + circuit breaker (5 ошибок → 5 мин блок) |
| RSS-сервер | Недоступен | Skip этого фида; health-check алертит если упало ≥50% |
| БД | Файл повреждён | Бэкап из orphan-ветки `backups` (см. RECOVERY.md) |
| Долго висящий pending | Модератор не нажал кнопку | health-check каждые 30 мин чистит >48ч → `FAILED` |
| Утечка секретов в коммит | — | gitleaks + GitHub secret-scanning + pre-commit detect-private-key |
| Уязвимость в зависимостях | — | pip-audit + Dependabot weekly |

---

## 🔁 Workflow-cron'ы

| Workflow | Расписание | Concurrency group | Что делает |
|----------|------------|-------------------|------------|
| `generate.yml` | `0 7,13,19 * * *` | `data-write` | Генерация поста |
| `check_approvals.yml` | `*/10 * * * *` | `data-write` | Публикация после одобрения |
| `health.yml` | `*/30 * * * *` | `data-write` | Самопроверка + cleanup |
| `backup.yml` | `30 3 * * *` | `backup` | Снимок БД в orphan-ветку |
| `ci.yml` | при push/PR | `ci-{ref}` | Lint + types + tests |
| `security.yml` | при push + еженедельно | (нет) | gitleaks + pip-audit |

**Concurrency `data-write`** общая у трёх первых — потому что все три пишут в `data/bot.db` и одновременный коммит сломал бы git.

---

## 🌐 Масштабирование на сеть каналов

Текущая архитектура — **per-channel SQLite в репо**. Для 1–15 каналов работает идеально (один файл БД на канал, нет внешних зависимостей).

Для 15+ каналов планируется (Стадия 3):
- Миграция на **Supabase Postgres** (one DB, multi-tenant по `channel_id`)
- Веб-админка модерации вместо личных сообщений
- Единый дашборд метрик
- Cross-posting между каналами

Сейчас замена БД-движка делается **одной env-переменной**:
```
DB_URL=postgresql://...
```

Потому что `bot.storage` ничего не знает о конкретной СУБД, а `bot.config` берёт URL из окружения. SQLAlchemy + Alembic поддерживают обе.

---

## 📦 Зависимости (production)

| Пакет | Версия | Назначение |
|-------|--------|-----------|
| `feedparser` | ≥6.0.11 | Парсинг RSS |
| `requests` | ≥2.32.0 | HTTP (через `bot.http`) |
| `SQLAlchemy` | ≥2.0.30 | ORM, нативный 2.0-стиль |
| `alembic` | ≥1.13.0 | Миграции БД |
| `pydantic` | ≥2.7.0 | Валидация конфига |
| `pydantic-settings` | ≥2.3.0 | env → объект Settings |
| `sentry-sdk` | ≥2.10.0 | Опциональная агрегация ошибок |
| `tenacity` | ≥8.5.0 | Retry с exponential backoff |

Dev-зависимости (для CI и pre-commit): `pytest`, `pytest-cov`, `ruff`, `black`, `mypy`, `types-requests`.

---

## 📍 Где что лежит

```
.
├── bot/                    переиспользуемая логика
│   ├── config.py           pydantic-settings: валидация env
│   ├── models.py           ORM-схема (7 таблиц)
│   ├── db.py               engine, session_scope, init_db с auto-alembic-stamp
│   ├── storage.py          высокоуровневые операции (save_article и т.д.)
│   ├── http.py             retry + circuit breaker + deadline (tenacity)
│   ├── logging_setup.py    stdout + БД + Telegram-alert + Sentry
│   └── utils.py            canonicalize_url, best_telegram_file_id
├── scripts/                точки входа (запускаются cron'ом)
│   ├── generate_post.py    основной генератор постов
│   ├── check_approvals.py  публикация после одобрения
│   ├── health_check.py     самопроверка + cleanup
│   ├── backup_db.py        снимок БД
│   ├── migrate_json_to_db.py одноразовая миграция со старой версии
│   └── status.py           CLI для просмотра состояния
├── migrations/             Alembic-миграции
├── tests/                  pytest (66 тестов, core coverage ~88%)
├── data/
│   └── bot.db              SQLite, единственное файловое состояние
├── .github/
│   ├── workflows/          generate, check, health, backup, ci, security
│   ├── dependabot.yml      weekly auto-PR с обновлениями
│   └── gitleaks.toml       allowlist для .env.example
├── alembic.ini             Alembic-конфиг
├── pyproject.toml          black/ruff/mypy/pytest
├── .pre-commit-config.yaml локальные pre-commit hooks
├── requirements.txt        production-зависимости
├── .env.example            шаблон env-переменных для local dev
├── ROADMAP.md              план 4 стадий
├── PROGRESS.md             чеклист 43 задач
├── AUDIT.md                аудит исходного кода
├── ARCHITECTURE.md         этот файл
├── SECURITY.md             политика безопасности
└── RECOVERY.md             восстановление БД + Alembic
```

---

## 🚀 Запуск с нуля (новый канал)

1. Форк репозитория → новое имя.
2. GitHub → Settings → Secrets: добавить `TELEGRAM_BOT_TOKEN`, `TELEGRAM_MODERATOR_ID`, `TELEGRAM_CHANNEL_ID`, `GH_MODELS_TOKEN`.
3. (Опционально) GitHub → Settings → Variables: задать `CHANNEL_TOPIC`, `CHANNEL_NICHE`, `CHANNEL_AUDIENCE`, `RSS_FEEDS`.
4. Actions → **«📝 Генерация поста»** → Run workflow.
5. Первый запуск создаст пустую БД через `init_db()` + `alembic stamp head`.
6. Готово.

Для локальной разработки: `cp .env.example .env`, заполнить, `pip install -r requirements.txt`, `python scripts/status.py`.
