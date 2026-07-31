"""Pure, bounded pagination helpers with auditable terminal reasons."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import (
    Any,
    Callable,
    Generic,
    Hashable,
    Iterable,
    Mapping,
    Sequence,
    TypeVar,
    cast,
)

from .util import canonical_json, sha256_text


T = TypeVar("T")
DAY_MS = 86_400_000


class PaginationError(RuntimeError):
    """Raised for an invalid or non-terminating pagination contract."""


@dataclass(frozen=True)
class OffsetPage(Generic[T]):
    items: Sequence[T]


@dataclass(frozen=True)
class CursorPage(Generic[T]):
    items: Sequence[T]
    next_id: str | int | None


@dataclass(frozen=True)
class TimeWindow:
    """Inclusive UTC millisecond bounds."""

    start_ms: int
    end_ms: int

    def __post_init__(self) -> None:
        if self.start_ms > self.end_ms:
            raise ValueError("window start_ms must not exceed end_ms")

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms + 1


@dataclass(frozen=True)
class PageAudit:
    cursor: str
    raw_count: int
    unique_added: int
    next_cursor: str | None = None
    repeated: bool = False
    split: bool = False


@dataclass
class PaginationResult(Generic[T]):
    items: list[T] = field(default_factory=list)
    pages: list[PageAudit] = field(default_factory=list)
    terminal_reason: str = ""
    duplicate_count: int = 0
    complete: bool = True


KeyFunction = Callable[[T], Hashable]


def _key_function(key: KeyFunction[T] | str | Sequence[str] | None) -> KeyFunction[T]:
    if callable(key):
        return key
    if isinstance(key, str):
        return lambda item: _mapping_value(item, key)
    if key is not None:
        names = tuple(key)
        return lambda item: tuple(_mapping_value(item, name) for name in names)
    return lambda item: sha256_text(canonical_json(item))


def _mapping_value(item: Any, name: str) -> Hashable:
    if not isinstance(item, Mapping):
        raise PaginationError(f"stable key {name!r} requires mapping items")
    value = item.get(name)
    try:
        hash(value)
    except TypeError:
        return canonical_json(value)
    return cast(Hashable, value)


def stable_deduplicate(
    items: Iterable[T], key: KeyFunction[T] | str | Sequence[str] | None = None
) -> tuple[list[T], int]:
    """Keep the first item for every stable key while retaining source order."""
    key_fn = _key_function(key)
    seen: set[Hashable] = set()
    output: list[T] = []
    duplicates = 0
    for item in items:
        item_key = key_fn(item)
        if item_key in seen:
            duplicates += 1
            continue
        seen.add(item_key)
        output.append(item)
    return output, duplicates


def _page_signature(items: Sequence[T], key_fn: KeyFunction[T]) -> str:
    return sha256_text(canonical_json([key_fn(item) for item in items]))


def paginate_offset(
    fetch_page: Callable[[int, int], Sequence[T] | OffsetPage[T]],
    *,
    page_size: int,
    start_offset: int = 0,
    key: KeyFunction[T] | str | Sequence[str] | None = None,
    max_pages: int = 100_000,
) -> PaginationResult[T]:
    """Walk offset pages and stop on empty, short, or repeated content."""
    if page_size <= 0 or start_offset < 0 or max_pages <= 0:
        raise ValueError(
            "page_size/max_pages must be positive and start_offset non-negative"
        )
    key_fn = _key_function(key)
    seen_keys: set[Hashable] = set()
    seen_pages: set[str] = set()
    result: PaginationResult[T] = PaginationResult()
    offset = start_offset

    for _ in range(max_pages):
        fetched = fetch_page(offset, page_size)
        items = list(fetched.items if isinstance(fetched, OffsetPage) else fetched)
        signature = _page_signature(items, key_fn)
        repeated = bool(items) and signature in seen_pages
        unique_added = 0
        for item in items:
            item_key = key_fn(item)
            if item_key in seen_keys:
                result.duplicate_count += 1
                continue
            seen_keys.add(item_key)
            result.items.append(item)
            unique_added += 1
        result.pages.append(
            PageAudit(
                cursor=str(offset),
                raw_count=len(items),
                unique_added=unique_added,
                repeated=repeated,
            )
        )
        if not items:
            result.terminal_reason = "empty_page"
            return result
        if repeated:
            result.terminal_reason = "repeated_page"
            result.complete = False
            return result
        seen_pages.add(signature)
        if len(items) < page_size:
            result.terminal_reason = "short_page"
            return result
        offset += page_size

    result.terminal_reason = "max_pages"
    result.complete = False
    return result


def paginate_next_id(
    fetch_page: Callable[
        [str | int | None, int], CursorPage[T] | tuple[Sequence[T], str | int | None]
    ],
    *,
    page_size: int,
    start_id: str | int | None = None,
    key: KeyFunction[T] | str | Sequence[str] | None = None,
    max_pages: int = 100_000,
) -> PaginationResult[T]:
    """Walk a ``next_id/start_id`` cursor and detect repeated cursors/pages."""
    if page_size <= 0 or max_pages <= 0:
        raise ValueError("page_size and max_pages must be positive")
    key_fn = _key_function(key)
    seen_keys: set[Hashable] = set()
    seen_pages: set[str] = set()
    seen_cursors: set[str] = set()
    result: PaginationResult[T] = PaginationResult()
    cursor: str | int | None = start_id

    for _ in range(max_pages):
        cursor_marker = "<START>" if cursor is None else str(cursor)
        if cursor_marker in seen_cursors:
            result.terminal_reason = "repeated_cursor"
            result.complete = False
            return result
        seen_cursors.add(cursor_marker)
        fetched = fetch_page(cursor, page_size)
        if isinstance(fetched, CursorPage):
            items, next_id = list(fetched.items), fetched.next_id
        else:
            items, next_id = list(fetched[0]), fetched[1]
        signature = _page_signature(items, key_fn)
        repeated_page = bool(items) and signature in seen_pages
        unique_added = 0
        for item in items:
            item_key = key_fn(item)
            if item_key in seen_keys:
                result.duplicate_count += 1
                continue
            seen_keys.add(item_key)
            result.items.append(item)
            unique_added += 1
        next_marker = None if next_id in (None, "") else str(next_id)
        repeated_cursor = next_marker is not None and next_marker in seen_cursors
        result.pages.append(
            PageAudit(
                cursor=cursor_marker,
                raw_count=len(items),
                unique_added=unique_added,
                next_cursor=next_marker,
                repeated=repeated_page or repeated_cursor,
            )
        )
        if not items and next_marker is None:
            result.terminal_reason = "empty_page"
            return result
        if repeated_cursor:
            result.terminal_reason = "repeated_cursor"
            result.complete = False
            return result
        if repeated_page:
            result.terminal_reason = "repeated_page"
            result.complete = False
            return result
        seen_pages.add(signature)
        if next_marker is None:
            result.terminal_reason = "no_next_id"
            return result
        cursor = next_id

    result.terminal_reason = "max_pages"
    result.complete = False
    return result


def utc_time_windows(
    start_ms: int,
    end_ms: int,
    *,
    window_days: int,
    overlap_days: int = 0,
) -> list[TimeWindow]:
    """Partition inclusive UTC bounds into fixed windows with explicit overlap."""
    if start_ms > end_ms:
        return []
    if window_days <= 0 or overlap_days < 0 or overlap_days >= window_days:
        raise ValueError("require window_days > overlap_days >= 0")
    span_ms = window_days * DAY_MS
    overlap_ms = overlap_days * DAY_MS
    windows: list[TimeWindow] = []
    cursor = int(start_ms)
    while cursor <= end_ms:
        window_end = min(int(end_ms), cursor + span_ms - 1)
        windows.append(TimeWindow(cursor, window_end))
        if window_end == end_ms:
            break
        cursor = window_end + 1 - overlap_ms
    return windows


def bisect_window(window: TimeWindow) -> tuple[TimeWindow, TimeWindow] | None:
    """Split an inclusive window without gaps or overlaps."""
    if window.start_ms == window.end_ms:
        return None
    midpoint = window.start_ms + (window.end_ms - window.start_ms) // 2
    return TimeWindow(window.start_ms, midpoint), TimeWindow(
        midpoint + 1, window.end_ms
    )


def paginate_rfq_bisection(
    fetch_window: Callable[[int, int, int], Sequence[T]],
    *,
    start_ms: int,
    end_ms: int,
    cap: int = 100,
    key: KeyFunction[T] | str | Sequence[str] | None = None,
    min_window_ms: int = 1,
    max_windows: int = 100_000,
) -> PaginationResult[T]:
    """Recursively bisect any RFQ window whose response reaches ``cap``.

    Rows from capped parent probes are audit evidence only; leaf rows form the
    returned dataset so the same result is not counted twice.
    """
    if cap <= 0 or min_window_ms <= 0 or max_windows <= 0:
        raise ValueError("cap, min_window_ms and max_windows must be positive")
    key_fn = _key_function(key)
    pending = [TimeWindow(int(start_ms), int(end_ms))]
    leaf_items: list[T] = []
    pages: list[PageAudit] = []
    capped_leaf = False
    processed = 0

    while pending:
        if processed >= max_windows:
            unique, duplicate_count = stable_deduplicate(leaf_items, key_fn)
            return PaginationResult(
                items=unique,
                pages=pages,
                terminal_reason="max_windows",
                duplicate_count=duplicate_count,
                complete=False,
            )
        window = pending.pop()
        processed += 1
        items = list(fetch_window(window.start_ms, window.end_ms, cap))
        should_split = len(items) >= cap and window.duration_ms > min_window_ms
        pages.append(
            PageAudit(
                cursor=f"{window.start_ms}:{window.end_ms}",
                raw_count=len(items),
                unique_added=0 if should_split else len(items),
                split=should_split,
            )
        )
        if should_split:
            halves = bisect_window(window)
            if halves is None:  # defensive; duration check makes this unreachable
                capped_leaf = True
                leaf_items.extend(items)
                continue
            left, right = halves
            # Stack right first so source chronology remains ascending.
            pending.append(right)
            pending.append(left)
            continue
        leaf_items.extend(items)
        if len(items) >= cap:
            capped_leaf = True

    unique, duplicate_count = stable_deduplicate(leaf_items, key_fn)
    for index, audit in enumerate(pages):
        if not audit.split:
            pages[index] = PageAudit(
                cursor=audit.cursor,
                raw_count=audit.raw_count,
                unique_added=audit.raw_count,
                split=False,
            )
    return PaginationResult(
        items=unique,
        pages=pages,
        terminal_reason="cap_at_min_window" if capped_leaf else "complete_bisected",
        duplicate_count=duplicate_count,
        complete=not capped_leaf,
    )
