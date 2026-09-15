"""Move the authority pin only when the contract it names actually differs.

The pin records a commit whose contract is the accepted authority. Repointing
it at HEAD unconditionally cannot settle: committing the pin advances HEAD, so
the next pass sees the pin naming the previous commit and moves it again, and
the loop chases its own tail.

What the verifier requires is weaker and sufficient -- that the pinned commit's
contract bytes equal the current ones. When they already do, the pin is correct
wherever it points and must be left alone.
"""
from pathlib import Path

path = Path(r'C:/Dev/scratch/quant-engine/pl12/epoch/move_history_pin.py')
raw = path.read_bytes()

old = b"""    sha = subprocess.run(
        ['git', 'rev-parse', 'HEAD'], cwd=REPO, capture_output=True, text=True
    ).stdout.strip()
    raw = subprocess.run(
        ['git', 'show', '%s:%s' % (sha, CONTRACT_PATH)], cwd=REPO, capture_output=True
    ).stdout
    if not raw:
        raise SystemExit('contract is not committed at HEAD')"""
new = b"""    sha = subprocess.run(
        ['git', 'rev-parse', 'HEAD'], cwd=REPO, capture_output=True, text=True
    ).stdout.strip()
    raw = subprocess.run(
        ['git', 'show', '%s:%s' % (sha, CONTRACT_PATH)], cwd=REPO, capture_output=True
    ).stdout
    if not raw:
        raise SystemExit('contract is not committed at HEAD')

    # The pin is satisfied by any commit carrying these exact contract bytes,
    # so a pin that already names such a commit is left where it is. Moving it
    # to HEAD regardless would make every pass dirty the tool that holds it.
    text = TOOL.read_text(encoding='utf-8')
    current = re.search(
        r'_HISTORY_AUTHORITY_COMMIT: Final = "([0-9a-f]+)"', text
    )
    if current:
        pinned = subprocess.run(
            ['git', 'show', '%s:%s' % (current.group(1), CONTRACT_PATH)],
            cwd=REPO,
            capture_output=True,
        )
        if pinned.returncode == 0 and pinned.stdout == raw:
            print('authority pin already names an identical contract; unchanged')
            return 0"""
assert raw.count(old) == 1, raw.count(old)
path.write_bytes(raw.replace(old, new))
print('ok')
