import tempfile
import unittest
from pathlib import Path

from PIL import Image

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from glass_image_sorter.contact_sheet import generate_contact_sheets  # noqa: E402
from glass_image_sorter.core import PlanConfig, build_plan  # noqa: E402


def write_image(path: Path, color: tuple[int, int, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (50, 40), color).save(path)


class ContactSheetTests(unittest.TestCase):
    def test_generates_start_middle_end_jpegs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            album = root / "album"
            for index in range(6):
                write_image(album / f"image_{index}.jpg", (index * 30, 80, 160))

            plan = build_plan(PlanConfig(root=root))
            sheets = generate_contact_sheets(plan, root / "sheets", per_sheet=2)

            self.assertEqual(len(sheets), 3)
            self.assertEqual({path.suffix.lower() for path in sheets}, {".jpg"})
            for sheet in sheets:
                with Image.open(sheet) as image:
                    self.assertEqual(image.format, "JPEG")
                    self.assertGreater(image.width, 100)
                    self.assertGreater(image.height, 100)


if __name__ == "__main__":
    unittest.main()
