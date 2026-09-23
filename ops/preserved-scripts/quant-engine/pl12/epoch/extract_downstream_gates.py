"""List the preconditions the post-analytics roles impose, so they can be
checked before another producer runs rather than discovered one per rebuild.

Only a fraction of the raise sites in these tools are preconditions on the
inputs a cycle chooses; most validate malformed documents, which well-formed
artifacts never hit. The ones that matter here talk about layout, mode,
bundles, roots, dates and identity -- exactly the class that has cost this
session its rebuilds -- so the listing is filtered to those and grouped by the
function that raises, which is what makes them checkable.
"""
import ast
from pathlib import Path

REPO = Path(
    r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine'
    r'/.codex-bootstrap/mvp2-execution/repo'
)
FILES = (
    'tools/portfolio_report.py',
    'tools/portfolio_control_panel.py',
    'tools/portfolio_release_builder.py',
)
# Words that mark a precondition on what the cycle supplied, rather than a
# structural check on a document's own well-formedness.
SIGNALS = (
    'lens', 'layout', 'mode', 'hybrid', 'plan', 'bundle', 'root', 'absent',
    'generation', 'pointer', 'as-of', 'as of', 'date', 'perimeter', 'surface',
    'governed', 'receipt', 'authority', 'qa', 'inventory', 'candidate',
)
NOISE = ('is invalid', 'differs:', 'is not a mapping', 'is not a list')


def literal(node):
    """The static text of a raised message, when it has one."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            else:
                parts.append('{...}')
        return ''.join(parts)
    if isinstance(node, ast.BinOp):
        return None
    return None


def main():
    total = 0
    for name in FILES:
        path = REPO / name
        tree = ast.parse(path.read_text(encoding='utf-8'))
        rows = []
        for function in ast.walk(tree):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(function):
                if not isinstance(node, ast.Raise) or node.exc is None:
                    continue
                call = node.exc
                if not isinstance(call, ast.Call) or not call.args:
                    continue
                message = literal(call.args[0])
                if not message:
                    continue
                lowered = message.lower()
                if not any(signal in lowered for signal in SIGNALS):
                    continue
                if any(noise in lowered for noise in NOISE):
                    continue
                rows.append((function.name, node.lineno, message))
        rows.sort(key=lambda row: row[1])
        print('=== %s  (%d precondition messages) ===' % (name, len(rows)))
        seen = set()
        for func, line, message in rows:
            key = message[:70]
            if key in seen:
                continue
            seen.add(key)
            print('  %-5d %-44s %s' % (line, func[:44], message[:96]))
        total += len(seen)
        print()
    print('distinct precondition messages: %d' % total)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
