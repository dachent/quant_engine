"""Stop recomputing a decomposition whose inputs have not moved.

`stage_candidate` calls `build_decomposition` and only then checks whether the
candidate already exists, and the release builder invokes `stage` and `publish`
as separate processes -- so every lens is solved twice per run, and a re-run
solves all of them again. On the hybrid surface that is hours of arithmetic
whose answer is already on disk.

The generation id cannot be known without solving, but the inputs can be. The
spec declares `expected_sha256` and `expected_size_bytes` for every source it
reads and `_verify_source` refuses any drift, so the spec's own bytes determine
the whole input set; the solver's bytes are the only other variable. Together
they key an index from inputs to the generation they produced.

A hit is not trusted on the strength of the index. The stored generation is
re-read and checked against itself -- the manifest's id must equal its
directory, and the result must hash to the artifact the manifest declares --
which is the same check a freshly solved candidate would face. A tampered or
truncated artifact fails and the solve happens.
"""
from pathlib import Path

REPO = Path(
    r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine'
    r'/.codex-bootstrap/mvp2-execution/repo'
)
TARGET = REPO / 'tools' / 'portfolio_decomposition.py'

HELPERS = '''

def _input_key(spec_path: Path) -> str:
    """Identify the inputs a decomposition would read, without reading them.

    The spec pins every source's digest and size and `_verify_source` refuses
    anything that differs, so the spec's bytes stand for the whole input set.
    The solver's own bytes are included because the same inputs under different
    code are a different answer.
    """

    digest = hashlib.sha256()
    digest.update(spec_path.read_bytes())
    digest.update(Path(__file__).resolve().read_bytes())
    return digest.hexdigest()


def _indexed_generation(output_root: Path, key: str, kind: str) -> Path | None:
    """The generation a previous run produced from these inputs, if it survives.

    Returns the directory only when it still verifies against itself, so a
    partially written or edited artifact re-solves rather than being served.
    """

    pointer = output_root.resolve() / ".decomposition-index" / f"{key}.txt"
    if pointer.is_symlink() or not pointer.is_file():
        return None
    generation_id = pointer.read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"[0-9a-f]{64}", generation_id):
        return None
    candidate = output_root.resolve() / kind / generation_id
    result = candidate / "decomposition.json"
    manifest_path = candidate / "generation.json"
    if (
        candidate.is_symlink()
        or not candidate.is_dir()
        or result.is_symlink()
        or not result.is_file()
        or manifest_path.is_symlink()
        or not manifest_path.is_file()
    ):
        return None
    try:
        manifest = GenerationManifest.from_json_bytes(manifest_path.read_bytes())
    except ValueError:
        return None
    if manifest.generation_id != generation_id:
        return None
    content = result.read_bytes()
    artifacts = {item.artifact_id: item for item in manifest.artifacts}
    declared = artifacts.get("decomposition.json")
    if (
        declared is None
        or declared.sha256 != hashlib.sha256(content).hexdigest()
        or declared.size_bytes != len(content)
    ):
        return None
    return candidate


def _record_input_key(output_root: Path, key: str, generation_id: str) -> None:
    index = output_root.resolve() / ".decomposition-index"
    index.mkdir(parents=True, exist_ok=True)
    pointer = index / f"{key}.txt"
    if pointer.is_symlink():
        raise DecompositionError("decomposition index entry is linked")
    pointer.write_text(generation_id, encoding="utf-8")
'''


def main():
    text = TARGET.read_text(encoding='utf-8')
    if '_input_key' in text:
        print('already indexed')
        return 0
    if '\r\n' in text:
        raise SystemExit('refusing to edit a file that already carries CRLF')

    anchor = '\n\ndef stage_candidate(\n'
    if text.count(anchor) != 1:
        raise SystemExit('helper anchor is not unique')
    text = text.replace(anchor, HELPERS + anchor, 1)

    old = '''    result, manifest, result_bytes = build_decomposition(spec_path, source_root)
    candidates = output_root.resolve() / "candidates"
'''
    new = '''    key = _input_key(spec_path)
    cached = _indexed_generation(output_root, key, "candidates")
    if cached is not None:
        payload = json.loads((cached / "decomposition.json").read_text("utf-8"))
        return {
            "analytics_hash": payload["analytics_hash"],
            "candidate": str(cached),
            "generation_id": cached.name,
            "model_count": len(cast(list[object], payload["model_results"])),
            "reused": True,
        }
    result, manifest, result_bytes = build_decomposition(spec_path, source_root)
    candidates = output_root.resolve() / "candidates"
'''
    if text.count(old) != 1:
        raise SystemExit('stage anchor is not unique (%d)' % text.count(old))
    text = text.replace(old, new, 1)

    old_return = '''    return {
        "analytics_hash": result["analytics_hash"],
        "candidate": str(candidate),
        "generation_id": manifest.generation_id,
        "model_count": len(cast(list[object], result["model_results"])),
    }
'''
    new_return = '''    _record_input_key(output_root, key, manifest.generation_id)
    return {
        "analytics_hash": result["analytics_hash"],
        "candidate": str(candidate),
        "generation_id": manifest.generation_id,
        "model_count": len(cast(list[object], result["model_results"])),
        "reused": False,
    }
'''
    if text.count(old_return) != 1:
        raise SystemExit('stage return anchor is not unique')
    text = text.replace(old_return, new_return, 1)

    TARGET.write_bytes(text.encode('utf-8'))
    print('staging now consults an input index before solving')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
