FROM web-bench/base:latest

RUN apt-get update && apt-get install -y --no-install-recommends wget gdebi-core \
    && wget -q -O /tmp/quarto.deb https://github.com/quarto-dev/quarto-cli/releases/download/v1.5.57/quarto-1.5.57-linux-amd64.deb \
    && gdebi -n /tmp/quarto.deb \
    && rm -f /tmp/quarto.deb \
    && rm -rf /var/lib/apt/lists/*
