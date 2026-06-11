FROM web-bench/base:latest

RUN apt-get update && apt-get install -y --no-install-recommends ruby-full build-essential \
    && gem install bundler jekyll \
    && rm -rf /var/lib/apt/lists/*
