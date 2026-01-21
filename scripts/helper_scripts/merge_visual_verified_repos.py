#!/usr/bin/env python3
"""
Merge successful visually verified repos from GH-25 and Stack sources.
"""
import json
from pathlib import Path

# Input files
GH25_FILE = Path("gh25_deploy_results_jekyll_hexo_static/successful_deployments.jsonl")
STACK_FILE = Path("stack_deploy_results_jekyll_hexo_static/successful_deployments.jsonl")

# Output file
OUTPUT_FILE = Path("cwv-bench-exps/successful_visual_verified_repos.jsonl")

def load_and_filter(file_path: Path, source: str) -> list:
    """Load JSONL and filter for successful visual verification."""
    results = []
    with open(file_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                
                # Filter: status must be "success" AND visual_verification.is_valid must be True
                if entry.get("status") != "success":
                    continue
                
                visual = entry.get("visual_verification", {})
                if not visual.get("is_valid", False):
                    continue
                
                # Extract repo_name from repo_id
                repo_id = entry.get("repo_id", "")
                repo_name = repo_id.split("/")[-1] if "/" in repo_id else repo_id
                
                # Build clean output entry
                results.append({
                    "repo_id": repo_id,
                    "repo_name": repo_name,
                    "framework": entry.get("framework", "Unknown"),
                    "source": source,
                })
            except json.JSONDecodeError:
                continue
    return results

def main():
    all_results = []
    seen_repos = set()
    
    # Load GH-25 results
    if GH25_FILE.exists():
        gh25_results = load_and_filter(GH25_FILE, "GH-25")
        for r in gh25_results:
            if r["repo_id"] not in seen_repos:
                all_results.append(r)
                seen_repos.add(r["repo_id"])
        print(f"GH-25: {len(gh25_results)} repos with valid visual verification")
    else:
        print(f"Warning: {GH25_FILE} not found")
    
    # Load Stack results
    if STACK_FILE.exists():
        stack_results = load_and_filter(STACK_FILE, "Stack")
        added = 0
        for r in stack_results:
            if r["repo_id"] not in seen_repos:
                all_results.append(r)
                seen_repos.add(r["repo_id"])
                added += 1
        print(f"Stack: {len(stack_results)} repos with valid visual verification ({added} new)")
    else:
        print(f"Warning: {STACK_FILE} not found")
    
    # Write output
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w") as f:
        for entry in all_results:
            f.write(json.dumps(entry) + "\n")
    
    # Summary
    print(f"\n{'='*50}")
    print(f"Total merged: {len(all_results)} unique repos")
    print(f"Output: {OUTPUT_FILE}")
    
    # Framework breakdown
    frameworks = {}
    sources = {"GH-25": 0, "Stack": 0}
    for r in all_results:
        fw = r.get("framework", "Unknown")
        frameworks[fw] = frameworks.get(fw, 0) + 1
        sources[r.get("source", "Unknown")] = sources.get(r.get("source"), 0) + 1
    
    print(f"\nFramework breakdown:")
    for fw, count in sorted(frameworks.items(), key=lambda x: -x[1]):
        print(f"  {fw}: {count}")
    
    print(f"\nSource breakdown:")
    for src, count in sources.items():
        print(f"  {src}: {count}")

if __name__ == "__main__":
    main()
