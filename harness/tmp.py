from datasets import load_dataset
import csv

out_csv = "harness/cwv-bench-v0.csv"
ds = load_dataset("behavior-in-the-wild/cwv-bench-v0", split = "train")
ds.to_csv(out_csv)

print(f"Saved {len(ds)} datapoints to {out_csv}")