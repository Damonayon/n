"""scripts/prompts_admin.py — CLI для управления промптами и few-shot.

Команды:
    python scripts/prompts_admin.py seed
        — загрузить все .md из prompts/ в БД (idempotent upsert)

    python scripts/prompts_admin.py list [kind]
        — показать все версии (опционально фильтр по kind)

    python scripts/prompts_admin.py show <kind> <version>
        — распечатать содержимое конкретной версии

    python scripts/prompts_admin.py activate <kind> <version>
        — переключить активную версию

    python scripts/prompts_admin.py rollback <kind>
        — откатить на предыдущую версию данного kind

    python scripts/prompts_admin.py few-shot
        — показать активные few-shot примеры с разбивкой по рубрикам
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import select  # noqa: E402

from bot.db import init_db, session_scope  # noqa: E402
from bot.logging_setup import setup_logging  # noqa: E402
from bot.models import FewShotExample  # noqa: E402
from bot.prompts import (  # noqa: E402
    list_versions,
    rollback,
    seed_all_from_files,
    set_active_version,
)


def cmd_seed() -> int:
    n_prompts, n_few_shot = seed_all_from_files()
    print(f"✅ Загружено: {n_prompts} промптов, {n_few_shot} few-shot примеров")
    return 0


def cmd_list(kind: str | None) -> int:
    rows = list_versions(kind)
    if not rows:
        print(f"Нет версий для kind={kind!r}" if kind else "В БД нет промптов")
        return 0
    print(f"{'kind':24s} {'version':12s} {'active':6s}  created_at  notes")
    print("─" * 80)
    for r in rows:
        marker = "✓" if r.is_active else " "
        created = r.created_at.strftime("%Y-%m-%d") if r.created_at else "-"
        notes = (r.notes or "")[:30]
        print(f"{r.kind:24s} {r.version:12s} {marker:6s}  {created}  {notes}")
    return 0


def cmd_show(kind: str, version: str) -> int:
    rows = [r for r in list_versions(kind) if r.version == version]
    if not rows:
        print(f"Не найдено: {kind} v{version}", file=sys.stderr)
        return 1
    r = rows[0]
    print(f"=== {r.kind} v{r.version} ({'ACTIVE' if r.is_active else 'inactive'}) ===")
    print(f"Created: {r.created_at}")
    if r.notes:
        print(f"Notes:   {r.notes}")
    print(f"\n--- system ({len(r.system_prompt)} chars) ---\n{r.system_prompt}")
    print(f"\n--- user_template ({len(r.user_template)} chars) ---\n{r.user_template}")
    return 0


def cmd_activate(kind: str, version: str) -> int:
    set_active_version(kind, version)
    print(f"✅ Активирована {kind} v{version}")
    return 0


def cmd_rollback(kind: str) -> int:
    new_version = rollback(kind)
    print(f"✅ Откат: {kind} → v{new_version}")
    return 0


def cmd_few_shot() -> int:
    with session_scope() as session:
        rows = list(
            session.execute(
                select(FewShotExample)
                .where(FewShotExample.is_active.is_(True))
                .order_by(FewShotExample.rubric, FewShotExample.quality_score.desc())
            ).scalars()
        )
        if not rows:
            print("Нет активных few-shot примеров. Запусти `seed`.")
            return 0
        current_rubric = None
        for r in rows:
            if r.rubric != current_rubric:
                print(f"\n=== {r.rubric} ===")
                current_rubric = r.rubric
            print(f"  [{r.quality_score}] {r.slug:30s} {(r.notes or '')[:40]}")
    return 0


def main() -> int:
    setup_logging()
    init_db()

    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 1

    cmd, *rest = args
    if cmd == "seed":
        return cmd_seed()
    if cmd == "list":
        return cmd_list(rest[0] if rest else None)
    if cmd == "show":
        if len(rest) != 2:
            print("usage: show <kind> <version>", file=sys.stderr)
            return 1
        return cmd_show(rest[0], rest[1])
    if cmd == "activate":
        if len(rest) != 2:
            print("usage: activate <kind> <version>", file=sys.stderr)
            return 1
        return cmd_activate(rest[0], rest[1])
    if cmd == "rollback":
        if len(rest) != 1:
            print("usage: rollback <kind>", file=sys.stderr)
            return 1
        return cmd_rollback(rest[0])
    if cmd == "few-shot":
        return cmd_few_shot()

    print(f"Unknown command: {cmd}\n{__doc__}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
