"""Text normalisation shared by the seeder (write side) and the server (read side).

The same function must produce ``name_norm`` at seed time and normalise queries at
request time, otherwise exact/prefix matching silently breaks.
"""

from __future__ import annotations

import re
import unicodedata

_WS = re.compile(r"\s+")
_TOKEN = re.compile(r"[a-z0-9]+")
STOPWORDS = frozenset(
    "a an and are as at be by for from how in is it of on or the to what which who why with does do "
    "between connect connected relate related relationship path show me tell about find".split()
)


def normalize(text: str) -> str:
    """NFKC, casefold, collapse whitespace."""
    return _WS.sub(" ", unicodedata.normalize("NFKC", text)).strip().casefold()


def tokens(text: str) -> list[str]:
    """Distinct content tokens of ``text`` in first-seen order (normalised, stop-words removed)."""
    seen: dict[str, None] = {}
    for tok in _TOKEN.findall(normalize(text)):
        if tok not in STOPWORDS and len(tok) >= 2:
            seen.setdefault(tok)
    return list(seen)
