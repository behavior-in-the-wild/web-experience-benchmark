FROM web-bench/base:latest

RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get update && apt-get install -y --no-install-recommends \
        build-essential hugo nodejs ruby-full \
    && npm install -g npm@latest \
    && gem install bundler jekyll \
    && rm -rf /var/lib/apt/lists/*
