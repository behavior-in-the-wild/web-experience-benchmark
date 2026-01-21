#!/usr/bin/env python3
"""
Script to enrich HuggingFace dataset with file stats from JSONL files.

Adds three columns:
1. file_size_avg - average of file sizes in the repo
2. total_file_size - sum of all file sizes in the repo
3. files - array of file stats objects with path, language, length_bytes
"""

import json
from datasets import load_dataset, Dataset
from pathlib import Path
from tqdm import tqdm

# Paths to JSONL files
GH25_JSONL = Path("/Users/ayushsingh/Desktop/Projects/coding_agent/cwv-bench-exps/gh_25_stack_heuristic_hexo_static_jekyll_dataset/gh_25_githubio_cwv_heuristic_results.jsonl")
STACK_JSONL = Path("/Users/ayushsingh/Desktop/Projects/coding_agent/cwv-bench-exps/gh_25_stack_heuristic_hexo_static_jekyll_dataset/stack_githubio_cwv_heuristic_results.jsonl")


def load_jsonl_as_dict(jsonl_path: Path, key_field: str) -> dict:
    """Load JSONL file and index by key field (repo_id or repo_name)."""
    data = {}
    with open(jsonl_path, 'r') as f:
        for line in f:
            if line.strip():
                entry = json.loads(line)
                key = entry.get(key_field)
                if key:
                    data[key] = entry
    return data


def normalize_gh25_entry(entry: dict) -> tuple[float, list]:
    """
    Normalize GH-25 format (separate size and file_path arrays) 
    to unified format (files array with objects).
    
    Returns: (file_size_avg, files_array)
    """
    sizes = entry.get('size', [])
    paths = entry.get('file_path', [])
    
    if not sizes:
        return None, None, None
    
    # Calculate average and total
    file_size_avg = sum(sizes) / len(sizes) if sizes else 0
    total_file_size = sum(sizes)
    
    # Build files array
    files = []
    for i, (path, size) in enumerate(zip(paths, sizes)):
        # GH-25 doesn't have language info, infer from extension
        ext = Path(path).suffix.lower()
        lang_map = {
            '.html': 'HTML', '.htm': 'HTML',
            '.css': 'CSS', '.scss': 'SCSS', '.sass': 'SASS',
            '.js': 'JavaScript', '.ts': 'TypeScript',
            '.py': 'Python', '.rb': 'Ruby',
            '.md': 'Markdown', '.markdown': 'Markdown',
            '.json': 'JSON', '.yml': 'YAML', '.yaml': 'YAML',
            '.xml': 'XML', '.svg': 'SVG',
            '.php': 'PHP', '.java': 'Java',
            '.ipynb': 'Jupyter Notebook',
        }
        language = lang_map.get(ext, 'Other')
        
        files.append({
            'path': path if path.startswith('/') else f'/{path}',
            'language': language,
            'length_bytes': size
        })
    
    return file_size_avg, total_file_size, files


def normalize_stack_entry(entry: dict) -> tuple[float, int, list]:
    """
    Normalize Stack format (files array with objects already).
    
    Returns: (file_size_avg, total_file_size, files_array)
    """
    files = entry.get('files', [])
    
    if not files:
        return None, None, None
    
    # Calculate average and total from files array
    sizes = [f.get('length_bytes', 0) for f in files]
    file_size_avg = sum(sizes) / len(sizes) if sizes else 0
    total_file_size = sum(sizes)
    
    # Files array is already in the right format
    return file_size_avg, total_file_size, files


def main():
    print("Loading HuggingFace dataset: Ayush-Singh/cwv-bench-v0...")
    dataset = load_dataset("Ayush-Singh/cwv-bench-v0", split="train")
    print(f"Loaded {len(dataset)} rows")
    
    print(f"\nLoading GH-25 JSONL: {GH25_JSONL}")
    gh25_data = load_jsonl_as_dict(GH25_JSONL, 'repo_id')
    print(f"Loaded {len(gh25_data)} GH-25 entries")
    
    print(f"\nLoading Stack JSONL: {STACK_JSONL}")
    stack_data = load_jsonl_as_dict(STACK_JSONL, 'repo_name')
    print(f"Loaded {len(stack_data)} Stack entries")
    
    # Process each row
    file_size_avgs = []
    total_file_sizes = []
    files_arrays = []
    matches = 0
    misses = 0
    
    print("\nProcessing dataset rows...")
    for row in tqdm(dataset, desc="Enriching"):
        source = row.get('source', '')
        repo_id = row.get('repo_id', '')
        repo_name = row.get('repo_name', '')
        
        file_size_avg = None
        total_file_size = None
        files = None
        
        if source == 'GH-25':
            # Use repo_id to look up in GH-25 data
            entry = gh25_data.get(repo_id)
            if entry:
                file_size_avg, total_file_size, files = normalize_gh25_entry(entry)
                matches += 1
            else:
                misses += 1
        else:
            # Stack JSONL uses owner/repo format in repo_name, matching HF repo_id
            entry = stack_data.get(repo_id)
            if entry:
                file_size_avg, total_file_size, files = normalize_stack_entry(entry)
                matches += 1
            else:
                misses += 1
        
        file_size_avgs.append(file_size_avg)
        total_file_sizes.append(total_file_size)
        files_arrays.append(files)
    
    print(f"\n✓ Matched: {matches}")
    print(f"✗ Missed: {misses}")
    
    # Add new columns to dataset (remove existing ones first if present)
    print("\nAdding new columns to dataset...")
    columns_to_add = ["file_size_avg", "total_file_size", "files"]
    for col in columns_to_add:
        if col in dataset.column_names:
            dataset = dataset.remove_columns([col])
    
    dataset = dataset.add_column("file_size_avg", file_size_avgs)
    dataset = dataset.add_column("total_file_size", total_file_sizes)
    dataset = dataset.add_column("files", files_arrays)
    
    # Show sample
    print("\nSample of enriched data:")
    sample = dataset[0]
    print(f"  repo_name: {sample.get('repo_name')}")
    print(f"  source: {sample.get('source')}")
    print(f"  file_size_avg: {sample.get('file_size_avg')}")
    print(f"  total_file_size: {sample.get('total_file_size')}")
    print(f"  files (first 3): {sample.get('files', [])[:3] if sample.get('files') else None}")
    
    # Push to HuggingFace
    print("\nPushing to HuggingFace: Ayush-Singh/cwv-bench-v0...")
    dataset.push_to_hub("Ayush-Singh/cwv-bench-v0", split="train")
    print("✓ Done!")


if __name__ == "__main__":
    main()
