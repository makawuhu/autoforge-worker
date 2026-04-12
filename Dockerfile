# Start from NVIDIA CUDA base — driver-agnostic, works with any CUDA 12.x host driver.
# PyTorch cu121 wheels require driver >= 525, which all modern RunPod workers have.
FROM nvidia/cuda:12.1.1-cudnn8-devel-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y python3 python3-pip && rm -rf /var/lib/apt/lists/*

RUN pip3 install --no-cache-dir \
    torch --index-url https://download.pytorch.org/whl/cu121

RUN pip3 install --no-cache-dir runpod AutoForge

WORKDIR /app
COPY handler.py .

CMD ["python3", "-u", "handler.py"]
