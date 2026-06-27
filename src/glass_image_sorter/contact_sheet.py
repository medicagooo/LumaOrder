"""Contact sheet generation for visual spot checks."""

from __future__ import annotations

import math
import re
from collections import defaultdict
from pathlib import Path
from typing import Iterable, Sequence

from PIL import Image, ImageDraw, ImageFont, ImageOps

from .core import RenamePlan, RenameRow


WINDOWS = ("start", "middle", "end")
SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def _source_path(row: RenameRow) -> Path:
    if row.new_path is not None and row.new_path.exists():
        return row.new_path
    return row.old_path


def _safe_part(value: str) -> str:
    stripped = value.strip().replace(":", "")
    safe = SAFE_NAME_RE.sub("_", stripped)
    return safe.strip("._") or "root"


def _directory_label(root: Path, directory: Path) -> str:
    try:
        relative = directory.resolve().relative_to(root.resolve())
    except ValueError:
        relative = directory
    label = "_".join(_safe_part(part) for part in relative.parts)
    return label or _safe_part(root.name)


def _window_rows(rows: Sequence[RenameRow], window: str, per_sheet: int) -> list[RenameRow]:
    planned = [row for row in rows if row.status == "planned"]
    if not planned:
        return []
    count = min(per_sheet, len(planned))
    if window == "start":
        start = 0
    elif window == "middle":
        start = max(0, (len(planned) - count) // 2)
    elif window == "end":
        start = max(0, len(planned) - count)
    else:
        raise ValueError(f"unknown contact sheet window: {window}")
    return planned[start : start + count]


def _fit_image(path: Path, size: tuple[int, int]) -> Image.Image:
    try:
        with Image.open(path) as image:
            try:
                image.seek(0)
            except EOFError:
                pass
            image = ImageOps.exif_transpose(image).convert("RGB")
            image.thumbnail(size, Image.Resampling.LANCZOS)
            canvas = Image.new("RGB", size, (245, 247, 250))
            x = (size[0] - image.width) // 2
            y = (size[1] - image.height) // 2
            canvas.paste(image, (x, y))
            return canvas
    except OSError:
        return Image.new("RGB", size, (226, 232, 240))


def _ellipsize(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int) -> str:
    if draw.textlength(text, font=font) <= max_width:
        return text
    suffix = "..."
    available = max_width - int(draw.textlength(suffix, font=font))
    if available <= 0:
        return suffix
    clipped = ""
    for character in text:
        candidate = clipped + character
        if draw.textlength(candidate, font=font) > available:
            break
        clipped = candidate
    return clipped.rstrip() + suffix


def _draw_sheet(
    rows: Sequence[RenameRow],
    title: str,
    output_path: Path,
    thumb_size: tuple[int, int] = (150, 150),
) -> None:
    columns = min(6, max(1, math.ceil(math.sqrt(len(rows)))))
    rows_count = math.ceil(len(rows) / columns)
    padding = 18
    label_height = 42
    header_height = 48
    cell_width = thumb_size[0] + padding
    cell_height = thumb_size[1] + label_height + padding
    width = (columns * cell_width) + padding
    height = header_height + (rows_count * cell_height) + padding

    sheet = Image.new("RGB", (width, height), (238, 242, 247))
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default()
    draw.text((padding, 18), title, fill=(25, 35, 55), font=font)

    for index, row in enumerate(rows):
        column = index % columns
        line = index // columns
        x = padding + (column * cell_width)
        y = header_height + (line * cell_height)
        image = _fit_image(_source_path(row), thumb_size)
        sheet.paste(image, (x, y))
        order_label = f"#{row.order or '-'}  group {row.group or '-'}"
        name_label = _ellipsize(draw, row.new_name or row.old_name, font, thumb_size[0])
        draw.text((x, y + thumb_size[1] + 7), order_label, fill=(35, 46, 70), font=font)
        draw.text((x, y + thumb_size[1] + 24), name_label, fill=(70, 82, 103), font=font)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output_path, "JPEG", quality=88)


def generate_contact_sheets(
    plan: RenamePlan,
    output_dir: str | Path,
    windows: Iterable[str] = WINDOWS,
    per_sheet: int = 48,
) -> list[Path]:
    """Generate start/middle/end JPEG contact sheets per planned directory."""

    if per_sheet < 1:
        raise ValueError("per_sheet must be >= 1")

    output_root = Path(output_dir).expanduser().resolve()
    by_directory: dict[Path, list[RenameRow]] = defaultdict(list)
    for row in plan.rows:
        by_directory[row.directory].append(row)

    root = Path(plan.config.root)
    created: list[Path] = []
    for directory in sorted(by_directory, key=lambda path: str(path).lower()):
        directory_rows = sorted(
            by_directory[directory],
            key=lambda row: (row.order is None, row.order or 0, row.old_name.lower()),
        )
        directory_name = _directory_label(root, directory)
        for window in windows:
            selected = _window_rows(directory_rows, window, per_sheet)
            if not selected:
                continue
            output_path = output_root / f"{directory_name}_{window}.jpg"
            title = f"{directory} - {window} ({len(selected)} images)"
            _draw_sheet(selected, title, output_path)
            created.append(output_path)

    return created
