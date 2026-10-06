"""Conservative structural analysis of the panel shell (``app.js`` / ``index.html``).

The code checks must find a defect *class*, not one spelling of it, so they work on
the shape of the code: which binding holds the payload global, which functions format
dates, which escape HTML, which callout renders the stress payload, and which literals
are rendered as a status. This is not a JavaScript parser; it is a lexer good enough to
separate code from strings, template text, comments and regular expressions, plus
brace matching. Every heuristic errs towards reporting (the runner fails closed).
"""

from __future__ import annotations

from dataclasses import dataclass
import re

_REGEX_PRECEDERS = frozenset("(,=:[!&|?{};+-*%<>~^")
_REGEX_KEYWORDS = frozenset(
    {"return", "typeof", "case", "in", "of", "new", "delete", "void", "throw", "else"}
)
_WORD = re.compile(r"[A-Za-z_$][\w$]*")


def mask(js: str) -> str:
    """Return ``js`` with strings, template text, comments and regexes blanked.

    Lengths and newlines are preserved, so offsets map 1:1 to the source. Template
    ``${ ... }`` interpolations stay as code; string/template delimiters are kept.
    """

    out = list(js)
    length = len(js)

    def blank(start: int, end: int) -> None:
        for index in range(max(0, start), min(end, length)):
            if out[index] != "\n":
                out[index] = " "

    interpolation_depths: list[int] = []
    depth = 0
    in_template = False
    last_sig = ""
    last_word = ""
    index = 0
    while index < length:
        char = js[index]
        if in_template:
            if char == "\\":
                blank(index, index + 2)
                index += 2
                continue
            if char == "`":
                in_template = False
                last_sig, last_word = ")", ""
                index += 1
                continue
            if js.startswith("${", index):
                interpolation_depths.append(depth)
                depth += 1
                in_template = False
                last_sig, last_word = "{", ""
                index += 2
                continue
            blank(index, index + 1)
            index += 1
            continue
        if js.startswith("//", index):
            end = js.find("\n", index)
            end = length if end < 0 else end
            blank(index, end)
            index = end
            continue
        if js.startswith("/*", index):
            end = js.find("*/", index + 2)
            end = length if end < 0 else end + 2
            blank(index, end)
            index = end
            continue
        if char in "'\"":
            end = index + 1
            while end < length and js[end] != char and js[end] != "\n":
                end += 2 if js[end] == "\\" else 1
            blank(index + 1, end)
            index = end + 1
            last_sig, last_word = ")", ""
            continue
        if char == "`":
            in_template = True
            index += 1
            continue
        if char == "/" and (
            last_sig == ""
            or last_sig in _REGEX_PRECEDERS
            or last_word in _REGEX_KEYWORDS
        ):
            end = index + 1
            in_class = False
            while end < length and js[end] != "\n":
                if js[end] == "\\":
                    end += 2
                    continue
                if js[end] == "[":
                    in_class = True
                elif js[end] == "]":
                    in_class = False
                elif js[end] == "/" and not in_class:
                    break
                end += 1
            end += 1
            while end < length and js[end].isalpha():
                end += 1
            blank(index, end)
            index = end
            last_sig, last_word = ")", ""
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            if interpolation_depths and depth - 1 == interpolation_depths[-1]:
                interpolation_depths.pop()
                depth -= 1
                in_template = True
                index += 1
                continue
            depth -= 1
        if char.isspace():
            index += 1
            continue
        word = _WORD.match(js, index)
        if word is not None:
            last_word = word.group(0)
            last_sig = last_word[-1]
            index = word.end()
            continue
        last_sig, last_word = char, ""
        index += 1
    return "".join(out)


_PAIRS: dict[str, dict[int, int]] = {}


def brace_pairs(masked: str) -> dict[int, int]:
    """Map every ``{`` offset to its matching ``}`` in one pass (memoised)."""

    cached = _PAIRS.get(masked)
    if cached is not None:
        return cached
    pairs: dict[int, int] = {}
    stack: list[int] = []
    for index, char in enumerate(masked):
        if char == "{":
            stack.append(index)
        elif char == "}" and stack:
            pairs[stack.pop()] = index
    if len(_PAIRS) > 8:
        _PAIRS.clear()
    _PAIRS[masked] = pairs
    return pairs


def matching_brace(masked: str, open_index: int) -> int:
    return brace_pairs(masked).get(open_index, len(masked) - 1)


def expression_end(masked: str, start: int) -> int:
    depth = 0
    for index in range(start, len(masked)):
        char = masked[index]
        if char in "([{":
            depth += 1
        elif char in ")]}":
            if depth == 0:
                return index
            depth -= 1
        elif char in ";," and depth == 0:
            return index
    return len(masked)


@dataclass(frozen=True)
class JsFunction:
    name: str | None
    params: tuple[str, ...]
    start: int
    body_start: int
    body_end: int
    iife: bool


def _params(raw: str) -> tuple[str, ...]:
    names = []
    for part in raw.split(","):
        word = _WORD.match(part.strip())
        if word is not None:
            names.append(word.group(0))
    return tuple(names)


_DECLARATION = re.compile(r"\bfunction\b\s*([A-Za-z_$][\w$]*)?\s*\(([^()]*)\)\s*\{")
_ARROW = re.compile(
    r"(?:\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*)?(?:\basync\s+)?"
    r"(?:\(([^()]*)\)|([A-Za-z_$][\w$]*))\s*=>"
)
_NAMED_FUNCTION_EXPRESSION = re.compile(
    r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s+)?function\b"
)


def functions(js: str, masked: str | None = None) -> list[JsFunction]:
    masked = mask(js) if masked is None else masked
    found: list[JsFunction] = []
    named_expressions = {
        match.end(): match.group(1)
        for match in _NAMED_FUNCTION_EXPRESSION.finditer(masked)
    }
    for match in _DECLARATION.finditer(masked):
        open_index = match.end() - 1
        close = matching_brace(masked, open_index)
        name = match.group(1)
        if name is None:
            prefix_end = match.start() + len("function")
            name = named_expressions.get(prefix_end)
        found.append(
            JsFunction(
                name=name,
                params=_params(match.group(2)),
                start=match.start(),
                body_start=open_index,
                body_end=close,
                iife=_is_iife(masked, match.start(), close),
            )
        )
    for match in _ARROW.finditer(masked):
        after = match.end()
        while after < len(masked) and masked[after].isspace():
            after += 1
        if after < len(masked) and masked[after] == "{":
            body_start, body_end = after, matching_brace(masked, after)
        else:
            body_start, body_end = after, expression_end(masked, after)
        raw = match.group(2) if match.group(2) is not None else (match.group(3) or "")
        found.append(
            JsFunction(
                name=match.group(1),
                params=_params(raw),
                start=match.start(),
                body_start=body_start,
                body_end=body_end,
                iife=_is_iife(masked, match.start(), body_end),
            )
        )
    return sorted(found, key=lambda item: item.start)


def _is_iife(masked: str, start: int, body_end: int) -> bool:
    before = masked[:start].rstrip()
    after = masked[body_end + 1 : body_end + 40]
    return before.endswith("(") and re.match(r"\s*\)\s*\(", after) is not None


def deferred_ranges(items: list[JsFunction]) -> list[tuple[int, int]]:
    """Bodies that run later (every function body except immediately invoked ones)."""

    return [(item.body_start, item.body_end) for item in items if not item.iife]


def inside(position: int, ranges: list[tuple[int, int]]) -> bool:
    return any(start <= position <= end for start, end in ranges)


def line_of(text: str, position: int) -> int:
    return text.count("\n", 0, position) + 1


# ----------------------------------------------------------------- payload guard (F2)

_GUARD_TEMPLATES = (
    r"!\s*{name}\b",
    r"\b{name}\s*(?:===?|!==?)\s*(?:null|undefined)\b",
    r"\b(?:null|undefined)\s*(?:===?|!==?)\s*{name}\b",
    r"\btypeof\s+{name}\b",
    r"\b{name}\s*(?:&&|\?\?|\|\|)",
    r"\bif\s*\(\s*{name}\s*\)",
)


@dataclass(frozen=True)
class EagerDereference:
    binding: str
    line: int
    guard_line: int | None


_PATH_SEGMENT = r"[A-Za-z_$][\w$]*"


def _keyed_lookup_guards(masked: str, position: int, escaped_name: str) -> bool:
    """``table[name?.a?.b] ? name.a.b : fallback``: the true branch reads what it keyed.

    With no payload the key is ``undefined``, the lookup finds nothing in the table (the
    table is a plain object literal or the payload's own optional-chained map) and the
    branch that dereferences the payload is never evaluated. The branch must read
    exactly the path the condition keyed by, immediately after the ternary ``?``.
    """

    chain = re.compile(
        rf"{escaped_name}((?:\s*\.\s*{_PATH_SEGMENT})+)", re.ASCII
    ).match(masked, position)
    if chain is None:
        return False
    read = re.findall(_PATH_SEGMENT, chain.group(1))
    condition = re.search(
        rf"\[\s*{escaped_name}\s*\?\.\s*"
        rf"({_PATH_SEGMENT}(?:\s*\??\.\s*{_PATH_SEGMENT})*)\s*\]\s*\?\s*$",
        masked[:position],
    )
    return (
        condition is not None and re.findall(_PATH_SEGMENT, condition.group(1)) == read
    )


def payload_dereferences_before_guard(
    js: str, payload_globals: set[str]
) -> list[EagerDereference]:
    """Payload bindings dereferenced at load time before any load-time null guard."""

    masked = mask(js)
    deferred = deferred_ranges(functions(js, masked))
    results: list[EagerDereference] = []
    names: set[str] = set()
    for global_name in payload_globals:
        source = rf"window\s*(?:\.\s*{re.escape(global_name)}\b|\[\s*[\"']{re.escape(global_name)}[\"']\s*\])"
        for match in re.finditer(
            rf"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*{source}", js
        ):
            if masked[match.start()] != " ":
                names.add(match.group(1))
        names.add(f"window.{global_name}")
    for name in sorted(names):
        escaped = re.escape(name).replace(r"\.", r"\s*\.\s*")
        dereference = re.compile(rf"(?<![\w$.]){escaped}\s*(?:\.(?!\.)|\[)")
        guards = [
            re.compile(template.format(name=escaped)) for template in _GUARD_TEMPLATES
        ]
        guard_matches = [
            match
            for guard in guards
            for match in guard.finditer(masked)
            if not inside(match.start(), deferred)
            and masked[match.end() : match.end() + 2] != "?."
        ]
        eager_guards = [match.start() for match in guard_matches]
        for match in dereference.finditer(masked):
            # The dot in ``data?.field`` is itself a safe optional access.
            if masked[match.end() : match.end() + 1] == "?":
                continue
            if inside(match.start(), deferred):
                continue
            if _keyed_lookup_guards(masked, match.start(), escaped):
                continue
            if any(guard < match.start() for guard in eager_guards):
                break
            first_guard = min((guard.start() for guard in guard_matches), default=None)
            results.append(
                EagerDereference(
                    binding=name,
                    line=line_of(js, match.start()),
                    guard_line=line_of(js, first_guard)
                    if first_guard is not None
                    else None,
                )
            )
            break
    return results


# ------------------------------------------------------------ date formatters (F7)

_DATE_MARKERS = re.compile(
    r"new\s+Date\s*\(|Date\s*\.\s*parse\s*\(|toLocaleDateString|DateTimeFormat|toISOString"
)
_ESCAPE_MARKERS = re.compile(r"&amp;|&lt;|&gt;|&#39;|&quot;|&#x27;")
_HTML_SINK_LINE = re.compile(
    r"innerHTML|outerHTML|insertAdjacentHTML|document\s*\.\s*write"
)


def escape_helpers(js: str, items: list[JsFunction]) -> set[str]:
    helpers = set()
    for item in items:
        body = js[item.body_start : item.body_end + 1]
        if item.name and ".replace" in body and _ESCAPE_MARKERS.search(body):
            helpers.add(item.name)
    return helpers


def _return_expressions(js: str, masked: str, item: JsFunction) -> list[str]:
    body_masked = masked[item.body_start : item.body_end + 1]
    if not body_masked.lstrip().startswith("{"):
        return [js[item.body_start : item.body_end]]
    expressions = []
    for match in re.finditer(r"\breturn\b", body_masked):
        start = item.body_start + match.end()
        end = expression_end(masked, start)
        expressions.append(js[start:end])
    return expressions


def _branches(expression: str) -> list[str]:
    """Split a top-level ``a ? b : c`` into its value branches (heuristic)."""

    masked = mask(expression)
    depth = 0
    question = -1
    for index, char in enumerate(masked):
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif (
            char == "?"
            and depth == 0
            and masked[index + 1 : index + 2] not in ("?", ".")
        ):
            if index > 0 and masked[index - 1] == "?":
                continue
            question = index
            break
    if question < 0:
        return [expression]
    depth = 0
    for index in range(question + 1, len(masked)):
        char = masked[index]
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == ":" and depth == 0:
            return _branches(expression[question + 1 : index]) + _branches(
                expression[index + 1 :]
            )
    return [expression]


def _is_raw_fallback(branch: str, params: tuple[str, ...], helpers: set[str]) -> bool:
    text = branch.strip()
    if not text or not any(re.search(rf"\b{re.escape(p)}\b", text) for p in params):
        return False
    if any(re.search(rf"\b{re.escape(helper)}\s*\(", text) for helper in helpers):
        return False
    if _DATE_MARKERS.search(text) or re.search(
        r"\.\s*(?:format|toLocale\w*)\s*\(", text
    ):
        return False
    return True


@dataclass(frozen=True)
class UnescapedFormatter:
    name: str
    line: int
    raw_returns: tuple[str, ...]
    sink_lines: tuple[int, ...]


def unescaped_date_formatters(js: str) -> list[UnescapedFormatter]:
    masked = mask(js)
    items = functions(js, masked)
    helpers = escape_helpers(js, items)
    results = []
    for item in items:
        if not item.name or item.name in helpers:
            continue
        body = js[item.body_start : item.body_end + 1]
        if not _DATE_MARKERS.search(body):
            continue
        raw = tuple(
            branch.strip()
            for expression in _return_expressions(js, masked, item)
            for branch in _branches(expression)
            if _is_raw_fallback(branch, item.params, helpers)
        )
        if not raw:
            continue
        sinks = []
        for match in re.finditer(rf"(?<![\w$.]){re.escape(item.name)}\s*\(", masked):
            if item.body_start <= match.start() <= item.body_end:
                continue
            prefix = masked[max(0, match.start() - 40) : match.start()]
            if any(re.search(rf"\b{re.escape(h)}\s*\(\s*$", prefix) for h in helpers):
                continue
            line_start = js.rfind("\n", 0, match.start()) + 1
            line_end = js.find("\n", match.start())
            line = js[line_start : line_end if line_end >= 0 else len(js)]
            if re.search(r"\.\s*(?:textContent|innerText)\s*=", line):
                continue
            in_template = re.search(r"\$\{\s*$", prefix) is not None and "<" in line
            if in_template or _HTML_SINK_LINE.search(line):
                sinks.append(line_of(js, match.start()))
        if sinks:
            results.append(
                UnescapedFormatter(
                    name=item.name,
                    line=line_of(js, item.start),
                    raw_returns=raw,
                    sink_lines=tuple(sorted(set(sinks))),
                )
            )
    return results


# ------------------------------------------------------------ stress callout (F1)

_CALLOUT = re.compile(r"""class\s*=\s*\\?["'][^"'<>]*\bcallout\b""")


@dataclass(frozen=True)
class StressCallout:
    line: int
    unconditional: bool
    literal_thresholds: tuple[str, ...]


def _open_interpolation_is_conditional(js: str, offset: int) -> bool:
    line_start = js.rfind("\n", 0, offset) + 1
    segment = js[line_start:offset]
    opens: list[int] = []
    position = 0
    while position < len(segment):
        if segment.startswith("${", position):
            opens.append(position)
            position += 2
            continue
        character = segment[position]
        if character == "{":
            opens.append(-1)
        elif character == "}" and opens:
            opens.pop()
        position += 1
    return any(
        start >= 0 and ("?" in segment[start:] or "&&" in segment[start:])
        for start in opens
    )


def stress_callouts(js: str) -> list[StressCallout]:
    """Callout markup rendered from the advisory stress payload, and its guard."""

    masked = mask(js)
    names: set[str] = set()
    for _ in range(3):
        for match in re.finditer(
            r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*([^;\n]*)", masked
        ):
            source = js[match.start(2) : match.end(2)]
            if re.search(r"advisory\s*\.\s*\w*stress\w*", source) or any(
                re.search(rf"\b{re.escape(name)}\b", source) for name in names
            ):
                names.add(match.group(1))
    if_blocks: list[tuple[int, int]] = []
    for match in re.finditer(r"\bif\s*\(([^)]*)\)", masked):
        condition = js[match.start(1) : match.end(1)]
        if not (
            re.search(r"advisory", condition)
            or any(re.search(rf"\b{re.escape(name)}\b", condition) for name in names)
        ):
            continue
        after = match.end()
        while after < len(masked) and masked[after].isspace():
            after += 1
        if after < len(masked) and masked[after] == "{":
            if_blocks.append((after, matching_brace(masked, after)))
        else:
            if_blocks.append((after, expression_end(masked, after)))

    def uses_stress(text: str) -> bool:
        return re.search(r"advisory\s*\.\s*\w*stress\w*", text) is not None or any(
            re.search(rf"\b{re.escape(name)}\b", text) for name in names
        )

    bodies = [
        (item.body_start, item.body_end)
        for item in functions(js, masked)
        if not item.iife
    ]
    results = []
    for match in _CALLOUT.finditer(js):
        line_start = js.rfind("\n", 0, match.start()) + 1
        line_end = js.find("\n", match.start())
        line = js[line_start : line_end if line_end >= 0 else len(js)]
        # Linked to the stress payload by data flow: on a line that uses it, or a
        # danger/warning callout inside the function that computes it.
        linked = uses_stress(line)
        if not linked and re.search(
            r"callout-(?:danger|warning)", match.group(0) + line
        ):
            enclosing = [
                (start, end) for start, end in bodies if start <= match.start() <= end
            ]
            if enclosing:
                start, end = min(enclosing, key=lambda span: span[1] - span[0])
                linked = uses_stress(js[start : end + 1])
        if not linked:
            continue
        template_start = js.rfind("`", line_start, match.start())
        prefix = masked[
            line_start : template_start if template_start >= 0 else match.start()
        ]
        conditional = (
            _open_interpolation_is_conditional(js, match.start())
            or re.search(r"\?|&&", prefix) is not None
            or inside(match.start(), if_blocks)
        )
        close = js.find("</", match.end())
        text = js[match.end() : close if close >= 0 else len(js)]
        text = re.sub(r"\$\{[^}]*\}", " ", text)
        thresholds = tuple(re.findall(r"(\d+(?:\.\d+)?)\s*%", text))
        results.append(
            StressCallout(
                line=line_of(js, match.start()),
                unconditional=not conditional,
                literal_thresholds=thresholds,
            )
        )
    return results


# ------------------------------------------------------- hard-coded statuses (D8)

# Positive availability/publication statuses. Upper-case tokens are matched anywhere in
# rendered text (status tokens are upper case by convention); the phrase list is
# matched only when it is the whole rendered value, so prose such as "Account live"
# is not mistaken for a status.
POSITIVE_STATUS_TOKENS = (
    "PUBLISHED",
    "PUBLICATION_READY",
    "VERIFIED",
    "READY",
    "READY_FOR_RELEASE",
    "RELEASE_READY",
    "PRODUCTION_READY",
    "APPROVED",
    "CERTIFIED",
    "RELEASED",
    "AVAILABLE",
    "ACCEPTED",
    "CLEARED",
    "PASSED",
    "GO_LIVE",
)
_STATUS_WORD = re.compile(r"\b(" + "|".join(POSITIVE_STATUS_TOKENS) + r")\b")
_PHRASE = re.compile(
    r"(?i)^\s*(?:status\s*[:=-]\s*)?("
    r"published|verified|ready|ready\s+(?:for|to)\s+(?:release|publish|publication)|"
    r"(?:release|publication|production)[\s_-]+ready|approved|certified|released|"
    r"available|accepted|cleared|passed|live|go[\s_-]+live|signed[\s_-]+off"
    r")\s*[.!]?\s*$"
)
# "NOT READY", "UN-VERIFIED", "NOT PM-ACCEPTED": a negation within the two words
# (or 16 characters) before the status word.
_NEGATED = re.compile(
    r"(?i)(?:\b(?:NOT|NO|NON|UN|PRE)\b|\b(?:UN|NON|PRE)-)[\s\w-]{0,16}$"
)
_READINESS_SOURCE = re.compile(r"(?i)readiness|publication|verification|release_state")


def _positive_words(text: str) -> list[str]:
    """Positive statuses a rendered literal asserts (token anywhere, or whole phrase)."""

    found = [
        match.group(1)
        for match in _STATUS_WORD.finditer(text)
        if not _NEGATED.search(text[: match.start()])
    ]
    phrase = _PHRASE.match(text)
    if phrase is not None:
        found.append(phrase.group(1))
    return found


@dataclass(frozen=True)
class StatusLiteral:
    file: str
    line: int
    context: str
    literal: str


def hard_coded_statuses(file: str, text: str) -> list[StatusLiteral]:
    """Positive statuses rendered from literals rather than from the payload.

    Contexts: HTML text nodes, status-bearing attributes, DOM text sinks, and string
    literals inside template interpolations (arguments, ``||``/``??`` fallbacks,
    ternary branches) unless that interpolation reads the readiness payload.
    """

    results: list[StatusLiteral] = []

    def add(position: int, context: str, literal: str) -> None:
        results.append(StatusLiteral(file, line_of(text, position), context, literal))

    for match in re.finditer(r">([^<>]{1,160})<", text):
        node_text = re.sub(r"\$\{[^}]*\}", " ", match.group(1))
        if not node_text.strip():
            continue
        for word in _positive_words(node_text):
            add(match.start(), "text-node", word)
    for match in re.finditer(
        r"\b(?:data-[\w-]+|aria-label|title|value|alt|class)\s*=\s*\\?[\"']"
        r"([^\"'<>]{1,160})\\?[\"']",
        text,
    ):
        value = match.group(1)
        if "${" in value:
            value = re.sub(r"\$\{[^}]*\}", " ", value)
        if match.group(0).startswith("class"):
            continue
        # Same negation filter as text nodes: title="NOT RELEASED" is not a claim.
        for word in _positive_words(value):
            add(match.start(), "attribute", word)
    for match in re.finditer(
        r"\.\s*(?:textContent|innerText|innerHTML)\s*=\s*[\"'`]([^\"'`]{1,160})[\"'`]",
        text,
    ):
        for word in _positive_words(match.group(1)):
            add(match.start(), "dom-text", word)
    masked = mask(text)
    pairs = brace_pairs(masked)
    for index in range(len(masked) - 1):
        if masked[index] != "$" or masked[index + 1] != "{":
            continue
        close = pairs.get(index + 1)
        if close is None:
            continue
        expression = text[index + 2 : close]
        if _READINESS_SOURCE.search(expression):
            continue
        for literal in re.finditer(r"([\"'])((?:(?!\1)[^\\\n]){1,60})\1", expression):
            before = expression[: literal.start()].rstrip()
            after = expression[literal.end() :].lstrip()
            # A literal compared against (x === "AVAILABLE") is not rendered.
            operators = ("===", "!==", "==", "!=", "case")
            if before.endswith(operators) or after.startswith(operators[:4]):
                continue
            if _PHRASE.match(literal.group(2)) or _STATUS_WORD.fullmatch(
                literal.group(2)
            ):
                add(index, "template-expression-literal", literal.group(2))
    unique = {(item.line, item.context, item.literal): item for item in results}
    return [unique[key] for key in sorted(unique)]
