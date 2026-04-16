#!/usr/bin/env python3
"""
khris-gpu API Client for AutoForge
Submits AutoForge jobs to the local GPU compute node instead of RunPod.

Usage:
    client = KhrisGPUClient()
    job = client.submit_autoforge(image_url=..., csv_url=..., iterations=200)
    result = client.wait_for_job(job["job_id"])

Docker invocation format (worker builds dynamically):
    docker run --rm --gpus all --shm-size {shm_size} --network {network}
        -e KEY=VAL ... -v /host:/container ... -w {workdir}
        --entrypoint {entrypoint} {image} bash -c {command}

Defaults: --shm-size 1g, --network host
"""

import os
import json
import time
import urllib.request
import urllib.parse
import urllib.error
from typing import Optional

# ── Config ──────────────────────────────────────────────────────────────────

KHRIS_GPU_URL = os.environ.get("KHRIS_GPU_URL", "https://khris-gpu.makawuhu.com")
KHRIS_GPU_API_KEY = os.environ.get("KHRIS_GPU_API_KEY", "")  # if auth is added later

# AutoForge Docker image
AUTOFORGE_IMAGE = os.environ.get("AUTOFORGE_IMAGE", "ghcr.io/makawuhu/autoforge-worker:latest")

# Polling defaults
DEFAULT_POLL_INTERVAL = 5   # seconds between status checks
DEFAULT_POLL_TIMEOUT = 1800 # max wait time (30 min)


# ── HTTP Helpers ─────────────────────────────────────────────────────────────

def _request(method: str, url: str, body: dict = None, headers: dict = None,
             timeout: int = 30) -> dict:
    """Make an HTTP request."""
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        error_body = e.read().decode() if e.fp else ""
        raise RuntimeError(
            f"API error: {e.code} {e.reason} — {error_body}"
        ) from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"API connection error: {e.reason}") from e


# ── Client ──────────────────────────────────────────────────────────────────

class KhrisGPUClient:
    """Client for the khris-gpu compute API (v2)."""

    def __init__(self, base_url: str = None, api_key: str = None):
        self.base_url = (base_url or KHRIS_GPU_URL).rstrip("/")
        self.api_key = api_key or KHRIS_GPU_API_KEY

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def _url(self, path: str, params: dict = None) -> str:
        url = f"{self.base_url}{path}"
        if params:
            url += f"?{urllib.parse.urlencode(params)}"
        return url

    def _req(self, method: str, path: str, body: dict = None,
             params: dict = None, timeout: int = 30) -> dict:
        return _request(method, self._url(path, params), body, self._headers(), timeout)

    # ── Service endpoints ──────────────────────────────────────────────

    def health(self) -> dict:
        """Health check with GPU status and queue depth."""
        return self._req("GET", "/health")

    def gpu_info(self) -> dict:
        """Detailed GPU information."""
        return self._req("GET", "/gpu")

    def queue_stats(self) -> dict:
        """Queue statistics."""
        return self._req("GET", "/queue/stats")

    # ── Job endpoints ──────────────────────────────────────────────────

    def submit_job(self, job_type: str, payload: dict,
                   priority: int = 0, timeout_seconds: int = 300,
                   callback_url: str = None) -> dict:
        """Submit a GPU compute job."""
        body = {
            "job_type": job_type,
            "payload": payload,
            "priority": max(0, min(10, priority)),
            "timeout_seconds": max(10, min(3600, timeout_seconds)),
        }
        if callback_url:
            body["callback_url"] = callback_url
        return self._req("POST", "/jobs", body)

    def get_job(self, job_id: str) -> dict:
        """Get job details and status."""
        return self._req("GET", f"/jobs/{job_id}")

    def list_jobs(self, status: str = None, limit: int = 50, offset: int = 0) -> dict:
        """List jobs, optionally filtered by status."""
        params = {"limit": limit, "offset": offset}
        if status:
            params["status"] = status
        return self._req("GET", "/jobs", params=params)

    def cancel_job(self, job_id: str) -> dict:
        """Cancel a queued job."""
        return self._req("DELETE", f"/jobs/{job_id}")

    # ── custom_docker / autoforge ───────────────────────────────────────

    def submit_docker(
        self,
        image: str,
        command: str,
        env: dict = None,
        volumes: dict = None,
        workdir: str = None,
        entrypoint: str = None,
        shm_size: str = "1g",
        network: str = "host",
        priority: int = 0,
        timeout_seconds: int = 300,
        callback_url: str = None,
    ) -> dict:
        """
        Submit a custom_docker job.

        The worker builds:
            docker run --rm --gpus all --shm-size {shm_size} --network {network}
                -e KEY=VAL ... -v /host:/container ... -w {workdir}
                --entrypoint {entrypoint} {image} bash -c {command}
        """
        payload = {
            "image": image,
            "command": command,
            "shm_size": shm_size,
            "network": network,
        }
        if env:
            payload["env"] = env
        if volumes:
            payload["volumes"] = volumes
        if workdir:
            payload["workdir"] = workdir
        if entrypoint:
            payload["entrypoint"] = entrypoint

        return self.submit_job(
            job_type="custom_docker",
            payload=payload,
            priority=priority,
            timeout_seconds=timeout_seconds,
            callback_url=callback_url,
        )

    def submit_autoforge(
        self,
        image_url: str,
        csv_url: str,
        json_url: str = None,
        pruning_max_colors: int = 8,
        pruning_max_swaps: int = 20,
        layer_height: float = 0.04,
        max_layers: int = 75,
        iterations: int = 2000,
        flatforge: bool = False,
        cap_layers: int = 0,
        priority: int = 0,
        timeout_seconds: int = 1800,
        callback_url: str = None,
    ) -> dict:
        """
        Submit an AutoForge job.

        Uses the 'autoforge' job type (v2 alias for custom_docker).
        The command is a JSON-serialized config that handler.py reads from
        the AUTOFORGE_CONFIG env var.

        The container runs handler.py which:
          1. Downloads image + CSV
          2. Runs AutoForge optimization
          3. Packages ZIP
          4. Uploads to GitHub releases
          5. Returns download_url
        """
        autoforge_config = {
            "image_url": image_url,
            "csv_url": csv_url,
            "pruning_max_colors": pruning_max_colors,
            "pruning_max_swaps": pruning_max_swaps,
            "layer_height": layer_height,
            "max_layers": max_layers,
            "iterations": iterations,
            "flatforge": flatforge,
            "cap_layers": cap_layers,
        }
        if json_url:
            autoforge_config["json_url"] = json_url

        # Serialize config as env var — handler.py reads AUTOFORGE_CONFIG
        config_json = json.dumps(autoforge_config)

        payload = {
            "image": AUTOFORGE_IMAGE,
            "command": "python3 -u /app/handler.py",
            "entrypoint": "",
            "shm_size": "4g",  # AutoForge needs more shared memory
            "network": "host",  # LAN access for pulling models / uploading to GitHub
            "env": {
                "AUTOFORGE_CONFIG": config_json,
                "GITHUB_TOKEN": os.environ.get("GITHUB_TOKEN", ""),
                "GITHUB_OWNER": os.environ.get("GITHUB_OWNER", "makawuhu"),
                "GITHUB_REPO": os.environ.get("GITHUB_REPO", "autoforge-worker"),
            },
            "workdir": "/app",
        }

        return self.submit_job(
            job_type="autoforge",
            payload=payload,
            priority=priority,
            timeout_seconds=timeout_seconds,
            callback_url=callback_url,
        )

    # ── Polling ────────────────────────────────────────────────────────

    def wait_for_job(
        self,
        job_id: str,
        poll_interval: int = DEFAULT_POLL_INTERVAL,
        timeout: int = DEFAULT_POLL_TIMEOUT,
    ) -> dict:
        """
        Poll a job until it completes or fails.
        Returns the final job response with result/error populated.
        Raises TimeoutError if timeout is exceeded.
        """
        start = time.time()
        last_status = "unknown"

        while time.time() - start < timeout:
            job = self.get_job(job_id)
            status = job.get("status", "unknown")
            last_status = status

            if status == "completed":
                return job
            elif status == "failed":
                return job
            elif status in ("queued", "running", "active", "waiting"):
                elapsed = int(time.time() - start)
                print(f"[khris-gpu] job {job_id} status={status} elapsed={elapsed}s")
                time.sleep(poll_interval)
            else:
                raise RuntimeError(f"Unknown job status: {status}")

        raise TimeoutError(
            f"Job {job_id} did not complete within {timeout}s "
            f"(last status: {last_status})"
        )

    def submit_and_wait(
        self,
        image_url: str,
        csv_url: str,
        **kwargs,
    ) -> dict:
        """Submit an AutoForge job and wait for completion."""
        poll_timeout = kwargs.pop("poll_timeout", DEFAULT_POLL_TIMEOUT)
        poll_interval = kwargs.pop("poll_interval", DEFAULT_POLL_INTERVAL)

        job = self.submit_autoforge(image_url=image_url, csv_url=csv_url, **kwargs)
        job_id = job["job_id"]
        print(f"[khris-gpu] Submitted AutoForge job {job_id}")

        result = self.wait_for_job(
            job_id, poll_interval=poll_interval, timeout=poll_timeout
        )
        status = result.get("status")

        if status == "completed":
            print(f"[khris-gpu] Job {job_id} completed ✓")
        else:
            print(f"[khris-gpu] Job {job_id} {status}: {result.get('error', 'unknown')}")

        return result


# ── CLI ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="khris-gpu API client for AutoForge")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("health", help="Check GPU service health")
    sub.add_parser("gpu", help="Show GPU info")
    sub.add_parser("queue", help="Show queue stats")

    # submit
    s = sub.add_parser("submit", help="Submit an AutoForge job")
    s.add_argument("--image-url", required=True, help="Input image URL")
    s.add_argument("--csv-url", required=True, help="Materials CSV URL")
    s.add_argument("--json-url", default=None, help="personal_library.json URL")
    s.add_argument("--iterations", type=int, default=200)
    s.add_argument("--layer-height", type=float, default=0.04)
    s.add_argument("--max-layers", type=int, default=75)
    s.add_argument("--timeout", type=int, default=1800, help="Job timeout in seconds")
    s.add_argument("--wait", action="store_true", help="Wait for completion")

    # docker
    d = sub.add_parser("docker", help="Submit a custom_docker job")
    d.add_argument("--image", required=True, help="Docker image")
    d.add_argument("--command", required=True, help="Command to run")
    d.add_argument("--env", default=None, help="JSON env dict")
    d.add_argument("--timeout", type=int, default=300)

    # list
    l = sub.add_parser("list", help="List jobs")
    l.add_argument("--status", default=None, help="Filter by status")
    l.add_argument("--limit", type=int, default=20)

    # get
    g = sub.add_parser("get", help="Get job details")
    g.add_argument("job_id", help="Job ID")

    # cancel
    c = sub.add_parser("cancel", help="Cancel a queued job")
    c.add_argument("job_id", help="Job ID")

    args = parser.parse_args()
    client = KhrisGPUClient()

    if args.command == "health":
        print(json.dumps(client.health(), indent=2))
    elif args.command == "gpu":
        print(json.dumps(client.gpu_info(), indent=2))
    elif args.command == "queue":
        print(json.dumps(client.queue_stats(), indent=2))
    elif args.command == "submit":
        job = client.submit_autoforge(
            image_url=args.image_url,
            csv_url=args.csv_url,
            json_url=args.json_url,
            iterations=args.iterations,
            layer_height=args.layer_height,
            max_layers=args.max_layers,
            timeout_seconds=args.timeout,
        )
        print(json.dumps(job, indent=2))
        if args.wait:
            result = client.wait_for_job(job["job_id"], timeout=args.timeout)
            print(json.dumps(result, indent=2))
    elif args.command == "docker":
        env = json.loads(args.env) if args.env else None
        job = client.submit_docker(
            image=args.image,
            command=args.command,
            env=env,
            timeout_seconds=args.timeout,
        )
        print(json.dumps(job, indent=2))
    elif args.command == "list":
        jobs = client.list_jobs(status=args.status, limit=args.limit)
        print(json.dumps(jobs, indent=2))
    elif args.command == "get":
        print(json.dumps(client.get_job(args.job_id), indent=2))
    elif args.command == "cancel":
        print(json.dumps(client.cancel_job(args.job_id), indent=2))
    else:
        parser.print_help()