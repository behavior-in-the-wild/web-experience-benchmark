#!/usr/bin/env bash
set -euo pipefail

PORT="${PORT:-4000}"
FRAMEWORK="${FRAMEWORK:-static}"
REPO_DIR="${REPO_DIR:-/workspace}"
LOG="${LOG:-/var/log/web-bench-host.log}"

cd "$REPO_DIR"
mkdir -p "$(dirname "$LOG")"

log() { echo "[$FRAMEWORK] $*" | tee -a "$LOG"; }

run_install() {
  local cmd="$1"
  log "install: $cmd"
  bash -lc "$cmd" >>"$LOG" 2>&1
}

serve_static() {
  local dir="${1:-.}"
  if [[ "${STATIC_HTTP2:-0}" == "1" && -f /usr/local/bin/http2_server.js && -f /usr/local/bin/localhost-key.pem && -f /usr/local/bin/localhost-cert.pem ]]; then
    exec node /usr/local/bin/http2_server.js "$dir" >>"$LOG" 2>&1
  fi
  exec python3 -m http.server "$PORT" --bind 0.0.0.0 --directory "$dir" >>"$LOG" 2>&1
}

case "$FRAMEWORK" in
  static)
    [[ -f index.html ]] && { log "serving static root"; serve_static "."; }
    for _dir in public dist build out site docs _site; do
      [[ -f "$_dir/index.html" ]] && { log "serving static $_dir"; serve_static "$_dir"; }
    done
    ;;
  hugo)
    for cfg in hugo.toml hugo.yaml hugo.yml config.toml config.yaml config.yml; do
      [[ -f "$cfg" ]] && { log "hugo server"; exec hugo server -p "$PORT" --bind 0.0.0.0 >>"$LOG" 2>&1; }
    done
    [[ -f public/index.html ]] && serve_static public
    [[ -f docs/index.html ]] && serve_static docs
    [[ -f index.html ]] && serve_static .
    ;;
  jekyll)
    if [[ -f Gemfile ]]; then
      run_install "bundle config set path /cache/bundle && bundle install"
      exec bundle exec jekyll serve --host 0.0.0.0 --port "$PORT" >>"$LOG" 2>&1
    fi
    [[ -f _config.yml ]] && exec jekyll serve --host 0.0.0.0 --port "$PORT" >>"$LOG" 2>&1
    [[ -f index.html ]] && serve_static .
    ;;
  flask)
    if [[ -f app.py || -f wsgi.py ]]; then
      [[ -f requirements.txt ]] && run_install "pip install --cache-dir /cache/pip -r requirements.txt"
      export FLASK_APP="$([[ -f app.py ]] && echo app.py || echo wsgi.py)"
      export FLASK_ENV=development
      export FLASK_RUN_PORT="$PORT"
      exec flask run --host=0.0.0.0 >>"$LOG" 2>&1
    fi
    [[ -f static/index.html ]] && serve_static static
    ;;
  pelican)
    if [[ -f pelicanconf.py || -f publishconf.py ]]; then
      [[ -f requirements.txt ]] && run_install "pip install --cache-dir /cache/pip -r requirements.txt"
      if [[ -f publishconf.py ]]; then pelican content -s publishconf.py >>"$LOG" 2>&1; else pelican content >>"$LOG" 2>&1; fi
      serve_static output
    fi
    [[ -f output/index.html ]] && serve_static output
    [[ -f index.html ]] && serve_static .
    ;;
  express|react|vue|next|hexo)
    export npm_config_cache=/cache/npm
    if [[ -f package.json ]]; then
      run_install "npm install --silent"
      export PORT
      case "$FRAMEWORK" in
        hexo) exec npx hexo server -p "$PORT" --silent >>"$LOG" 2>&1 ;;
        next) npm run build --silent >>"$LOG" 2>&1; exec npm run start -- -p "$PORT" >>"$LOG" 2>&1 ;;
        react) exec npm start >>"$LOG" 2>&1 ;;
        vue) exec npm run serve >>"$LOG" 2>&1 ;;
        express) exec npm run start >>"$LOG" 2>&1 ;;
      esac
    fi
    for vf in vite.config.ts vite.config.js; do
      [[ -f "$vf" ]] && { run_install "npm install --silent"; exec npm run dev -- --host 0.0.0.0 --port "$PORT" >>"$LOG" 2>&1; }
    done
    for f in server.js app.js index.js; do
      [[ -f "$f" ]] && { run_install "npm install --silent"; exec node "$f" >>"$LOG" 2>&1; }
    done
    [[ -f out/index.html ]] && serve_static out
    [[ -f dist/index.html ]] && serve_static dist
    [[ -f build/index.html ]] && serve_static build
    [[ -f index.html ]] && serve_static .
    ;;
esac

log "ERROR: no recognized entrypoint"
exit 2
