#!/usr/bin/env bash
# Serve the KCRO GPU explorer (its backend lives on the v0.4.0-provenance branch)
# pointed at the FROZEN v0.3.0 ABox (ontology/kcro-abox.ttl, 537,947 triples) — the
# thesis snapshot — so screenshots are of v0.3.0, NOT the v0.4.0 provenance graph.
#
# Run from anywhere:   bash figures/serve_kg_v030.sh
# Then open:           http://localhost:8000/full2d
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
EXP="$ROOT/.kg_explorer_v030"          # ephemeral, git-ignored
BRANCH="v0.4.0-provenance"

mkdir -p "$EXP/src" "$EXP/web" "$EXP/ontology" "$EXP/results"

# 1. explorer code from the v0.4.0 branch (the only place the explorer exists)
git -C "$ROOT" show "$BRANCH:src/kcro_server.py"          > "$EXP/src/kcro_server.py"
git -C "$ROOT" show "$BRANCH:web/full_2d_visualizer.html" > "$EXP/web/full_2d_visualizer.html"
git -C "$ROOT" show "$BRANCH:web/full_3d_visualizer.html" > "$EXP/web/full_3d_visualizer.html"
git -C "$ROOT" show "$BRANCH:web/hero_visualizer.html"    > "$EXP/web/hero_visualizer.html"

# 2. recolour the 2D view to a white (acmart) theme for thesis figures
"$ROOT/.venv/bin/python" - "$EXP/web/full_2d_visualizer.html" <<'PY'
import sys
p = sys.argv[1]; s = open(p, encoding="utf-8").read()
for a, b in [
    ("#0d1117", "#ffffff"), ("#c9d1d9", "#1a2230"),
    ("rgba(13,17,23,0.92)", "rgba(255,255,255,0.94)"),
    ("#8b949e", "#566069"), ("#21262d", "#eef1f5"), ("#30363d", "#cbd5e0"),
    ("#3d5a80", "#9aa3b0"),
    ("#58a6ff", "#2b6cb0"), ("#d2a8ff", "#6b46c1"), ("#ff7b72", "#c53030"),
    ("0: [0.345, 0.651, 1.0, 1.0]", "0: [0.169, 0.424, 0.690, 1.0]"),  # Object  -> #2b6cb0
    ("1: [0.824, 0.659, 1.0, 1.0]", "1: [0.420, 0.275, 0.757, 1.0]"),  # Relator -> #6b46c1
    ("2: [1.0, 0.482, 0.447, 1.0]", "2: [0.773, 0.188, 0.188, 1.0]"),  # Vuln    -> #c53030
]:
    s = s.replace(a, b)
open(p, "w", encoding="utf-8").write(s)
print("patched 2D view -> white theme")
PY

# 3. the v0.3.0 data (TBox + the 537,947-triple ABox) from the current checkout
cp "$ROOT/ontology/kcro-abox.ttl" "$EXP/ontology/kcro-abox.ttl"
cp "$ROOT/ontology/kcro.ttl"      "$EXP/ontology/kcro.ttl"

echo "Explorer ROOT = $EXP"
echo "Loading the v0.3.0 ABox (537,947 triples) — ~15-40 s — then serving on :8000"
echo "Verify it's v0.3.0:  curl -s localhost:8000/full | python3 -c 'import sys,json;d=json.load(sys.stdin);print(\"nodes\",d[\"count\"],\"repos\",len(d[\"repos\"]))'"
exec "$ROOT/.venv/bin/python" "$EXP/src/kcro_server.py"
