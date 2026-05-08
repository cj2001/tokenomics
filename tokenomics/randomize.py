import argparse
import random


def main():
    parser = argparse.ArgumentParser(description="Randomize and sample lines from a JSONL file.")
    parser.add_argument("--input", required=True, help="Path to the input JSONL file")
    parser.add_argument("--output", required=True, help="Path to the output JSONL file")
    parser.add_argument("--seed", type=int, required=True, help="Random seed for reproducibility")
    parser.add_argument("--num-lines", type=int, required=True, help="Number of lines to output")
    args = parser.parse_args()

    with open(args.input, "r") as f:
        lines = f.readlines()

    random.seed(args.seed)
    random.shuffle(lines)

    with open(args.output, "w") as f:
        f.writelines(lines[: args.num_lines])


if __name__ == "__main__":
    main()
