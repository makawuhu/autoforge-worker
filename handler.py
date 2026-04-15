#!/usr/bin/env python3
"""
AutoForge Runpod Serverless Handler
Receives image URL + config, runs AutoForge, uploads ZIP to Gitea releases.
"""

import os
import io
import json
import tempfile
import zipfile
import shutil
import urllib.request
import urllib.parse
from typing import Optional
from pathlib import Path

from autoforge_wrapper import AutoForgeWrapper

# GitHub config for output hosting (publicly accessible)
import base64

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
if not GITHUB_TOKEN:
    raise RuntimeError("GITHUB_TOKEN environment variable not set")
GITHUB_OWNER = os.environ.get("GITHUB_OWNER", "makawuhu")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "autoforge-worker")


def upload_to_github(zip_path: str, job_id: str, material_count: int, layer_count: int) -> str:
    """Create a GitHub release and upload the ZIP. Returns the public download URL."""
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
    create_payload = json.dumps({
        "tag_name": tag_name,
        "name": release_name,
        "body": release_body,
        "draft": False,
        "prerelease": False,
    }).encode()

    req = urllib.request.Request(
        create_url, data=create_payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
        }
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        release = json.loads(resp.read())

    release_id = release.get("id")
    upload_url = release.get("upload_url", "").replace("{?name,label}", f"?name={urllib.parse.quote(filename)}")
    html_url = release.get("html_url")

    print(f"[GitHub] Created release {release_id} with tag {tag_name}")

    # Upload asset
    with open(zip_path, "rb") as f:
        zip_data = f.read()

    req = urllib.request.Request(
        upload_url,
        data=zip_data,
        headers={
            "Content-Type": "application/zip",
            "Content-Length": str(len(zip_data)),
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
        },
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        asset = json.loads(resp.read())

    download_url = asset.get("browser_download_url", html_url)
    print(f"[GitHub] Asset URL: {download_url}")
    return download_url


def download_file(url: str, dest_dir: str) -> Optional[str]:
    """Download a file to temp dir, return local path or None."""
    if not url:
        return None
    filename = url.split("/")[-1].split("?")[0] or "input_file"
    dest = os.path.join(dest_dir, filename)
    try:
        urllib.request.urlretrieve(url, dest)
        return dest
    except Exception as e:
        print(f"Warning: failed to download {url}: {e}")
        return None


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
    flatforge: bool = False,
    cap_layers: int = 0,
) -> dict:
    """Run AutoForge and return metadata."""
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
        flatforge=flatforge,
        cap_layers=cap_layers,
    )

    # Package outputs into a zip
    zip_path = os.path.join(output_dir, "autoforge_output.zip")
    
    # Write diagnostic sidecar — this is never truncated by Runpod
    import json as _json
    diag = {
        "run_autoforge_material_count": result.get("material_count", 0),
        "run_autoforge_layer_count": result.get("layer_count", 0),
        "run_autoforge_keys": list(result.keys()),
        "voxel_dimensions": result.get("voxel_dimensions", {}),
    }
    diag_path = os.path.join(output_dir, "_diag.json")
    with open(diag_path, 'w') as f:
        _json.dump(diag, f)
    print(f"[DEBUG ZIP] _diag.json contents: {diag}", flush=True)
    
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
    print(f"[DEBUG ZIP] zip created, files in zip: {os.listdir(output_dir)}", flush=True)

    return {
        "zip_path": zip_path,
        "zip_size_bytes": os.path.getsize(zip_path),
        "stl_files": result.get("stl_files", []),
        "material_count": result.get("material_count"),
        "layer_count": result.get("layer_count"),
        "voxel_dimensions": result.get("voxel_dimensions", {}),
        "flatforge": flatforge,
    }


def handler(event):
    """
    Runpod Serverless handler.

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
    try:
        job_id = event.get("id", "unknown")

        # Support both queue-based (event["input"]) and direct calls
        if "input" in event:
            raw = event["input"]
        else:
            raw = event

        # Parse input
        image_url = raw.get("image_url") or raw.get("image")
        if not image_url:
            return {"error": "image_url is required", "status": "failed"}

        work_dir = tempfile.mkdtemp(prefix="autoforge_")
        print(f"[AutoForge] Work dir: {work_dir}")
        print(f"[AutoForge] Image URL: {image_url}")

        # Download input image
        local_image = download_file(image_url, work_dir)
        if not local_image or not os.path.exists(local_image):
            raise FileNotFoundError(f"Failed to download image from {image_url}")

        # Download optional material files
        csv_path = download_file(raw.get("csv_url") or raw.get("csv_file"), work_dir)
        json_path = download_file(raw.get("json_url") or raw.get("json_file"), work_dir)

        # Run AutoForge
        result = run_autoforge(
            image_path=local_image,
            output_dir=work_dir,
            csv_path=csv_path,
            json_path=json_path,
            pruning_max_colors=int(raw.get("pruning_max_colors", 8)),
            pruning_max_swaps=int(raw.get("pruning_max_swaps", 20)),
            layer_height=float(raw.get("layer_height", 0.04)),
            max_layers=int(raw.get("max_layers", 75)),
            iterations=int(raw.get("iterations", 2000)),
            flatforge=bool(raw.get("flatforge", False)),
            cap_layers=int(raw.get("cap_layers", 0)),
        )

        print(f"[AutoForge] Done. zip_size={result['zip_size_bytes']} bytes")
        print(f"[AutoForge] Uploading to GitHub...")

        # Upload to GitHub releases and get download URL
        marker = f"MARKER_DEBUG_1234_HASH_{job_id[:8]}"
        print(f"[AutoForge] {marker}")
        print(f"[AutoForge] run_autoforge result keys: {list(result.keys())}")
        print(f"[AutoForge] material_count={result.get('material_count')}, layer_count={result.get('layer_count')}, stl_files={result.get('stl_files')}, voxel={result.get('voxel_dimensions')}")
        print(f"[AutoForge] wrapper result: {result}")

        download_url = upload_to_github(
            zip_path=result["zip_path"],
            job_id=job_id,
            material_count=result["material_count"],
            layer_count=result["layer_count"],
        )

        print(f"[AutoForge] UPLOAD_COMPLETE marker={marker}")

        ret = {
            "status": "completed",
            "download_url": download_url,
            "zip_size_bytes": result["zip_size_bytes"],
            "stl_files": result["stl_files"],
            "material_count": result["material_count"],
            "layer_count": result["layer_count"],
            "voxel_dimensions": result["voxel_dimensions"],
            "flatforge": result["flatforge"],
            "_debug_marker": marker,
            "_result_keys": list(result.keys()),
            "_material_count_raw": result.get('material_count'),
            "_layer_count_raw": result.get('layer_count'),
        }
        print(f"[AutoForge] Returning: {ret}")
        return ret

    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"[AutoForge] Error: {e}\n{tb}")
        return {"error": str(e), "status": "failed", "traceback": tb}

    finally:
        if "work_dir" in locals():
            shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    import runpod
    runpod.serverless.start({"handler": handler})
