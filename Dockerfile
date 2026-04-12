# RunPod serverless worker for AutoForge
# Targets RTX 3090 / 4090 / A40 / A100 (sm_86/sm_80) — cu124 covers all of these.
# For RTX 5090 (sm_120 / Blackwell), rebuild with cu128 PyTorch.
FROM runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04

WORKDIR /app

RUN pip install --no-cache-dir runpod AutoForge

COPY handler.py .

CMD ["python", "-u", "handler.py"]
