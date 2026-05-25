# PROMPTS.md — журнал эволюции промптов

История изменений промптов и эталонных постов (few-shot) с обоснованиями.

**Принципы:**
- Каждое значимое изменение — новая версия (`v1` → `v2`)
- Старые версии остаются в БД для отката (`scripts/prompts_admin.py rollback <kind>`)
- Источник правды — `.md`-файлы в каталоге [prompts/](prompts/), они синхронизируются в БД через `seed`
- Каждый сгенерированный пост помечается `prompt_version_id` (для retro-анализа)

---

## 🧰 Команды

```bash
# Загрузить новые/изменённые промпты из prompts/ в БД (idempotent)
python scripts/prompts_admin.py seed

# Список всех версий
python scripts/prompts_admin.py list
python scripts/prompts_admin.py list generator   # только generator

# Распечатать конкретную версию
python scripts/prompts_admin.py show generator v1

# Активировать конкретную версию
python scripts/prompts_admin.py activate filter v2

# Откат на предыдущую версию (по created_at)
python scripts/prompts_admin.py rollback critic

# Список активных few-shot эталонов
python scripts/prompts_admin.py few-shot
```

---

## 📐 Структура промптов

| Kind | Назначение | Tier | JSON mode | Файл |
|------|-----------|------|-----------|------|
| `filter` | HIGH/MEDIUM/LOW классификация статей | CHEAP (mini) | ✅ | [filter_v1.md](prompts/filter_v1.md) |
| `generator` | Генерация поста + image_prompt | SMART (4o) | ✅ | [generator_v1.md](prompts/generator_v1.md) |
| `critic` | Оценка по 6 критериям | CRITIC (mini) | ✅ | [critic_v1.md](prompts/critic_v1.md) |

**Placeholders для подстановки:**

| Promp | Placeholder | Источник |
|-------|------------|----------|
| filter system | `{channel_topic}`, `{channel_niche}`, `{channel_audience}` | env-конфиг канала |
| filter user | `{title}`, `{summary}` | поля статьи |
| generator system | `{channel_topic}`, `{channel_niche}`, `{channel_audience}`, `{channel_lang}` | env |
| generator user | `{title}`, `{summary}` , `{url}`, `{rubric}`, `{lang}`, `{few_shot_examples}` | статья + ротация |
| critic user | `{post_text}` | сгенерированный пост |

---

## 📋 История

### 2026-05-25 — `filter v1` / `generator v1` / `critic v1`

**Источник:** baseline-промпты из T1.2 (filter) и T2.1-T2.3 (generator + critic).

**Зачем:** перенос из хардкод-строк в Python в версионированную БД (T2.4 — Promptops). Это даёт:
- A/B-тестирование (T2.11) — можно держать 2 версии параллельно, метить посты `prompt_version_id` и сравнивать `quality_score`
- Откат за секунду через CLI без релиза кода
- Изменение промпта без `git push` (правишь .md, `seed`, готово)

**Что внутри:**
- `filter v1` — 5 категорий HIGH/MEDIUM/LOW (запуски, скандалы, цифры — HIGH; туториалы, философия — LOW)
- `generator v1` — структура из 10 строк (хук → суть → 2 сегмента пользы → вопрос → хэштеги → ссылка), 5 эмодзи, цифра в крючке, длина 180-260 слов
- `critic v1` — 6 критериев + калибровка через эталоны 4/10 и 9/10, anti-AI-fingerprint в system

**Few-shot:** 6 эталонов с разной квалификацией по рубрикам (см. ниже).

---

## 🎯 Эталонные few-shot посты

Хранятся в [prompts/few_shot/](prompts/few_shot/). Ротация: при каждой генерации **3 случайных** примера выбираются с приоритетом по совпадению рубрики + взвешенный shuffle по `quality_score`.

| Slug | Рубрика | Quality | Заметка |
|------|---------|---------|---------|
| `launch_001` | 🚀 Запуск/Релиз | 9 | Хук с цифрой + сегментная польза. Эталон формата. |
| `scandal_001` | 🔻 Скандал | 9 | Удар хуком + конкретика бренда + угроза читателю. |
| `number_001` | 📊 Цифра дня | 9 | Цифра в крючке + bullet-список + провокация. |
| `investment_001` | 💰 Инвестиции | 9 | Деньги в хуке + конкретика + сарказм. |
| `tool_001` | 🔧 Новый инструмент | 8 | Use-case + цифры производительности. |
| `breakthrough_001` | any | 9 | Универсальный шаблон — резонансное событие + рефлексия. |

**Зачем ротация:** при 3 хардкод-эталонах модель быстро «выучивает» один стиль и копирует. Случайная подборка из пула 6+ даёт стилистическое разнообразие, без потери качества.

---

## 🔮 План на будущее

| Версия | Когда | Зачем |
|--------|-------|-------|
| `generator v2` | T2.5 | 15+ рубрик-специфичных шаблонов |
| `generator v3` | T2.11 | A/B-тест: уже-конкретный hook vs провокационный |
| `critic v2` | T2.6 | Усиление anti-hallucination проверки (cross-reference) |
| `*_en_v1` | T3.9 | Английские версии для мультиязычной поддержки |

---

## 🛠 Как добавить новую версию

1. Скопируй `prompts/generator_v1.md` → `prompts/generator_v2.md`
2. В frontmatter укажи `version: v2` и `notes:` с обоснованием изменения
3. Внеси правки в текст
4. Запусти `python scripts/prompts_admin.py seed` — новая версия активна, старая деактивирована (но сохранена)
5. Добавь раздел в этот файл с датой и обоснованием
6. Закоммить
7. Если что-то сломалось — `python scripts/prompts_admin.py rollback generator`
