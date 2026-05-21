# Host Files

Each script in this directory starts a local HTTP server for a specific web framework. `evaluate.sh` selects the right one per repo based on the `HOST_FILE_PATH` column in the input CSV.

| Script | Framework | Runtime required |
|---|---|---|
| `host_static_html.sh` | Plain HTML/CSS/JS | Python 3 (stdlib) |
| `host_express.sh` | Express / Node static | Node.js + npm |
| `host_react.sh` | React (Vite / CRA) | Node.js + npm |
| `host_next.sh` | Next.js | Node.js + npm |
| `host_vue.sh` | Vue.js | Node.js + npm |
| `host_hexo.sh` | Hexo | Node.js + npm |
| `host_flask.sh` | Flask / Python apps | Python 3 + pip |
| `host_pelican.sh` | Pelican static sites | Python 3 + pelican |
| `host_jekyll.sh` | Jekyll | Ruby + jekyll gem |
| `host_hugo.sh` | Hugo | Hugo binary |
| `host_quarto.sh` | Quarto | Python 3 (stdlib) |

---

## Setup

### Python environment (required for all)

```bash
# Use Python 3.12 — 3.14 is too new for easyocr/aider-chat
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt     # vllm is excluded on macOS (Linux/GPU only)

# Extra hosting-specific packages
pip install tldextract pelican

playwright install chromium
```

> **Note:** `vllm` in `requirements.txt` is only installable on Linux with a GPU. It is skipped automatically on macOS. All other packages install fine on both platforms.

---

### Node.js + npm (for Express, React, Next, Vue, Hexo)

**macOS**
```bash
brew install node
```

**Ubuntu / Debian**
```bash
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt install -y nodejs
```

Verify: `node --version && npm --version`

---

### Hugo + Go (for `host_hugo.sh`)

**macOS**
```bash
brew install hugo go
```

**Ubuntu / Debian**
```bash
sudo apt install -y golang-go
# Hugo — apt version is often outdated; use the official binary instead:
wget https://github.com/gohugoio/hugo/releases/latest/download/hugo_extended_linux_amd64.tar.gz
tar -xzf hugo_extended_linux_amd64.tar.gz && sudo mv hugo /usr/local/bin/
```

Verify: `hugo version && go version`

---

### Ruby + Jekyll (for `host_jekyll.sh`)

**macOS**

The system Ruby (2.6) shipped with macOS is read-only. Install a writable Ruby via Homebrew:

```bash
brew install ruby

# Add Homebrew Ruby and its gems to PATH (add to ~/.zshrc to persist)
export PATH="/opt/homebrew/opt/ruby/bin:/opt/homebrew/lib/ruby/gems/$(ruby -e 'puts RUBY_VERSION.match(/^\d+\.\d+/)[0]').0/bin:$PATH"

gem install jekyll bundler
```

Verify: `jekyll --version && bundler --version`

**Ubuntu / Debian**
```bash
sudo apt install -y ruby-full build-essential
gem install jekyll bundler
```

---

## Quick verification

Run this to confirm all runtimes are available:

```bash
echo "Node: $(node --version 2>/dev/null || echo MISSING)"
echo "npm:  $(npm --version 2>/dev/null || echo MISSING)"
echo "Hugo: $(hugo version 2>/dev/null | head -1 || echo MISSING)"
echo "Go:   $(go version 2>/dev/null || echo MISSING)"
echo "Ruby: $(ruby --version 2>/dev/null || echo MISSING)"
echo "Jekyll: $(jekyll --version 2>/dev/null || echo MISSING)"
echo "Pelican: $(pelican --version 2>/dev/null || echo MISSING)"
echo "Python: $(.venv/bin/python --version 2>/dev/null || echo MISSING)"
```
