# Copyright (c) 2026 Eliot Abramo
# SPDX-License-Identifier: MIT

"""
Tolerant s-expression scanner for KiCad files.

Why this exists
---------------
The component index needs to know what is on every board. Doing that with
``pcbnew.LoadBoard`` costs 1-3 s per board because it also builds the
connectivity engine and the design-rule state. Scanning the file as text needs
no pcbnew at all and works on boards a given KiCad version could not load.

It is not free: about **0.25 s per MB** on a KiCad-10-shaped board, so a very
large one takes a couple of seconds. (An earlier version of this docstring
claimed 0.2 s for a 10 MB board, which was off by a factor of twenty.) Two
things keep that off the user's path: scans are cached on ``(mtime_ns, size)``
so only an edited board is re-read, and ``scan_pcb_file`` reports progress while
it works so the caller can keep its event loop alive.

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

import re
from collections.abc import Iterator
from typing import Optional, Union

# The scanner is the hot path: it runs over every board file on every refresh,
# on the GUI thread. Each of these replaces a per-character Python loop with one
# C-level scan, which is where the bulk of the speed comes from -- the logic is
# unchanged, and tests/test_sexpr.py pins the behaviour either way.
_STRUCTURAL = re.compile(r'["();]')
"""Characters that can change parser state. Everything between them is skipped."""

_PARENS = re.compile(r'["()]')

_OPEN_TAG = re.compile(r'[ \t\r\n]*([^ \t\r\n()"]*)')
"""Whitespace then the tag token, anchored just after an opening paren."""

_STRING = re.compile(r'"(?:[^"\\]|\\.)*(?:"|\Z)', re.DOTALL)
"""A quoted token, escapes included. The ``\\Z`` arm matches an unterminated one,
which :func:`_skip_string` also treats as running to end of file."""

_LINE_END = re.compile(r"[^\n\r]*")

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
    level = -1  # -1 == outside the document node
    start = -1  # offset of the wanted node we are inside, or -1

    # One C-level pass over the file yields every character that can change
    # parser state; a repeated `search(text, pos)` would restart the engine at
    # each one. Regions we must not interpret -- string bodies and comments --
    # are stepped over by ignoring matches below this offset.
    opaque_until = 0

    for match in _STRUCTURAL.finditer(text):
        i = match.start()
        if i < opaque_until:
            continue
        c = text[i]

        if c == '"':
            opaque_until = _skip_string(text, i)
            continue

        if c == ";":
            # KiCad does not emit comments, but hand-edited files may have them.
            if i == 0 or text[i - 1] in "\n\r":
                opaque_until = _LINE_END.match(text, i).end()
            continue

        if c == "(":
            level += 1
            # Read the tag only at the level being searched. Doing it at every
            # opening paren allocates a match object per node in the file, which
            # on a real board is two orders of magnitude more work than this.
            if level == depth and _OPEN_TAG.match(text, i + 1).group(1) == want:
                start = i
            continue

        # ")". Nodes nest properly, so the close that returns us from `depth` to
        # `depth - 1` is this node's own -- which is why no stack is needed.
        if level == depth and start >= 0:
            yield (start, i + 1)
            start = -1
        level -= 1


def scan_health(text: str) -> tuple[bool, int]:
    """
    Return ``(balanced, final_depth)`` for a document.

    ``final_depth != 0`` means the file is truncated (positive) or has stray
    closing parens (negative). The block-footprint generator in v12 produced
    ``-2`` on every file it wrote, which is why every generated block footprint
    was unparseable; this function is what the Doctor check uses to detect the
    damage.

    Counted over the whole text, then corrected for parens inside string
    literals. ``str.count`` runs in C over the file in one pass, where the
    obvious character loop runs in Python over every byte. Only the literals
    themselves are materialised, and KiCad's are short, so peak memory does not
    depend on file size.
    """
    depth = text.count("(") - text.count(")")
    for match in _STRING.finditer(text):
        literal = match.group()
        depth -= literal.count("(") - literal.count(")")
    return depth == 0, depth


def _skip_string(text: str, i: int) -> int:
    """
    Given ``text[i] == '"'``, return the index just past the closing quote.

    An unterminated string runs to end of file, which is the ``\\Z`` arm of
    :data:`_STRING` and matches what a truncated board file should do.
    """
    return _STRING.match(text, i).end()


def parse_span(text: str, start: int, end: int, keep: Optional[frozenset] = None) -> Node:
    """
    Build a tuple tree for ``text[start:end]``, which must be one node.

    ``keep``, when given, is the set of tags worth descending into; a child node
    tagged with anything else is stepped over without being built. Omit it and
    everything is parsed, as before.

    This is the difference between *reading* a board and *parsing* one. A
    footprint's useful fields are a handful of small nodes -- reference, value,
    position, layer, pads and their nets -- while nearly all of its bytes are 3D
    model references, courtyards, silkscreen polygons and font settings.
    Building those allocated tens of thousands of tuples per board, once per
    refresh, and discarded every one. Skipping them measured 1.7x on a
    realistic KiCad 10 board.
    """
    node, _pos = _parse_node(text, start, end, keep)
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


def _parse_node(text: str, i: int, end: int, keep: Optional[frozenset] = None) -> tuple[Optional[Node], int]:
    # Hand-rolled character loops, deliberately. Replacing these with compiled
    # regexes was measurably *slower* (0.8-0.87x): the tokens here are a few
    # characters long, so allocating a match object per token costs more than
    # scanning it. The regex machinery pays off in iter_spans and scan_health,
    # which skip over long runs; it does not pay off here.
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
            if keep is not None and _peek_tag(text, i, end) not in keep:
                i = _skip_node(text, i, end)
                continue
            child, i = _parse_node(text, i, end, keep)
            if child is None:
                break
            children.append(child)
            continue
        atom, i = _read_atom(text, i, end)
        children.append(atom)

    # Truncated: return what we have rather than raising.
    return (tag, children), i


def _peek_tag(text: str, i: int, end: int) -> str:
    """The tag of the node at ``text[i] == '('``, without consuming anything."""
    j = i + 1
    while j < end and text[j] in " \t\r\n":
        j += 1
    k = j
    while k < end and text[k] not in ' \t\r\n()"':
        k += 1
    return text[j:k]


def _skip_node(text: str, i: int, end: int) -> int:
    """Index just past the node at ``text[i] == '('``, building nothing."""
    depth = 0
    search = _PARENS.search
    while i < end:
        match = search(text, i, end)
        if match is None:
            return end
        i = match.start()
        c = text[i]
        if c == '"':
            i = min(_skip_string(text, i), end)
            continue
        if c == "(":
            depth += 1
        else:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return end


def _read_atom(text: str, i: int, end: int) -> tuple[str, int]:
    while i < end and text[i] in " \t\r\n":
        i += 1
    if i >= end:
        return "", i
    if text[i] == '"':
        j = min(_skip_string(text, i), end)
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
        if "\\" not in body:
            return body  # the overwhelmingly common case; skip the escape walk
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
