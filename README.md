# 🌏 Python Webflow Exporter

[![PyPI version](https://img.shields.io/pypi/v/python-webflow-exporter)](https://pypi.org/project/python-webflow-exporter/)
[![python](https://img.shields.io/badge/Python-3.10-3776AB.svg?style=flat&logo=python&logoColor=white)](https://www.python.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://spdx.org/licenses/MIT.html)

A command-line tool to recursively scrape and download all assets (HTML, CSS, JS, images, media) from a public `.webflow.io` website. It also provides the option to automatically remove the Webflow badge from downloaded JavaScript files.

> [!CAUTION]
> ⚠️ DISCLAIMER: This repository is intended for **educational and personal use only**. It includes scripts and tools that may interact with websites created using Webflow. **The purpose of this repository is not to harm, damage, or interfere with Webflow’s platform, branding, or services.**
> By using this repository, you agree to the following:
>
> - You are solely responsible for how you use the contents of this repository.
> - The author does not condone the use of this code for commercial projects or to violate Webflow’s terms of service.
> - The author is not affiliated with Webflow Inc. in any way.
> - The author assumes no liability or responsibility for any damage, loss, or legal issues resulting from the use of this repository.
>
> If you are unsure about whether your intended use complies with applicable laws or platform terms, please consult legal counsel or refrain from using this repository.

## Features

- Recursively scans and downloads:
  - All linked internal pages
  - Stylesheets, JavaScript, images, and media files from Webflow CDN
- Optional removal of Webflow badge
- Fast processing
- Complete export of site
- Automatic creation of a sitemap.xml

## Installation

### Option 1: Install with pip

```bash
pip install python-webflow-exporter
```

### Option 2: Run directly with uv (recommended for one-time use)

First, install `uv` if you haven't already:

**Linux/macOS:**
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**macOS (with Homebrew):**
```bash
brew install uv
```

**Windows:**
```bash
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

For other installation methods, see the [uv installation guide](https://docs.astral.sh/uv/getting-started/installation/).

Then run the tool directly:

```bash
uv tool run --from python-webflow-exporter webexp --url https://example.webflow.io
```

No installation required - `uv` will automatically handle dependencies and run the tool.

## Usage

### After pip installation

```bash
webexp --url https://example.webflow.io
```

### Run as an HTTP API

You can expose the exporter over HTTP (ideal for hosting on DigitalOcean and serving a Next.js frontend) by running the FastAPI app with uvicorn:

```bash
uvicorn webexp.api:app --host 0.0.0.0 --port 8000
```

Once the server is running, create an export job:

```bash
curl -X POST \
     -H "Content-Type: application/json" \
     -d '{
           "url": "https://example.webflow.io",
           "remove_badge": true,
           "generate_sitemap": true
         }' \
     http://localhost:8000/exports
# => {"job_id": "d2f2c6..."}
```

Poll the job to view live progress events:

```bash
curl http://localhost:8000/exports/<job_id>/progress
```

When the job status becomes `complete` (and `file_ready` is true), download the archive:

```bash
curl -L -o webflow-export.zip http://localhost:8000/exports/<job_id>/download
```

Each archive contains the exported site plus `manifest.json` and `progress.json` files describing the assets and recorded steps. A basic health check is available at `GET /health`.

### Arguments

| Argument             | Description                                | Default | Required |
| -------------------- | ------------------------------------------ | ------- | -------- |
| `--help`             | Show a help with available commands        | -       | ❌       |
| `--version`          | Print the current version                  | -       | ❌       |
| `--url`              | The public Webflow site URL to scrape      | –       | ✅       |
| `--output`           | Output folder where the site will be saved | out     | ❌       |
| `--remove-badge`     | remove Webflow badge                       | false   | ❌       |
| `--generate-sitemap` | generate a sitemap.xml file                | false   | ❌       |
| `--debug`            | Enable debug output                        | false   | ❌       |
| `--silent`           | Enable silent, no output                   | false   | ❌       |

### Output

After execution, your specified output folder will contain:

- All crawled HTML pages
- Associated assets like CSS, JS, images, and media
- Cleaned HTML and JS files with Webflow references rewritten
- Optionally removing the webflow badge

## Development Requirements

Make sure you have Python 3.8+ installed. Required packages are:

- requests
- argparse
- beautifulsoup4
- halo
- fastapi
- uvicorn

_Optional:_

- pyinstaller
- pylint

They are included in `requirements.txt`.


## Deploying to Render

The repository includes a `render.yaml` blueprint that provisions a free Python web service running the FastAPI API.

1. **Push your fork to GitHub/GitLab** – Render deploys from a Git repository. Ensure `render.yaml` sits in the repository root.
2. **Create a new Blueprint deployment** in Render and point it at your repository. Render will detect `render.yaml` and configure the service using the supplied build and start commands (`pip install -r requirements.txt` and `uvicorn webexp.api:app --host 0.0.0.0 --port $PORT`).
3. **Set environment variables** when prompted:
   - `CORS_ALLOW_ORIGINS` – comma-separated list of origins allowed to call the API, for example `https://your-next-app.netlify.app`.
   - `PYTHONUNBUFFERED` is already set in the blueprint to keep logs streaming in real time.
4. **Trigger the first deploy**. Once the service status becomes “Live”, visit `https://<your-service>.onrender.com/health` to verify it responds with `{"status": "ok"}`.

Render automatically exposes the public URL that you can reference from your Next.js frontend. Adjust `CORS_ALLOW_ORIGINS` whenever you add more client applications.

## Next.js integration

You can connect a local Next.js app to the exporter while both projects run on your machine. The example below uses the App Router (Next.js 13+), but the same idea works with the Pages Router.

1. **Run the FastAPI server**
   ```bash
   source .venv/bin/activate  # or your preferred venv activation
   uvicorn webexp.api:app --host 127.0.0.1 --port 8000
   ```
2. **Expose the base URL to Next.js** – add the following to `.env.local` inside your Next.js project:
   ```env
   NEXT_PUBLIC_WFEXP_BASE_URL=http://127.0.0.1:8000
   ```
3. **Create a small client helper** (optional but keeps code tidy). For example, create `lib/wfexp-client.ts`:
   ```ts
   const baseUrl = process.env.NEXT_PUBLIC_WFEXP_BASE_URL ?? 'http://127.0.0.1:8000';
   
   type ExportPayload = {
     url: string;
     remove_badge?: boolean;
     generate_sitemap?: boolean;
     debug?: boolean;
     silent?: boolean;
     output_name?: string;
   };
   
   export async function requestExport(payload: ExportPayload): Promise<Blob> {
     const res = await fetch(`${baseUrl}/exports`, {
       method: 'POST',
       headers: { 'Content-Type': 'application/json' },
       body: JSON.stringify(payload),
     });
   
     if (!res.ok) {
       const errorText = await res.text();
       throw new Error(`Exporter error (${res.status}): ${errorText}`);
     }
   
     return await res.blob();
   }
   ```
4. **Add a Next.js API route** to proxy requests from the browser. This prevents the browser from downloading the ZIP directly and gives you a place to add auth/logging. Create `app/api/export/route.ts` (or `pages/api/export.ts` in Pages Router projects):
   ```ts
   import { NextRequest } from 'next/server';
   
   const baseUrl = process.env.NEXT_PUBLIC_WFEXP_BASE_URL ?? 'http://127.0.0.1:8000';
   
   export async function POST(req: NextRequest) {
     const payload = await req.json();
     const exporterResponse = await fetch(`${baseUrl}/exports`, {
       method: 'POST',
       headers: { 'Content-Type': 'application/json' },
       body: JSON.stringify(payload),
     });
   
     if (!exporterResponse.ok) {
       const text = await exporterResponse.text();
       return new Response(text, { status: exporterResponse.status });
     }
   
     const buffer = await exporterResponse.arrayBuffer();
     return new Response(buffer, {
       status: 200,
       headers: {
         'Content-Type': 'application/zip',
         'Content-Disposition': 'attachment; filename="webflow-export.zip"',
       },
     });
   }
   ```
5. **Call the API route from your UI** – for example, inside a React Server Action or client component:
   ```ts
   const response = await fetch('/api/export', {
     method: 'POST',
     headers: { 'Content-Type': 'application/json' },
     body: JSON.stringify({
       url: 'https://example.webflow.io',
       remove_badge: true,
       generate_sitemap: true,
     }),
   });
   
   if (!response.ok) {
     throw new Error('Export failed');
   }
   
   const blob = await response.blob();
   const downloadUrl = URL.createObjectURL(blob);
   const link = document.createElement('a');
   link.href = downloadUrl;
   link.download = 'webflow-export.zip';
   link.click();
   URL.revokeObjectURL(downloadUrl);
   ```

When the exporter runs on Render, set `NEXT_PUBLIC_WFEXP_BASE_URL` (and any server-side equivalents) to the Render service URL, e.g. `https://python-webflow-exporter.onrender.com`. Because the FastAPI app now reads allowed origins from `CORS_ALLOW_ORIGINS`, make sure that variable includes your Netlify domain so browser requests succeed.

## Local development

Clone the script and run the following commands to test it

```bash
git clone https://github.com/KoblerS/python-webflow-exporter.git
cd python-webflow-exporter

pip install -e .
```

Refer to [#usage](#usage) for more information on how to use the CLI.

## License

This project is released under the [MIT License](https://github.com/KoblerS/python-webflow-exporter/blob/main/LICENSE.md).

## Disclaimer

This tool is provided "as-is" without any warranties. The author is not responsible for misuse or damage caused by this software. For full terms, see [DISCLAIMER.md](https://github.com/KoblerS/python-webflow-exporter/blob/main/DISCLAIMER.md).
