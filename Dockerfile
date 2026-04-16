# Start from NVIDIA CUDA base — driver-agnostic, works with any CUDA 12.x host driver.
# PyTorch cu121 wheels require driver >= 525, which all modern RunPod workers have.
FROM nvidia/cuda:12.1.1-cudnn8-devel-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y python3 python3-pip \
    libxcb1 libx11-6 libgl1 libglib2.0-0 \
    libsm6 libxext6 libxrender1 libfontconfig1 libice6 \
    && rm -rf /var/lib/apt/lists/*

RUN pip3 install --no-cache-dir \
    torch --index-url https://download.pytorch.org/whl/cu121

RUN pip3 install --no-cache-dir runpod AutoForge

WORKDIR /app
COPY handler.py autoforge_wrapper.py .

CMD ["python3", "-u", "handler.py"]