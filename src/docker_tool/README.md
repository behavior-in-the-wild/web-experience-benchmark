# Docker Tool

Shared hosting tool for CWV and regression jobs.

Build images:

```bash
bash src/docker_tool/build_images.sh
```

Start a host:

```bash
python -m docker_tool host \
  --repo-dir /path/to/repo \
  --framework "Static HTML" \
  --port 4000 \
  --log /tmp/host.log \
  --mode auto
```

`--mode auto` tries Docker first and falls back to the legacy local host scripts
when Docker or the framework image is unavailable. Use `--mode docker` to fail
fast when Docker isolation is required.
