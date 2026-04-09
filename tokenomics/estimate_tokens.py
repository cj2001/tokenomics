#!/usr/bin/env python3
"""Estimate the number of Claude API tokens for two JSONL files."""

import argparse
import os
import sys

import anthropic
import tiktoken
from dotenv import load_dotenv


def read_file(path: str) -> str:
    with open(path, "r") as f:
        return f.read()


def count_lines(path: str) -> int:
    with open(path, "r") as f:
        return sum(1 for _ in f)


def tiktoken_estimate(text: str) -> int:
    """Estimate tokens using tiktoken's cl100k_base encoding (closest public proxy for Claude)."""
    enc = tiktoken.get_encoding("cl100k_base")
    return len(enc.encode(text))


def anthropic_count(client: anthropic.Anthropic, text: str, model: str) -> int:
    """Get exact token count from the Anthropic API."""
    resp = client.messages.count_tokens(
        model=model,
        messages=[{"role": "user", "content": text}],
    )
    return resp.input_tokens


def main():
    parser = argparse.ArgumentParser(
        description="Estimate Claude API tokens for two JSONL files."
    )
    parser.add_argument("file1", help="Path to the first JSONL file")
    parser.add_argument("file2", help="Path to the second JSONL file")
    parser.add_argument(
        "--model",
        default="claude-sonnet-4-20250514",
        help="Claude model to use for token counting (default: claude-sonnet-4-20250514)",
    )
    args = parser.parse_args()

    # Load API key from .env
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("Error: ANTHROPIC_API_KEY not found in .env file", file=sys.stderr)
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)

    for path in [args.file1, args.file2]:
        if not os.path.isfile(path):
            print(f"Error: file not found: {path}", file=sys.stderr)
            sys.exit(1)

    content1 = read_file(args.file1)
    content2 = read_file(args.file2)
    combined = content1 + "\n" + content2

    print(f"{'File':<45} {'Lines':>8} {'Size (KB)':>10} {'tiktoken':>10} {'API tokens':>12}")
    print("-" * 90)

    for label, path, content in [
        ("File 1", args.file1, content1),
        ("File 2", args.file2, content2),
        ("Combined", "—", combined),
    ]:
        name = os.path.basename(path) if path != "—" else "Combined"
        lines = count_lines(path) if path != "—" else count_lines(args.file1) + count_lines(args.file2)
        size_kb = len(content.encode("utf-8")) / 1024

        tik = tiktoken_estimate(content)
        api = anthropic_count(client, content, args.model)

        print(f"{name:<45} {lines:>8,} {size_kb:>9,.1f} {tik:>10,} {api:>12,}")

    print()
    print(f"Model used for API count: {args.model}")


if __name__ == "__main__":
    main()
