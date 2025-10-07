"""FastAPI application exposing the Webflow exporter as an HTTP API."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import uuid
import zipfile
from datetime import datetime
from typing import Any, Callable, Dict

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, HttpUrl

from .cli import (
    VERSION_NUM,
    check_output_path_exists,
    check_url,
    clear_output_folder,
    download_assets,
    generate_sitemap,
    remove_badge_from_output,
    scan_html,
    logger as exporter_logger,
)

DEFAULT_ALLOWED_ORIGINS = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]


def _load_allowed_origins() -> list[str]:
    """Return the list of CORS origins, optionally sourced from the environment."""

    configured = os.environ.get("CORS_ALLOW_ORIGINS")
    if not configured:
        return DEFAULT_ALLOWED_ORIGINS

    configured = configured.strip()
    if not configured:
        return DEFAULT_ALLOWED_ORIGINS

    if configured == "*":
        return ["*"]

    origins = [origin.strip() for origin in configured.split(",") if origin.strip()]
    if origins:
        return origins

    exporter_logger.warning(
        "CORS_ALLOW_ORIGINS environment variable provided but no valid origins were parsed;"
        " falling back to defaults."
    )
    return DEFAULT_ALLOWED_ORIGINS


app = FastAPI(
    title="Python Webflow Exporter API",
    version=VERSION_NUM,
    description="HTTP wrapper around the python-webflow-exporter CLI workflow.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_load_allowed_origins(),
    allow_methods=["*"],
    allow_headers=["*"],
)


class ExportRequest(BaseModel):
    """Payload describing an export job."""

    url: HttpUrl
    remove_badge: bool = False
    generate_sitemap: bool = False
    debug: bool = False
    silent: bool = False
    output_name: str | None = None


class ExportJob:
    """Represents an exporter job running in the background."""

    def __init__(self, job_id: str, request: ExportRequest, output_dir: str) -> None:
        self.id = job_id
        self.request = request
        self.output_dir = output_dir
        self.archive_name = _ensure_zip_suffix(request.output_name or "webflow-export.zip")
        self.archive_path: str | None = None
        self.archive_size: int | None = None
        self.status = "queued"
        self.error: str | None = None
        self.created_at = datetime.utcnow()
        self.updated_at = self.created_at
        self.events: list[dict[str, Any]] = []
        self.lock = threading.Lock()

    def _touch(self) -> None:
        self.updated_at = datetime.utcnow()

    def add_event(self, event: dict[str, Any]) -> None:
        with self.lock:
            self.events.append(event)
            self._touch()

    def set_status(self, status: str) -> None:
        with self.lock:
            self.status = status
            self._touch()

    def set_error(self, message: str) -> None:
        with self.lock:
            self.error = message
            self._touch()

    def set_archive(self, path: str, size: int) -> None:
        with self.lock:
            self.archive_path = path
            self.archive_size = size
            self._touch()

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            return {
                "job_id": self.id,
                "status": self.status,
                "events": list(self.events),
                "error": self.error,
                "file_ready": self.archive_path is not None and self.status == "complete",
                "file_name": self.archive_name,
                "archive_size": self.archive_size,
                "updated_at": self.updated_at.isoformat() + "Z",
            }

    def snapshot_events(self) -> list[dict[str, Any]]:
        with self.lock:
            return list(self.events)


JOB_STORE: dict[str, ExportJob] = {}
JOB_LOCK = threading.Lock()


def _register_job(job: ExportJob) -> None:
    with JOB_LOCK:
        JOB_STORE[job.id] = job


def _get_job(job_id: str) -> ExportJob:
    with JOB_LOCK:
        job = JOB_STORE.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.get("/health")
def healthcheck() -> Dict[str, Any]:
    """Simple health endpoint."""

    return {"status": "ok", "version": VERSION_NUM}


@app.post("/exports")
def create_export_job(request: ExportRequest) -> Dict[str, str]:
    """Create a new export job and start it asynchronously."""

    if request.debug and request.silent:
        raise HTTPException(status_code=400, detail="'debug' and 'silent' options cannot be combined")

    job_id = uuid.uuid4().hex
    output_dir = tempfile.mkdtemp(prefix=f"webexp-{job_id}-")
    job = ExportJob(job_id, request, output_dir)
    _register_job(job)

    thread = threading.Thread(target=_run_export_job, args=(job,), daemon=True)
    thread.start()

    return {"job_id": job_id}


@app.get("/exports/{job_id}/progress")
def export_progress(job_id: str) -> Dict[str, Any]:
    """Return the current status and progress events for a job."""

    job = _get_job(job_id)
    return job.snapshot()


@app.get("/exports/{job_id}/download")
def download_export(job_id: str) -> FileResponse:
    """Send the finished archive for the given job."""

    job = _get_job(job_id)
    if job.archive_path is None or job.status != "complete":
        raise HTTPException(status_code=404, detail="Archive not ready")

    return FileResponse(
        job.archive_path,
        media_type="application/zip",
        filename=job.archive_name,
    )


class _ProgressRecorder:
    """Collects progress events during an export."""

    def __init__(self, on_event: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.events: list[dict[str, Any]] = []
        self._on_event = on_event

    def add(self, event_type: str, **payload: Any) -> None:
        """Store an event with a timestamp."""

        event: dict[str, Any] = {
            "type": event_type,
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }
        event.update(payload)
        self.events.append(event)

        if self._on_event is not None:
            try:
                self._on_event(dict(event))
            except Exception:  # pragma: no cover - defensive
                logging.getLogger(__name__).exception("Failed to forward progress event")


class _ProgressLogHandler(logging.Handler):
    """Intercept downloader log messages and convert them into progress events."""

    def __init__(self, recorder: _ProgressRecorder) -> None:
        super().__init__(level=logging.INFO)
        self.recorder = recorder
        self.output_root: str | None = None

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D401 - signature required
        if record.levelno < logging.INFO:
            return

        message = record.getMessage()
        self.recorder.add("log", level=record.levelname.lower(), message=message)

        if message.startswith("Downloading "):
            remainder = message[len("Downloading ") :]
            if " to " not in remainder:
                return

            source, target = remainder.split(" to ", 1)
            source = source.strip()
            target = target.strip()
            target_display = target

            if self.output_root:
                abs_target = os.path.abspath(target)
                try:
                    if os.path.commonpath([self.output_root, abs_target]) == self.output_root:
                        target_display = os.path.relpath(abs_target, self.output_root)
                except ValueError:
                    target_display = abs_target

            self.recorder.add("download", source=source, target=target_display, status="start")
            return

        if message.startswith("Downloaded "):
            # Messages like "Downloaded image: <url>"
            parts = message.split(":", 1)
            url = parts[1].strip() if len(parts) == 2 else message[len("Downloaded ") :].strip()
            if url:
                self.recorder.add("download", source=url, status="complete")


def _run_export_job(job: ExportJob) -> None:
    """Execute an export job on a background thread."""

    recorder = _ProgressRecorder(on_event=job.add_event)
    handler = _ProgressLogHandler(recorder)
    handler.setFormatter(logging.Formatter('%(message)s'))
    exporter_logger.addHandler(handler)

    job.set_status("running")
    recorder.add("stage", name="start")

    try:
        result = _execute_export_with_progress(
            url=str(job.request.url),
            output=os.path.join(job.output_dir, "export"),
            remove_badge=job.request.remove_badge,
            create_sitemap=job.request.generate_sitemap,
            debug=job.request.debug,
            silent=job.request.silent,
            ensure_parent_dir=True,
            recorder=recorder,
            handler=handler,
        )

        manifest = result["assets"]

        recorder.add("stage", name="zipping")
        export_root = result["output_path"]
        archive_path = os.path.join(job.output_dir, job.archive_name)

        with zipfile.ZipFile(archive_path, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            for root, _, files in os.walk(export_root):
                for filename in files:
                    file_path = os.path.join(root, filename)
                    arcname = os.path.relpath(file_path, export_root)
                    zf.write(file_path, arcname)
            zf.writestr("manifest.json", json.dumps(manifest, indent=2))

        size = os.path.getsize(archive_path)
        job.set_archive(archive_path, size)

        recorder.add("stage", name="zipped")
        recorder.add("stage", name="complete")
        with zipfile.ZipFile(archive_path, mode="a", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("progress.json", json.dumps(recorder.events, indent=2))
        job.set_status("complete")
    except Exception as exc:  # pragma: no cover - defensive
        message = str(exc)
        recorder.add("log", level="error", message=message)
        recorder.add("stage", name="error")
        job.set_error(message)
        job.set_status("error")
    finally:
        exporter_logger.removeHandler(handler)


def _execute_export_with_progress(
    *,
    url: str,
    output: str,
    remove_badge: bool,
    create_sitemap: bool,
    debug: bool,
    silent: bool,
    ensure_parent_dir: bool,
    recorder: _ProgressRecorder,
    handler: _ProgressLogHandler | None,
) -> dict[str, Any]:
    """Run the exporter while recording progress information."""

    if recorder is None:
        raise ValueError("Progress recorder is required")

    if debug and silent:
        raise ValueError("Invalid configuration: 'debug' and 'silent' options cannot be used together.")

    previous_level = exporter_logger.level

    if silent:
        exporter_logger.setLevel(logging.ERROR)
    elif debug:
        exporter_logger.info("Debug mode enabled.")
        exporter_logger.setLevel(logging.DEBUG)
    else:
        exporter_logger.setLevel(logging.INFO)

    try:
        recorder.add("stage", name="validate_url")
        check_url(url)

        output_path = os.path.abspath(output)
        if handler is not None:
            handler.output_root = output_path

        if not check_output_path_exists(output_path, create=ensure_parent_dir):
            raise ValueError("Output path does not exist. Please provide a valid path.")

        recorder.add("stage", name="clear_output")
        clear_output_folder(output_path)

        recorder.add("stage", name="scanning")
        assets_manifest = scan_html(url)
        recorder.add(
            "stage",
            name="scanned",
            counts={
                key: len(value)
                for key, value in assets_manifest.items()
                if isinstance(value, (list, set, tuple))
            },
        )

        recorder.add("stage", name="downloading")
        download_assets(assets_manifest, output_path)
        recorder.add("stage", name="downloaded")

        if remove_badge:
            recorder.add("stage", name="removing_badge")
            remove_badge_from_output(output_path)
            recorder.add("stage", name="badge_removed")

        if create_sitemap:
            recorder.add("stage", name="generating_sitemap")
            generate_sitemap(output_path, assets_manifest)
            recorder.add("stage", name="sitemap_generated")

        recorder.add("stage", name="ready_for_archive")

        return {
            "output_path": output_path,
            "assets": assets_manifest,
        }
    finally:
        exporter_logger.setLevel(previous_level)


def _ensure_zip_suffix(filename: str) -> str:
    """Ensure the provided filename ends with the .zip suffix."""

    return filename if filename.lower().endswith(".zip") else f"{filename}.zip"
