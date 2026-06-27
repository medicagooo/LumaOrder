"""Safe two-stage file renaming for image sorter plans."""

from __future__ import annotations

import uuid
from dataclasses import replace
from pathlib import Path
from typing import Sequence

from .core import RenamePlan, RenameRow, RenameSummary, write_csv


class RenameConflictError(RuntimeError):
    """Raised when applying a plan would overwrite an unrelated path."""


def temporary_rename_path(path: Path, token: str, index: int) -> Path:
    return path.with_name(f".rename_tmp_{token}_{index}.tmp")


def planned_rows(rows: Sequence[RenameRow]) -> list[RenameRow]:
    return [row for row in rows if row.status == "planned" and row.new_path is not None]


def find_conflicts(rows: Sequence[RenameRow]) -> list[str]:
    planned = planned_rows(rows)
    sources = {row.old_path.resolve() for row in planned}
    targets: dict[Path, RenameRow] = {}
    conflicts: list[str] = []

    for row in planned:
        target = row.new_path.resolve()
        source = row.old_path.resolve()
        existing = targets.get(target)
        if existing is not None and existing.old_path.resolve() != source:
            conflicts.append(f"{row.old_path} and {existing.old_path} both target {target}")
            continue
        targets[target] = row
        if source == target:
            continue
        if target.exists() and target not in sources:
            conflicts.append(f"{row.old_path} would overwrite existing {target}")

    return conflicts


def validate_no_conflicts(plan_or_rows: RenamePlan | Sequence[RenameRow]) -> None:
    rows = plan_or_rows.rows if isinstance(plan_or_rows, RenamePlan) else plan_or_rows
    conflicts = find_conflicts(rows)
    if conflicts:
        joined = "\n".join(conflicts)
        raise RenameConflictError(f"rename plan has conflicts:\n{joined}")


def _apply_renames(rows: Sequence[RenameRow]) -> int:
    validate_no_conflicts(rows)
    rename_rows = [
        row
        for row in planned_rows(rows)
        if row.new_path is not None and row.old_path.resolve() != row.new_path.resolve()
    ]
    if not rename_rows:
        return 0

    token = uuid.uuid4().hex
    staged: list[tuple[Path, Path, Path]] = []
    try:
        for index, row in enumerate(rename_rows):
            temp_path = temporary_rename_path(row.old_path, token, index)
            if temp_path.exists():
                raise RenameConflictError(f"temporary path already exists: {temp_path}")
            row.old_path.rename(temp_path)
            staged.append((temp_path, row.old_path, row.new_path))

        for temp_path, _old_path, new_path in staged:
            temp_path.rename(new_path)
    except Exception:
        for temp_path, _old_path, new_path in reversed(staged):
            if not temp_path.exists() and new_path.exists():
                new_path.rename(temp_path)
        for temp_path, old_path, _new_path in reversed(staged):
            if temp_path.exists() and not old_path.exists():
                temp_path.rename(old_path)
        raise

    return len(rename_rows)


def apply_plan(plan: RenamePlan) -> RenameSummary:
    renamed = _apply_renames(plan.rows)
    write_csv(plan.rows, Path(plan.config.output), applied=True)
    unchanged = plan.summary.planned - renamed
    return replace(plan.summary, renamed=renamed, unchanged=unchanged)
