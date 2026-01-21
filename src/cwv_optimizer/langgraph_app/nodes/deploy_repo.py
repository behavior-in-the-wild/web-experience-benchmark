"""Node for deploying the cloned repository using Claude CLI."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any, Dict

from cwv_optimizer.core.logger import get_logger
from cwv_optimizer.langgraph_app.nodes.base import run_with_timing

logger = get_logger(__name__)


def format_model_name(model_name, for_code_apply=False):
    """Simple model name formatting."""
    return model_name

# Refined deployment prompt focusing on serving existing code
DEPLOYMENT_PROMPT = """You are a senior DevOps engineer. Create a deploy_local.sh script that gets this application running on port 8000 in ONE attempt.

REQUIREMENTS:
- Script must be executable and handle all errors gracefully
- Detect the framework automatically (Node.js, Python, etc.)
- Install ALL dependencies (including dev dependencies) if needed
- Handle build tools (gulp, webpack, etc.) if present
- Handle databases (MongoDB, PostgreSQL, etc.) if needed
- Start the development server on port 8000
- Include comprehensive logging and error checking
- Use simple, reliable commands that work in most environments

FRAMEWORK DETECTION:
1. Check for package.json -> Node.js project (may need gulp, webpack, MongoDB)
2. Check for requirements.txt/pyproject.toml -> Python project (may need Django, Flask)
3. Check for other indicators

CRITICAL FOR NODE.JS PROJECTS:
- Always install dev dependencies: npm install --include=dev --legacy-peer-deps
- Set correct Python version for node-gyp: export npm_config_python=python3
- Handle native dependencies that require compilation (bcrypt, node-sass, etc.)
- Continue despite compilation failures (apps often work without all native deps)
- Check for Babel transpilation: look for .babelrc, babel.config.js, or babel/register in code
- Install @babel/register and @babel/core if Babel is used for entry point
- Check for build tools like gulp, webpack, grunt in package.json scripts
- Handle MongoDB if MONGO_DB_URI is set or mongodb dependency exists
- Handle certificates if HTTPS is needed
- Use npm run dev or npm start, set PORT=8000

WORKING NODE.JS EXAMPLE:
```bash
#!/bin/bash
set -e

log() { echo "[$(date +'%Y-%m-%d %H:%M:%S')] $*" >&2; }

log "Starting Node.js deployment..."

# Check Node.js and npm
command -v node >/dev/null 2>&1 || { log "ERROR: Node.js not found"; exit 1; }
command -v npm >/dev/null 2>&1 || { log "ERROR: npm not found"; exit 1; }

log "Node version: $(node --version)"
log "npm version: $(npm --version)"

# Set Python for node-gyp (critical for native dependencies)
export npm_config_python=python3
export PYTHON=$(which python3)
log "Using Python 3 for node-gyp"

# Install ALL dependencies including dev dependencies
if [ -f "package.json" ]; then
  if [ -f "package-lock.json" ]; then
    log "Installing dependencies with npm ci..."
    if ! npm ci --include=dev --legacy-peer-deps; then
      log "npm ci failed, trying npm install..."
      if ! npm install --include=dev --legacy-peer-deps; then
        log "npm install failed, trying to fix node-gyp and retry..."
        # Install a node-gyp version compatible with current Node.js version
        npm install -g node-gyp@10.2.0
        # Try installing again
        npm install --include=dev --legacy-peer-deps || {
          log "WARNING: Dependency installation still failed, but continuing..."
        }
      fi
    fi
  else
    log "Installing dependencies with npm install..."
    if ! npm install --include=dev --legacy-peer-deps; then
      log "npm install failed, trying to fix node-gyp and retry..."
      # Install a node-gyp version compatible with current Node.js version
      npm install -g node-gyp@10.2.0
      # Try installing again
      npm install --include=dev --legacy-peer-deps || {
        log "WARNING: Dependency installation still failed, but continuing..."
      }
    fi
  fi
fi

# Handle deprecated node-sass issues
if [ -f "package.json" ] && grep -q '"node-sass"' package.json; then
  log "WARNING: node-sass detected - this package is deprecated and often fails to compile"
  log "Attempting automatic fix: installing 'sass' as modern replacement..."
  if npm install sass --save-dev --legacy-peer-deps; then
    log "Successfully installed 'sass' - you may need to update your webpack config"
    log "Change: sass-loader with node-sass -> sass-loader with sass"
  else
    log "Automatic fix failed, manual intervention required"
    log "RECOMMENDATION: Replace node-sass with 'sass' or 'sass-embedded' in your dependencies"
    log "To fix manually:"
    log "  1. Run: npm uninstall node-sass"
    log "  2. Run: npm install sass --save-dev"
    log "  3. Update any require('node-sass') to require('sass') in your code"
    log "  4. Update webpack config to use 'sass-loader' instead of 'node-sass'"
  fi
fi

# Handle other deprecated packages that commonly cause issues
if [ -f "package.json" ]; then
  if grep -q '"phantomjs"' package.json; then
    log "WARNING: phantomjs detected - this package is deprecated and unmaintained"
    log "RECOMMENDATION: Consider using puppeteer, playwright, or headless Chrome instead"
  fi
  if grep -q '"babel-eslint"' package.json; then
    log "WARNING: babel-eslint detected - replaced by @babel/eslint-parser"
    log "RECOMMENDATION: Run: npm uninstall babel-eslint && npm install @babel/eslint-parser --save-dev"
  fi
  if grep -q '"request"' package.json && ! grep -q '"axios\|node-fetch\|got"' package.json; then
    log "WARNING: 'request' package detected - deprecated, consider using axios or node-fetch"
  fi
fi

# Handle build tools if present
if [ -f "package.json" ] && grep -q '"gulp"' package.json; then
  log "Gulp detected, ensuring gulp-cli..."
  npm list -g gulp-cli >/dev/null 2>&1 || npm install -g gulp-cli
fi

# Check for common build tools and provide guidance if they're missing
if [ -f "package.json" ]; then
  if grep -q '"webpack"' package.json && ! npm list webpack >/dev/null 2>&1; then
    log "WARNING: webpack detected in package.json but not installed"
    log "Build scripts may fail - try: npm install webpack --save-dev"
  fi
  if grep -q '"babel-loader\|@babel"' package.json && ! npm list @babel/core >/dev/null 2>&1; then
    log "WARNING: Babel loader detected but @babel/core not installed"
    log "Transpilation may fail - try: npm install @babel/core @babel/preset-env --save-dev"
  fi
fi

# Handle Babel transpilation if needed
if [ -f ".babelrc" ] || [ -f "babel.config.js" ] || grep -q "babel/register" *.js 2>/dev/null; then
  log "Babel detected, checking for babel/register..."
  if ! node -e "require('babel/register')" 2>/dev/null; then
    log "babel/register not available, attempting installation..."
    if npm install babel-register@^6.26.0 --save-dev --legacy-peer-deps; then
      log "Successfully installed babel-register@6"
    else
      log "WARNING: Babel installation failed; trying alternative startup methods..."
      # Try to run with npx babel-node if available
      if npx babel-node --version >/dev/null 2>&1; then
        log "Using npx babel-node as alternative..."
        START_CMD="npx babel-node index.js"
      else
        log "ERROR: Babel setup failed and no alternatives available"
        log "Please manually install Babel dependencies and try again"
        exit 1
      fi
    fi
  else
    log "babel/register already available"
  fi
fi

# Set environment variables
export NODE_ENV="${NODE_ENV:-development}"
export PORT="${PORT:-8000}"
export HOST="${HOST:-0.0.0.0}"

# Try to run frontend build if scripts exist
if [ -f "package.json" ] && grep -q '"build"' package.json; then
  log "Frontend build scripts detected, attempting to build..."
  if npm run build-dev >/dev/null 2>&1; then
    log "Development build completed successfully"
  elif npm run build-prod >/dev/null 2>&1; then
    log "Production build completed successfully"
  elif npm run build >/dev/null 2>&1; then
    log "Build completed successfully"
  else
    log "WARNING: Frontend build failed - this may be due to missing build tools (webpack, etc.)"
    log "The app may still work if it doesn't require a build step"
  fi
fi

# Start server
if [ -n "$START_CMD" ]; then
  log "Starting server with custom command: $START_CMD"
  eval "$START_CMD"
elif npm run dev >/dev/null 2>&1; then
  log "Starting dev server..."
  npm run dev
elif npm start >/dev/null 2>&1; then
  log "Starting production server..."
  npm start
elif [ -f "index.js" ]; then
  log "No npm scripts found, trying to run index.js directly..."
  node index.js
elif [ -f "server.js" ]; then
  log "No npm scripts found, trying to run server.js directly..."
  node server.js
else
  log "ERROR: No valid start script or entry point found"
  exit 1
fi
```

WORKING PYTHON EXAMPLE:
```bash
#!/bin/bash
set -e

log() { echo "[$(date +'%Y-%m-%d %H:%M:%S')] $*" >&2; }

log "Starting Python deployment..."

# Check Python
command -v python3 >/dev/null 2>&1 || { log "ERROR: Python3 not found"; exit 1; }

# Create and activate virtual environment
if [ ! -d "venv" ]; then
  log "Creating virtual environment..."
  python3 -m venv venv
fi
source venv/bin/activate

# Install dependencies
if [ -f "requirements.txt" ]; then
  log "Installing Python dependencies..."
  if ! pip install -r requirements.txt; then
    log "WARNING: Some Python dependencies failed to install"
    log "Common causes:"
    log "  - Git dependencies failing due to network timeouts"
    log "  - Missing system dependencies (e.g., database drivers)"
    log "  - Incompatible Python version"
    log "RECOMMENDATION:"
    log "  1. Check network connection for git dependencies"
    log "  2. Install system packages: apt-get install python3-dev libpq-dev etc."
    log "  3. Try installing problematic packages individually"
    log "  4. Consider using --no-deps flag for problematic packages"
  fi
elif [ -f "pyproject.toml" ]; then
  log "Installing with pip from pyproject.toml..."
  if ! pip install -e .; then
    log "WARNING: Python package installation failed, but continuing..."
  fi
fi

# Start server
if [ -f "manage.py" ]; then
  log "Django project detected, checking if Django is available..."
  if python3 -c "import django" 2>/dev/null; then
    log "Starting Django server..."
    python manage.py runserver 0.0.0.0:8000
  else
    log "WARNING: Django not available, cannot start Django server"
    exit 1
  fi
elif [ -f "app.py" ] || [ -f "main.py" ]; then
  log "Starting Python app..."
  python3 app.py || python3 main.py
else
  log "ERROR: No Python app entry point found"
  exit 1
fi
```

INSTRUCTIONS:
1. Analyze package.json/requirements.txt to understand dependencies and scripts
2. Create deploy_local.sh that handles the specific framework requirements
3. Always install dev dependencies for Node.js projects
4. Handle native dependencies with proper Python configuration for node-gyp
5. Continue despite native compilation failures (apps often work without them)
6. Detect and handle Babel transpilation requirements (@babel/register, @babel/core)
7. Handle build tools and databases appropriately
8. Use proper error handling and logging
9. Ensure the server starts on 0.0.0.0:8000

OUTPUT: Create deploy_local.sh that works immediately on first run, even with native dependency issues."""

FIX_ERROR_PROMPT = """The deployment failed. Apply senior engineering debugging to fix deploy_local.sh:
====================================================

1. PROJECT STRUCTURE ANALYSIS:
   - Walk the directory tree to understand the architecture
   - Multi-tier apps often have backend/ or server/ + frontend/ or client/ subdirectories
   - Monorepos may have packages/, apps/, or services/ with multiple deployable units
   - Look for configuration files that reveal the tech stack

2. DEPENDENCY MANIFESTS (Read these carefully):
   - package.json: Check "engines" field for Node/npm version requirements
   - requirements.txt / Pipfile / pyproject.toml: Python dependencies
   - Gemfile: Ruby gems and version constraints
   - composer.json: PHP dependencies
   - pom.xml / build.gradle: JVM dependencies
   - *.csproj / *.sln: .NET projects
   
3. FRAMEWORK DETECTION HEURISTICS:
   - package.json "dependencies": React, Vue, Angular, Next.js, Express, Nest
   - Look for framework CLI configs: angular.json, next.config.js, vue.config.js
   - Python: Check for wsgi.py (Django/Flask), asgi.py (FastAPI), app.py
   - Check imports in main files to confirm framework

4. BUILD CONFIGURATION:
   - Check package.json "scripts" for build, start, dev commands
   - Look for build tools: webpack.config.js, vite.config.js, rollup.config.js
   - CI/CD files (github/workflows, .gitlab-ci.yml) often show deployment steps
   - Dockerfile or docker-compose.yml reveal production setup

5. DATABASE DETECTION:
   - Search for DB connection strings in config files
   - Common locations: .env.example, config/database.yml, settings.py
   - If external DB (RDS, remote Postgres), you'll need to override with local SQLite
   - Look for migration directories: migrations/, db/migrate/, alembic/

PHASE 2 - DEPENDENCY RESOLUTION STRATEGY
=========================================

The #1 cause of deployment failures is dependency hell. Here's how to handle it:

1. VERSION COMPATIBILITY MATRIX:
   - Node projects with old Angular (v11-12) expect npm 6.x, node 14.x
   - New Node projects need npm 8+ and node 16+
   - Python 2 vs 3 incompatibility in legacy codebases
   - Check if project specifies engines - respect those constraints

2. HANDLING LEGACY NODE PROJECTS:
   - Old projects fail with new npm due to peer dependency strictness
   - Strategy: Use --legacy-peer-deps flag globally in all npm commands
   - node-sass is notorious: It needs rebuild for each Node version
   - Modern fix: Replace node-sass with sass (dart-sass) in package.json before install
   - For very old projects (2018-2020): Consider using npx to install specific npm version

3. PYTHON DEPENDENCY CONFLICTS:
   - Use virtual environments ALWAYS: python -m venv venv && source venv/bin/activate
   - Legacy projects may have unpinned versions causing conflicts
   - Strategy: Install with --no-deps first for critical packages, then fill gaps
   - Binary dependencies (pillow, numpy) may need system libraries

4. TRANSITIVE DEPENDENCY PROBLEMS:
   - A depends on B v1, C depends on B v2 - common in npm
   - Check if you can update the direct dependency to resolve
   - Last resort: Use npm overrides or yarn resolutions in package.json

5. MISSING SYSTEM DEPENDENCIES:
   - Node-gyp needs python3 + build tools (gcc, make)
   - Some npm packages need libvips, cairo, pango (image processing)
   - .NET needs SDK + runtime (different from .NET Framework)
   - Ruby needs native extensions build tools

PHASE 3 - SMART BUILD STRATEGY
===============================

1. INCREMENTAL VERIFICATION:
   Don't run the full build immediately. Verify each layer:
   - First: Install backend dependencies only
   - Second: Install frontend dependencies only  
   - Third: Build frontend if separate
   - Fourth: Start backend with frontend assets

2. FRONTEND-BACKEND INTEGRATION PATTERNS:

   Pattern A - Backend Serves Frontend:
   - Django/Flask with React/Vue built into static/
   - Rails with Webpacker
   - ASP.NET with Angular/React in ClientApp
   - Build frontend FIRST, then backend serves it
   
   Pattern B - Separate Servers (Proxy):
   - Backend on :3000, Frontend on :8000 (dev mode)
   - Frontend proxies API calls to backend
   - Check package.json for "proxy" field
   - Both must run simultaneously
   
   Pattern C - Monorepo with Workspace:
   - Yarn workspaces or npm workspaces
   - Build all packages first: npm install at root
   - Then build each package in dependency order

3. FRONTEND BUILD OPTIMIZATION:
   - Check if dev mode is available (faster, no optimization)
   - For Angular: ng serve vs ng build (serve is faster for dev)
   - For React (CRA): SKIP build, use npm start (webpack-dev-server)
   - For Next.js: npm run dev (fast refresh) vs npm run build + start
   - For Vue: npm run serve vs npm run build

4. HANDLING BUILD ERRORS:

   Missing Dependencies Error:
   - Angular material missing @angular/cdk: Install peer deps manually
   - npm install @angular/cdk@<version-matching-material>
   - Check package.json of the failing package for peerDependencies
   
   TypeScript Compilation Errors:
   - Old projects may have TS errors with new TS compiler
   - Quick fix: Add "skipLibCheck": true to tsconfig.json
   - Or downgrade TS to version in package.json if specified
   
   Webpack/Build Tool Errors:
   - Memory errors: Increase heap: NODE_OPTIONS=--max-old-space-size=4096
   - Usually means dependencies are partially installed
   
   Sass/CSS Preprocessor Errors:
   - node-sass vs sass (dart-sass) incompatibility
   - Replace node-sass with sass in package.json, delete lock file, reinstall

PHASE 4 - DATABASE & ENVIRONMENT SETUP
=======================================

1. LOCAL DATABASE STRATEGY:
   - Production apps use Postgres/MySQL/MSSQL - don't try to install these
   - Override with SQLite for local dev (supported by most ORMs)
   - Django: Override DATABASES in local settings or env var
   - Rails: Modify database.yml to use sqlite3 adapter
   - Node/Sequelize: Change dialect to sqlite in config
   
2. ENVIRONMENT VARIABLES:
   - Copy .env.example to .env if it exists
   - Set DEBUG=True for frameworks that need it
   - Set DATABASE_URL to local SQLite path
   - Disable external services (Redis, Celery, S3) for local dev
   - Set SECRET_KEY to any string for local dev
   
3. DATABASE MIGRATIONS:
   - For new setup: Run migrations to create schema
   - If migrations fail: Use --run-syncdb or --fake flags
   - Some old migrations may reference deleted models - skip them
   - Last resort: Create tables manually from models

PHASE 5 - RUNTIME CONFIGURATION
================================

1. PORT BINDING:
   - Ensure the app binds to 0.0.0.0:8000 (not 127.0.0.1 or localhost)
   - Check how framework handles port config
   - Override via CLI args, env vars, or config file
   
2. PROCESS MANAGEMENT:
   - Use development server, not production (gunicorn/uwsgi)
   - Enable hot reload for frameworks that support it
   - Don't daemonize - run in foreground for debugging
   
3. STATIC FILES:
   - Django: python manage.py collectstatic for admin assets
   - Rails: Assets precompiled in dev by default
   - Express: Ensure static middleware is configured

PHASE 6 - SPECIAL CASES & WORKAROUNDS
======================================

1. MONOLITHIC FRAMEWORKS (.NET, Java Spring):
   - May need SDK installed locally - check for installer scripts
   - ASP.NET Core: Can run cross-platform, but needs SDK
   - Java: Needs JDK + Maven/Gradle
   
2. MISSING TOOLING:
   - If dotnet/java/ruby not available and project is complex:
     - Check if there's a Dockerfile - it shows exact steps
     - Look for setup.sh or install scripts in repo
     - Check CI config for build steps
   
3. FRONTEND FRAMEWORK CLI:
   - Angular needs @angular/cli globally or in devDeps
   - Vue needs @vue/cli
   - Don't install globally - use npx or local node_modules/.bin/

4. HYBRID APPS (Electron, Mobile):
   - May have native dependencies that fail on Mac/Linux/Windows
   - Focus on web portion only
   - Check if there's a web target in build scripts

CRITICAL SUCCESS PATTERNS:
=========================

✓ Read all README files and documentation in repo
✓ Check GitHub issues for "setup" or "install" problems
✓ Look at recent commits - may show dependency fixes
✓ If project has CI/CD, replicate those exact steps locally
✓ Start backend first, verify it runs, then add frontend
✓ Use framework's dev server when available (ng serve, npm start)
✓ Override production configs for local dev
✓ When stuck: Simplify - disable features, use SQLite, skip auth

ANTI-PATTERNS TO AVOID:
======================

✗ Don't install global tools (use npx, or project-local)
✗ Don't use sudo for npm/pip (permission errors)
✗ Don't skip reading error messages completely
✗ Don't try to connect to production databases
✗ Don't ignore package.json scripts - they show intent
✗ Don't build when dev server is available
✗ Don't give up after first error - dependencies install iteratively

OUTPUT REQUIREMENT:
==================
Create deploy_local.sh that implements this engineering approach to get a fully working development build on port 8000."""

FIX_ERROR_PROMPT = """The deployment failed. Apply senior engineering debugging to fix deploy_local.sh:

ERROR:
{error}

DEBUGGING METHODOLOGY:
======================

STEP 1 - ERROR CLASSIFICATION:
------------------------------

Read the error carefully and classify it:

Type A: Missing Dependency
  - Symptoms: "Cannot find module", "ModuleNotFoundError", "missing package"
  - Root cause: Incomplete installation or peer dependency not auto-installed
  - Fix direction: Install the specific missing package

Type B: Version Incompatibility  
  - Symptoms: "Unsupported engine", "EBADENGINE", "requires node X"
  - Root cause: Tool versions don't match project expectations
  - Fix direction: Use version-specific flags or downgrade tool

Type C: Build Tool Error
  - Symptoms: "gyp ERR", "node-sass", "webpack failed", "compilation error"
  - Root cause: Native dependencies or build configuration issues
  - Fix direction: Replace problematic package or adjust build config

Type D: Configuration Error
  - Symptoms: "connection refused", "ECONNREFUSED", "cannot connect to database"
  - Root cause: App trying to reach external service
  - Fix direction: Override config to use local alternatives

Type E: Runtime Error
  - Symptoms: "Permission denied", "EACCES", "port already in use"
  - Root cause: System-level issues
  - Fix direction: Fix permissions or change port

Type F: Babel Transpilation Error
  - Symptoms: "Cannot find module 'babel/register'", "@babel/register missing"
  - Root cause: Project uses Babel but transpilation dependencies not installed
  - Fix direction: Install @babel/register and @babel/core, check .babelrc config

STEP 2 - ROOT CAUSE ANALYSIS:
-----------------------------

For the error above, identify:
1. WHAT failed: Which command/step in the script
2. WHY it failed: Underlying technical reason
3. WHAT was expected: What should have happened
4. CONTEXT: What succeeded before this point

Example analysis:
- Error: "@angular/material/progress-spinner has missing dependencies: @angular/cdk/coercion"
- What: Angular build (ng build)
- Why: @angular/material has peer dependency on @angular/cdk, but it wasn't installed
- Expected: npm install should have installed peer dependencies
- Context: npm install ran but with warnings about unsupported engines

STEP 3 - SOLUTION STRATEGIES:
-----------------------------

Based on error type, apply the right fix:

For Missing Dependencies:
→ Check the error for exact package and version needed
→ Install it explicitly: npm install @angular/cdk@<version>
→ Version must match the dependent package (check package.json)
→ For Python: pip install <package>

For Version Incompatibility:
→ Check package.json "engines" field
→ If Node too new: Add --legacy-peer-deps to ALL npm commands
→ If truly incompatible: Use nvm/n to switch Node version temporarily
→ Update package.json engines to current system version if too old

For node-sass Errors:
→ Never try to fix node-sass - it's deprecated and version-sensitive
→ Replace it: npm uninstall node-sass && npm install sass
→ Or edit package.json dependencies, delete lock file, reinstall

For Build Compilation Errors:
→ Check if dev mode available (skip build optimization)
→ Increase Node memory: export NODE_OPTIONS="--max-old-space-size=4096"
→ For TypeScript errors: Add "skipLibCheck": true to tsconfig.json
→ For specific module errors: Install that module's peer dependencies

For Database Connection Errors:
→ Override database config to SQLite
→ Set environment variable: DATABASE_URL="sqlite:///./local.db"
→ Or create local config file that overrides production settings
→ Disable external services in config (Redis, message queues)

For Permission Errors:
→ Never use sudo
→ Fix ownership: chown -R $USER:$USER node_modules/
→ Clear cache: npm cache clean --force
→ Reinstall in user context

For Python/gyp Errors:
→ Ensure python3 is in PATH
→ Set npm config: npm config set python python3
→ Install build tools if needed
→ Skip problematic package if not critical

For Babel Transpilation Errors:
→ Install Babel runtime: npm install @babel/register @babel/core
→ Check for .babelrc or babel.config.js configuration file
→ If missing, create basic .babelrc: {"presets": ["@babel/preset-env"]}
→ Ensure babel/register is required at entry point: require('babel/register')
→ For ES6+ projects: May need additional Babel plugins/presets

STEP 4 - IMPLEMENTATION PATTERN:
--------------------------------

When you fix the script, follow this pattern:

1. ADD VERIFICATION BEFORE ACTION:
   # Check if package exists before trying to use it
   if [ -f "package.json" ]; then
     # Proceed with npm commands
   fi

2. INSTALL MISSING DEPENDENCIES EXPLICITLY:
   # Don't rely on auto-install of peer deps
   npm install <specific-package>@<specific-version>

3. USE APPROPRIATE FLAGS:
   # For old projects
   npm install --legacy-peer-deps --no-audit
   # For stubborn projects
   npm install --force --legacy-peer-deps

4. HANDLE ERRORS GRACEFULLY:
   # Try preferred method, fall back if it fails
   npm run build || npm run dev

5. ADD DEBUGGING OUTPUT:
   echo "[INFO] Installing dependencies..."
   echo "[DEBUG] Node version: $(node --version)"
   echo "[DEBUG] npm version: $(npm --version)"

STEP 5 - PREVENTION:
-------------------

Add these to prevent similar errors:

- Check tool versions before operations
- Install peer dependencies explicitly for problematic packages
- Use virtual environments (Python venv, Node version managers)
- Set environment variables early in script
- Clear caches if dealing with stale data
- Read package.json to understand what's expected

SPECIFIC GUIDANCE FOR COMMON SCENARIOS:
=======================================

Scenario: Angular project with missing @angular/cdk
→ Check @angular/material version in package.json
→ Install matching @angular/cdk: npm install @angular/cdk@11.2.8
→ Install @angular/platform-browser if also missing

Scenario: Python project can't find module
→ Activate virtual environment first
→ Install with: pip install -r requirements.txt
→ If specific module missing: pip install <module>

Scenario: Node project says "Unsupported engine"
→ Add --legacy-peer-deps to ALL npm install commands
→ Or use older npm: npx npm@6 install

Scenario: Webpack/build runs out of memory
→ Add: export NODE_OPTIONS="--max-old-space-size=4096"
→ Place BEFORE the build command

Scenario: Database connection fails
→ Set DATABASE_URL environment variable
→ Or create local_settings.py (Django) / database.yml (Rails)
→ Use SQLite instead of Postgres/MySQL

THE FIX MUST:
============
1. Address the specific error shown above
2. Use the correct technical approach for that error type
3. Be minimal - only change what's needed
4. Add verification/logging for debugging
5. Consider what might fail next and prevent it

Update deploy_local.sh with the fix that solves this specific error."""

def get_run_logger(log_file: str | None, repo_name: str) -> logging.Logger:
    """Get or create a run logger for file logging."""
    if not log_file:
        return logger
    
    run_logger = logging.getLogger(f"cwv_run.{repo_name}")
    if not run_logger.handlers:
        file_handler = logging.FileHandler(log_file)
        file_handler.setLevel(logging.DEBUG)
        formatter = logging.Formatter(
            "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        file_handler.setFormatter(formatter)
        run_logger.addHandler(file_handler)
    
    return run_logger


def ensure_claudeignore(workspace_dir: str) -> None:
    """Create .claudeignore file in workspace if it doesn't exist."""
    claudeignore_path = Path(workspace_dir) / ".claudeignore"
    
    if claudeignore_path.exists():
        return
    
    default_patterns = [
        "# Build artifacts and dependencies",
        "node_modules/",
        "target/",
        "dist/",
        "build/",
        ".next/",
        "out/",
        "",
        "# Version control",
        ".git/",
        "",
        "# IDE files",
        ".idea/",
        ".vscode/",
        "",
        "# Python",
        "__pycache__/",
        "*.pyc",
        ".pytest_cache/",
        "*.egg-info/",
        "",
        "# Logs and databases",
        "*.log",
        "*.sqlite",
        "",
        "# OS files",
        ".DS_Store",
        "",
        "# Minified files",
        "*.min.js",
        "*.min.css",
    ]
    
    claudeignore_path.write_text("\n".join(default_patterns))
    logger.info("Created .claudeignore at %s", claudeignore_path)


async def _run_code_command(
    workspace_dir: str,
    prompt: str,
    run_logger: logging.Logger,
    agent: str = "claude",
    model: str = "azure/gpt-5",
) -> Dict[str, Any]:
    """Run Claude CLI or Codex with the given prompt on the workspace."""
    run_logger.info("=" * 60)
    run_logger.info("%s CLI COMMAND", agent.upper())
    run_logger.info("=" * 60)
    if agent == "claude":
        run_logger.info(f"ANTHROPIC_BASE_URL: {os.environ.get('ANTHROPIC_BASE_URL', 'not set')}")
        run_logger.info(f"ANTHROPIC_API_KEY: {'set' if os.environ.get('ANTHROPIC_API_KEY') else 'not set'}")
    run_logger.info(f"Workspace: {workspace_dir}")
    run_logger.info(f"Prompt length: {len(prompt)} chars")
    run_logger.info(f"Prompt preview:\n{prompt[:500]}...")

    try:
        if agent == "claude":
            ensure_claudeignore(workspace_dir)
            command = [
                "claude",
                "--print",
                prompt,
                "--dangerously-skip-permissions",
            ]
        elif agent == "codex":
            command = [
                "codex",
                "exec",
                prompt,
                "--full-auto",
            ]
        elif agent == "aider":
            import tempfile
            with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
                f.write(prompt)
                temp_file = f.name
            formatted_model = format_model_name(model, for_code_apply=True)
            command = [
                "aider",
                "deploy_local.sh",
                "--model", formatted_model,
                "--message-file", temp_file,
                "--no-show-model-warnings",
                "--no-auto-commits",
                "--no-gitignore",
                "--yes"
            ]
        else:
            return {"status": "error", "error": f"Unknown agent: {agent}"}

        run_logger.info(f"Command: {' '.join(command[:2])} '<prompt>' ...")
        run_logger.info("Executing %s CLI...", agent)
        logger.info("Running %s CLI command in %s", agent, workspace_dir)

        result = subprocess.run(
            command,
            cwd=workspace_dir,
            capture_output=True,
            text=True,
            timeout=600,
        )

        if agent == "aider":
            import os
            os.unlink(temp_file)

        run_logger.info("-" * 40)
        run_logger.info("%s CLI STDOUT:", agent.upper())
        run_logger.info("-" * 40)
        stdout_preview = result.stdout[:3000] if result.stdout else "(empty)"
        run_logger.info(stdout_preview)
        
        if result.stderr:
            run_logger.info("-" * 40)
            run_logger.info("%s CLI STDERR:", agent.upper())
            run_logger.info("-" * 40)
            run_logger.info(result.stderr[:1000])
        
        run_logger.info(f"Return code: {result.returncode}")
        run_logger.info("=" * 60)

        deploy_script = Path(workspace_dir) / "deploy_local.sh"
        if deploy_script.exists():
            script_content = deploy_script.read_text()
            run_logger.info(f"SUCCESS: deploy_local.sh created ({len(script_content)} chars)")
            run_logger.info(f"Script preview:\n{script_content[:500]}...")
            return {
                "status": "success",
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        
        run_logger.warning("deploy_local.sh was NOT created by %s CLI", agent)
        return {
            "status": "error",
            "stdout": result.stdout,
            "stderr": result.stderr,
            "error": f"{agent} CLI completed but deploy_local.sh was not created",
        }

    except subprocess.TimeoutExpired:
        error = f"{agent} CLI command timed out after 10 minutes"
        run_logger.error(error)
        return {"status": "error", "error": error}
    except FileNotFoundError:
        if agent == "claude":
            error = "Claude CLI not found. Install with: npm install -g @anthropic-ai/claude-code"
        elif agent == "codex":
            error = "Codex CLI not found. Install Codex."
        else:
            error = "Aider CLI not found. Install with: pip install aider-chat"
        run_logger.error(error)
        return {"status": "error", "error": error}
    except Exception as e:
        error = f"Error running {agent} CLI: {e}"
        logger.error(error, exc_info=True)
        run_logger.error(error, exc_info=True)
        return {"status": "error", "error": str(e)}


async def _run_deployment_script(
    workspace_dir: str,
    run_logger: logging.Logger,
) -> Dict[str, Any]:
    """Run the deployment script and return the result."""
    deploy_script = Path(workspace_dir) / "deploy_local.sh"

    if not deploy_script.exists():
        error = "deploy_local.sh not found after Claude Code generation"
        run_logger.error(error)
        return {"status": "error", "error": error}

    subprocess.run(["chmod", "+x", str(deploy_script)], check=True)

    try:
        run_logger.info("=" * 60)
        run_logger.info("RUNNING DEPLOYMENT SCRIPT")
        run_logger.info("=" * 60)
        run_logger.info(f"Script: {deploy_script}")
        logger.info("Running deployment script: %s", deploy_script)

        result = subprocess.run(
            ["bash", str(deploy_script)],
            cwd=workspace_dir,
            capture_output=True,
            text=True,
            timeout=300,  # Reduced to 5 minutes for simpler serving
            env={**subprocess.os.environ, "PORT": "8000"},
        )

        run_logger.info("-" * 40)
        run_logger.info("DEPLOYMENT STDOUT:")
        run_logger.info("-" * 40)
        run_logger.info(result.stdout if result.stdout else "(empty)")
        
        if result.stderr:
            run_logger.info("-" * 40)
            run_logger.info("DEPLOYMENT STDERR:")
            run_logger.info("-" * 40)
            run_logger.info(result.stderr)
        
        run_logger.info(f"Return code: {result.returncode}")
        run_logger.info("=" * 60)

        if result.returncode == 0:
            return {"status": "success", "stdout": result.stdout, "stderr": result.stderr}
        else:
            return {
                "status": "error",
                "error": f"Deployment failed:\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}",
            }

    except subprocess.TimeoutExpired:
        error = "Deployment script timed out after 5 minutes"
        run_logger.error(error)
        return {"status": "error", "error": error}
    except Exception as e:
        run_logger.error(f"Deployment error: {e}", exc_info=True)
        return {"status": "error", "error": str(e)}


async def _start_server_background(
    workspace_dir: str,
    run_logger: logging.Logger,
) -> Dict[str, Any]:
    """Start the server in the background and verify it's responding."""
    deploy_script = Path(workspace_dir) / "deploy_local.sh"

    if not deploy_script.exists():
        error = "deploy_local.sh not found"
        run_logger.error(error)
        return {"status": "error", "error": error}

    subprocess.run(["chmod", "+x", str(deploy_script)], check=True)

    try:
        run_logger.info("=" * 60)
        run_logger.info("STARTING SERVER IN BACKGROUND")
        run_logger.info("=" * 60)
        logger.info("Starting server in background...")

        log_file = Path(workspace_dir).parent / "server.log"
        with open(log_file, "w") as f:
            process = subprocess.Popen(
                ["bash", str(deploy_script)],
                cwd=workspace_dir,
                stdout=f,
                stderr=subprocess.STDOUT,
                env={**subprocess.os.environ, "PORT": "8000"},
            )

        run_logger.info(f"Server process started with PID: {process.pid}")
        run_logger.info(f"Server log: {log_file}")

        import urllib.request
        import urllib.error
        
        max_wait = 30
        check_interval = 2
        elapsed = 0
        server_ready = False
        
        while elapsed < max_wait:
            await asyncio.sleep(check_interval)
            elapsed += check_interval
            
            if process.poll() is not None:
                with open(log_file, "r") as f:
                    log_content = f.read()[-2000:]
                run_logger.error(f"Server process exited with code {process.returncode}")
                run_logger.error(f"Server log:\n{log_content}")
                return {
                    "status": "error", 
                    "error": f"Server exited with code {process.returncode}. Log: {log_content[-500:]}"
                }
            
            try:
                req = urllib.request.Request("http://127.0.0.1:8000", method="HEAD")
                urllib.request.urlopen(req, timeout=5)
                server_ready = True
                run_logger.info(f"Server is responding after {elapsed}s")
                break
            except urllib.error.URLError:
                run_logger.info(f"Waiting for server... ({elapsed}s)")
            except Exception as e:
                run_logger.info(f"Server check error: {e}")

        if server_ready:
            run_logger.info(f"Server started with PID: {process.pid}")
            run_logger.info("Server URL: http://127.0.0.1:8000")
            return {
                "status": "success",
                "pid": process.pid,
                "deployed_url": "http://127.0.0.1:8000",
            }
        else:
            if process.poll() is None:
                run_logger.warning("Server process running but not responding on port 8000")
                return {
                    "status": "success",
                    "pid": process.pid,
                    "deployed_url": "http://127.0.0.1:8000",
                    "warning": "Server may not be fully ready",
                }
            else:
                with open(log_file, "r") as f:
                    log_content = f.read()[-1000:]
                error = f"Server failed to start. Log: {log_content}"
                run_logger.error(error)
                return {"status": "error", "error": error}

    except Exception as e:
        run_logger.error(f"Error starting server: {e}", exc_info=True)
        return {"status": "error", "error": str(e)}


async def deploy_repo_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """LangGraph node that deploys the cloned repository using Claude Code."""

    async def _impl(current_state: Dict[str, Any]) -> Dict[str, Any]:
        workspace_dir = current_state.get("workspace_dir")
        if not workspace_dir:
            raise RuntimeError("workspace_dir is required (run clone_repo first)")

        agent = current_state.get("agent", "claude")
        log_file = current_state.get("log_file")
        repo_name = current_state.get("repo_name", "unknown")
        max_retries = 2
        
        run_logger = get_run_logger(log_file, repo_name)
        
        run_logger.info("=" * 60)
        run_logger.info("DEPLOY REPOSITORY NODE")
        run_logger.info("=" * 60)
        run_logger.info(f"Workspace: {workspace_dir}")

        # Step 1: Generate initial deployment script
        logger.info("Generating deployment script...")
        run_logger.info("Step 1: Generating deployment script with %s...", agent)
        
        result = await _run_code_command(
            workspace_dir=workspace_dir,
            prompt=DEPLOYMENT_PROMPT,
            run_logger=run_logger,
            agent=agent,
            model=current_state.get("model", "azure/gpt-5"),
        )

        deploy_script = Path(workspace_dir) / "deploy_local.sh"
        if not deploy_script.exists():
            run_logger.warning("deploy_local.sh not created, exiting deployment node.")
            raise RuntimeError("Failed to generate deploy_local.sh")

        # Step 2: Try to run deployment, fix errors iteratively with SIMPLER approaches
        for attempt in range(max_retries):
            logger.info("Deployment attempt %d/%d", attempt + 1, max_retries)
            run_logger.info(f"Deployment attempt {attempt + 1}/{max_retries}")

            deploy_result = await _run_deployment_script(workspace_dir, run_logger)

            if deploy_result.get("status") == "success":
                run_logger.info("Deployment script ran successfully")
                break

            if attempt < max_retries - 1:
                error = deploy_result.get("error", "Unknown error")
                # Keep more context in errors to help debugging
                error_truncated = error[:2000] if len(error) > 2000 else error
                # Escape quotes in error message to prevent formatting issues
                error_safe = error_truncated.replace('"', '\\"').replace("'", "\\'")
                logger.warning("Deployment failed, attempting simpler fix...")
                run_logger.warning(f"Deployment failed: {error}")
                run_logger.info("Attempting SIMPLER approach with %s...", agent)
                
                # Emphasize simplification in retry
                await _run_code_command(
                    workspace_dir=workspace_dir,
                    prompt=FIX_ERROR_PROMPT.format(error=error_safe),
                    run_logger=run_logger,
                    agent=agent,
                    model=current_state.get("model", "azure/gpt-5"),
                )

        # Start server in background
        run_logger.info("Starting server in background...")
        server_result = await _start_server_background(workspace_dir, run_logger)

        if server_result.get("status") != "success":
            error = server_result.get("error", "Failed to start server")
            current_state.setdefault("errors", []).append(error)
            run_logger.error(f"Failed to start server: {error}")
            raise RuntimeError(error)

        current_state["deployed_url"] = server_result["deployed_url"]
        current_state["server_pid"] = server_result.get("pid")
        current_state["url"] = server_result["deployed_url"]

        run_logger.info("=" * 60)
        run_logger.info(f"SERVER DEPLOYED SUCCESSFULLY")
        run_logger.info(f"URL: {server_result['deployed_url']}")
        run_logger.info(f"PID: {server_result.get('pid')}")
        run_logger.info("=" * 60)
        
        logger.info("Server deployed at: %s", server_result["deployed_url"])
        return current_state

    return await run_with_timing("deploy_repo", state, _impl)