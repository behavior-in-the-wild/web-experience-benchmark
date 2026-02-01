import subprocess
from datasets import load_dataset
from tqdm import tqdm

# =========================
# CONFIG
# =========================
DATASET_NAME = "behavior-in-the-wild/cwv-bench-v0"
SPLIT = "train"
OUT_COL = "last_commit_sha"

# =========================
# CORE LOGIC
# =========================
def get_last_commit_sha(repo_id: str):
    """
    repo_id: owner/repo
    """
    repo_url = f"https://github.com/{repo_id}.git"
    try:
        result = subprocess.run(
            ["git", "ls-remote", repo_url, "HEAD"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=15,
        )

        if result.returncode != 0 or not result.stdout:
            return None

        # output: "<sha>\tHEAD"
        return result.stdout.split()[0]

    except Exception:
        return None

# =========================
# LOAD DATASET
# =========================
ds = load_dataset(DATASET_NAME, split=SPLIT)

# =========================
# MAP
# =========================
def add_commit(example):
    example[OUT_COL] = get_last_commit_sha(example["repo_id"])
    return example

ds = ds.map(
    add_commit,
    desc="Fetching last commit via git ls-remote",
    num_proc=4
)

# =========================
# OPTIONAL SAVE
# =========================
ds.push_to_hub(DATASET_NAME, split=SPLIT)
