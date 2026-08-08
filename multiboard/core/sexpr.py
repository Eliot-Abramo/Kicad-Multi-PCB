"""
Tolerant s-expression scanner for KiCad files.

Why this exists
---------------
The component index needs to know what is on every board. Doing that with
``pcbnew.LoadBoard`` costs 1-3 s per board because it also builds the
connectivity engine and the design-rule state, and it can only run on KiCad's
GUI thread. Scanning the file as text costs ~0.2 s for a 10 MB board, runs on a
worker thread, needs no pcbnew at all, and works on boards a given KiCad
version could not load.

The scanner is deliberately *not* a full parser. ``iter_spans`` makes one linear
pass tracking paren depth and string state, yielding byte offsets for the
top-level nodes you asked for; ``parse_span`` then builds a small tuple tree for
just that span. Footprints are a few hundred bytes each, so we never materialise
a tree for the whole file.

Tolerance requirements, all covered by tests: UTF-8 BOM, CRLF, bare atoms,
quoted strings containing parens or newlines, escaped quotes and backslashes,
non-ASCII reference designators, and truncated files (which report rather than
raise, so a half-written board still yields what it has).
"""

from collections.abc import Iterator
from typing import Optional, Union

Atom = str
Node = tuple[str, list[Union["Node", Atom]]]
"""A parsed node: ``(tag, children)`` where a child is a Node or a bare string."""


class SexprError(ValueError):
    """Raised only for input that cannot be interpreted at all."""


def strip_preamble(text: str) -> str:
    """Remove a UTF-8 BOM. CRLF is handled inline by the scanner."""
    return text[1:] if text.startswith("﻿") else text


def iter_spans(text: str, tag: str, depth: int = 1) -> Iterator[tuple[int, int]]:
    """
    Yield ``(start, end)`` offsets of every ``(tag ...)`` node at ``depth``.

    ``start`` indexes the opening paren, ``end`` is one past the closing paren,
    so ``text[start:end]`` is the complete node. Depth 0 is the document node
    itself, depth 1 its direct children.

    A truncated final node is not yielded; use :func:`scan_health` if you need to
    know that happened.
    """
    want = tag
    n = len(text)
    i = 0
    level = -1  # -1 == outside the document node
    # Stack of (level, start_offset) for nodes we are inside and may want.
    pending: list[tuple[int, int, bool]] = []

    while i < n:
        c = text[i]

        if c == '"':
            i = _skip_string(text, i)
            continue

        if c == ";" and (i == 0 or text[i - 1] in "\n\r"):
            # KiCad does not emit comments, but hand-edited files may have them.
            while i < n and text[i] not in "\n\r":
                i += 1
            continue

        if c == "(":
            level += 1
            start = i
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            k = j
            while k < n and text[k] not in ' \t\r\n()"':
                k += 1
            name = text[j:k]
            pending.append((level, start, level == depth and name == want))
            i = k
            continue

        if c == ")":
            if pending:
                _plevel, pstart, wanted = pending.pop()
                if wanted:
                    yield (pstart, i + 1)
            level -= 1
            i += 1
            continue

        i += 1


def scan_health(text: str) -> tuple[bool, int]:
    """
    Return ``(balanced, final_depth)`` for a document.

    ``final_depth != 0`` means the file is truncated (positive) or has stray
    closing parens (negative). The block-footprint generator in v12 produced
    ``-2`` on every file it wrote, which is why every generated block footprint
    was unparseable; this function is what the Doctor check uses to detect the
    damage.
    """
    n = len(text)
    i = 0
    depth = 0
    while i < n:
        c = text[i]
        if c == '"':
            i = _skip_string(text, i)
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        i += 1
    return depth == 0, depth


def _skip_string(text: str, i: int) -> int:
    """Given ``text[i] == '"'``, return the index just past the closing quote."""
    n = len(text)
    i += 1
    while i < n:
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c == '"':
            return i + 1
        i += 1
    return n  # unterminated; treat the rest of the file as string


def parse_span(text: str, start: int, end: int) -> Node:
    """Build a tuple tree for ``text[start:end]``, which must be one node."""
    node, _pos = _parse_node(text, start, end)
    if node is None:
        raise SexprError(f"no node at offset {start}")
    return node


def parse(text: str) -> Node:
    """Parse a whole document (the outermost node). Use sparingly on big files."""
    text = strip_preamble(text)
    i = 0
    while i < len(text) and text[i] in " \t\r\n":
        i += 1
    return parse_span(text, i, len(text))


def _parse_node(text: str, i: int, end: int) -> tuple[Optional[Node], int]:
    while i < end and text[i] in " \t\r\n":
        i += 1
    if i >= end or text[i] != "(":
        return None, i
    i += 1

    tag, i = _read_atom(text, i, end)
    children: list[Union[Node, Atom]] = []

    while i < end:
        while i < end and text[i] in " \t\r\n":
            i += 1
        if i >= end:
            break
        c = text[i]
        if c == ")":
            return (tag, children), i + 1
        if c == "(":
            child, i = _parse_node(text, i, end)
            if child is None:
                break
            children.append(child)
            continue
        atom, i = _read_atom(text, i, end)
        children.append(atom)

    # Truncated: return what we have rather than raising.
    return (tag, children), i


def _read_atom(text: str, i: int, end: int) -> tuple[str, int]:
    while i < end and text[i] in " \t\r\n":
        i += 1
    if i >= end:
        return "", i
    if text[i] == '"':
        j = _skip_string(text, i)
        return unquote(text[i:j]), j
    j = i
    while j < end and text[j] not in ' \t\r\n()"':
        j += 1
    return text[i:j], j


# =============================================================================
# Node access
# =============================================================================


def find(node: Node, tag: str) -> Optional[Node]:
    """First direct child node with ``tag``, or None."""
    for child in node[1]:
        if isinstance(child, tuple) and child[0] == tag:
            return child
    return None


def find_all(node: Node, tag: str) -> list[Node]:
    """All direct child nodes with ``tag``."""
    return [c for c in node[1] if isinstance(c, tuple) and c[0] == tag]


def atoms(node: Optional[Node]) -> list[str]:
    """Bare-string children of ``node``, in order. ``None`` yields ``[]``."""
    if node is None:
        return []
    return [c for c in node[1] if isinstance(c, str)]


def atom(node: Optional[Node], index: int = 0, default: str = "") -> str:
    """The ``index``-th bare-string child, or ``default``."""
    vals = atoms(node)
    return vals[index] if index < len(vals) else default


def last_atom(node: Optional[Node], default: str = "") -> str:
    """
    The final bare-string child, or ``default``.

    This is how net names must be read. Board file format 20251028 stopped
    serialising netcodes, so a pad's net is ``(net 3 "GND")`` in older files and
    may be ``(net "GND")`` in KiCad 10 files. Taking the last atom is correct for
    both; taking index 1 silently empties the net index on new files.
    """
    vals = atoms(node)
    return vals[-1] if vals else default


def number(node: Optional[Node], index: int = 0, default: float = 0.0) -> float:
    """The ``index``-th bare-string child as a float, or ``default``."""
    try:
        return float(atom(node, index))
    except (ValueError, IndexError):
        return default


def has_flag(node: Node, flag: str) -> bool:
    """True if ``flag`` appears as a bare atom of ``node`` (e.g. ``(attr dnp)``)."""
    return flag in atoms(node)


# =============================================================================
# Emission
# =============================================================================

_NEEDS_QUOTE = set(' \t\r\n()"\\;')


def quote(s: str) -> str:
    """
    Quote a string for emission into an s-expression.

    v12 interpolated board and port names directly into generated ``.kicad_mod``
    text, so a name containing a quote or backslash produced a broken file. Every
    write path now goes through here.
    """
    if s and not any(c in _NEEDS_QUOTE for c in s):
        return s
    escaped = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def unquote(s: str) -> str:
    """Inverse of :func:`quote` for a single token."""
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        body = s[1:-1]
        out = []
        i = 0
        while i < len(body):
            if body[i] == "\\" and i + 1 < len(body):
                out.append(body[i + 1])
                i += 2
            else:
                out.append(body[i])
                i += 1
        return "".join(out)
    return s
