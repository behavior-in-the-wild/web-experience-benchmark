#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

BUILD="docker build --network=host"

$BUILD -t web-bench/base:latest        -f "$DIR/dockerfiles/base.Dockerfile"   "$DIR"
$BUILD -t web-bench/host-static:latest -f "$DIR/dockerfiles/static.Dockerfile" "$DIR"
$BUILD -t web-bench/host-node:latest   -f "$DIR/dockerfiles/node.Dockerfile"   "$DIR"
$BUILD -t web-bench/host-python:latest -f "$DIR/dockerfiles/python.Dockerfile" "$DIR"
$BUILD -t web-bench/host-ruby:latest   -f "$DIR/dockerfiles/ruby.Dockerfile"   "$DIR"
$BUILD -t web-bench/host-hugo:latest   -f "$DIR/dockerfiles/hugo.Dockerfile"   "$DIR"
$BUILD -t web-bench/host-quarto:latest -f "$DIR/dockerfiles/quarto.Dockerfile" "$DIR"
