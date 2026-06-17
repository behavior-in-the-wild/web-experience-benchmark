# Visual Regression Validator

This package compares a patched site against a baseline site and writes a
`visual.json` verdict for the harness.

The public release uses three checks:

1. `jaccard_text`: rendered text-token similarity.
2. `gpt_visual`: screenshot comparison between baseline and patched pages.
3. `console_errors`: newly introduced browser console errors after localhost
   noise filtering.

When more than one check is valid, visual regression is reported only when at
least two checks vote `true`. If only one check is valid, that check determines
the verdict. If no checks are valid, the verdict is inconclusive.

## Usage

The harness calls this module through `harness/evaluate.sh`:

```bash
python3 src/regression_tool/visual_validate.py \
  --url http://127.0.0.1:14000/ \
  --screenshot-path /tmp/result/patched.png \
  --repo-id owner/repo \
  --commit-id abc1234 \
  --framework "Static HTML" \
  --output-json /tmp/result/visual.json
```

For normal benchmark runs, prefer:

```bash
cd harness
./evaluate.sh --config configs/closed/codex.env
```

## Environment

Set the model provider credentials required by the configured screenshot
comparison backend. For Azure OpenAI mode, the validator reads:

- `AZURE_OPENAI_API_KEY`
- `AZURE_OPENAI_ENDPOINT`
- `AZURE_DEPLOYMENT`
- `OPENAI_API_VERSION`
