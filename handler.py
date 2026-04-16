#!/usr/bin/env python3
"""
AutoForge Handler
Receives image URL + config, runs AutoForge, uploads ZIP to GitHub releases.

Supports two entry points:
1. RunPod Serverless — runpod.start() passes events to handler()
2. khris-gpu local — AUTOFORGE_CONFIG env var provides config as JSON
Strictly stage-based, stateless, structured output.
"""

import os
import json
import math
import sys
import time
import tempfile
import zipfile
import shutil
import urllib.request
import urllib.parse
import urllib.error
from typing import Optional

from autoforge_wrapper import AutoForgeWrapper

# ── Config ──────────────────────────────────────────────────────────────────

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_OWNER = os.environ.get("GITHUB_OWNER", "makawuhu")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "autoforge-worker")

# ── Helpers ─────────────────────────────────────────────────────────────────


def _fail(stage: str, error: str, **extra) -> dict:
    """Return a structured failure response."""
    ret = {"ok": False, "stage": stage, "error": error}
    ret.update(extra)
    print(f"[FAIL] stage={stage} error={error}")
    return ret


def _ok(stage: str, **fields) -> dict:
    """Return a structured success response."""
    ret = {"ok": True, "stage": stage}
    ret.update(fields)
    return ret


def _download_file(url: str, dest_dir: str) -> Optional[str]:
    """Download a file to dest_dir, return local path or None."""
    if not url:
        return None
    filename = url.split("/")[-1].split("?")[0] or "input_file"
    dest = os.path.join(dest_dir, filename)
    try:
        urllib.request.urlretrieve(url, dest)
        print(f"[download] {url} → {dest} ({os.path.getsize(dest)} bytes)")
        return dest
    except Exception as e:
        print(f"[download] FAILED {url}: {e}")
        return None


def _upload_to_github(zip_path: str, job_id: str, material_count: int, layer_count: int) -> str:
    """Create a GitHub release and upload the ZIP. Returns public download URL."""
    filename = os.path.basename(zip_path)
    tag_name = f"autoforge-{job_id[:12]}"
    release_name = f"AutoForge Output ({job_id[:8]})"
    release_body = json.dumps({
        "job_id": job_id,
        "material_count": material_count,
        "layer_count": layer_count,
        "zip_size_bytes": os.path.getsize(zip_path),
    })

    # Create release
    create_url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases"
    payload = json.dumps({
        "tag_name": tag_name,
        "name": release_name,
        "body": release_body,
        "draft": False,
        "prerelease": False,
    }).encode()

    req = urllib.request.Request(
        create_url, data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            release = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 422:
            # Tag already exists — find and reuse the existing release
            existing_url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/tags/{urllib.parse.quote(tag_name)}"
            req2 = urllib.request.Request(existing_url, headers={
                "Authorization": f"token {GITHUB_TOKEN}",
                "Accept": "application/vnd.github+json",
            })
            with urllib.request.urlopen(req2, timeout=10) as resp2:
                release = json.loads(resp2.read())
            print(f"[upload] Reusing existing release {release.get('id')} tag={tag_name}")
        else:
            raise

    release_id = release.get("id")
    upload_url = release.get("upload_url", "").replace(
        "{?name,label}", f"?name={urllib.parse.quote(filename)}"
    )
    html_url = release.get("html_url")
    print(f"[upload] Created release {release_id} tag={tag_name}")

    # Upload asset
    with open(zip_path, "rb") as f:
        zip_data = f.read()

    max_retries = 3
    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(
                upload_url, data=zip_data,
                headers={
                    "Content-Type": "application/zip",
                    "Content-Length": str(len(zip_data)),
                    "Authorization": f"token {GITHUB_TOKEN}",
                    "Accept": "application/vnd.github+json",
                },
            )
            with urllib.request.urlopen(req, timeout=300) as resp:
                asset = json.loads(resp.read())
            break
        except urllib.error.HTTPError as e:
            if e.code == 422 and attempt < max_retries - 1:
                # Asset name conflict — try with unique suffix
                suffix = f"-{int(time.time())}"
                upload_url_retry = release.get("upload_url", "").replace(
                    "{?name,label}", f"?name={urllib.parse.quote(filename.replace('.zip', f'{suffix}.zip'))}"
                )
                upload_url = upload_url_retry
                print(f"[upload] Retrying with unique filename (attempt {attempt+1})")
                continue
            elif e.code == 422:
                error_body = e.read().decode() if e.fp else ""
                raise RuntimeError(f"GitHub upload failed after {max_retries} attempts: 422 {error_body}")
            else:
                raise

    download_url = asset.get("browser_download_url", html_url)
    print(f"[upload] Asset URL: {download_url}")
    return download_url


# ── Background Height Calculation ───────────────────────────────────────────

def _bg_height(layer_height: float) -> float:
    """
    Calculate optimal background height based on layer height.
    Ensures enough base layers for structural integrity.
    Formula: n = max(7, ceil(0.56 / layer_height)), bg_height = n * layer_height
    The while loop ensures n * layer_height is an integer multiple of layer_height.
    """
    lh = float(layer_height)
    n = max(7, math.ceil(0.56 / lh))
    # Ensure clean integer layer count
    while not (round(n * lh, 6) / lh).is_integer():
        n += 1
    return round(n * lh, 6)


# ── Core Pipeline ──────────────────────────────────────────────────────────


def run_autoforge(
    image_path: str,
    output_dir: str,
    csv_path: Optional[str] = None,
    json_path: Optional[str] = None,
    pruning_max_colors: int = 8,
    pruning_max_swaps: int = 20,
    layer_height: float = 0.04,
    max_layers: int = 75,
    iterations: int = 2000,
    background_height: Optional[float] = None,
    nozzle_diameter: Optional[float] = None,
    flatforge: bool = False,
    cap_layers: int = 0,
) -> dict:
    """Run AutoForge and return metadata dict."""
    if not csv_path or not os.path.exists(csv_path):
        raise FileNotFoundError(
            f"CSV file required for AutoForge but not found at: {csv_path}. "
            "Please provide a valid csv_url in the request."
        )

    wrapper = AutoForgeWrapper(output_dir=output_dir)
    result = wrapper.run(
        input_image=image_path,
        csv_file=csv_path,
        json_file=json_path,
        pruning_max_colors=pruning_max_colors,
        pruning_max_swaps=pruning_max_swaps,
        layer_height=layer_height,
        max_layers=max_layers,
        iterations=iterations,
        background_height=background_height,
        nozzle_diameter=nozzle_diameter,
        flatforge=flatforge,
        cap_layers=cap_layers,
    )
    print(f"[process] wrapper result keys: {list(result.keys())}")
    print(f"[process] material_count={result.get('material_count')} layer_count={result.get('layer_count')}")

    # Package outputs into a zip
    zip_path = os.path.join(output_dir, "autoforge_output.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for fname in os.listdir(output_dir):
            fpath = os.path.join(output_dir, fname)
            if fname == "autoforge_output.zip":
                continue
            if os.path.isfile(fpath):
                zf.write(fpath, fname)
            elif os.path.isdir(fpath):
                for sub in os.listdir(fpath):
                    subpath = os.path.join(fpath, sub)
                    if os.path.isfile(subpath):
                        zf.write(subpath, os.path.join(fname, sub))
    print(f"[package] zip created: {zip_path} ({os.path.getsize(zip_path)} bytes)")

    return {
        "zip_path": zip_path,
        "zip_size_bytes": os.path.getsize(zip_path),
        "stl_files": result.get("stl_files", []),
        "material_count": result.get("material_count"),
        "layer_count": result.get("layer_count"),
        "voxel_dimensions": result.get("voxel_dimensions", {}),
        "flatforge": flatforge,
    }


# ── Handler ────────────────────────────────────────────────────────────────


def handler(event):
    """
    RunPod Serverless handler. Stage-based, stateless, structured output.

    Input (event["input"] or event for queue-based):
        image_url: URL to input image (required)
        csv_url: URL to materials CSV (required)
        json_url: URL to personal_library.json (optional)
        pruning_max_colors: int (default 8)
        pruning_max_swaps: int (default 20)
        layer_height: float (default 0.04)
        max_layers: int (default 75)
        iterations: int (default 2000)
        flatforge: bool (default False)
        cap_layers: int (default 0)
    """
    work_dir = None

    try:
        job_id = event.get("id", "unknown")

        # ── Phase 1: Validate ──────────────────────────────────────────
        print(f"[validate] job_id={job_id}")

        raw = event.get("input", event)

        if not GITHUB_TOKEN:
            return _fail("validate", "GITHUB_TOKEN environment variable not set")

        image_url = raw.get("image_url") or raw.get("image")
        if not image_url:
            return _fail("validate", "image_url is required")

        csv_url = raw.get("csv_url") or raw.get("csv_file")
        if not csv_url:
            return _fail("validate", "csv_url is required")

        # Parse numeric params with safe defaults
        # Calculate background_height dynamically if not explicitly provided
        layer_height = float(raw.get("layer_height", 0.04))
        bg_height_raw = raw.get("background_height")
        if bg_height_raw is not None:
            background_height = float(bg_height_raw)
        else:
            background_height = _bg_height(layer_height)

        nozzle_diameter_raw = raw.get("nozzle_diameter")
        nozzle_diameter = float(nozzle_diameter_raw) if nozzle_diameter_raw is not None else None

        params = {
            "pruning_max_colors": int(raw.get("pruning_max_colors", 8)),
            "pruning_max_swaps": int(raw.get("pruning_max_swaps", 20)),
            "layer_height": layer_height,
            "max_layers": int(raw.get("max_layers", 75)),
            "iterations": int(raw.get("iterations", 2000)),
            "background_height": background_height,
            "nozzle_diameter": nozzle_diameter,
            "flatforge": bool(raw.get("flatforge", False)),
            "cap_layers": int(raw.get("cap_layers", 0)),
        }
        print(f"[validate] OK image_url={image_url} csv_url={csv_url} params={params}")

        # ── Phase 2: Download ───────────────────────────────────────────
        work_dir = tempfile.mkdtemp(prefix="autoforge_")
        print(f"[download] work_dir={work_dir}")

        local_image = _download_file(image_url, work_dir)
        if not local_image or not os.path.exists(local_image):
            return _fail("download", f"Failed to download image from {image_url}")

        csv_path = _download_file(csv_url, work_dir)
        if not csv_path or not os.path.exists(csv_path):
            return _fail("download", f"Failed to download CSV from {csv_url}")

        json_path = _download_file(raw.get("json_url") or raw.get("json_file"), work_dir)
        print(f"[download] OK image={local_image} csv={csv_path}")

        # ── Phase 3: Process (AutoForge pipeline) ───────────────────────
        print(f"[process] starting AutoForge")
        result = run_autoforge(
            image_path=local_image,
            output_dir=work_dir,
            csv_path=csv_path,
            json_path=json_path,
            **params,
        )
        print(f"[process] OK zip_size={result['zip_size_bytes']} bytes")

        # ── Phase 4: Package ────────────────────────────────────────────
        # ZIP already created in run_autoforge — just verify it
        zip_path = result["zip_path"]
        if not os.path.exists(zip_path):
            return _fail("package", f"ZIP not found at {zip_path}")
        print(f"[package] OK zip={zip_path}")

        # ── Phase 5: Upload ─────────────────────────────────────────────
        download_url = _upload_to_github(
            zip_path=zip_path,
            job_id=job_id,
            material_count=result["material_count"],
            layer_count=result["layer_count"],
        )
        print(f"[upload] OK url={download_url}")

        # ── Phase 6: Return ─────────────────────────────────────────────
        return _ok("return",
            status="completed",
            download_url=download_url,
            zip_size_bytes=result["zip_size_bytes"],
            material_count=result["material_count"],
            layer_count=result["layer_count"],
            stl_files=result["stl_files"],
            voxel_dimensions=result["voxel_dimensions"],
            flatforge=result["flatforge"],
        )

    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        return _fail("process", str(e), traceback=tb)

    finally:
        if work_dir and os.path.exists(work_dir):
            shutil.rmtree(work_dir, ignore_errors=True)
            print(f"[cleanup] removed {work_dir}")


# ── khris-gpu Entry Point ────────────────────────────────────────────────────

def _run_from_env():
    """
    Entry point for khris-gpu (local GPU compute node).
    Reads AUTOFORGE_CONFIG env var, builds an event dict, and calls handler().
    """
    config_json = os.environ.get("AUTOFORGE_CONFIG")
    if not config_json:
        print("[FAIL] AUTOFORGE_CONFIG env var not set. No job to run.")
        return

    try:
        config = json.loads(config_json)
    except json.JSONDecodeError as e:
        print(f"[FAIL] AUTOFORGE_CONFIG is not valid JSON: {e}")
        return

    # Build an event dict that handler() understands
    # Use a deterministic job_id from timestamp + pid
    job_id = f"local-{int(time.time())}-{os.getpid()}-{id(config) % 10000:04d}"
    event = {"id": job_id, "input": config}

    print(f"[khris-gpu] Starting AutoForge job {job_id}")
    print(f"[khris-gpu] Config: {json.dumps(config, indent=2)}")

    result = handler(event)

    # Print the result as JSON — the khris-gpu worker captures stdout
    print(f"\n[khris-gpu] Result:")
    print(json.dumps(result, indent=2))

    # Exit with non-zero if the job failed
    if not result.get("ok", False):
        sys.exit(1)


if __name__ == "__main__":
    # Detect which entry point to use
    if os.environ.get("AUTOFORGE_CONFIG"):
        # khris-gpu local compute node
        import sys
        import time
        _run_from_env()
    else:
        # RunPod Serverless
        import runpod
        runpod.serverless.start({"handler": handler})
