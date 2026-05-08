import argparse
import json


def main():
    parser = argparse.ArgumentParser(
        description="Filter a JSONL file to keep only PERSON records."
    )
    parser.add_argument("input", help="Path to the input JSONL file")
    parser.add_argument("output", help="Path to the output JSONL file")
    args = parser.parse_args()

    kept = 0
    skipped = 0

    with open(args.input, "r") as infile, open(args.output, "w") as outfile:
        for line in infile:
            stripped = line.strip()
            if not stripped:
                continue
            record = json.loads(stripped)
            if record.get("RECORD_TYPE") == "PERSON":
                outfile.write(line)
                kept += 1
            else:
                skipped += 1

    print(f"Done. Kept {kept} PERSON records, skipped {skipped} non-PERSON records.")


if __name__ == "__main__":
    main()
