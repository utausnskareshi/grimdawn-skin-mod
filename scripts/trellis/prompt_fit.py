"""Fit a text prompt into CLIP's 77 tokens so that D-013's "no weapon" is not cut off.

TRELLIS-text-xlarge encodes the prompt with openai/clip-vit-large-patch14, truncated to 77 tokens including the
start and end tokens (trellis_text_to_3d.py encode_text: max_length=77, truncation=True). with_no_weapon() adds
", empty hands, no weapon" at the end, so a long prompt (about 300 or more characters of English) silently lost it.

fit_prompt keeps every prompt that fits exactly as before, and only when it does not fit keeps the longest start of
the user's text that fits together with the suffix (never decoding tokens back into text). It cuts at a word
boundary (white space or punctuation) when it can, and inside a run of characters when the boundaries alone would
drop much more of the text: Japanese with only a final 「。」, a first 「、」 that comes too late, or long hyphenated
English. The words are not moved to the front: that would change the conditioning of every prompt.
No torch / transformers here: the caller passes count_tokens (generate_glb.py uses the pipeline's tokenizer).
"""
from __future__ import annotations

import re

CLIP_MAX_TOKENS = 77
NO_WEAPON_SUFFIX = ", empty hands, no weapon"
_NO_WEAPON_RE = re.compile(r"\bno weapons?\b", re.IGNORECASE)
# word boundaries: white space and punctuation, including the Japanese 、。
_BOUNDARY_RE = re.compile(r"[\s,.;:、。，．]+")
_TRAILING_RE = re.compile(r"[\s,.;:、。，．]+$")
# a cut inside a run of characters is used only when it keeps at least this many more tokens than the last word
# boundary that fits (one English word is 1-3 tokens; a long hyphenated compound or Japanese run is much more)
_CHAR_CUT_MIN_GAIN = 8
# token counts can go down by a little when a cut moves inside a word: cuts this far above the one found are tried
_SCAN_ABOVE = 8


def with_no_weapon(prompt: str) -> str:
    """D-013: make sure a text prompt asks for empty hands."""
    if _NO_WEAPON_RE.search(prompt):
        return prompt
    return prompt.rstrip(" .,") + NO_WEAPON_SUFFIX


def _longest_fit(cuts, at, limit: int):
    """cuts: ascending prefix lengths; at(cut) -> (cut, text, tokens) or None (nothing of the user's text left).
    -> the longest candidate that fits in limit, or None.

    Binary search (a longer prefix does not need fewer tokens; exact at word boundaries), then the next few cuts above
    the one found are tried too, because inside a word a longer prefix can sometimes need one token fewer.
    """
    def ok(i):
        r = at(cuts[i])
        return r is None or r[2] <= limit       # an empty prefix is shorter than any text that fits

    lo, hi, best = 0, len(cuts) - 1, -1
    while lo <= hi:
        mid = (lo + hi) // 2
        if ok(mid):
            best, lo = mid, mid + 1
        else:
            hi = mid - 1
    for i in [*range(min(len(cuts) - 1, best + _SCAN_ABOVE), best, -1), *range(best, -1, -1)]:
        r = at(cuts[i])
        if r is not None and r[2] <= limit:
            return r
    return None


def fit_prompt(prompt: str, count_tokens, limit: int = CLIP_MAX_TOKENS, add_suffix: bool = True) -> tuple[str, dict]:
    """-> (text to encode, info). count_tokens(s) is the token count of s including the start and end tokens.

    info: prompt_tokens {"original", "used", and "with_suffix" (the text with the suffix, before shortening) when the
    suffix had to be added}, prompt_truncated (part of the user's text is not read by CLIP), no_weapon_kept (the text
    CLIP reads asks for no weapon: the suffix, or the user's own words inside the limit), token_limit.
    add_suffix=False (--allow-weapons) never changes the text and only reports the truncation.
    Only when not even the first character of the user's text fits with the suffix is the suffix alone returned.
    """
    n_orig = count_tokens(prompt)
    m = _NO_WEAPON_RE.search(prompt)
    own_kept = bool(m) and count_tokens(prompt[:m.end()]) <= limit      # the user's own "no weapon(s)" is read
    info = {"prompt_tokens": {"original": n_orig, "used": n_orig}, "prompt_truncated": n_orig > limit,
            "no_weapon_kept": own_kept, "token_limit": limit}
    if not add_suffix or own_kept:
        return prompt, info
    info["no_weapon_kept"] = True
    full = with_no_weapon(prompt) if not m else prompt.rstrip(" .,") + NO_WEAPON_SUFFIX
    n = count_tokens(full)
    info["prompt_tokens"]["with_suffix"] = n
    if n <= limit:
        info["prompt_tokens"]["used"] = n
        info["prompt_truncated"] = False
        return full, info
    body = prompt.rstrip(" .,")
    memo: dict[int, tuple | None] = {}

    def at(cut: int):
        if cut not in memo:
            part = _TRAILING_RE.sub("", body[:cut])
            memo[cut] = (cut, part + NO_WEAPON_SUFFIX, count_tokens(part + NO_WEAPON_SUFFIX)) if part.strip() else None
        return memo[cut]

    bounds = sorted({b.start() for b in _BOUNDARY_RE.finditer(body) if b.start() > 0})
    word = _longest_fit(bounds, at, limit)
    # inside the run of characters after the boundary used (or from the start) up to the next boundary, which
    # did not fit; no boundary at all: the whole text
    lower = word[0] if word else 0
    upper = next((b for b in bounds if b > lower), len(body))
    char = _longest_fit(range(lower + 1, upper), at, limit)
    if word and (char is None or char[2] - word[2] < _CHAR_CUT_MIN_GAIN):
        _, text, n = word
    elif char:
        _, text, n = char
    else:
        text = NO_WEAPON_SUFFIX.lstrip(", ")
        n = count_tokens(text)
    info["prompt_tokens"]["used"] = n
    info["prompt_truncated"] = True
    return text, info
