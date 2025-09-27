"""FastAPI application exposing the Webflow exporter as an HTTP API."""

from __future__ import annotations

import io
import json
import os
import tempfile
import zipfile
from typing import Any, Dict

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, HttpUrl

from .cli import VERSION_NUM, run_export

app = FastAPI(
    title="Python Webflow Exporter API",
    version=VERSION_NUM,
    description="HTTP wrapper around the python-webflow-exporter CLI workflow.",
)


class ExportRequest(BaseModel):
    """Payload describing an export job."""

    url: HttpUrl
    remove_badge: bool = False
    generate_sitemap: bool = False
    debug: bool = False
    silent: bool = False
    output_name: str | None = None


@app.get("/health")
def healthcheck() -> Dict[str, Any]:
    """Simple health endpoint."""

    return {"status": "ok", "version": VERSION_NUM}


@app.post("/exports")
def create_export(request: ExportRequest) -> StreamingResponse:
    """Run an export and stream the resulting archive back to the client."""

    if request.debug and request.silent:
        raise HTTPException(status_code=400, detail="'debug' and 'silent' options cannot be combined")

    with tempfile.TemporaryDirectory() as tmp_root:
        output_dir = os.path.join(tmp_root, "export")

        try:
            result = run_export(
                url=str(request.url),
                output=output_dir,
                remove_badge=request.remove_badge,
                create_sitemap=request.generate_sitemap,
                debug=request.debug,
                silent=request.silent,
                use_spinner=False,
                ensure_parent_dir=True,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        manifest = result["assets"]
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            for root, _, files in os.walk(result["output_path"]):
                for filename in files:
                    file_path = os.path.join(root, filename)
                    arcname = os.path.relpath(file_path, result["output_path"])
                    zf.write(file_path, arcname)
            zf.writestr("manifest.json", json.dumps(manifest, indent=2))

        archive.seek(0)
        filename = request.output_name or "webflow-export.zip"
        if not filename.endswith(".zip"):
            filename = f"{filename}.zip"

        headers = {
            "Content-Disposition": f"attachment; filename={filename}",
        }

        return StreamingResponse(
            archive,
            media_type="application/zip",
            headers=headers,
        )
