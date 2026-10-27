"""Where a verified quote sits in a step's text, for highlighting.

The verifier compares folded text (Unicode-normalized, punctuation unified,
case-folded, whitespace collapsed), so a quote's position in the folded text
says nothing directly about where it is in what the reader sees. This folds
the step text one character at a time, keeping a map from every folded
character back to the original, so a match can be mapped back to a span of
the original.

The fold must equal the verifier's ``normalize`` exactly. Folding character
by character can differ from folding the whole string in rare cases
(combining sequences), so the result is checked against ``normalize`` and no
span is returned if they disagree: the page then marks the whole step rather
than the wrong characters.
"""

from __future__ import annotations

import re
import unicodedata

from pinpoint.trace import Step
from pinpoint.verifier import Scope, clean_quote, normalize

# The verifier's punctuation equivalences. Kept in step by the check against
# normalize() in _fold: if they ever diverge, no span is returned.
_EQUIVALENTS = str.maketrans(
    {0x2018: "'", 0x2019: "'", 0x201C: '"', 0x201D: '"', 0x2013: "-", 0x2014: "-", 0x2212: "-"}
)
_ALNUM = re.compile(r"[^\W_]")

Span = tuple[int, int]


def _fold(text: str) -> tuple[str, list[int]] | None:
    """Fold ``text`` like ``normalize``, with each folded character's source index."""
    chars: list[str] = []
    source: list[int] = []
    after_space = True  # drops leading whitespace
    for i, ch in enumerate(text):
        for c in unicodedata.normalize("NFKC", ch).translate(_EQUIVALENTS).casefold():
            if c.isspace():
                if not after_space:
                    chars.append(" ")
                    source.append(i)
                after_space = True
            else:
                chars.append(c)
                source.append(i)
                after_space = False
    if chars and chars[-1] == " ":
        chars.pop()
        source.pop()
    folded = "".join(chars)
    return (folded, source) if folded == normalize(text) else None


def locate(text: str, quote: str) -> Span | None:
    """The span of ``text`` that the verifier would match ``quote`` against.

    Uses the verifier's own rules: the quote is cleaned the same way, and a
    quote starting or ending with a letter or digit must meet a word boundary.
    Returns the first match, as ``(start, end)`` indices into ``text``, or
    ``None`` if the quote is not there (or the text cannot be mapped).
    """
    phrase = clean_quote(quote)
    folded = _fold(text)
    if not phrase or folded is None:
        return None
    flat, source = folded
    left = r"(?<![^\W_])" if _ALNUM.match(phrase[0]) else ""
    right = r"(?![^\W_])" if _ALNUM.match(phrase[-1]) else ""
    match = re.search(left + re.escape(phrase) + right, flat)
    if match is None:
        return None
    return source[match.start()], source[match.end() - 1] + 1


def locate_in_step(step: Step, quote: object, scope: Scope = "step") -> Span | None:
    """Like ``locate``, within ``step.text()``.

    With ``scope="action"`` only the agent's action is searched, as the
    offense rule requires. The action is the start of ``step.text()``, so
    the span indexes ``step.text()`` either way.
    """
    if not isinstance(quote, str):
        return None
    return locate(step.action_text() if scope == "action" else step.text(), quote)
