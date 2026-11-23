"""Repository hygiene: no credentials committed, no auth-disable switch, release guards present."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SECRET_PATTERNS = [
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{35}"),
    re.compile(r"ya29\.[0-9A-Za-z_\-]{20,}"),
    re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),  # a real-looking JWT
    re.compile(r"neo4j\+s://[^\s\"']*[A-Za-z0-9]{8}\.databases\.neo4j\.io"),
]


def tracked_files() -> list[Path]:
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout  # noqa: S607
    return [ROOT / f for f in out.splitlines() if (ROOT / f).is_file()]


def test_no_secret_patterns_in_tracked_files():
    hits = []
    for f in tracked_files():
        if f.suffix in {".lock"} or f.name == "test_repo_hygiene.py":
            continue
        text = f.read_text(errors="ignore")
        hits += [f"{f.relative_to(ROOT)}: {p.pattern[:30]}" for p in SECRET_PATTERNS if p.search(text)]
    assert not hits, hits


def test_env_files_and_local_state_are_not_tracked():
    names = {f.relative_to(ROOT).as_posix() for f in tracked_files()}
    assert not [n for n in names if n.startswith(".local/") or n == ".env" or n.endswith(".pem")]


def test_serving_image_does_not_ship_seeder():
    df = (ROOT / "Dockerfile").read_text()
    runtime = df.split("AS runtime")[1]
    assert "COPY tools" not in runtime and "COPY data" not in runtime and "USER 10001" in runtime


def test_no_auth_bypass_in_source():
    src = "\n".join(p.read_text() for p in (ROOT / "src").rglob("*.py")).lower()
    for needle in ("auth_disabled", "dev_auth", "skip_auth", "allow_unauthenticated", "insecure"):
        assert needle not in src, needle
