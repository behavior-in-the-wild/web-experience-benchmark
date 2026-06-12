FROM mcr.microsoft.com/playwright/python:v1.49.0-jammy

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl git python3-pip \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    playwright==1.49.0 \
    beautifulsoup4 \
    html5lib \
    lxml \
    numpy \
    openai \
    Pillow \
    python-dotenv \
    requests

WORKDIR /workspace
