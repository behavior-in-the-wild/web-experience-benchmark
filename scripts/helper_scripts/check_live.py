import asyncio
import nest_asyncio
import aiohttp
from datasets import load_dataset, Dataset
from tqdm.asyncio import tqdm

nest_asyncio.apply()

# =========================
# CONFIG
# =========================
DATASET_NAME = "Ayush-Singh/cwv-bench-v0"
SPLIT = "train"
OUTPUT_DATASET = "Ayush-Singh/cwv-bench-v0"
TIMEOUT_SEC = 10
MAX_CONCURRENT_REQUESTS = 10

# =========================
# HELPERS: URL CONSTRUCTORS
# =========================
def get_site_url(repo_name: str):
    """Generates the GitHub Pages URL (the site being checked)."""
    repo_name = repo_name.strip()
    if "/" in repo_name and "." not in repo_name.split("/")[0]:
        try:
            owner, repo = repo_name.split("/", 1)
            return f"https://{owner}.github.io/{repo}"
        except ValueError:
            return f"https://{repo_name}"
    if not repo_name.startswith("http"):
        return f"https://{repo_name}"
    return repo_name

def get_repo_url(repo_name: str):
    """Generates the GitHub Repository URL (the source code)."""
    repo_name = repo_name.strip()
    # If it looks like "owner/repo", construct github.com link
    if "/" in repo_name and "." not in repo_name.split("/")[0]:
        return f"https://github.com/{repo_name}"
    # Fallback if it's not a standard repo string
    return None

# =========================
# ASYNC SITE CHECK
# =========================
async def check_site(session, url, semaphore):
    async with semaphore:
        try:
            async with session.get(url, timeout=TIMEOUT_SEC) as response:
                status = response.status
                if 200 <= status < 300:
                    return True, status, None
                if status == 404:
                    return False, status, "404_not_found"
                return False, status, f"bad_status_{status}"

        except asyncio.TimeoutError:
            return False, None, "timeout"
        except aiohttp.ClientError as e:
            return False, None, f"connection_error: {str(e)}"
        except Exception as e:
            return False, None, str(e)

# =========================
# MAIN LOOP
# =========================
async def main():
    print(f"Loading dataset {DATASET_NAME}...")
    dataset = load_dataset(DATASET_NAME, split=SPLIT)
    
    rows = [dict(row) for row in dataset]
    updated_rows = []
    
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

    print(f"Checking {len(rows)} sites...")
    
    async with aiohttp.ClientSession() as session:
        tasks = []
        
        # 1. Prepare all tasks and URLs
        for row in rows:
            repo_name = row["repo_name"]
            
            # Construct URLs
            site_url = get_site_url(repo_name) # e.g. owner.github.io/repo
            repo_url = get_repo_url(repo_name) # e.g. github.com/owner/repo
            
            # Store them in the row immediately
            row["repo_url"] = repo_url
            row["checked_url"] = site_url 
            
            # Create async task
            tasks.append(check_site(session, site_url, semaphore))
        
        # 2. Run all checks concurrently
        results = await tqdm.gather(*tasks)

    # 3. Merge results back into rows
    for row, (is_live, status, error) in zip(rows, results):
        row["is_live"] = is_live
        row["site_status"] = status
        row["site_error"] = error
        updated_rows.append(row)

    print("Creating HF dataset...")
    new_dataset = Dataset.from_list(updated_rows)

    print(f"Pushing to hub: {OUTPUT_DATASET}...")
    new_dataset.push_to_hub(OUTPUT_DATASET)

    print("Done ✅")

if __name__ == "__main__":
    asyncio.get_event_loop().run_until_complete(main())