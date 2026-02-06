import csv

in_csv = "cwv-bench-v0.csv"
out_csv = "tmp.csv"

ROW_COUNT = 10

# Columns you want in the output, in order
FIELDS = [
    "ID",
    "REPO_ID",
    "COMMIT_ID",
    "ZIP_REPO_PATH",
    "HOST_FILE_PATH",
]

with open(in_csv, newline="", encoding="utf-8") as fin, \
     open(out_csv, "w", newline="", encoding="utf-8") as fout:

    reader = csv.DictReader(fin)
    writer = csv.DictWriter(fout, fieldnames=FIELDS)

    writer.writeheader()

    count = 0
    for row in reader:
        writer.writerow({k: row.get(k, "") for k in FIELDS})
        count += 1
        if count >= ROW_COUNT:
            break

print(f"Wrote {count} rows to {out_csv}")
