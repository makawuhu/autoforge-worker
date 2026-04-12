import base64
import gzip
import math
import os
import subprocess
import tempfile

import runpod


def _bg_height(layer_height: float) -> float:
    """Return background height as a valid multiple of layer_height (min 7 layers, min 0.56mm)."""
    lh = float(layer_height)
    n = max(7, math.ceil(0.56 / lh))
    while not (round(n * lh, 6) / lh).is_integer():
        n += 1
    return round(n * lh, 6)


def handler(job):
    inp = job["input"]

    image_b64    = inp["image_b64"]
    csv_content  = inp["csv_content"]
    layer_height = str(inp.get("layer_height", "0.08"))
    nozzle       = float(inp.get("nozzle", 0.4))
    max_layers   = int(inp.get("max_layers", 75))
    max_swaps    = int(inp.get("max_swaps", 8))
    max_colors   = int(inp.get("max_colors", 4))
    bg_h         = _bg_height(float(layer_height))

    with tempfile.TemporaryDirectory() as tmpdir:
        img_path = os.path.join(tmpdir, "image.png")
        with open(img_path, "wb") as f:
            f.write(base64.b64decode(image_b64))

        csv_path = os.path.join(tmpdir, "filaments.csv")
        with open(csv_path, "w") as f:
            f.write(csv_content)

        out_dir = os.path.join(tmpdir, "output")
        os.makedirs(out_dir)

        cmd = [
            "autoforge",
            "--input_image", img_path,
            "--csv_file",    csv_path,
            "--max_layers",           str(max_layers),
            "--pruning_max_swaps",    str(max_swaps),
            "--pruning_max_colors",   str(max_colors),
            "--layer_height",         layer_height,
            "--background_height",    str(bg_h),
            "--nozzle_diameter",      str(nozzle),
            "--output_folder",        out_dir,
        ]

        proc = subprocess.run(cmd, capture_output=True, text=True)
        log = proc.stdout
        if proc.stderr.strip():
            log += "\n" + proc.stderr

        # Write run.log into output so it gets returned with the other files
        with open(os.path.join(out_dir, "run.log"), "w") as f:
            f.write(log)

        if proc.returncode != 0:
            return {
                "error": f"AutoForge exited with code {proc.returncode}",
                "log":   log,
            }

        # Collect output files, gzip + base64 encode each one
        files = {}
        for root, _, filenames in os.walk(out_dir):
            for fname in filenames:
                fpath = os.path.join(root, fname)
                rel   = os.path.relpath(fpath, out_dir)
                with open(fpath, "rb") as f:
                    raw = f.read()
                files[rel] = base64.b64encode(gzip.compress(raw, compresslevel=6)).decode()

        return {"files": files, "log": log}


runpod.serverless.start({"handler": handler})
