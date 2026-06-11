FROM mcr.microsoft.com/playwright/python:v1.49.0-jammy

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl git python3-pip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace
