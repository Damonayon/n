"""bot.rubrics — каталог 16 динамических рубрик постов (T2.5).

Каждая рубрика — это не просто тег. Это:
  - tone-of-voice (как писать)
  - structure_hint (какая структура нужна)
  - cta_style (какой вопрос/призыв в конце)

Эти подсказки подмешиваются в промпт генератора, чтобы пост был стилистически
точным под тип контента. AI-классификатор (см. classify_rubric) выбирает рубрику
по тексту статьи; балансировщик (recent_rubrics + apply_anti_repeat) не даёт
одной рубрике появляться подряд.

Принципы:
- DEFAULT_RUBRIC — безопасный fallback при низкой уверенности классификатора
- LOW_CONFIDENCE_THRESHOLD — ниже которого → fallback
- Каждая рубрика имеет уникальный emoji-префикс для визуального отличия в логах
"""

from __future__ import annotations

from dataclasses import dataclass

# ─── DTO ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Rubric:
    """Определение рубрики поста."""

    slug: str  # стабильный ID для БД (en_lower_snake_case)
    name: str  # человекочитаемое имя с emoji (для рубрики в Post)
    description: str  # что это за рубрика (для AI-классификатора)
    tone: str  # tone-of-voice инструкция (для генератора)
    structure_hint: str  # какая структура нужна (для генератора)
    cta_style: str  # тип CTA в конце поста
    triggers: tuple[str, ...]  # ключевые слова для backup-эвристики (если AI лежит)


# ─── Каталог 16 рубрик ───────────────────────────────────────────────────────
# Порядок — по приоритету в эвристике (запуски встречаются часто, breaking редко).


RUBRICS: dict[str, Rubric] = {
    "launch": Rubric(
        slug="launch",
        name="🚀 Запуск/Релиз",
        description="Релиз нового продукта/инструмента/функции с реальной датой и доступом",
        tone="excited, energetic — это событие, не описание",
        structure_hint="Хук с цифрой (пользователи / время с релиза) → что это → польза → доступ",
        cta_style="Призыв попробовать: «попробуешь?», «забираешь в работу?»",
        triggers=("launch", "release", "запуск", "релиз", "выпустил", "представил", "анонс"),
    ),
    "scandal": Rubric(
        slug="scandal",
        name="🔻 Скандал",
        description="Увольнение, конфликт, разоблачение, корпоративный кризис",
        tone="dramatic, sharp — без злорадства, но с напряжением",
        structure_hint="Хук-удар (кто кого) → факты Bloomberg/FT → угроза читателю → острый вопрос",
        cta_style="Провокация: «доверил бы X?», «согласен с CEO?»",
        triggers=("уволил", "fired", "laid off", "сократил", "закрыл", "scandal", "скандал"),
    ),
    "number_of_day": Rubric(
        slug="number_of_day",
        name="📊 Цифра дня",
        description="Исследование с конкретными цифрами/процентами, опрос, отчёт",
        tone="analytical, factual — цифры говорят сами",
        structure_hint="Цифра в крючке → 3 bullet факта → инсайт → вопрос-зеркало",
        cta_style="«Ты в этих X%?», «это тебя удивляет?»",
        triggers=("%", "percent", "study", "research", "исследование", "опрос", "report"),
    ),
    "case": Rubric(
        slug="case",
        name="💡 Кейс",
        description="История применения с конкретным результатом, до/после, измеримый эффект",
        tone="practical, business-like",
        structure_hint="Хук с метрикой → что сделали → результат в цифрах → что значит для тебя",
        cta_style="«как бы применил?», «есть похожий опыт?»",
        triggers=("case", "кейс", "case study", "результат", "x10", "boosted"),
    ),
    "inside": Rubric(
        slug="inside",
        name="🕵 Инсайд",
        description="Слив, утечка, внутренняя информация, неофициальный источник",
        tone="confidential, intriguing — «нам стало известно»",
        structure_hint="Хук-загадка → источник (с осторожностью) → что значит → что дальше",
        cta_style="«знал?», «ждал такого поворота?»",
        triggers=("leak", "leaked", "слив", "утечка", "insider", "инсайд"),
    ),
    "trend": Rubric(
        slug="trend",
        name="📈 Тренд",
        description="Описание набирающего обороты явления, рост направления",
        tone="analytical with momentum",
        structure_hint="Хук с динамикой (X→Y за месяц) → 2-3 примера → прогноз → польза",
        cta_style="«оседлаешь?», «видишь у себя?»",
        triggers=("trend", "тренд", "rising", "набирает", "popular"),
    ),
    "forecast": Rubric(
        slug="forecast",
        name="🔮 Прогноз",
        description="Предсказание на будущее, экспертная оценка перспектив",
        tone="speculative but grounded",
        structure_hint="Кто прогнозирует → что → к какому сроку → аргументы → что готовить",
        cta_style="«согласен?», «как готовишься?»",
        triggers=("forecast", "прогноз", "predict", "ожидается", "к 2030"),
    ),
    "comparison": Rubric(
        slug="comparison",
        name="⚖ Сравнение",
        description="Сравнение продуктов/подходов с конкретными метриками",
        tone="neutral, evidence-based",
        structure_hint="Контекст выбора → 3 параметра сравнения → вердикт → когда какой",
        cta_style="«какой выберешь?», «какой пробовал?»",
        triggers=("vs", "сравнение", "лучше", "compared", "benchmark"),
    ),
    "mistakes": Rubric(
        slug="mistakes",
        name="❌ Ошибки",
        description="Типичные провалы, антипаттерны, как НЕ надо делать",
        tone="educational, slightly cautionary",
        structure_hint="Хук-провал → 3 типичные ошибки → как избегать → проверь себя",
        cta_style="«узнал себя?», «делал так?»",
        triggers=("mistake", "ошибк", "fail", "провал", "anti-pattern"),
    ),
    "lifehack": Rubric(
        slug="lifehack",
        name="🪄 Лайфхак",
        description="Конкретный полезный приём, который читатель может применить сегодня",
        tone="helpful, hands-on",
        structure_hint="Хук-обещание → 3-4 шага → результат → попробуй прямо сейчас",
        cta_style="«сохрани в избранное», «попробуй сегодня»",
        triggers=("лайфхак", "tip", "trick", "хитрость", "приём"),
    ),
    "opinion": Rubric(
        slug="opinion",
        name="💬 Мнение",
        description="Личная позиция автора, спорный тезис, провокация дискуссии",
        tone="personal, opinionated but argued",
        structure_hint="Тезис → 2-3 аргумента → контр-аргумент → честный вопрос",
        cta_style="«согласен?», «в чём не прав?»",
        triggers=("opinion", "мнение", "считаю", "уверен", "I think"),
    ),
    "story": Rubric(
        slug="story",
        name="📚 История",
        description="Нарратив: предыстория события, как X пришёл к Y",
        tone="narrative, with story arc",
        structure_hint="Завязка (год, место) → поворот → результат → урок",
        cta_style="«знал такое?», «удивлён?»",
        triggers=("история", "история о", "как стало", "story", "история"),
    ),
    "research": Rubric(
        slug="research",
        name="🧪 Исследование",
        description="Научная работа, академическое исследование, peer-reviewed paper",
        tone="scientific but accessible",
        structure_hint="Кто исследовал → что нашли → методология (кратко) → что это меняет",
        cta_style="«удивлён результатом?», «доверяешь?»",
        triggers=("research", "study", "paper", "исследование", "Стэнфорд", "MIT"),
    ),
    "interview": Rubric(
        slug="interview",
        name="🎤 Интервью",
        description="Цитата от CEO/основателя/эксперта, фрагмент интервью",
        tone="curious, with reverence to source",
        structure_hint="Цитата как хук → кто это → контекст → разбор смысла",
        cta_style="«что думаешь о его позиции?»",
        triggers=("интервью", "interview", "сказал", "заявил", "told"),
    ),
    "review": Rubric(
        slug="review",
        name="🔍 Обзор",
        description="Детальный разбор продукта/функции с плюсами и минусами",
        tone="detailed, balanced",
        structure_hint="Что обозреваем → 3 плюса → 3 минуса → кому подходит",
        cta_style="«нужен тебе?», «закроет твою задачу?»",
        triggers=("обзор", "review", "разбор", "детально"),
    ),
    "breaking": Rubric(
        slug="breaking",
        name="🔥 Breaking",
        description="Только что произошло, важная новость в реальном времени",
        tone="urgent but not panicked",
        structure_hint="🔥 + цифра/имя → что произошло → пока известно мало → что дальше",
        cta_style="«следим вместе?», «успел увидеть?»",
        triggers=("breaking", "срочно", "только что", "just announced"),
    ),
    "investment": Rubric(
        slug="investment",
        name="💰 Инвестиции",
        description="Раунд финансирования, IPO, оценка компании, движение капитала",
        tone="financial, with implications",
        structure_hint="Сумма в крючке → кто/у кого → во что пойдёт → что для рынка",
        cta_style="«кто следующий?», «как смотришь на оценку?»",
        triggers=(
            "billion",
            "million",
            "raised",
            "funding",
            "ipo",
            "млрд",
            "млн",
            "инвест",
            "раунд",
        ),
    ),
}


# ─── Константы ───────────────────────────────────────────────────────────────


# Безопасный fallback, когда классификатор не уверен или ничего не нашёл.
# 'case' выбран потому что подходит к большинству деловых новостей.
DEFAULT_RUBRIC = RUBRICS["case"]

# Если confidence классификатора ниже — используем DEFAULT_RUBRIC.
LOW_CONFIDENCE_THRESHOLD = 0.5

# Сколько последних постов учитываем для «не повторять подряд».
RECENT_HISTORY_SIZE = 5


# ─── Утилиты ─────────────────────────────────────────────────────────────────


def by_slug(slug: str) -> Rubric:
    """Найти рубрику по slug. Бросает KeyError если не найдена."""
    return RUBRICS[slug]


def by_name(name: str) -> Rubric | None:
    """Найти рубрику по человекочитаемому имени (содержащее emoji)."""
    for r in RUBRICS.values():
        if r.name == name:
            return r
    return None


def list_all() -> list[Rubric]:
    return list(RUBRICS.values())


def slugs() -> list[str]:
    return list(RUBRICS.keys())


def heuristic_detect(text: str) -> str | None:
    """Резервная классификация по ключевым словам (если AI лежит).

    Возвращает slug рубрики или None.
    """
    text_lower = text.lower()
    for rubric in RUBRICS.values():
        if any(t in text_lower for t in rubric.triggers):
            return rubric.slug
    return None


def apply_anti_repeat(
    primary_slug: str,
    fallback_slug: str,
    recent_slugs: list[str],
    max_repeats_in_row: int = 2,
) -> str:
    """Балансировщик: если primary рубрика встречалась N раз подряд недавно —
    переключаемся на fallback. Защищает канал от монотонности.

    Пример: новостей про запуски много, но 3 поста подряд про «🚀 Запуск» — скучно.
    """
    if not recent_slugs:
        return primary_slug
    tail = recent_slugs[-max_repeats_in_row:]
    if len(tail) >= max_repeats_in_row and all(s == primary_slug for s in tail):
        if fallback_slug != primary_slug:
            return fallback_slug
        return DEFAULT_RUBRIC.slug
    return primary_slug
