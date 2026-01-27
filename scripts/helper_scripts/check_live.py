import asyncio
import nest_asyncio
import aiohttp
from datasets import load_dataset, Dataset
from tqdm.asyncio import tqdm

nest_asyncio.apply()

# =========================
# CONFIG
# =========================
DATASET_NAME = "Ayush-Singh/cwv-bench-v1"
SPLIT = "train"
REVISION = "f3342b9814e311ce9c0d3a3d71d60bb633092fe5"  # optional
OUTPUT_DATASET = "Ayush-Singh/cwv-bench-v1"
TIMEOUT_SEC = 15
MAX_CONCURRENT_REQUESTS = 100

# =========================
# SAFETY CHECK
# =========================
# if OUTPUT_DATASET == DATASET_NAME:
#     raise RuntimeError("Refusing to overwrite source dataset")

# =========================
# URL GENERATION (DUAL)
# =========================
def get_site_urls(repo_id: str):
    repo_id = repo_id.strip()

    if repo_id.startswith("http://") or repo_id.startswith("https://"):
        return [repo_id]

    urls = []

    if "/" in repo_id:
        owner, repo = repo_id.split("/", 1)

        # User / Org page candidate
        if repo.lower().endswith(".github.io"):
            urls.append(f"https://{repo}")

        # Project page candidate
        urls.append(f"https://{owner}.github.io/{repo}")

    else:
        urls.append(f"https://{repo_id}")

    # Deduplicate while preserving order
    return list(dict.fromkeys(urls))


def get_repo_url(repo_id: str):
    if "/" in repo_id and not repo_id.startswith("http"):
        return f"https://github.com/{repo_id}"
    return None


# =========================
# ASYNC CHECK (MULTI URL)
# =========================
async def check_site_multi(session, urls, semaphore):
    async with semaphore:
        for url in urls:
            try:
                async with session.get(url, timeout=TIMEOUT_SEC, allow_redirects=True) as resp:
                    status = resp.status

                    if 200 <= status < 300:
                        return True, status, None, url

                    if status == 404:
                        continue

            except asyncio.TimeoutError:
                continue
            except aiohttp.ClientError:
                continue
            except Exception:
                continue

        return False, None, "all_urls_failed", None


# =========================
# MAIN
# =========================
async def main():
    print(f"Loading dataset: {DATASET_NAME} @ {REVISION}")
    dataset = load_dataset(DATASET_NAME, split=SPLIT, revision=REVISION)

    rows = [dict(row) for row in dataset]
    updated_rows = []

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

    print(f"Checking {len(rows)} sites...")

    async with aiohttp.ClientSession() as session:
        tasks = []
        metadata = []

        for row in rows:
            repo_id = (
                row.get("repo_id")
                or row.get("REPO_ID")
                or row.get("repo_name")
                or ""
            )

            if not repo_id:
                tasks.append(asyncio.create_task(asyncio.sleep(0)))
                metadata.append((None, None))
                continue

            site_urls = get_site_urls(repo_id)
            repo_url = get_repo_url(repo_id)

            metadata.append((site_urls, repo_url))
            tasks.append(check_site_multi(session, site_urls, semaphore))

        results = await tqdm.gather(*tasks)

    for row, result, (site_urls, repo_url) in zip(rows, results, metadata):

        if result is None:
            is_live_data = {
                "LIVE": False,
                "STATUS": None,
                "ERROR": "missing_repo_id",
                "CHECKED_URL": None,
                "REPO_URL": None
            }
        else:
            is_live, status, error, working_url = result
            is_live_data = {
                "LIVE": is_live,
                "STATUS": status,
                "ERROR": error,
                "CHECKED_URL": working_url,
                "REPO_URL": repo_url
            }

        row["IS_LIVE"] = is_live_data
        updated_rows.append(row)

    assert len(updated_rows) > 0, "No rows processed — aborting"

    print("Creating HF dataset...")
    new_dataset = Dataset.from_list(updated_rows)

    print(f"Pushing to hub: {OUTPUT_DATASET}")
    new_dataset.push_to_hub(OUTPUT_DATASET)

    print("Done ✅")


if __name__ == "__main__":
    asyncio.get_event_loop().run_until_complete(main())
