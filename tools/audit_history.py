"""Pre-publication audit of EVERY commit: secrets, personal/local identifiers, sensitive words, copied source.

    python -m tools.audit_history [--reference DIR_OF_THIRD_PARTY_CHECKOUT] [--out report.json]

Exit code 1 if any HIGH finding exists. Findings list file@commit only (never the matched secret value).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

SECRET = {
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY"),
    "aws_key": re.compile(r"AKIA[0-9A-Z]{16}"),
    "google_api_key": re.compile(r"AIza[0-9A-Za-z_\-]{35}"),
    "oauth_token": re.compile(r"ya29\.[0-9A-Za-z_\-]{20,}"),
    "github_token": re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
    "slack_token": re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    "jwt": re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
    "aura_uri": re.compile(r"[a-z0-9]{8}\.databases\.neo4j\.io"),
    "url_with_credentials": re.compile(r"[a-z+]+://[^/\s:@]+:[^/\s:@]{4,}@[^/\s]+"),
    "openai_key": re.compile(r"sk-[A-Za-z0-9]{32,}"),
}
PERSONAL = {
    "local_path": re.compile(r"/mnt/[a-z0-9_]+/|/home/[a-z0-9_]+/|/Users/[A-Za-z0-9_]+/"),
    "email": re.compile(r"[A-Za-z0-9._%+-]+@(?!example\.|users\.noreply\.github\.com)[A-Za-z0-9.-]+\.[a-z]{2,}"),
}
SENSITIVE_WORDS = re.compile(r"confidential|proprietary|internal use|customer data|credit (?:score|report)", re.I)
BINARY_EXT = {".png", ".jpg", ".gif", ".ico", ".pdf", ".zip", ".gz"}


def git(*a: str) -> str:
    return subprocess.run(["git", *a], capture_output=True, text=True, check=True).stdout  # noqa: S603, S607


def entropy(s: str) -> float:
    c = Counter(s)
    return -sum(n / len(s) * math.log2(n / len(s)) for n in c.values())


def norm_lines(text: str) -> set[str]:
    return {
        h
        for ln in text.splitlines()
        if len(t := " ".join(ln.split())) >= 45 and (h := hashlib.sha1(t.encode(), usedforsecurity=False).hexdigest())
    }  # noqa: S324


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reference", help="checkout of third-party source to compare against")
    ap.add_argument("--out")
    ap.add_argument(
        "--words", default="", help="extra |-separated sensitive words to flag (e.g. employer names, local usernames)"
    )
    a = ap.parse_args()
    global SENSITIVE_WORDS
    if a.words:
        SENSITIVE_WORDS = re.compile(SENSITIVE_WORDS.pattern + "|" + a.words, re.I)
    commits = git("rev-list", "--all").split()
    findings: list[dict[str, str]] = []
    seen_blobs: dict[str, str] = {}
    for c in commits:
        meta = git("log", "-1", "--format=%an|%ae|%cn|%ce|%B", c)
        for _m in PERSONAL["email"].finditer(meta):
            findings.append(
                {"severity": "MEDIUM", "kind": "commit_identity_email", "where": f"{c[:7]} (author/committer metadata)"}
            )
            break
        if SENSITIVE_WORDS.search(meta):
            findings.append({"severity": "LOW", "kind": "sensitive_word_in_message", "where": c[:7]})
        for line in git("ls-tree", "-r", "--format=%(objectname) %(path)", c).splitlines():
            blob, path = line.split(" ", 1)
            if (
                blob in seen_blobs
                or Path(path).suffix in BINARY_EXT
                or path.endswith(".lock")
                or path == "tools/audit_history.py"
            ):  # skip self: its patterns are the matches
                continue
            seen_blobs[blob] = path
            text = subprocess.run(["git", "cat-file", "-p", blob], capture_output=True).stdout.decode("utf-8", "ignore")  # noqa: S603, S607
            for name, rx in SECRET.items():
                if rx.search(text) and not (
                    name == "url_with_credentials" and "example" in text and "user:pass" in text
                ):
                    findings.append({"severity": "HIGH", "kind": f"secret:{name}", "where": f"{path}@{c[:7]}"})
            for name, rx in PERSONAL.items():
                if name == "email":
                    continue
                if rx.search(text):
                    findings.append({"severity": "MEDIUM", "kind": f"personal:{name}", "where": f"{path}@{c[:7]}"})
            for mm in SENSITIVE_WORDS.finditer(text):
                findings.append(
                    {"severity": "LOW", "kind": f"sensitive_word:{mm.group(0).lower()}", "where": f"{path}@{c[:7]}"}
                )
            for tok in re.findall(r"[A-Za-z0-9+/_\-]{32,}", text):
                if entropy(tok) > 4.6 and not re.fullmatch(r"[0-9a-f]+", tok) and "/" not in tok:
                    findings.append(
                        {"severity": "LOW", "kind": "high_entropy_token(review)", "where": f"{path}@{c[:7]}"}
                    )
                    break
    copied: list[dict[str, str]] = []
    if a.reference:
        ref: set[str] = set()
        for f in Path(a.reference).rglob("*"):
            if f.is_file() and ".git" not in f.parts and f.suffix in {".py", ".md", ".txt", ".ipynb", ".svg"}:
                ref |= norm_lines(f.read_text(errors="ignore"))
        for path in git("ls-files").splitlines():
            p = Path(path)
            if p.suffix in {".py", ".md", ".yml", ".yaml", ".txt", ".toml"} and p.is_file():
                lines = norm_lines(p.read_text(errors="ignore"))
                hit = lines & ref
                if hit:
                    copied.append({"file": path, "overlapping_lines": str(len(hit))})
        for cp in copied:  # a couple of generic idiom lines (e.g. pydantic Field defaults) are not copying
            sev = "HIGH" if int(cp["overlapping_lines"]) >= 5 else "LOW"
            findings.append(
                {
                    "severity": sev,
                    "kind": "overlap_with_reference(review)" if sev == "LOW" else "possible_copied_source",
                    "where": f"{cp['file']} ({cp['overlapping_lines']} lines)",
                }
            )
    uniq = [dict(t) for t in {tuple(sorted(f.items())) for f in findings}]
    summary = Counter(f["severity"] for f in uniq)
    rep = {
        "commits_scanned": len(commits),
        "blobs_scanned": len(seen_blobs),
        "summary": dict(summary),
        "reference_compared": bool(a.reference),
        "findings": sorted(uniq, key=lambda f: (f["severity"], f["kind"], f["where"])),
    }
    if a.out:
        Path(a.out).write_text(json.dumps(rep, indent=2) + "\n")
    print(json.dumps({k: v for k, v in rep.items() if k != "findings"}))
    for item in sorted(uniq, key=lambda x: (x["severity"], x["kind"], x["where"])):
        print(item["severity"], item["kind"], item["where"])
    return 1 if summary.get("HIGH") else 0


if __name__ == "__main__":
    sys.exit(main())
