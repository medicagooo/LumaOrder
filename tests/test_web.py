import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from glass_image_sorter.web import create_server  # noqa: E402


def write_image(path: Path, color: tuple[int, int, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (40, 30), color).save(path)


def request_json(url: str, payload: dict | None = None) -> dict:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


class WebApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = create_server(port=0)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def wait_for_job(self, job_id: str) -> dict:
        for _ in range(80):
            snapshot = request_json(f"{self.base_url}/api/jobs/{job_id}")
            if snapshot["status"] in {"completed", "failed"}:
                return snapshot
            time.sleep(0.05)
        self.fail(f"job did not finish: {job_id}")

    def test_dry_run_job_returns_csv_and_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_image(root / "album" / "red.jpg", (220, 20, 20))
            write_image(root / "album" / "blue.jpg", (20, 20, 220))

            created = request_json(
                f"{self.base_url}/api/jobs",
                {"root": str(root), "mode": "dry-run", "output": str(root / "preview.csv")},
            )
            snapshot = self.wait_for_job(created["id"])
            artifacts = request_json(f"{self.base_url}/api/jobs/{created['id']}/artifacts")

            self.assertEqual(snapshot["status"], "completed")
            self.assertEqual(snapshot["summary"]["planned"], 2)
            self.assertTrue(Path(artifacts["csv"]).exists())

    def test_apply_job_is_refused_when_plan_has_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_image(root / "album" / "photo.jpg", (10, 10, 10))
            (root / "album" / "0001_photo.jpg").write_bytes(b"external blocker")

            created = request_json(f"{self.base_url}/api/jobs", {"root": str(root), "mode": "apply"})
            snapshot = self.wait_for_job(created["id"])

            self.assertEqual(snapshot["status"], "failed")
            self.assertIn("conflicts", snapshot["error"])
            self.assertTrue((root / "album" / "photo.jpg").exists())

    def test_examples_endpoint_returns_list(self) -> None:
        response = request_json(f"{self.base_url}/examples")

        self.assertIn("examples", response)
        self.assertIsInstance(response["examples"], list)

    def test_static_files_do_not_escape_web_root(self) -> None:
        request = urllib.request.Request(f"{self.base_url}/%2e%2e/README.md")

        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request, timeout=10)

        self.assertEqual(error.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
