#!/usr/bin/env python3
"""
Script to enrich HuggingFace dataset with file stats from JSONL files.

Adds a single column:
1. METADATA - Dictionary containing:
   - SIZE_STATS: { FILE_SIZE_AVG, TOTAL_FILE_SIZE }
   - FILES: List of objects { PATH, LANGUAGE, LENGTH_BYTES }
"""

import json
from datasets import load_dataset, Dataset
from pathlib import Path
from tqdm import tqdm

# Paths to JSONL files (Filtered versions)
WORKSPACE_DIR = Path("/Users/ayushsingh/Desktop/Projects/cwv_adobe_work/web-experience-benchmark")
GH25_JSONL = WORKSPACE_DIR / "cwv-bench-exps/gh_25_github_io_repos_filtered.jsonl"
STACK_JSONL = WORKSPACE_DIR / "cwv-bench-exps/stack_github_io_websites_filtered.jsonl"


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
    # 1. Validation of inputs
    if not GH25_JSONL.exists():
        raise FileNotFoundError(f"Missing GH25 JSONL file: {GH25_JSONL}")
    if not STACK_JSONL.exists():
        raise FileNotFoundError(f"Missing Stack JSONL file: {STACK_JSONL}")

    print("Loading HuggingFace dataset: Ayush-Singh/cwv-bench-v1...")
    dataset = load_dataset("Ayush-Singh/cwv-bench-v1", split="train")
    print(f"Loaded {len(dataset)} rows")
    
    print(f"\nLoading GH-25 JSONL: {GH25_JSONL}")
    gh25_data = load_jsonl_as_dict(GH25_JSONL, 'repo_id')
    print(f"Loaded {len(gh25_data)} GH-25 entries")
    
    print(f"\nLoading Stack JSONL: {STACK_JSONL}")
    stack_data = load_jsonl_as_dict(STACK_JSONL, 'repo_name')
    print(f"Loaded {len(stack_data)} Stack entries")
    
    # Process each row
    metadata_list = []
    matches = 0
    misses = 0
    
    print("\nProcessing dataset rows...")
    for row in tqdm(dataset, desc="Enriching"):
        # Handle uppercase keys from new schema
        source = row.get('SOURCE') or row.get('source', '')
        repo_id = row.get('REPO_ID') or row.get('repo_id') or row.get('repo_name', '')
        
        # Normalize source string
        source_lower = str(source).lower()
        
        file_size_avg = None
        total_file_size = None
        files = None
        
        if 'gh-25' in source_lower or source == 'GH-25':
            # Use repo_id to look up in GH-25 data
            entry = gh25_data.get(repo_id)
            if entry:
                file_size_avg, total_file_size, files = normalize_gh25_entry(entry)
                matches += 1
            else:
                misses += 1
        elif 'stack' in source_lower:
            # Stack JSONL uses repo_name/repo_id as matching key
            entry = stack_data.get(repo_id)
            if entry:
                file_size_avg, total_file_size, files = normalize_stack_entry(entry)
                matches += 1
            else:
                misses += 1
        else:
            # Fallback try generic match in both if source is ambiguous
            if repo_id in gh25_data:
                file_size_avg, total_file_size, files = normalize_gh25_entry(gh25_data[repo_id])
                matches += 1
            elif repo_id in stack_data:
                file_size_avg, total_file_size, files = normalize_stack_entry(stack_data[repo_id])
                matches += 1
            else:
                misses += 1
        
        # Construct METADATA dict with uppercase keys
        if files is not None:
            # Convert files keys to uppercase
            files_upper = []
            for f in files:
                files_upper.append({
                    'PATH': f['path'],
                    'LANGUAGE': f['language'],
                    'LENGTH_BYTES': f['length_bytes']
                })
            
            metadata = {
                'SIZE_STATS': {
                    'FILE_SIZE_AVG': file_size_avg,
                    'TOTAL_FILE_SIZE': total_file_size
                },
                'FILES': files_upper
            }
        else:
            metadata = None
            
        metadata_list.append(metadata)
    
    print(f"\n✓ Matched: {matches}")
    print(f"✗ Missed: {misses}")
    
    # Add new column to dataset
    print("\nAdding new columns to dataset...")
    # Remove old columns if checking re-run locally (optional)
    for col in ["file_size_avg", "total_file_size", "files", "METADATA"]:
        if col in dataset.column_names:
            dataset = dataset.remove_columns([col])
    
    dataset = dataset.add_column("METADATA", metadata_list)
    
    # Show sample
    print("\nSample of enriched data:")
    sample = dataset[0]
    # Handle sample display safely with both new/old keys potentially present
    s_repo = sample.get('REPO_ID') or sample.get('repo_id') or sample.get('repo_name')
    s_source = sample.get('SOURCE') or sample.get('source')
    print(f"  repo: {s_repo}")
    print(f"  source: {s_source}")
    metadata = sample.get('METADATA')
    if metadata:
        print(f"  METADATA keys: {list(metadata.keys())}")
        if 'SIZE_STATS' in metadata:
            print(f"  SIZE_STATS: {metadata['SIZE_STATS']}")
        if 'FILES' in metadata:
            print(f"  FILES (first 3): {metadata['FILES'][:3]}")
    else:
        print("  METADATA: None")
    
    # Push to HuggingFace
    print("\nPushing to HuggingFace: Ayush-Singh/cwv-bench-v1...")
    dataset.push_to_hub("Ayush-Singh/cwv-bench-v1", split="train")
    print("✓ Done!")


if __name__ == "__main__":
    main()
