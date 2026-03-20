import os
import json
import re
from datetime import datetime

def extract_stats():
    dumps_dir = "/home/ssm-user/working/arnav/adobe/web-experience-benchmark/dumps"
    
    # regex to parse repo_name_YYYYMMDD_HHMMSS
    # some repos might have dots in them, etc.
    # example: 00btwebhub.io_20260302_103322
    pattern = re.compile(r"^(.*)_(\d{8})_(\d{6})$")
    
    repo_latest_runs = {}
    
    for entry in os.listdir(dumps_dir):
        path = os.path.join(dumps_dir, entry)
        if not os.path.isdir(path):
            continue
            
        match = pattern.match(entry)
        if match:
            repo_name = match.group(1)
            date_str = match.group(2)
            time_str = match.group(3)
            
            try:
                timestamp = datetime.strptime(f"{date_str}_{time_str}", "%Y%m%d_%H%M%S")
                
                if repo_name not in repo_latest_runs or timestamp > repo_latest_runs[repo_name]['timestamp']:
                    repo_latest_runs[repo_name] = {
                        'timestamp': timestamp,
                        'path': path
                    }
            except ValueError:
                continue

    total_suggestions = 0
    repos_with_data = 0
    
    print(f"Found {len(repo_latest_runs)} unique repositories in dumps.")
    print("-" * 60)
    print(f"{'Repository':<40} | {'Suggestions':<12}")
    print("-" * 60)

    results = []

    for repo_name, info in sorted(repo_latest_runs.items()):
        suggestions_file = os.path.join(info['path'], "results", "cwv_suggestions_mobile.json")
        
        if os.path.exists(suggestions_file):
            try:
                with open(suggestions_file, 'r') as f:
                    data = json.load(f)
                    suggestion_count = len(data.get("suggestions", []))
                    total_suggestions += suggestion_count
                    repos_with_data += 1
                    print(f"{repo_name:<40} | {suggestion_count:<12}")
                    results.append((repo_name, suggestion_count))
            except (json.JSONDecodeError, IOError) as e:
                print(f"{repo_name:<40} | Error reading file: {e}")
        else:
            print(f"{repo_name:<40} | No suggestions file found")

    print("-" * 60)
    if repos_with_data > 0:
        avg = total_suggestions / repos_with_data
        print(f"Total Unique Repos with data: {repos_with_data}")
        print(f"Total Suggestions: {total_suggestions}")
        print(f"Average Suggestions PER UNIQUE REPO: {avg:.2f}")
    else:
        print("No suggestion data found.")

if __name__ == "__main__":
    extract_stats()
