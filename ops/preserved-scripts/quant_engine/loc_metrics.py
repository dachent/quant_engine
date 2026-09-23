import pathlib, collections

r = pathlib.Path(r"C:\Users\BorisVaisman\V Capital\investing - Documents\_quant_engine\repo")

def loc(p):
    try:
        with open(p, encoding="utf-8", errors="ignore") as f:
            return sum(1 for _ in f)
    except Exception:
        return 0

print("== per-dir (excl __pycache__) ==")
tot_py_files, tot_py_loc = 0, 0
for d in ["tools", "tests", "packages", "apps", "skills", "contracts", "schemas", "docs", "provenance", "generated", "config.example.toml"]:
    p = r / d
    if not p.exists():
        print(d, "MISSING"); continue
    files = [x for x in p.rglob("*") if x.is_file() and "__pycache__" not in x.parts] if p.is_dir() else [p]
    py = [x for x in files if x.suffix == ".py"]
    md = [x for x in files if x.suffix == ".md"]
    js = [x for x in files if x.suffix == ".json"]
    lloc = sum(loc(x) for x in py)
    mloc = sum(loc(x) for x in md)
    jloc = sum(loc(x) for x in js)
    tot_py_files += len(py); tot_py_loc += lloc
    print(f"{d}: files={len(files)} py={len(py)} pyLOC={lloc} md={len(md)} mdLOC={mloc} json={len(js)} jsonLOC={jloc}")

print(f"TOTAL py: files={tot_py_files} LOC={tot_py_loc}")

print("== top-15 py by LOC ==")
allpy = [x for x in r.rglob("*.py") if x.is_file() and "__pycache__" not in x.parts]
sized = sorted(((loc(x), str(x.relative_to(r))) for x in allpy), reverse=True)[:15]
for n, p in sized:
    print(f"{n} {p}")

print("== tests vs tools+packages ==")
def dloc(d):
    return sum(loc(x) for x in (r/d).rglob("*.py") if x.is_file() and "__pycache__" not in x.parts)
t, to, pa = dloc("tests"), dloc("tools"), dloc("packages")
print(f"tests={t} tools={to} packages={pa} tests/(tools+packages)={t/(to+pa):.2f}")

print("== keyword match-lines in tools+packages (excl tests) ==")
import re
for kw in ["receipt", "authority", "admission", "PROCEED", "guard"]:
    c = 0
    for x in list((r/"tools").rglob("*.py")) + list((r/"packages").rglob("*.py")):
        if "__pycache__" in x.parts: continue
        try:
            t = open(x, encoding="utf-8", errors="ignore").read()
            c += len(re.findall(kw, t))
        except Exception:
            pass
    print(f"{kw} occurrences={c}")
