"""What the mutation tool's ``run`` and ``ticks`` commands share, kept to the stdlib alone.

That is the classification of a survivor (``classify``, ``message_sink``), the report's schema,
and the bounded reading of a report someone else produced (``read_report``).
"""

from __future__ import annotations

import ast
import copy
import io
import json
import keyword
import re
import tokenize
import tomllib
from pathlib import Path

SCHEMA = "bakobo.mutation-report/2"
# The most a report may hold before it is refused unread: 16 MiB. A report is one record
# per surviving mutant, around a kilobyte each, so this allows thousands of survivors while
# keeping a corrupt or hostile artifact from being loaded whole.
MAX_REPORT_BYTES = 16 * 1024 * 1024

BEHAVIOURAL = "behavioural"
STRING_ONLY = "string-only"
DIAGNOSTIC = "diagnostic"
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
#     message_sinks = ["Refusal.missing", "Note", "console.out.write()"]
# ``Name`` makes every string argument of a call spelled exactly ``Name(...)`` message text;
# ``Name.kwarg`` makes only that keyword argument so; and ``a.b.c()``, a dotted callee of any
# length followed by ``()``, makes every string argument of a call spelled exactly
# ``a.b.c(...)`` message text. A survivor whose literal reached one is recorded with the sink
# ``declared <entry>``.
# pyproject.toml is read at most this far, 1 MiB, and refused unread beyond it.
MAX_PYPROJECT_BYTES = 1024 * 1024
DECLARED = "declared "
DOTTED = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*")


CALLEE = "()"


def _name(part: str) -> bool:
    return part.isidentifier() and not keyword.iskeyword(part)


def sink_entry(entry: object) -> bool:
    """Whether ``entry`` is Name, Name.kwarg, or a dotted callee followed by ``()``.

    Every dotted part must be an identifier and not a keyword.
    """
    if not isinstance(entry, str):
        return False
    if entry.endswith(CALLEE):
        return all(_name(part) for part in entry.removesuffix(CALLEE).split("."))
    parts = entry.split(".")
    return len(parts) <= 2 and all(_name(part) for part in parts)


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
                f"[tool.mutation] message_sinks holds {entry!r}, which is not Name, "
                "Name.kwarg or a dotted.callee()")
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
        if sink.endswith(CALLEE):
            if callee == sink.removesuffix(CALLEE):
                return DECLARED + sink
            continue
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



class _MaskRaiseArguments(ast.NodeTransformer):
    """Blank what a raise's constructed exception is given, and its from-cause.

    The callee stays, and so does whether there is a cause, so a mutation that changes which
    exception is raised, or drops the cause, still shows up as a difference.
    """

    def visit_Raise(self, node: ast.Raise) -> ast.Raise:
        if isinstance(node.exc, ast.Call):
            node.exc.args, node.exc.keywords = [], []
        if node.cause is not None:
            node.cause = ast.Constant(value=...)
        return node


def _masked(tree: ast.AST) -> str:
    return ast.dump(_MaskRaiseArguments().visit(tree))


def _placeholder_in(node: ast.AST, marks: str) -> bool:
    """Whether any str constant under ``node`` holds one of the characters in ``marks``."""
    return any(isinstance(n, ast.Constant) and isinstance(n.value, str)
               and any(mark in n.value for mark in marks) for n in ast.walk(node))


def _is_format_call(node: ast.AST) -> bool:
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "format")


def _text_flows(node: ast.AST, name: str, value: ast.AST, other: ast.AST) -> bool:
    """Whether a str in field ``name`` of ``node`` still reaches the argument as message text.

    It does through ``+``, ``%`` and ``.format``, as ``_sink_of_literal`` follows it, and as an
    f-string's literal part; a format string carrying its placeholders does not, since
    changing them can make the formatting itself fail.
    """
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
        holds = isinstance(node.op, ast.Mod) and name == "left"
        return not (holds and (_placeholder_in(value, "%") or _placeholder_in(other, "%")))
    if _is_format_call(node):
        return name == "func"
    if isinstance(node, ast.Attribute) and node.attr == "format" and name == "value":
        return not (_placeholder_in(value, "{}") or _placeholder_in(other, "{}"))
    return isinstance(node, ast.JoinedStr)


def _only_text(old: object, new: object, text: bool = True) -> bool:
    """Whether two subtrees are the same but for the values of str constants that are text.

    ``text`` says whether a str here reaches the raise's argument as message text (see
    ``_text_flows``); a str anywhere else, a key, a call's argument, a format spec, must not
    change at all.
    """
    if isinstance(old, list) and isinstance(new, list):
        return len(old) == len(new) and all(
            _only_text(a, b, text) for a, b in zip(old, new, strict=True))
    if not (isinstance(old, ast.AST) and isinstance(new, ast.AST)):
        return old == new
    if type(old) is not type(new):
        return False
    if text and isinstance(old, ast.Constant) and isinstance(old.value, str) and isinstance(
            new.value, str):  # type: ignore[attr-defined]  # new is a Constant too
        return True
    for name, value in ast.iter_fields(old):
        other = getattr(new, name)
        inner = text and _text_flows(old, name, value, other)
        if not _only_text(value, other, inner):
            return False
    return True


def _inert_value(old: ast.AST, new: ast.AST) -> bool:
    """A whole argument, keyword value or cause that changed only inertly (see ``diagnostic``)."""
    return (isinstance(new, ast.Constant) and new.value is None) or _only_text(old, new)


def _inert_raise(old: ast.Raise, new: ast.Raise) -> bool:
    """Every difference between two raises that mask equal lies in an inert change."""
    if isinstance(old.exc, ast.Call):
        was, call = old.exc, new.exc
        assert isinstance(call, ast.Call)  # the masks are equal, so the callee is too
        if len(was.args) != len(call.args) or not all(
                _inert_value(a, b) for a, b in zip(was.args, call.args, strict=True)):
            return False
        remaining = iter(was.keywords)
        for kept in call.keywords:
            match = next((k for k in remaining if k.arg == kept.arg), None)
            if match is None or not _inert_value(match.value, kept.value):
                return False
    if old.cause is not None:
        assert new.cause is not None  # the masks are equal, so a cause is still there
        return _inert_value(old.cause, new.cause)
    return True


def _indent(line: str) -> str:
    return line[:len(line) - len(line.lstrip())]


def diagnostic(source: str, line: int, removed: list[str], added: list[str]) -> bool:
    """Whether the mutation changes only what a raise's exception says, not which one escapes.

    The mutation is applied to the source (mutmut's diff of a method is dedented, so the file's
    own indentation is restored), both versions are parsed, and they must differ and become
    identical once every raise's constructor arguments and from-cause are blanked. Anything
    that cannot be checked that way, a fragment that is not the source, a mutant that does not
    parse, a mutation that removes nothing, is not vouched for.

    Lying inside the arguments is not enough: ``ValueError(1 / 0)`` raises ZeroDivisionError
    and ``ValueError(1 * 0)`` raises ValueError. So each raise is paired with the one in the
    same position in the mutant, and every difference between them must be inert: (a) the
    value of a str constant changed that reaches the argument only through ``+``, ``%``,
    ``.format`` or an f-string's literal parts, as ``_sink_of_literal`` follows it, and is not
    the left of ``%`` holding a ``%`` or the receiver of ``.format`` holding a brace, since
    those can make the formatting itself fail (a str that is a key, a call's argument or a
    format spec is behaviour: ``d["k"]`` and ``int("3")`` raise when it changes); (b) a whole argument, keyword value
    or cause became the constant None; or (c) keyword arguments were dropped, the remaining
    ones, matched by name, being inert themselves. (b) and (c) remove evaluation rather than
    change it: if the original argument had itself raised, the raise would never have worked,
    and a test would have noticed. Any other difference, an operator, a number, a name, a call,
    an attribute, a subscript, a changed count of positional arguments, is behavioural.
    """
    lines = source.splitlines()
    end = line - 1 + len(removed)
    if not removed or line < 1 or end > len(lines):
        return False
    original = lines[line - 1:end]
    if [text.strip() for text in original] != [text.strip() for text in removed]:
        return False
    file_ws, diff_ws = _indent(original[0]), _indent(removed[0])
    if not file_ws.endswith(diff_ws):
        return False
    pad = file_ws[:len(file_ws) - len(diff_ws)]
    mutated = lines[:line - 1] + [pad + text for text in added] + lines[end:]
    try:
        before, after = ast.parse(source), ast.parse("\n".join(mutated))
    except (SyntaxError, ValueError, RecursionError):
        return False
    if ast.dump(before) == ast.dump(after):
        return False
    if _masked(copy.deepcopy(before)) != _masked(copy.deepcopy(after)):
        return False
    # The masks are equal, so both hold the same raises in the same places.
    old = [n for n in ast.walk(before) if isinstance(n, ast.Raise)]
    new = [n for n in ast.walk(after) if isinstance(n, ast.Raise)]
    return all(_inert_raise(a, b) for a, b in zip(old, new, strict=True))


def survivor_class(source: str | None, line: int, removed: list[str], added: list[str],
                   sink: str | None) -> str:
    """string-only, else diagnostic, else behavioural; without the source, never diagnostic."""
    if classify(removed, added, sink) == STRING_ONLY:
        return STRING_ONLY
    if source is not None and diagnostic(source, line, removed, added):
        return DIAGNOSTIC
    return BEHAVIOURAL
