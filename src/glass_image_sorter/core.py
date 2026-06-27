"""Core image feature extraction, clustering, and rename planning."""

from __future__ import annotations

import csv
import math
import os
import re
import warnings
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable, Sequence, TextIO

from PIL import Image, ImageOps, UnidentifiedImageError


SUPPORTED_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".bmp",
    ".gif",
    ".tif",
    ".tiff",
}
DEFAULT_EXCLUDED_DIR_NAMES = ("review_samples",)
GRAY_FEATURE_SIZE = 12 * 12
SPATIAL_RGB_FEATURE_SIZE = 8 * 8 * 3
PREFIX_RE = re.compile(r"^\d+_(.+)$")
CSV_FIELDS = [
    "directory",
    "old_name",
    "new_name",
    "old_path",
    "new_path",
    "status",
    "group",
    "order",
    "distance",
    "message",
]


@dataclass(frozen=True)
class PlanConfig:
    """Configuration for a dry-run rename plan."""

    root: str | Path
    output: str | Path = "rename_preview.csv"
    threshold: str | float = "auto"
    strip_existing_prefix: bool = True
    prefix_width: int = 4
    exclude_dirs: Sequence[str] = DEFAULT_EXCLUDED_DIR_NAMES
    progress: TextIO | None = None


@dataclass(frozen=True)
class ImageRecord:
    path: Path
    feature: tuple[float, ...]
    sort_key: tuple[float, ...]


@dataclass(frozen=True)
class RenameRow:
    directory: Path
    old_name: str
    new_name: str
    old_path: Path
    new_path: Path | None
    status: str
    group: int | None
    order: int | None
    distance: float | None = None
    message: str = ""


@dataclass(frozen=True)
class RenameSummary:
    directories: int = 0
    planned: int = 0
    renamed: int = 0
    unchanged: int = 0
    errors: int = 0
    conflicts: int = 0
    output: Path | None = None
    contact_sheets: tuple[Path, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "directories": self.directories,
            "planned": self.planned,
            "renamed": self.renamed,
            "unchanged": self.unchanged,
            "errors": self.errors,
            "conflicts": self.conflicts,
            "output": str(self.output) if self.output is not None else "",
            "contactSheets": [str(path) for path in self.contact_sheets],
        }


@dataclass(frozen=True)
class RenamePlan:
    rows: list[RenameRow]
    config: PlanConfig
    summary: RenameSummary

    def with_summary(self, summary: RenameSummary) -> "RenamePlan":
        return replace(self, summary=summary)


def is_image_file(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS


def resolve_existing_directory(root: str | Path) -> Path:
    root_path = Path(root).expanduser().resolve()
    if not root_path.exists():
        raise ValueError(f"root does not exist: {root_path}")
    if not root_path.is_dir():
        raise ValueError(f"root is not a directory: {root_path}")
    return root_path


def resolve_output_path(output: str | Path) -> Path:
    return Path(output).expanduser().resolve()


def strip_existing_prefix(name: str) -> str:
    match = PREFIX_RE.match(name)
    return match.group(1) if match else name


def image_pixels(image: Image.Image):
    getter = getattr(image, "get_flattened_data", None)
    return getter() if getter is not None else image.getdata()


def extract_feature(path: Path) -> tuple[float, ...]:
    """Extract a size-independent visual feature vector from an image."""

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with Image.open(path) as image:
                try:
                    image.seek(0)
                except EOFError:
                    pass
                try:
                    image.draft("RGB", (64, 64))
                except (AttributeError, OSError):
                    pass
                image = ImageOps.exif_transpose(image)
                rgb = image.convert("RGB")
    except (OSError, UnidentifiedImageError) as exc:
        raise ValueError(f"cannot read image: {exc}") from exc

    gray_image = rgb.resize((12, 12), Image.Resampling.LANCZOS).convert("L")
    gray_values = [pixel / 255.0 for pixel in image_pixels(gray_image)]

    spatial_image = rgb.resize((8, 8), Image.Resampling.LANCZOS)
    spatial_values: list[float] = []
    for red, green, blue in image_pixels(spatial_image):
        spatial_values.extend((red / 255.0, green / 255.0, blue / 255.0))

    color_image = rgb.resize((24, 24), Image.Resampling.BOX)
    histogram = [0.0] * 64
    for red, green, blue in image_pixels(color_image):
        red_bin = min(red // 64, 3)
        green_bin = min(green // 64, 3)
        blue_bin = min(blue // 64, 3)
        histogram[(red_bin * 16) + (green_bin * 4) + blue_bin] += 1.0

    total = float(color_image.width * color_image.height)
    histogram = [value / total for value in histogram]

    return (
        tuple(value * 0.55 for value in gray_values)
        + tuple(value * 0.95 for value in spatial_values)
        + tuple(value * 2.0 for value in histogram)
    )


def feature_distance(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right):
        raise ValueError("feature vectors have different lengths")
    if not left:
        return 0.0
    total = 0.0
    for left_value, right_value in zip(left, right):
        difference = left_value - right_value
        total += difference * difference
    return math.sqrt(total / len(left))


def feature_sort_key(feature: Sequence[float]) -> tuple[float, ...]:
    gray_values = feature[:GRAY_FEATURE_SIZE]
    histogram = feature[GRAY_FEATURE_SIZE + SPATIAL_RGB_FEATURE_SIZE :]
    brightness = sum(gray_values) / len(gray_values) if gray_values else 0.0
    dominant = max(range(len(histogram)), key=lambda index: histogram[index]) if histogram else 0
    color_energy = sum(value * value for value in histogram)
    return (dominant, brightness, color_energy)


def parse_threshold(value: str | float) -> str | float:
    if isinstance(value, (int, float)):
        threshold = float(value)
        if not math.isfinite(threshold) or threshold < 0.0:
            raise ValueError("threshold must be a finite number >= 0")
        return threshold
    normalized = value.strip().lower()
    if normalized == "auto":
        return "auto"
    threshold = float(normalized)
    if not math.isfinite(threshold) or threshold < 0.0:
        raise ValueError("threshold must be a finite number >= 0")
    return threshold


def normalize_prefix_width(prefix_width: int) -> int:
    try:
        width = int(prefix_width)
    except (TypeError, ValueError) as exc:
        raise ValueError("prefix width must be an integer") from exc
    if width < 1 or width > 9:
        raise ValueError("prefix width must be between 1 and 9")
    return width


def iter_image_directories(
    root: Path,
    exclude_dirs: Sequence[str] = DEFAULT_EXCLUDED_DIR_NAMES,
) -> Iterable[tuple[Path, list[Path]]]:
    excluded_names = {name.lower() for name in exclude_dirs}
    for current, directories, files in os.walk(root):
        directories[:] = [
            directory_name
            for directory_name in directories
            if directory_name.lower() not in excluded_names
        ]
        directory = Path(current)
        images = [
            directory / file_name
            for file_name in files
            if (directory / file_name).suffix.lower() in SUPPORTED_EXTENSIONS
        ]
        if images:
            yield directory, sorted(images, key=lambda path: path.name.lower())


def percentile(values: Sequence[float], ratio: float) -> float:
    if not values:
        return 0.0
    index = (len(values) - 1) * ratio
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return values[int(index)]
    lower_value = values[lower]
    upper_value = values[upper]
    return lower_value + ((upper_value - lower_value) * (index - lower))


def automatic_threshold(records: Sequence[ImageRecord]) -> float:
    if len(records) < 2:
        return 0.0
    sorted_records = sorted(records, key=lambda record: (record.sort_key, record.path.name.lower()))
    distances = [
        feature_distance(left.feature, right.feature)
        for left, right in zip(sorted_records, sorted_records[1:])
    ]
    distances = sorted(distance for distance in distances if distance > 0.0)
    if not distances:
        return 0.001

    q25 = percentile(distances, 0.25)
    q50 = percentile(distances, 0.50)
    q75 = percentile(distances, 0.75)
    spread = max(q75 - q25, 0.005)
    threshold = q50 + (0.5 * spread)
    return max(0.01, min(threshold, q75, 0.32))


def average_feature(records: Sequence[ImageRecord]) -> tuple[float, ...]:
    length = len(records[0].feature)
    totals = [0.0] * length
    for record in records:
        for index, value in enumerate(record.feature):
            totals[index] += value
    count = float(len(records))
    return tuple(value / count for value in totals)


def cluster_sort_key(cluster: Sequence[ImageRecord]) -> tuple[float, ...]:
    centroid = average_feature(cluster)
    return feature_sort_key(centroid)


def build_clusters(records: Sequence[ImageRecord], threshold: float) -> list[list[ImageRecord]]:
    if not records:
        return []

    sorted_records = sorted(records, key=lambda record: (record.sort_key, record.path.name.lower()))
    clusters: list[list[ImageRecord]] = []
    current: list[ImageRecord] = []
    centroid: tuple[float, ...] | None = None

    for record in sorted_records:
        if not current or centroid is None:
            current = [record]
            centroid = record.feature
            continue

        distance = feature_distance(record.feature, centroid)
        if distance <= threshold:
            current.append(record)
            centroid = average_feature(current)
        else:
            clusters.append(current)
            current = [record]
            centroid = record.feature

    if current:
        clusters.append(current)

    return sorted(clusters, key=cluster_sort_key)


def nearest_neighbor_order(records: Sequence[ImageRecord]) -> list[ImageRecord]:
    if len(records) <= 2:
        return sorted(records, key=lambda record: (record.sort_key, record.path.name.lower()))

    remaining = sorted(records, key=lambda record: (record.sort_key, record.path.name.lower()))
    ordered = [remaining.pop(0)]
    while remaining:
        current = ordered[-1]
        nearest_index = min(
            range(len(remaining)),
            key=lambda index: (
                feature_distance(current.feature, remaining[index].feature),
                remaining[index].path.name.lower(),
            ),
        )
        ordered.append(remaining.pop(nearest_index))
    return ordered


def build_rows_for_directory(
    directory: Path,
    paths: Sequence[Path],
    threshold: str | float,
    strip_prefix: bool,
    prefix_width: int,
    progress: TextIO | None = None,
) -> list[RenameRow]:
    parsed_threshold = parse_threshold(threshold)
    width = normalize_prefix_width(prefix_width)
    records: list[ImageRecord] = []
    rows: list[RenameRow] = []

    total_paths = len(paths)
    for index, path in enumerate(paths, start=1):
        if progress is not None and (index == 1 or index % 250 == 0 or index == total_paths):
            print(f"Processing {directory} ({index}/{total_paths})", file=progress, flush=True)
        try:
            feature = extract_feature(path)
        except ValueError as exc:
            rows.append(
                RenameRow(
                    directory=directory,
                    old_name=path.name,
                    new_name="",
                    old_path=path,
                    new_path=None,
                    status="error",
                    group=None,
                    order=None,
                    message=str(exc),
                )
            )
            continue
        records.append(ImageRecord(path=path, feature=feature, sort_key=feature_sort_key(feature)))

    actual_threshold = automatic_threshold(records) if parsed_threshold == "auto" else float(parsed_threshold)
    clusters = build_clusters(records, actual_threshold)

    sequence = 1
    for group_index, cluster in enumerate(clusters, start=1):
        previous: ImageRecord | None = None
        for ordered_record in nearest_neighbor_order(cluster):
            original_name = (
                strip_existing_prefix(ordered_record.path.name)
                if strip_prefix
                else ordered_record.path.name
            )
            new_name = f"{sequence:0{width}d}_{original_name}"
            new_path = ordered_record.path.with_name(new_name)
            distance = feature_distance(previous.feature, ordered_record.feature) if previous else 0.0
            rows.append(
                RenameRow(
                    directory=directory,
                    old_name=ordered_record.path.name,
                    new_name=new_name,
                    old_path=ordered_record.path,
                    new_path=new_path,
                    status="planned",
                    group=group_index,
                    order=sequence,
                    distance=distance,
                    message=f"threshold={actual_threshold:.6f}",
                )
            )
            previous = ordered_record
            sequence += 1

    return rows


def summarize_rows(rows: Sequence[RenameRow], output: Path | None = None) -> RenameSummary:
    directories = {row.directory for row in rows}
    planned = sum(1 for row in rows if row.status == "planned")
    errors = sum(1 for row in rows if row.status == "error")
    return RenameSummary(directories=len(directories), planned=planned, errors=errors, output=output)


def build_plan(config: PlanConfig) -> RenamePlan:
    root = resolve_existing_directory(config.root)
    parsed_threshold = parse_threshold(config.threshold)
    prefix_width = normalize_prefix_width(config.prefix_width)
    output = resolve_output_path(config.output)
    rows: list[RenameRow] = []

    normalized_config = replace(
        config,
        root=root,
        output=output,
        threshold=parsed_threshold,
        prefix_width=prefix_width,
        exclude_dirs=tuple(config.exclude_dirs),
    )
    for directory, paths in iter_image_directories(root, exclude_dirs=normalized_config.exclude_dirs):
        rows.extend(
            build_rows_for_directory(
                directory=directory,
                paths=paths,
                threshold=parsed_threshold,
                strip_prefix=normalized_config.strip_existing_prefix,
                prefix_width=prefix_width,
                progress=normalized_config.progress,
            )
        )

    return RenamePlan(rows=rows, config=normalized_config, summary=summarize_rows(rows, output))


def write_csv(rows: Sequence[RenameRow], output: Path, applied: bool) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for row in rows:
            status = row.status
            if applied and status == "planned":
                status = "unchanged" if row.old_path == row.new_path else "renamed"
            writer.writerow(
                {
                    "directory": str(row.directory),
                    "old_name": row.old_name,
                    "new_name": row.new_name,
                    "old_path": str(row.old_path),
                    "new_path": str(row.new_path) if row.new_path is not None else "",
                    "status": status,
                    "group": "" if row.group is None else row.group,
                    "order": "" if row.order is None else row.order,
                    "distance": "" if row.distance is None else f"{row.distance:.6f}",
                    "message": row.message,
                }
            )
