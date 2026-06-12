FROM web-bench/base:latest

ARG TARGETARCH

RUN apt-get update && apt-get install -y --no-install-recommends wget gdebi-core \
    && case "$TARGETARCH" in \
        amd64) quarto_arch=amd64 ;; \
        arm64) quarto_arch=arm64 ;; \
        *) echo "Unsupported Quarto architecture: $TARGETARCH" >&2; exit 1 ;; \
    esac \
    && wget -q -O /tmp/quarto.deb "https://github.com/quarto-dev/quarto-cli/releases/download/v1.5.57/quarto-1.5.57-linux-${quarto_arch}.deb" \
    && gdebi -n /tmp/quarto.deb \
    && rm -f /tmp/quarto.deb \
    && rm -rf /var/lib/apt/lists/*
