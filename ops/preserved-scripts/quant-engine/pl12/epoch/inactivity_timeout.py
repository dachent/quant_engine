"""Bound a node by lack of progress rather than by total duration.

`timeout_seconds` meant total wall clock, and the schema refused any value above
3600. That conflates two different jobs. The real one is liveness: a wedged
child must not block the pipeline forever, which is what the `process.wait`
timeout implements. The 3600 ceiling was doing a second job -- sanity-bounding a
number read out of a hand-editable JSON spec -- and as a duration budget it
cannot tell slow work from stuck work.

It also does not scale with the surface. One evidence mode's lenses fit inside
an hour; the hybrid surface doubled the lens count and stopped fitting, and
raising the ceiling only moves the wall to the next time the surface grows.

So the field keeps its name and every declared value, and only the predicate
changes: it is now the longest a node may go without making progress. Nothing
downstream moves -- no schema key, no spec bytes, no recipe or generation
digest. A hung node still dies on the same budget it always had; work that is
merely long no longer does. `TimeoutExpired` is still what a kill raises, so the
receipt and evidence path is untouched.

Progress is bytes arriving on stdout or stderr. The drain threads already read
in blocks, so a per-block callback is all it takes to observe it.
"""
from pathlib import Path

REPO = Path(
    r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine'
    r'/.codex-bootstrap/mvp2-execution/repo'
)
TARGET = REPO / 'packages/core/src/quant_engine/core/pipeline.py'

EDITS = [
    (
        'import time',
        'import tempfile\nimport threading\n',
        'import tempfile\nimport threading\nimport time\n',
    ),
    (
        'ceiling',
        '''    if (
        not isinstance(timeout_seconds, int)
        or timeout_seconds < 1
        or timeout_seconds > 3600
    ):
        raise PipelineValidationError(f"{node_id}.timeout_seconds is invalid")
''',
        '''    if (
        not isinstance(timeout_seconds, int)
        or isinstance(timeout_seconds, bool)
        or timeout_seconds < 1
    ):
        raise PipelineValidationError(f"{node_id}.timeout_seconds is invalid")
''',
    ),
    (
        'capture',
        '''def _capture_stream(stream: BinaryIO) -> _CapturedStream:
    digest = hashlib.sha256()
    size = 0
    for block in iter(lambda: stream.read(64 * 1024), b""):
        digest.update(block)
        size += len(block)
''',
        '''def _capture_stream(
    stream: BinaryIO, on_progress: Callable[[], None] | None = None
) -> _CapturedStream:
    digest = hashlib.sha256()
    size = 0
    for block in iter(lambda: stream.read(64 * 1024), b""):
        digest.update(block)
        size += len(block)
        if on_progress is not None:
            on_progress()
''',
    ),
    (
        'drain',
        '''    captured: dict[str, _CapturedStream] = {}
    failures: list[BaseException] = []

    def drain(name: str, stream: BinaryIO) -> None:
        try:
            captured[name] = _capture_stream(stream)
        except BaseException as exc:
            failures.append(exc)
''',
        '''    captured: dict[str, _CapturedStream] = {}
    failures: list[BaseException] = []
    last_progress = [time.monotonic()]

    def note_progress() -> None:
        # A bare store, no lock: under the GIL the float write is atomic, and
        # the only consequence of reading a stale value is that a kill waits
        # one more poll interval.
        last_progress[0] = time.monotonic()

    def drain(name: str, stream: BinaryIO) -> None:
        try:
            captured[name] = _capture_stream(stream, note_progress)
        except BaseException as exc:
            failures.append(exc)
''',
    ),
    (
        'poll',
        '''    try:
        return_code = process.wait(timeout=node.timeout_seconds)
    except BaseException:
''',
        '''    try:
        return_code = _wait_for_progress(
            process, node.timeout_seconds, lambda: last_progress[0]
        )
    except BaseException:
''',
    ),
]

HELPER = '''

_PROGRESS_POLL_SECONDS: Final = 5.0


def _wait_for_progress(
    process: subprocess.Popen[bytes],
    idle_budget: int,
    last_progress: Callable[[], float],
) -> int:
    """Wait for the child, killing it only once it has gone quiet for too long.

    The budget is time without progress, not total runtime, so a node that keeps
    producing output runs as long as its work takes. `TimeoutExpired` is still
    what a kill raises, so every caller and every receipt field is unchanged.
    """

    slice_seconds = min(float(idle_budget), _PROGRESS_POLL_SECONDS)
    while True:
        try:
            return process.wait(timeout=slice_seconds)
        except subprocess.TimeoutExpired:
            if time.monotonic() - last_progress() >= idle_budget:
                raise
'''


def main():
    text = TARGET.read_text(encoding='utf-8')
    if '_wait_for_progress' in text:
        print('already applied')
        return 0
    if '\r\n' in text:
        raise SystemExit('refusing to edit a file that already carries CRLF')
    for label, old, new in EDITS:
        if text.count(old) != 1:
            raise SystemExit('%s anchor is not unique (%d)' % (label, text.count(old)))
        text = text.replace(old, new, 1)
        print('applied:', label)
    anchor = '\n\ndef _default_command_runner('
    if text.count(anchor) != 1:
        raise SystemExit('helper anchor is not unique')
    text = text.replace(anchor, HELPER + anchor, 1)
    TARGET.write_bytes(text.encode('utf-8'))
    print('wrote', TARGET.name)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
