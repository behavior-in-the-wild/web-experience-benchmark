import json
from collections import Counter
from pathlib import Path

def print_framework_distribution(jsonl_path):
    counts = Counter()

    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue

            obj = json.loads(line)
            fw = obj.get("framework")

            if fw:
                counts[fw] += 1

    for fw, cnt in counts.most_common():
        print(f"{fw}: {cnt}")


print_framework_distribution(Path("cwv-bench-exps/11_tech_stacks_filtered/gh_25_11_tech_stacks_filtered.jsonl"))