"""What the mutation tool's ``run`` and ``ticks`` commands share, kept to the stdlib alone.

That is the classification of a survivor (``classify``, ``message_sink``), the report's schema,
and the bounded reading of a report someone else produced (``read_report``).
"""

from __future__ import annotations

import ast
import io
import json
import keyword
import re
import tokenize
import tomllib
from pathlib import Path

SCHEMA = "bakobo.mutation-report/1"
# The most a report may hold before it is refused unread: 16 MiB. A report is one record
# per surviving mutant, around a kilobyte each, so this allows thousands of survivors while
# keeping a corrupt or hostile artifact from being loaded whole.
MAX_REPORT_BYTES = 16 * 1024 * 1024

BEHAVIOURAL = "behavioural"
STRING_ONLY = "string-only"
# Tokens whose text is a string literal's content.
STRING_TOKENS = {tokenize.STRING, tokenize.FSTRING_MIDDLE, tokenize.TSTRING_MIDDLE}
# Tokens that carry no meaning a mutation could change.
LAYOUT_TOKENS = {tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT,
                 tokenize.ENDMARKER, tokenize.COMMENT}
# A literal next to one of these is compared, tested for membership, used as a key or used as
# an index, so changing its text can change what the code does.
DECIDING = {"==", "!=", "<", ">", "<=", ">=", "in", "is", "[", "]", ":"}
# A string token found inside an f-string's {...}: an argument or a format spec, never text.
CODE_LITERAL = -1
FSTRING_STARTS = {tokenize.FSTRING_START, tokenize.TSTRING_START}
FSTRING_ENDS = {tokenize.FSTRING_END, tokenize.TSTRING_END}
# The one tokenize failure that still yields every token: a fragment of a call whose closing
# bracket is on a line the diff did not show.
OPEN_BRACKET = "unexpected EOF in multi-line statement"


# A repo may declare more message sinks than the built-in ones, in pyproject.toml:
#     [tool.mutation]
#     message_sinks = ["Refusal.missing", "Note"]
# ``Name`` makes every string argument of a call spelled exactly ``Name(...)`` message text;
# ``Name.kwarg`` makes only that keyword argument so. A survivor whose literal reached one is
# recorded with the sink ``declared <entry>``.
# pyproject.toml is read at most this far, 1 MiB, and refused unread beyond it.
MAX_PYPROJECT_BYTES = 1024 * 1024
DECLARED = "declared "
DOTTED = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*")


def sink_entry(entry: object) -> bool:
    """Whether ``entry`` is Name or Name.kwarg: one or two identifiers, neither a keyword."""
    if not isinstance(entry, str):
        return False
    parts = entry.split(".")
    return len(parts) <= 2 and all(
        part.isidentifier() and not keyword.iskeyword(part) for part in parts)


class SinkConfigError(Exception):
    """[tool.mutation] message_sinks is not a list of Name or Name.kwarg entries."""


def message_sinks(pyproject: dict) -> tuple[str, ...]:
    """The message sinks a parsed pyproject.toml declares, or SinkConfigError saying why not."""
    tool = pyproject.get("tool", {})
    if not isinstance(tool, dict):
        raise SinkConfigError("[tool] is not a table")
    table = tool.get("mutation", {})
    if not isinstance(table, dict):
        raise SinkConfigError("[tool.mutation] is not a table")
    sinks = table.get("message_sinks", [])
    if not isinstance(sinks, list):
        raise SinkConfigError(f"[tool.mutation] message_sinks is not a list: {sinks!r}")
    for entry in sinks:
        if not sink_entry(entry):
            raise SinkConfigError(
                f"[tool.mutation] message_sinks holds {entry!r}, which is neither Name nor "
                "Name.kwarg")
    return tuple(sinks)


def known_sink(sink: object) -> bool:
    """Whether ``sink`` is a form message_sink records: null, built-in, or ``declared <entry>``.

    A report is someone else's output, so a sink field it carries is checked against these
    forms before it can vouch for anything.
    """
    if sink is None:
        return True
    if not isinstance(sink, str):
        return False
    if sink in ("print", "warnings.warn"):
        return True
    if sink.startswith("raise "):
        return bool(DOTTED.fullmatch(sink.removeprefix("raise ")))
    if sink.startswith(DECLARED):
        return sink_entry(sink.removeprefix(DECLARED))
    owner, dot, method = sink.partition(".")
    return bool(dot) and owner in LOG_OWNERS and method in LOG_METHODS


def read_pyproject(path: Path) -> dict:
    """A pyproject.toml, parsed, read no further than MAX_PYPROJECT_BYTES.

    Raises SinkConfigError for a file over the limit, and lets OSError and ValueError (not
    UTF-8, not TOML) through for the caller to code.
    """
    with open(path, "rb") as handle:
        raw = handle.read(MAX_PYPROJECT_BYTES + 1)
    if len(raw) > MAX_PYPROJECT_BYTES:
        raise SinkConfigError(f"{path.name} is larger than {MAX_PYPROJECT_BYTES} bytes")
    try:
        return tomllib.loads(raw.decode("utf-8"))
    except RecursionError:  # small enough, but nested deeper than the parser's stack
        raise SinkConfigError(f"{path.name} is nested too deeply to parse") from None


class UnreadableReportError(Exception):
    """A report could not be read as JSON within the size bound; the message says why."""


def read_report(path: Path) -> object:
    """The parsed report, bounded to MAX_REPORT_BYTES before any of it is decoded."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_REPORT_BYTES + 1)
    except OSError as error:
        problem = f"the report could not be read ({error.strerror})"
        raise UnreadableReportError(problem) from None
    if len(raw) > MAX_REPORT_BYTES:
        raise UnreadableReportError(f"the report is larger than {MAX_REPORT_BYTES} bytes")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise UnreadableReportError("the report is not UTF-8") from None
    try:
        return json.loads(text)
    except ValueError:
        raise UnreadableReportError("the report is not JSON") from None
    except RecursionError:  # small enough, but nested deeper than the parser's stack
        raise UnreadableReportError("the report is nested too deeply to parse") from None


def _tokens(lines: list[str]) -> list[tuple[int, str]] | None:
    """The meaningful tokens of a diff fragment, or None if tokenize could not read it all.

    A string token inside an f-string's replacement field, ``{...}`` and its format spec
    included, is code rather than text, so it is given the type CODE_LITERAL, which classify
    never treats as text.
    """
    text = "\n".join(line.strip() for line in lines)
    found = []
    depths: list[int] = []  # brace depth inside each f-string still open
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            kind = tok.type
            if kind in FSTRING_STARTS:
                depths.append(0)
            elif kind in FSTRING_ENDS and depths:
                depths.pop()
            elif kind == tokenize.OP and depths and tok.string in "{}":
                depths[-1] += 1 if tok.string == "{" else -1
            if kind in STRING_TOKENS and any(depth > 0 for depth in depths):
                kind = CODE_LITERAL
            if kind not in LAYOUT_TOKENS:
                found.append((kind, tok.string))
    except tokenize.TokenError as error:
        if error.args[0] != OPEN_BRACKET:
            return None
    return found


def classify(removed: list[str], added: list[str], sink: str | None) -> str:
    """``string-only`` if the mutation changes only the text inside message literals.

    ``sink`` is where ``message_sink`` found the mutated lines' literals going; without one, a
    changed literal is a returned value, an argument, a key or a pattern, and behavioural.

    Both sides must tokenize completely to the same sequence, differing only in string-literal
    tokens, none of which sits beside a comparison, a membership test, a subscript or a key.
    Anything else, including a change too fragmentary to read, is ``behavioural``: a survivor
    wrongly called string-only is a test gap nobody hears about, while one wrongly called
    behavioural costs a tick someone closes.
    """
    before, after = _tokens(removed), _tokens(added)
    if sink is None or not before or not after or len(before) != len(after):
        return BEHAVIOURAL
    changed = [i for i, (old, new) in enumerate(zip(before, after, strict=True)) if old != new]
    if not changed:
        return BEHAVIOURAL
    for i in changed:
        (old_type, old), (new_type, new) = before[i], after[i]
        if old_type != new_type or old_type not in STRING_TOKENS:
            return BEHAVIOURAL
        if old_type == tokenize.STRING and not _same_kind_of_text(old, new):
            return BEHAVIOURAL
        neighbours = before[max(i - 1, 0):i] + before[i + 1:i + 2]
        if any(text in DECIDING for _, text in neighbours):
            return BEHAVIOURAL
    return STRING_ONLY


def _same_kind_of_text(old: str, new: str) -> bool:
    """Both literals evaluate to text of one type, and neither or both are empty.

    Emptiness is behaviour: a validator that requires a non-empty ground refuses "" (utina's
    Refusal does), so a mutation into or out of the empty string is never just text.
    """
    try:
        before, after = ast.literal_eval(old), ast.literal_eval(new)
    except (ValueError, SyntaxError):
        return False
    return type(before) is type(after) and bool(before) == bool(after)


LOG_OWNERS = {"logging", "log", "logger", "_log", "_logger", "LOG", "LOGGER"}
LOG_METHODS = {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}


def _sink_of_call(call: ast.Call, parent: ast.AST | None, kwarg: str | None,
                  sinks: tuple[str, ...]) -> str | None:
    """What kind of message a call delivers, or None if it is not a message sink.

    ``kwarg`` is the keyword the literal was passed as, None for a positional argument, and
    ``sinks`` the repo's declared ones, which are matched after the built-in ones.
    """
    func = call.func
    if isinstance(parent, ast.Raise) and parent.exc is call:
        callee = ast.unparse(func)
        return f"raise {callee}" if DOTTED.fullmatch(callee) else None
    if isinstance(func, ast.Name) and func.id == "print":
        return "print"
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        owner, method = func.value.id, func.attr
        if (owner == "warnings" and method == "warn") or (
                owner in LOG_OWNERS and method in LOG_METHODS):
            return f"{owner}.{method}"
    callee = ast.unparse(func)
    for sink in sinks:
        name, _, keyword = sink.partition(".")
        if callee == name and (not keyword or keyword == kwarg):
            return DECLARED + sink
    return None


def _text_of(node: ast.AST) -> str:
    """The literal text of a string constant or of an f-string's literal parts."""
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value for v in node.values if isinstance(v, ast.Constant))
    return node.value  # type: ignore[attr-defined]


def _sink_of_literal(node: ast.AST, parents: dict, sinks: tuple[str, ...]) -> str | None:
    """Follow a literal's value up through concatenation and formatting to a call argument.

    A literal that becomes a format string, the left of ``%`` or the receiver of ``.format``,
    is not message text if it carries that formatting's placeholders: changing them can make
    the formatting itself fail.
    """
    text = _text_of(node)
    kwarg = None
    while True:
        parent = parents.get(node)
        if isinstance(parent, ast.BinOp) and isinstance(parent.op, (ast.Add, ast.Mod)):
            if isinstance(parent.op, ast.Mod) and parent.left is node and "%" in text:
                return None
            node = parent
        elif isinstance(parent, ast.Attribute) and parent.attr == "format":
            if "{" in text or "}" in text:
                return None
            call = parents.get(parent)
            if not (isinstance(call, ast.Call) and call.func is parent):
                return None
            node = call
        elif isinstance(parent, ast.keyword):
            kwarg = parent.arg
            node = parent
        elif isinstance(parent, ast.Call) and node is not parent.func:
            return _sink_of_call(parent, parents.get(parent), kwarg, sinks)
        else:
            return None


def message_sink(source: str, first: int, last: int,
                 sinks: tuple[str, ...] = ()) -> str | None:
    """Where the string literals on lines ``first``..``last`` end up, if all are messages.

    A literal is message text when its value, after any ``+``, ``%`` or ``.format``, is passed
    straight to an exception constructor in a ``raise``, to ``print``, to a logging method, to
    ``warnings.warn``, or to one of ``sinks``, the repo's declared ones (see ``message_sinks``).
    Returns that sink when every literal touching the lines is one, else None
    — a line holding no literal, or one literal that is anything else, is not vouched for.
    """
    tree = ast.parse(source)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}

    def inside_fstring(node: ast.AST) -> bool:
        while (node := parents.get(node)) is not None:
            if isinstance(node, ast.JoinedStr):
                return True
        return False

    literals = [n for n in ast.walk(tree)
                if (isinstance(n, ast.JoinedStr)
                    or (isinstance(n, ast.Constant) and isinstance(n.value, str)))
                and n.lineno <= last and (n.end_lineno or n.lineno) >= first
                and not inside_fstring(n)]
    found = [_sink_of_literal(n, parents, sinks) for n in literals]
    if not found or None in found or len(set(found)) > 1:
        return None  # a literal that is no message, or two sinks, neither vouching for both
    return found[0]
