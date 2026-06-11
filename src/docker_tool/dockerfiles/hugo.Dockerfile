FROM web-bench/base:latest

RUN apt-get update && apt-get install -y --no-install-recommends hugo golang-go \
    && rm -rf /var/lib/apt/lists/*
