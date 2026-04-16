# AutoForge RunPod Serverless Worker

A serverless worker that converts images into 3D-printable layered STL files using [AutoForge](https://github.com/hvoss-techfak/AutoForge). Deployed on RunPod Serverless, triggered via API, outputs delivered as GitHub Release ZIPs.

## Architecture

```
┌──────────────┐     ┌──────────────────────────────────────────────┐     ┌──────────────┐
│  Your App    │────▶│  RunPod Serverless Endpoint (htmxqulr6focry) │────▶│  GitHub      │
│  (Frontend)  │     │                                              │     │  Releases    │
│              │◀────│  handler.py → autoforge_wrapper.py           │     │  (ZIP download)│
└──────────────┘     │       ↓                                       │     └──────────────┘
                     │  AutoForge CLI (subprocess)                   │
                     │       ↓                                       │
                     │  ZIP → Upload to GitHub → Return URL         │
                     └──────────────────────────────────────────────┘
```

## Data Flow

1. **Submit** — `POST /v2/{endpoint_id}/run` with image URL + parameters
2. **Download** — Handler fetches input image + CSV from URLs
3. **Process** — AutoForge CLI runs optimization, produces STL + instructions
4. **Package** — All outputs zipped into `autoforge_output.zip`
5. **Upload** — ZIP uploaded to GitHub Releases as a public asset
6. **Return** — Structured response with download URL + metadata

## Handler Output Format

Every response follows the ChatGPT plan's stage-based structure:

### Success
```json
{
  "ok": true,
  "stage": "return",
  "status": "completed",
  "download_url": "https://github.com/makawuhu/autoforge-worker/releases/download/autoforge-{job_id}/autoforge_output.zip",
  "zip_size_bytes": 17738083,
  "material_count": 3,
  "layer_count": 12,
  "stl_files": [],
  "voxel_dimensions": {"width": 750, "height": 400},
  "flatforge": false
}
```

### Failure
```json
{
  "ok": false,
  "stage": "validate|download|process|package|upload",
  "error": "descriptive error message",
  "traceback": "..."
}
```

## Execution Phases

The handler strictly follows these phases, each with entry/exit logging:

| Phase | Description |
|-------|-------------|
| `validate` | Check required inputs (image_url, csv_url, GITHUB_TOKEN) |
| `download` | Fetch input image and CSV from URLs |
| `process` | Run AutoForge CLI via `autoforge_wrapper.py` |
| `package` | Create `autoforge_output.zip` from all output files |
| `upload` | Upload ZIP to GitHub Releases |
| `return` | Return structured response |

If any phase fails, the handler returns immediately with `ok: false` and the failed `stage`.

## Input Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `image_url` | string | *required* | URL to input image |
| `csv_url` | string | *required* | URL to materials CSV file |
| `json_url` | string | null | URL to personal_library.json |
| `pruning_max_colors` | int | 8 | Max colors after pruning |
| `pruning_max_swaps` | int | 20 | Max filament swaps |
| `layer_height` | float | 0.04 | Layer thickness in mm |
| `max_layers` | int | 75 | Maximum number of layers |
| `iterations` | int | 2000 | Optimization iterations |
| `flatforge` | bool | false | Enable FlatForge mode (separate STL per color) |
| `cap_layers` | int | 0 | Transparent cap layers (FlatForge only) |

## Output ZIP Contents

The `autoforge_output.zip` contains:

| File | Description |
|------|-------------|
| `final_model.stl` | The 3D-printable STL model (~50-60MB) |
| `final_model.png` | Composite color preview image |
| `swap_instructions.txt` | Filament swap instructions for BambuLab |
| `project_file.hfp` | HueForge project file |
| `00001-*.png` | Input image (echoed back) |
| `hue-forge-photo-pack.csv` | Materials CSV (echoed back) |
| `auto_background_color.txt` | Auto-detected background color |
| `spike_removal_stats.txt` | Spike removal statistics |
| `final_loss.txt` | Final optimization loss value |
| `vis_temp.png` | Visualization temp file |

### Swap Instructions Format
```
Print at 100% infill with a layer height of 0.12mm with a base layer of 0.24mm using background filament PolyTerra - Charcoal Black.

Start with PolyTerra - Charcoal Black, with a layer height of 0.24mm for the first layer.
At layer #2 (0.36mm) swap to PolyLite - Natural
At layer #8 (1.08mm) swap to PolyTerra - Charcoal Black
...
For the rest, use PolyLite - White
```

## Component: `autoforge_wrapper.py`

`AutoForgeWrapper` wraps the AutoForge CLI (`autoforge` command) for serverless use:

1. Constructs CLI arguments from structured input
2. Runs `autoforge` as a subprocess
3. Parses `swap_instructions.txt` to extract `material_count` and `layer_count`
4. Collects all output files into a metadata dict
5. Reads composite PNG dimensions for `voxel_dimensions`

Key method: `run()` → returns dict with `material_count`, `layer_count`, `voxel_dimensions`, `stl_files`, etc.

## Component: `handler.py`

The RunPod serverless handler following the stage-based protocol:

1. **Validate** — Checks `image_url`, `csv_url`, and `GITHUB_TOKEN` env var
2. **Download** — Fetches remote files to temp directory
3. **Process** — Calls `AutoForgeWrapper.run()`
4. **Package** — Creates `autoforge_output.zip` from all output files
5. **Upload** — Creates GitHub Release + uploads ZIP asset
6. **Return** — Returns structured `{ok, stage, ...}` response

Cleanup: temp directory is always removed in `finally` block.

## Deployment

### Current Endpoint
- **ID**: `htmxqulr6focry`
- **Template**: `gv6tyda2t8`
- **Image**: `ghcr.io/makawuhu/autoforge-worker:latest`
- **GPU**: RTX 3090 (serverless, pay-per-use)
- **Timeout**: 20 minutes (1200000ms)
- **Flashboot**: enabled

### Docker Image
Built from `nvidia/cuda:12.1.1-cudnn8-devel-ubuntu22.04` with:
- PyTorch cu121
- AutoForge (pip)
- OpenCV system deps (`libxcb1`, `libglib2.0-0`, `libgl1`, etc.)

### Environment Variables
| Variable | Required | Description |
|----------|----------|-------------|
| `GITHUB_TOKEN` | Yes | GitHub PAT for upload to Releases (needs `repo` scope) |

### API Endpoints
| Action | Method | URL |
|--------|--------|-----|
| Submit job | `POST` | `https://api.runpod.ai/v2/{endpoint_id}/run` |
| Sync submit | `POST` | `https://api.runpod.ai/v2/{endpoint_id}/runsync` |
| Check status | `GET` | `https://api.runpod.ai/v2/{endpoint_id}/status/{job_id}` |
| Health | `GET` | `https://api.runpod.ai/v2/{endpoint_id}/health` |

Headers: `Authorization: Bearer {RUNPOD_API_KEY}`

### Example Request
```bash
curl -X POST "https://api.runpod.ai/v2/htmxqulr6focry/run" \
  -H "Authorization: Bearer $RUNPOD_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "input": {
      "image_url": "https://example.com/photo.png",
      "csv_url": "https://example.com/materials.csv",
      "pruning_max_colors": 4,
      "pruning_max_swaps": 3,
      "layer_height": 0.12,
      "max_layers": 15,
      "iterations": 400
    }
  }'
```

### Example Response
```json
{
  "id": "1e573fdb-b594-4e9f-bf5a-043e5b963f90-u2",
  "status": "IN_QUEUE"
}
```

Poll for completion:
```bash
curl "https://api.runpod.ai/v2/htmxqulr6focry/status/1e573fdb-b594" \
  -H "Authorization: Bearer $RUNPOD_API_KEY"
```

## Performance Notes

| Config | Layers (output) | Time | GPU Cost |
|--------|----------------|------|----------|
| 0.12mm, 15 max, 400 iter, 3 swaps | 12 | ~2 min | ~$0.04 |
| 0.08mm, 80 max, 200 iter, 8 swaps | 21 | ~15 min | ~$0.30 |
| 0.08mm, 80 max, 500 iter, 8 swaps | timeout (>10min) | — | — |

- Lower `iterations` = faster but less optimized output
- Higher `max_layers` with low `pruning_max_swaps` → AutoForge prunes aggressively
- RTX 3090 at $0.000336/sec ($1.21/hr)

## RunPod Management API

### Template Updates (triggers rolling release)
```bash
curl -X PATCH "https://rest.runpod.io/v1/templates/{template_id}" \
  -H "Authorization: Bearer $RUNPOD_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"imageName": "ghcr.io/makawuhu/autoforge-worker:latest"}'
```

### Endpoint Updates (triggers worker refresh)
```bash
curl -X PATCH "https://rest.runpod.io/v1/endpoints/{endpoint_id}" \
  -H "Authorization: Bearer $RUNPOD_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"flashboot": true, "executionTimeoutMs": 1200000}'
```

### Key Lessons
- **Worker cache**: RunPod workers cache images. Use `PATCH /v1/templates/{id}` with new `imageName` to trigger a rolling release, NOT GraphQL.
- **OpenCV deps**: The `nvidia/cuda` base image lacks `libxcb1`, `libglib2.0-0`, etc. Must install them in Dockerfile.
- **Old endpoints**: If you delete an endpoint from the RunPod console, it may still exist in the queue API. Create a new endpoint via `POST /v1/endpoints` instead.

## Known Issues

1. **RunPod status API truncation**: `layer_count` and `material_count` previously showed `0` due to old cached worker images. This is now fixed — values come through correctly from the stage-based handler.

2. **Timeout**: 80+ layers at 400+ iterations can exceed 10-minute default timeout. Set `executionTimeoutMs` to at least 1200000 (20 min) for high-layer-count jobs.

3. **GitHub rate limits**: Release uploads use the GitHub API. Rate limit is 5000 requests/hr for authenticated users.

## File Locations

| File | Purpose |
|------|---------|
| `handler.py` | RunPod serverless handler (stage-based, structured output) |
| `autoforge_wrapper.py` | Wraps `autoforge` CLI for serverless use |
| `Dockerfile` | CUDA 12.1 + PyTorch cu121 + OpenCV deps |

GitHub: `makawuhu/autoforge-worker`
FileBrowser: `Lowe_docs/autoforge-code/`