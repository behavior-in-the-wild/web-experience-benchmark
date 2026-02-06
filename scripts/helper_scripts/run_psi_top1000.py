import os, json, time, random
import requests
from tqdm import tqdm
from datasets import load_dataset

DATASET_NAME = "behavior-in-the-wild/cwv-bench-v0"
SPLIT = "train"
TOP_N = 1000

STRATEGY = "mobile"     # or "desktop"
OUT_JSONL = "psi_metrics.jsonl"
CHECKPOINT_FILE = "checkpoint.json"
CHECKPOINT_EVERY = 20

PSI_API_KEY = os.environ.get("PSI_API_KEY")

# Faster pacing (but still polite). If 429 happens a lot, increase base_sleep.
BASE_SLEEP = 0.6
JITTER = 0.3

# Fail fast on 429 instead of stalling
MAX_429_RETRIES = 2          # only retry a couple times
RETRY_WAIT_S = 5             # short waits
TIMEOUT_S = 30               # don't hang too long

def repo_id_to_pages_url(repo_id: str):
    if not repo_id or "/" not in repo_id:
        return None
    owner, repo = repo_id.split("/", 1)
    owner, repo = owner.strip(), repo.strip()
    if not owner or not repo:
        return None
    if repo.lower() == f"{owner.lower()}.github.io":
        return f"https://{owner}.github.io/"
    return f"https://{owner}.github.io/{repo}/"

def call_psi_failfast(url: str, strategy: str):
    endpoint = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
    params = [("url", url), ("strategy", strategy), ("category", "performance")]
    if PSI_API_KEY:
        params.append(("key", PSI_API_KEY))

    for attempt in range(MAX_429_RETRIES + 1):
        r = requests.get(endpoint, params=params, timeout=TIMEOUT_S)

        if r.status_code == 429:
            if attempt == MAX_429_RETRIES:
                r.raise_for_status()
            time.sleep(RETRY_WAIT_S + random.uniform(0, 1.0))
            continue

        # retry a bit for transient server errors
        if 500 <= r.status_code < 600:
            if attempt == MAX_429_RETRIES:
                r.raise_for_status()
            time.sleep(RETRY_WAIT_S + random.uniform(0, 1.0))
            continue

        r.raise_for_status()
        return r.json()

def audit_num(audits: dict, audit_id: str):
    a = audits.get(audit_id) or {}
    return a.get("numericValue")

def extract_metrics(psi_json: dict):
    lr = psi_json.get("lighthouseResult") or {}
    audits = lr.get("audits") or {}

    metrics = {
        "LCP":  audit_num(audits, "largest-contentful-paint"),
        "CLS":  audit_num(audits, "cumulative-layout-shift"),
        "INP":  audit_num(audits, "interaction-to-next-paint"),
        "TTFB": audit_num(audits, "server-response-time"),
        "FCP":  audit_num(audits, "first-contentful-paint"),
    }

    # optional lcp element label (may be missing)
    lcp_a = audits.get("largest-contentful-paint") or {}
    items = ((lcp_a.get("details") or {}).get("items") or [])
    lcp_elem = None
    if items and isinstance(items, list) and isinstance(items[0], dict):
        node = items[0].get("node") or {}
        lcp_elem = node.get("nodeLabel")
    metrics["lcp_element"] = lcp_elem

    return metrics

def load_checkpoint():
    if os.path.exists(CHECKPOINT_FILE):
        with open(CHECKPOINT_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"next_index": 0, "done_repo_ids": []}

def save_checkpoint(next_index: int, done_repo_ids: list[str]):
    with open(CHECKPOINT_FILE, "w", encoding="utf-8") as f:
        json.dump({"next_index": next_index, "done_repo_ids": done_repo_ids}, f, indent=2)

def main():
    ds = load_dataset(DATASET_NAME, split=SPLIT)
    ds = ds.select(range(min(TOP_N, len(ds))))

    cp = load_checkpoint()
    next_index = cp.get("next_index", 0)
    done = set(cp.get("done_repo_ids", []))

    # append mode so reruns don't delete prior results
    out_f = open(OUT_JSONL, "a", encoding="utf-8")

    processed_since_cp = 0
    pbar = tqdm(range(next_index, len(ds)), desc=f"PSI {STRATEGY} top{TOP_N}")

    for i in pbar:
        row = ds[i]
        repo_id = row.get("REPO_ID")

        if repo_id in done:
            continue

        url = repo_id_to_pages_url(repo_id)
        out = {"REPO_ID": repo_id, "url": url, "strategy": STRATEGY,
               "status": "ok", "metrics": None, "error": None}

        if not url:
            out["status"] = "bad_repo_id"
            done.add(repo_id)
            out_f.write(json.dumps(out) + "\n")
            processed_since_cp += 1
            continue

        try:
            psi_json = call_psi_failfast(url, STRATEGY)
            out["metrics"] = extract_metrics(psi_json)
        except Exception as e:
            out["status"] = "psi_failed"
            out["error"] = str(e)

        done.add(repo_id)
        out_f.write(json.dumps(out) + "\n")
        out_f.flush()

        processed_since_cp += 1

        # checkpoint every 20 new processed items
        if processed_since_cp >= CHECKPOINT_EVERY:
            # store the next index i+1 so resume continues correctly
            save_checkpoint(i + 1, sorted(done))
            processed_since_cp = 0

        # pacing (keep small; bump BASE_SLEEP if 429 is frequent)
        time.sleep(BASE_SLEEP + random.uniform(0, JITTER))

    # final checkpoint at end
    save_checkpoint(len(ds), sorted(done))
    out_f.close()
    print("DONE. Results:", OUT_JSONL, "Checkpoint:", CHECKPOINT_FILE)

if __name__ == "__main__":
    main()
