"""Small stdlib HTTP server for the local web UI."""

from __future__ import annotations

import argparse
import io
import json
import mimetypes
import threading
import traceback
import uuid
import webbrowser
from dataclasses import replace
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import unquote, urlparse

from .contact_sheet import generate_contact_sheets
from .core import DEFAULT_EXCLUDED_DIR_NAMES, PlanConfig, RenamePlan, RenameSummary, build_plan, write_csv
from .renamer import RenameConflictError, apply_plan, find_conflicts


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WEB_ROOT = PROJECT_ROOT / "web"
EXAMPLES_ROOT = PROJECT_ROOT / "docs" / "examples"


class ProgressBuffer(io.TextIOBase):
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._lines: list[str] = []

    def write(self, value: str) -> int:
        with self._lock:
            for line in value.splitlines():
                if line.strip():
                    self._lines.append(line.strip())
        return len(value)

    def flush(self) -> None:
        return None

    def lines(self) -> list[str]:
        with self._lock:
            return list(self._lines)


class Job:
    def __init__(self, job_id: str, request: dict[str, Any]) -> None:
        self.id = job_id
        self.request = request
        self.status = "queued"
        self.error = ""
        self.traceback = ""
        self.summary = RenameSummary()
        self.artifacts: dict[str, Any] = {"csv": "", "contactSheets": []}
        self.progress = ProgressBuffer()
        self.plan: RenamePlan | None = None
        self._lock = threading.Lock()

    def update(self, **values: Any) -> None:
        with self._lock:
            for key, value in values.items():
                setattr(self, key, value)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "id": self.id,
                "status": self.status,
                "request": self.request,
                "progress": self.progress.lines(),
                "summary": self.summary.to_dict(),
                "error": self.error,
            }


JOBS: dict[str, Job] = {}
JOBS_LOCK = threading.Lock()


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _as_exclude_dirs(value: Any) -> tuple[str, ...]:
    names: list[str] = list(DEFAULT_EXCLUDED_DIR_NAMES)
    if isinstance(value, str):
        candidates = [part.strip() for part in value.replace(";", ",").split(",")]
    elif isinstance(value, Sequence):
        candidates = [str(part).strip() for part in value]
    else:
        candidates = []
    for candidate in candidates:
        if candidate and candidate not in names:
            names.append(candidate)
    return tuple(names)


def _job_output(root: Path, requested_output: str | None, job_id: str) -> Path:
    if requested_output:
        return Path(requested_output).expanduser().resolve()
    return (root / f"rename_preview_{job_id[:8]}.csv").resolve()


def _run_job(job: Job) -> None:
    job.update(status="running")
    try:
        request = job.request
        root = Path(str(request.get("root") or ".")).expanduser().resolve()
        mode = str(request.get("mode") or "dry-run").lower()
        output = _job_output(root, request.get("output") or None, job.id)
        contact_dir = request.get("contactSheets")
        prefix_width = int(request.get("prefixWidth") or 4)
        strip_existing_prefix = _as_bool(request.get("stripExistingPrefix"), True)
        threshold = request.get("threshold") or "auto"
        exclude_dirs = _as_exclude_dirs(request.get("excludeDirs"))

        plan = build_plan(
            PlanConfig(
                root=root,
                output=output,
                threshold=threshold,
                strip_existing_prefix=strip_existing_prefix,
                prefix_width=prefix_width,
                exclude_dirs=exclude_dirs,
                progress=job.progress,
            )
        )
        conflicts = find_conflicts(plan.rows)
        summary = replace(plan.summary, conflicts=len(conflicts))
        job.update(plan=plan, summary=summary)

        if mode == "apply":
            if conflicts:
                raise RenameConflictError("dry-run has conflicts; apply was refused")
            summary = apply_plan(plan)
            job.update(summary=summary)
        elif mode == "dry-run":
            write_csv(plan.rows, Path(plan.config.output), applied=False)
        else:
            raise ValueError("mode must be 'dry-run' or 'apply'")

        sheet_paths: list[Path] = []
        if contact_dir:
            sheet_paths = generate_contact_sheets(plan, contact_dir)
            summary = replace(job.summary, contact_sheets=tuple(sheet_paths))
            job.update(summary=summary)

        job.artifacts = {
            "csv": str(plan.config.output),
            "contactSheets": [str(path) for path in sheet_paths],
        }
        job.update(status="completed")
    except Exception as exc:
        job.update(status="failed", error=str(exc), traceback=traceback.format_exc())


def create_job(request: dict[str, Any]) -> Job:
    job_id = uuid.uuid4().hex
    job = Job(job_id, request)
    with JOBS_LOCK:
        JOBS[job_id] = job
    thread = threading.Thread(target=_run_job, args=(job,), daemon=True)
    thread.start()
    return job


def get_job(job_id: str) -> Job | None:
    with JOBS_LOCK:
        return JOBS.get(job_id)


def _json_bytes(payload: object, status: HTTPStatus = HTTPStatus.OK) -> tuple[int, bytes, str]:
    data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    return status.value, data, "application/json; charset=utf-8"


def _read_json(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0") or "0")
    if length <= 0:
        return {}
    raw = handler.rfile.read(length)
    return json.loads(raw.decode("utf-8"))


def _send(handler: BaseHTTPRequestHandler, status: int, data: bytes, content_type: str) -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(data)


def _file_response(path: Path) -> tuple[int, bytes, str]:
    if not path.exists() or not path.is_file():
        return _json_bytes({"error": "not found"}, HTTPStatus.NOT_FOUND)
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return HTTPStatus.OK.value, path.read_bytes(), content_type


def _safe_child(root: Path, requested: str) -> Path | None:
    candidate = (root / unquote(requested).lstrip("/")).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    return candidate


def _examples() -> list[dict[str, str]]:
    if not EXAMPLES_ROOT.exists():
        return []
    examples: list[dict[str, str]] = []
    for path in sorted(EXAMPLES_ROOT.glob("*")):
        if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
            examples.append({"name": path.name, "path": str(path), "url": f"/examples/{path.name}"})
    return examples


def make_handler() -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "GlassImageSorter/0.1"

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            status: int
            data: bytes
            content_type: str

            if path == "/api/health":
                status, data, content_type = _json_bytes({"ok": True})
            elif path == "/examples":
                status, data, content_type = _json_bytes({"examples": _examples()})
            elif path.startswith("/examples/"):
                name = Path(unquote(path.removeprefix("/examples/"))).name
                status, data, content_type = _file_response(EXAMPLES_ROOT / name)
            elif path.startswith("/api/jobs/"):
                parts = [part for part in path.split("/") if part]
                job_id = parts[2] if len(parts) >= 3 else ""
                job = get_job(job_id)
                if job is None:
                    status, data, content_type = _json_bytes({"error": "job not found"}, HTTPStatus.NOT_FOUND)
                elif len(parts) == 4 and parts[3] == "artifacts":
                    status, data, content_type = _json_bytes(job.artifacts)
                else:
                    status, data, content_type = _json_bytes(job.snapshot())
            else:
                requested = "index.html" if path in {"", "/"} else path
                static_path = _safe_child(WEB_ROOT, requested)
                if static_path is None:
                    status, data, content_type = _json_bytes({"error": "not found"}, HTTPStatus.NOT_FOUND)
                else:
                    status, data, content_type = _file_response(static_path)

            _send(self, status, data, content_type)

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path != "/api/jobs":
                status, data, content_type = _json_bytes({"error": "not found"}, HTTPStatus.NOT_FOUND)
                _send(self, status, data, content_type)
                return
            try:
                payload = _read_json(self)
                job = create_job(payload)
                status, data, content_type = _json_bytes({"id": job.id, "status": job.status})
            except Exception as exc:
                status, data, content_type = _json_bytes({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            _send(self, status, data, content_type)

        def log_message(self, format: str, *args: object) -> None:
            return None

    return Handler


def create_server(port: int = 8765, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler())


def run_server(port: int = 8765, open_browser: bool = False, host: str = "127.0.0.1") -> None:
    server = create_server(port=port, host=host)
    url = f"http://{host}:{server.server_address[1]}"
    if open_browser:
        webbrowser.open(url)
    print(f"Glass Image Sorter listening on {url}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the local Glass Image Sorter web UI.")
    parser.add_argument("--port", type=int, default=8765, help="Port to listen on. Default: 8765")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind. Default: 127.0.0.1")
    parser.add_argument("--open", action="store_true", help="Open the browser after the server starts.")
    args = parser.parse_args(argv)
    run_server(port=args.port, host=args.host, open_browser=args.open)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
