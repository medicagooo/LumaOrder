"""Command line entry point for the image similarity sorter."""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path
from typing import Sequence

from .contact_sheet import generate_contact_sheets
from .core import DEFAULT_EXCLUDED_DIR_NAMES, PlanConfig, build_plan, write_csv
from .renamer import RenameConflictError, apply_plan


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Sort images in each folder by visual similarity and rename them by sequence."
    )
    parser.add_argument("root", nargs="?", default=".", help="Root folder to scan recursively.")
    parser.add_argument(
        "--output",
        default="rename_preview.csv",
        help="CSV preview/report path. Default: rename_preview.csv",
    )
    parser.add_argument(
        "--threshold",
        default="auto",
        help="Cluster threshold: 'auto' or a non-negative number. Default: auto",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Only write the CSV preview.")
    mode.add_argument("--apply", action="store_true", help="Apply the rename plan.")
    parser.add_argument(
        "--strip-existing-prefix",
        dest="strip_existing_prefix",
        action="store_true",
        default=True,
        help="Strip an existing 0001_ prefix before creating the new name. Enabled by default.",
    )
    parser.add_argument(
        "--keep-existing-prefix",
        dest="strip_existing_prefix",
        action="store_false",
        help="Keep any existing 0001_ prefix in the original file name.",
    )
    parser.add_argument(
        "--prefix-width",
        type=int,
        default=4,
        help="Number of digits in the sequence prefix. Default: 4",
    )
    parser.add_argument(
        "--exclude-dir",
        action="append",
        default=[],
        help="Directory name to skip while walking. Can be provided multiple times. "
        "review_samples is skipped by default.",
    )
    parser.add_argument(
        "--contact-sheets",
        metavar="DIR",
        help="Generate start/middle/end JPEG contact sheets under DIR.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    exclude_dirs = DEFAULT_EXCLUDED_DIR_NAMES + tuple(args.exclude_dir)

    try:
        plan = build_plan(
            PlanConfig(
                root=args.root,
                output=args.output,
                threshold=args.threshold,
                strip_existing_prefix=args.strip_existing_prefix,
                prefix_width=args.prefix_width,
                exclude_dirs=exclude_dirs,
                progress=sys.stderr,
            )
        )
        if args.apply:
            summary = apply_plan(plan)
        else:
            write_csv(plan.rows, Path(plan.config.output), applied=False)
            summary = plan.summary

        contact_sheets: list[Path] = []
        if args.contact_sheets:
            contact_sheets = generate_contact_sheets(plan, args.contact_sheets)
            summary = replace(summary, contact_sheets=tuple(contact_sheets))
    except (RenameConflictError, ValueError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    mode = "applied" if args.apply else "dry-run"
    print(
        f"{mode}: directories={summary.directories}, planned={summary.planned}, "
        f"renamed={summary.renamed}, errors={summary.errors}, output={summary.output}"
    )
    if contact_sheets:
        print(f"contact-sheets: {len(contact_sheets)} files under {Path(args.contact_sheets).resolve()}")
    if not args.apply:
        print("No files were renamed. Re-run with --apply to perform the rename.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
