"""The ONE guard for live capture tools that act in the owner's signed-in browser window.

Allowed in a signed-in window, for any non-trading read: navigate, click, scroll, select, download.
There is no other restriction. The guard refuses exactly this:

* a control whose accessible name, text, aria-label, value, title or href matches the trading
  pattern (trade, transfer, withdraw, deposit, order, buy, sell, submit, confirm payment) or an
  account-changing one (delete, remove, cancel, unsubscribe, close account, save/apply changes,
  change/reset password, update profile/email/settings, link/unlink/connect/disconnect broker). The
  match is on tokens after normalization (camelCase and punctuation split, case folded), so a
  word that merely contains ``order`` as a suffix (``border``) is not a match;
* a submit control, an image submit or a password control. A ``<button>`` with no ``type`` inside
  a form IS a submit (its effective type is ``submit``);
* a link that leaves the tool's own origin;
* any typing: there is no key, insert-text or text-entry message in the accepted set, so
  credentials and 2FA codes cannot be entered.

Every action is logged value-free (verb, role, first 8 hex characters of the SHA-256 of the label,
never the label itself).

For raw CDP drivers this module also builds and validates the few messages they may send
(``Runtime.evaluate`` of a fixed read-only probe, ``Input.dispatchMouseEvent`` with a left click,
move or wheel, ``Page.navigate``). Anything else is refused by :func:`check_driving_message`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
import hashlib
import json
import re
from typing import Any

TRADING_WORDS = (
    "trade",
    "transfer",
    "withdraw",
    "deposit",
    "order",
    "buy",
    "sell",
    "submit",
    "confirm payment",
)
# Account-changing controls (the owner's own limit: never change account settings). Same guard,
# same refusal path. Single words match as whole-token prefixes; phrases match consecutive tokens.
MUTATION_WORDS = (
    "delete",
    "remove",
    "cancel",
    "unsubscribe",
    "close account",
    "save changes",
    "apply changes",
    "change password",
    "reset password",
    "update profile",
    "update email",
    "update settings",
    "update password",
    "link broker",
    "unlink broker",
    "connect broker",
    "disconnect broker",
)
_MUTATION_STEMS = ("delet", "remov", "cancel", "unsubscrib")
_MUTATION_PHRASES = (
    ("close", "account"),
    ("save", "changes"),
    ("apply", "changes"),
    ("change", "password"),
    ("reset", "password"),
    ("update", "profile"),
    ("update", "email"),
    ("update", "settings"),
    ("update", "password"),
)
_BROKER_VERBS = ("link", "unlink", "connect", "disconnect")
# Short words match as whole-token prefixes (trades, trading, orders, buyer, sells); the long,
# unambiguous ones also match glued into a longer token (placetransfer, xsubmitx).
_PREFIX_STEMS = ("trade", "trading", "order", "buy", "sell")
_CONTAINED_STEMS = ("transfer", "withdraw", "deposit", "submit")
_BLOCKED_TYPES = frozenset({"submit", "image", "password"})
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")

WHEEL_MAX_ABS_DELTA = 5000
COORD_MAX = 20000


class ActionRefused(ValueError):
    """The action targets a trading control, a submit/password control or leaves the origin."""


class ControlNotFound(ActionRefused):
    """No control with that exact name is on the page, so nothing was clicked."""


def label_hash(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()[:8]


def tokens(text: str) -> list[str]:
    """Lower-case word tokens: camelCase boundaries, digits and punctuation split."""

    return [t for t in _NON_ALNUM.sub(" ", _CAMEL.sub(" ", text).lower()).split() if t]


def matches_trading(text: object) -> bool:
    if not isinstance(text, str):
        return False
    words = tokens(text)
    for index, word in enumerate(words):
        if word.startswith(_PREFIX_STEMS) or any(s in word for s in _CONTAINED_STEMS):
            return True
        if word == "confirm" and words[index + 1 : index + 2] == ["payment"]:
            return True
        if word == "confirmpayment":
            return True
        if word.startswith(_MUTATION_STEMS):
            return True
        # a phrase may have filler words inside it ("Close the account", "Change your password")
        window = words[index + 1 : index + 4]
        if any(
            word == first and second in window for first, second in _MUTATION_PHRASES
        ):
            return True
        if word in _BROKER_VERBS and "broker" in window:
            return True
    return False


def refuse_if_trading(*texts: object) -> None:
    for text in texts:
        if matches_trading(text):
            raise ActionRefused(
                "refused: the control matches the trading guard "
                f"(label {label_hash(str(text))}); only non-trading reads are allowed"
            )


def effective_type(facts: Mapping[str, Any]) -> str:
    """``submit`` for a typeless ``<button>`` in a form; else the element's own ``type``."""

    kind = facts.get("type")
    kind = kind.casefold() if isinstance(kind, str) else ""
    if facts.get("tag") == "button" and kind in ("", "submit") and facts.get("in_form"):
        return "submit"
    return kind


def check_control(facts: Mapping[str, Any], *, origin_prefix: str) -> None:
    """Refuse the control described by ``facts`` (see :data:`FACTS_FIELDS`) or return."""

    name = str(facts.get("text") or facts.get("aria_label") or facts.get("name") or "")
    require_origin(facts, origin_prefix=origin_prefix)
    kind = effective_type(facts)
    if kind in _BLOCKED_TYPES:
        raise ActionRefused(
            f"refused: a {kind} control (label {label_hash(name)}) is never touched"
        )
    href = facts.get("href")
    if isinstance(href, str) and href and not href.startswith(origin_prefix):
        raise ActionRefused(
            f"refused: a link leaving {origin_prefix} (label {label_hash(name)})"
        )
    refuse_if_trading(
        facts.get("text"),
        facts.get("aria_label"),
        facts.get("name"),
        facts.get("value"),
        facts.get("title"),
        facts.get("labelled_by"),
        facts.get("labels"),
        facts.get("alt"),
        facts.get("onclick"),
        facts.get("href_attr"),
        href,
    )


def require_origin(facts: Mapping[str, Any] | None, *, origin_prefix: str) -> None:
    """Refuse unless the probed document is on the tool's origin (checked before every input).

    A probe that carries no ``page_url`` is not checked here; the live probes always carry it.
    """

    if facts is None:
        raise ActionRefused("refused: the page origin could not be verified")
    page_url = facts.get("page_url")
    if page_url is not None and not (
        isinstance(page_url, str) and page_url.startswith(origin_prefix)
    ):
        raise ActionRefused(f"refused: the page left {origin_prefix}")


def check_url(url: str, *, origin_prefix: str) -> None:
    if not url.startswith(origin_prefix):
        raise ActionRefused(f"refused: only {origin_prefix} pages are opened")
    refuse_if_trading(url[len(origin_prefix) :])


class ActionLog:
    """Value-free action log."""

    def __init__(self) -> None:
        self.entries: list[dict[str, str]] = []

    def record(self, verb: str, role: str, label: str) -> None:
        self.entries.append(
            {"action": verb, "role": role, "label_sha8": label_hash(label)}
        )


# ---------------------------------------------------------------------------
# Raw CDP: fixed read-only probes and the accepted message set

FACTS_FIELDS = (
    "tag",
    "type",
    "in_form",
    "text",
    "aria_label",
    "name",
    "value",
    "title",
    "labelled_by",
    "labels",
    "alt",
    "onclick",
    "page_url",
    "href",
    "href_attr",
)
# Reads facts about one element and its nearest interactive ancestor; changes nothing.
_FACTS_JS = r"""(target) => {
  const el = target && target.closest
    ? (target.closest('a,button,input,select,summary,[role=button],[role=link],[role=tab]') || target)
    : null;
  if (!el) { return {page_url: location.href, tag: null}; }
  const labelled = (el.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean)
    .map((id) => { const n = document.getElementById(id); return n ? n.textContent : ''; })
    .join(' ').trim().slice(0, 200);
  const labels = el.labels ? Array.from(el.labels).map((l) => l.textContent).join(' ').trim().slice(0, 200) : '';
  const text = (el.innerText || el.textContent || '').trim().slice(0, 200);
  const r = el.getBoundingClientRect();
  return {
    tag: el.tagName.toLowerCase(),
    type: el.getAttribute('type'),
    in_form: !!el.closest('form'),
    text: text,
    aria_label: el.getAttribute('aria-label') || '',
    name: el.getAttribute('name') || '',
    value: el.getAttribute('value') || '',
    title: el.getAttribute('title') || '',
    labelled_by: labelled,
    labels: labels,
    alt: el.getAttribute('alt') || '',
    onclick: (el.getAttribute('onclick') || '').slice(0, 200),
    page_url: location.href,
    href: el.href || '',
    href_attr: el.getAttribute('href') || '',
    x: Math.round(r.left + r.width / 2),
    y: Math.round(r.top + r.height / 2),
    w: Math.round(r.width),
    h: Math.round(r.height)
  };
}"""
#: The same probe as a Playwright ``locator.evaluate`` function (takes the element).
FACTS_SCRIPT = _FACTS_JS
_BY_NAME_JS = (
    "(name) => { const hit = Array.from(document.querySelectorAll("
    "'a,button,input,select,summary,[role=button],[role=link],[role=tab]'))"
    ".find((e) => ((e.getAttribute('aria-label') || e.innerText || e.value || e.title || '')"
    ".trim() === name)); if (!hit) { return null; } "
    "hit.scrollIntoView({block: 'center'}); return (" + _FACTS_JS + ")(hit); }"
)
_AT_POINT_JS = "(x, y) => (" + _FACTS_JS + ")(document.elementFromPoint(x, y))"
_BY_NAME_PREFIX = "(" + _BY_NAME_JS + ")("
_AT_POINT_PREFIX = "(" + _AT_POINT_JS + ")("


def probe_by_name(name: str) -> dict[str, Any]:
    """``Runtime.evaluate`` params that read the facts of the control with this exact name."""

    return {
        "expression": _BY_NAME_PREFIX + json.dumps(name) + ")",
        "returnByValue": True,
    }


def probe_at_point(x: int, y: int) -> dict[str, Any]:
    return {
        "expression": _AT_POINT_PREFIX + f"{int(x)}, {int(y)})",
        "returnByValue": True,
    }


def _check_evaluate(params: object) -> None:
    if not isinstance(params, dict) or set(params) != {"expression", "returnByValue"}:
        raise ActionRefused("refused: only the fixed element probes may be evaluated")
    expression = params["expression"]
    if params["returnByValue"] is not True or not isinstance(expression, str):
        raise ActionRefused("refused: only the fixed element probes may be evaluated")
    if expression.startswith(_BY_NAME_PREFIX) and expression.endswith(")"):
        try:
            if isinstance(json.loads(expression[len(_BY_NAME_PREFIX) : -1]), str):
                return
        except ValueError:
            pass
    if expression.startswith(_AT_POINT_PREFIX) and re.fullmatch(
        r"-?\d{1,5}, -?\d{1,5}\)", expression[len(_AT_POINT_PREFIX) :]
    ):
        return
    raise ActionRefused("refused: only the fixed element probes may be evaluated")


def _check_input(params: object) -> None:
    allowed = {"type", "x", "y", "deltaX", "deltaY", "button", "buttons", "clickCount"}
    if not isinstance(params, dict) or not set(params) <= allowed:
        raise ActionRefused("refused: input event shape")
    kind = params.get("type")
    if kind not in ("mouseWheel", "mouseMoved", "mousePressed", "mouseReleased"):
        raise ActionRefused(
            "refused: only mouse move, wheel and left click; no key or touch"
        )
    for axis in ("x", "y"):
        value = params.get(axis)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ActionRefused("refused: input coordinates")
        if not 0 <= value <= COORD_MAX:
            raise ActionRefused("refused: input coordinates")
    if params.get("button", "left") not in ("left", "none"):
        raise ActionRefused("refused: only the left button")
    if params.get("buttons", 0) not in (0, 1):
        raise ActionRefused("refused: only the left button")
    for delta in ("deltaX", "deltaY"):
        value = params.get(delta, 0)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ActionRefused("refused: wheel delta")
        if abs(value) > WHEEL_MAX_ABS_DELTA:
            raise ActionRefused("refused: wheel delta")


def check_driving_message(
    message: Mapping[str, Any], *, navigate_ok: Callable[[str], None]
) -> bool:
    """Validate a driving message; True if it is one (and valid), False if it is not one.

    ``navigate_ok`` raises :class:`ActionRefused` for a URL the tool may not open.
    """

    method = message.get("method")
    params = message.get("params")
    if method == "Runtime.evaluate":
        _check_evaluate(params)
    elif method == "Input.dispatchMouseEvent":
        _check_input(params)
    elif method == "Page.navigate":
        if not isinstance(params, dict) or set(params) != {"url"}:
            raise ActionRefused("refused: navigate carries exactly one url")
        url = params["url"]
        if not isinstance(url, str):
            raise ActionRefused("refused: navigate carries exactly one url")
        navigate_ok(url)
    else:
        return False
    return True


def click_target(facts: Mapping[str, Any], *, origin_prefix: str) -> tuple[int, int]:
    """Guard the control and return its centre; refuse a control with no visible area."""

    check_control(facts, origin_prefix=origin_prefix)
    x, y, w, h = (facts.get(k) for k in ("x", "y", "w", "h"))
    if not all(isinstance(v, int) and not isinstance(v, bool) for v in (x, y, w, h)):
        raise ActionRefused("refused: the control has no usable position")
    if w <= 0 or h <= 0 or x < 0 or y < 0:  # type: ignore[operator]
        raise ActionRefused("refused: the control is not visible")
    return x, y  # type: ignore[return-value]


def wheel_params(delta_y: int, x: int = 300, y: int = 300) -> dict[str, Any]:
    return {"type": "mouseWheel", "x": x, "y": y, "deltaX": 0, "deltaY": delta_y}


def click_by_name(
    name: str,
    *,
    evaluate: Callable[[dict[str, Any]], dict[str, Any] | None],
    dispatch: Callable[[dict[str, Any]], object],
    origin_prefix: str,
    log: ActionLog,
) -> None:
    """Click the control with this exact name unless the guard refuses it.

    ``evaluate`` sends a ``Runtime.evaluate`` params dict and returns the probe's facts (or
    None); ``dispatch`` sends one ``Input.dispatchMouseEvent`` params dict. The guard runs on
    the named control AND on the element that would actually receive the click.
    """

    found = evaluate(probe_by_name(name))
    if found is None:
        raise ControlNotFound("refused: no control with that name is on the page")
    x, y = click_target(found, origin_prefix=origin_prefix)
    under = evaluate(probe_at_point(x, y))
    if under is None:
        raise ControlNotFound("refused: nothing is at the control's position")
    check_control(under, origin_prefix=origin_prefix)
    log.record("click", str(found.get("tag") or "control"), name)
    base = {"x": x, "y": y, "button": "left"}
    dispatch({**base, "type": "mouseMoved"})
    dispatch({**base, "type": "mousePressed", "buttons": 1, "clickCount": 1})
    dispatch({**base, "type": "mouseReleased", "buttons": 0, "clickCount": 1})
