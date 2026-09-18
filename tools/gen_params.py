"""Generate quantum_hd8/params.json from the installed Universal Control defs.
Keeps verified/hidden flags already set in the existing params.json."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from quantum_hd8.defs import DEFS_XML, load_template  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "quantum_hd8" / "params.json"

old = {p["path"]: p for p in json.loads(OUT.read_text())} if OUT.exists() else {}
rows = []
for p in load_template(sys.argv[1] if len(sys.argv) > 1 else DEFS_XML):
    d = p.to_dict()
    prev = old.get(p.path, {})
    d["verified"] = prev.get("verified", False)
    d["hidden"] = prev.get("hidden", False)
    rows.append(d)
OUT.write_text(json.dumps(rows, indent=1, ensure_ascii=False) + "\n")
print(f"{len(rows)} params -> {OUT}")
