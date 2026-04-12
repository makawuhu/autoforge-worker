# RunPod serverless worker for AutoForge
# pytorch/pytorch:2.3.1-cuda12.1-cudnn8-devel is an official, well-known tag.
# Targets RTX 3090 / 4090 / A40 / A100 (sm_86/sm_80) — cu121 covers all of these.
FROM pytorch/pytorch:2.3.1-cuda12.1-cudnn8-devel

WORKDIR /app

RUN pip install --no-cache-dir runpod AutoForge

COPY handler.py .

CMD ["python", "-u", "handler.py"]
