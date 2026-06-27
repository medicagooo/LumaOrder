import csv
import io
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, JpegImagePlugin

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from glass_image_sorter.core import (  # noqa: E402
    PlanConfig,
    build_plan,
    extract_feature,
    feature_distance,
    parse_threshold,
)
from glass_image_sorter.renamer import RenameConflictError, apply_plan, temporary_rename_path  # noqa: E402


def write_image(path: Path, size=(40, 30), color=(200, 40, 40)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path)


def write_split_image(path: Path, top_color: tuple[int, int, int], bottom_color: tuple[int, int, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (60, 60), top_color)
    for y in range(30, 60):
        for x in range(60):
            image.putpixel((x, y), bottom_color)
    image.save(path)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


class CorePlanTests(unittest.TestCase):
    def test_extract_feature_ignores_size_and_uses_low_resolution_decode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            small = root / "small.jpg"
            large = root / "large.jpg"
            write_image(small, size=(24, 18), color=(120, 80, 210))
            write_image(large, size=(800, 600), color=(120, 80, 210))

            with patch.object(JpegImagePlugin.JpegImageFile, "draft", autospec=True) as draft:
                distance = feature_distance(extract_feature(small), extract_feature(large))

            self.assertLess(distance, 0.001)
            draft.assert_called()
            self.assertEqual(draft.call_args.args[1], "RGB")
            self.assertEqual(draft.call_args.args[2], (64, 64))

    def test_extract_feature_includes_spatial_color_layout(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            red = (255, 0, 0)
            green_with_matching_luma = (0, 130, 0)
            top_red = root / "top_red.png"
            top_green = root / "top_green.png"
            write_split_image(top_red, red, green_with_matching_luma)
            write_split_image(top_green, green_with_matching_luma, red)

            distance = feature_distance(extract_feature(top_red), extract_feature(top_green))

            self.assertGreater(distance, 0.02)

    def test_build_plan_dry_run_rows_and_progress(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_image(root / ".hidden" / "secret.jpg", color=(10, 200, 10))
            write_image(root / "album" / "b.jpg", color=(20, 20, 200))
            write_image(root / "album" / "a.jpg", color=(200, 20, 20))
            write_image(root / "review_samples" / "sheet.jpg", color=(200, 200, 20))
            progress = io.StringIO()

            plan = build_plan(PlanConfig(root=root, progress=progress))

            self.assertEqual(plan.summary.directories, 2)
            self.assertEqual(plan.summary.planned, 3)
            self.assertIn("Processing", progress.getvalue())
            self.assertNotIn("review_samples", {row.directory.name for row in plan.rows})
            album_rows = [row for row in plan.rows if row.directory.name == "album"]
            self.assertTrue(all(row.new_name.startswith(("0001_", "0002_")) for row in album_rows))

    def test_prefix_width_and_existing_prefix_handling(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_image(Path(root) / "album" / "0007_photo.jpg", color=(10, 10, 10))

            stripped = build_plan(PlanConfig(root=root, prefix_width=3, strip_existing_prefix=True))
            kept = build_plan(PlanConfig(root=root, prefix_width=3, strip_existing_prefix=False))

            self.assertEqual(stripped.rows[0].new_name, "001_photo.jpg")
            self.assertEqual(kept.rows[0].new_name, "001_0007_photo.jpg")

    def test_invalid_inputs_fail_before_reading_images(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_image(root / "album" / "photo.jpg", color=(220, 20, 20))
            with patch("glass_image_sorter.core.extract_feature", side_effect=AssertionError("read image")):
                with self.assertRaises(ValueError):
                    build_plan(PlanConfig(root=root, threshold="nan"))

            with self.assertRaises(ValueError):
                parse_threshold("inf")
            with self.assertRaises(ValueError):
                build_plan(PlanConfig(root=root / "missing"))
            file_root = root / "not_a_directory.txt"
            file_root.write_text("not a directory", encoding="utf-8")
            with self.assertRaises(ValueError):
                build_plan(PlanConfig(root=file_root))

    def test_zero_threshold_keeps_different_images_in_separate_groups(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_image(root / "album" / "red.jpg", color=(220, 20, 20))
            write_image(root / "album" / "blue.jpg", color=(20, 20, 220))

            plan = build_plan(PlanConfig(root=root, threshold=0.0))

            self.assertEqual({row.group for row in plan.rows}, {1, 2})


class RenamerTests(unittest.TestCase):
    def test_apply_plan_renames_and_writes_csv(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_image(root / "album" / "red.jpg", color=(220, 20, 20))
            write_image(root / "album" / "blue.jpg", color=(20, 20, 220))
            output = root / "result.csv"

            plan = build_plan(PlanConfig(root=root, output=output))
            summary = apply_plan(plan)

            self.assertEqual(summary.renamed, 2)
            self.assertEqual(len(list((root / "album").glob("000*_*.jpg"))), 2)
            self.assertEqual({row["status"] for row in read_csv(output)}, {"renamed"})

    def test_apply_plan_blocks_external_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_image(root / "album" / "photo.jpg", color=(10, 10, 10))
            (root / "album" / "0001_photo.jpg").write_bytes(b"blocks target")
            plan = build_plan(PlanConfig(root=root))

            with self.assertRaises(RenameConflictError):
                apply_plan(plan)

            self.assertTrue((root / "album" / "photo.jpg").exists())
            self.assertEqual((root / "album" / "0001_photo.jpg").read_bytes(), b"blocks target")

    def test_failed_second_phase_rename_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_image(root / "album" / "first.jpg", color=(220, 20, 20))
            write_image(root / "album" / "second.jpg", color=(20, 20, 220))
            plan = build_plan(PlanConfig(root=root))
            original_rename = Path.rename
            final_calls = 0

            def fail_on_second_final(source: Path, target: Path) -> Path:
                nonlocal final_calls
                if Path(source).name.startswith(".rename_tmp_") and Path(target).suffix != ".tmp":
                    final_calls += 1
                    if final_calls == 2:
                        raise OSError("simulated final rename failure")
                return original_rename(source, target)

            with patch.object(Path, "rename", fail_on_second_final):
                with self.assertRaises(OSError):
                    apply_plan(plan)

            self.assertTrue((root / "album" / "first.jpg").exists())
            self.assertTrue((root / "album" / "second.jpg").exists())
            self.assertFalse(any((root / "album").glob(".rename_tmp_*")))

    def test_temporary_path_is_short_and_not_image_suffix(self) -> None:
        source = Path("album") / f"{'a' * 240}.jpg"

        temp_path = temporary_rename_path(source, "b" * 32, 123)

        self.assertEqual(temp_path.parent, source.parent)
        self.assertLessEqual(len(temp_path.name), 80)
        self.assertEqual(temp_path.suffix, ".tmp")


if __name__ == "__main__":
    unittest.main()
