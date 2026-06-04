"""
compute_layout.py --- precompute a 3D "galaxy" layout for the full KCRO graph.

The KCRO instance graph is a forest of ~tens of thousands of small star-clusters
(each Pod with its vulnerability aspects and relators, relators reaching out to
assets). A global force layout degenerates on a graph this disconnected, so instead
we lay it out as a galaxy:

  * each connected component (one Pod's security neighbourhood) gets a center,
    distributed uniformly through a 3D ball;
  * the component's highest-degree node (the hub, usually the Pod) sits at the
    center, the rest scatter in a small ball around it -> a little star cluster.

This is O(n), runs in ~1 s for 208k nodes, and is deterministic. It reuses the
running server's /full endpoint so it doesn't re-parse the 26 MB turtle and so the
node indices line up exactly with what the visualizer fetches.

Run (with kcro_server.py already serving):
    .venv/bin/python compute_layout.py
Writes:
    full_positions.bin  -- Float32 little-endian, 3 floats (x,y,z) per node
"""

import json
import struct
import sys
import urllib.request

import numpy as np
import scipy.sparse as sp
import scipy.sparse.csgraph as csgraph

SERVER = "http://localhost:8000/full"
OUT = "full_positions.bin"
SEED = 7

# Tunables for the look of the galaxy.
GALAXY_SPREAD = 220.0   # overall radius scale of the cloud of component centers
CLUSTER_SCALE = 2.2     # how far a cluster's members spread from their hub


def fetch_full():
    print(f"Fetching topology from {SERVER} ...", flush=True)
    try:
        with urllib.request.urlopen(SERVER, timeout=120) as r:
            return json.load(r)
    except Exception as exc:
        sys.exit(f"Could not reach {SERVER} ({exc}). Start kcro_server.py first.")


def main():
    data = fetch_full()
    n = data["count"]
    links = np.asarray(data["links"], dtype=np.int64).reshape(-1, 2)
    print(f"{n:,} nodes, {len(links):,} edges", flush=True)

    rng = np.random.default_rng(SEED)

    # Connected components + per-node degree (to pick each cluster's hub).
    src, tgt = links[:, 0], links[:, 1]
    adj = sp.coo_matrix((np.ones(len(links)), (src, tgt)), shape=(n, n))
    n_comp, labels = csgraph.connected_components(adj, directed=False)
    print(f"{n_comp:,} connected components (clusters)", flush=True)

    degree = np.zeros(n, dtype=np.int64)
    np.add.at(degree, src, 1)
    np.add.at(degree, tgt, 1)

    # 1. Component centers: uniform in a 3D ball (r ~ U^(1/3) for even density).
    radius = GALAXY_SPREAD * (n_comp ** (1 / 3)) / 20.0
    u = rng.random(n_comp) ** (1 / 3)
    theta = rng.uniform(0, 2 * np.pi, n_comp)
    phi = np.arccos(rng.uniform(-1, 1, n_comp))
    centers = np.empty((n_comp, 3), dtype=np.float64)
    centers[:, 0] = radius * u * np.sin(phi) * np.cos(theta)
    centers[:, 1] = radius * u * np.sin(phi) * np.sin(theta)
    centers[:, 2] = radius * u * np.cos(phi)

    # 2. Each node sits near its component center; the hub sits exactly on it.
    comp_size = np.bincount(labels, minlength=n_comp)
    local_r = CLUSTER_SCALE * np.cbrt(np.maximum(comp_size[labels], 1))
    offsets = rng.normal(0, 1, (n, 3))
    offsets *= (local_r / np.maximum(np.linalg.norm(offsets, axis=1), 1e-9))[:, None]

    pos = centers[labels] + offsets

    # Pin the highest-degree node of each component to its center (the hub).
    order = np.lexsort((-degree, labels))           # within each label, hub first
    first = np.ones(n_comp, dtype=bool)
    seen = np.zeros(n_comp, dtype=bool)
    hub_idx = np.empty(n_comp, dtype=np.int64)
    for node in order:                              # cheap: one pass, hub is first seen
        lab = labels[node]
        if not seen[lab]:
            seen[lab] = True
            hub_idx[lab] = node
    pos[hub_idx] = centers

    pos = pos.astype(np.float32)
    with open(OUT, "wb") as f:
        f.write(struct.pack("<I", n))               # node count header
        f.write(pos.tobytes())
    print(f"Wrote {OUT}: {n:,} positions "
          f"({4 + pos.nbytes:,} bytes). Galaxy radius ~{radius:.0f}.", flush=True)


if __name__ == "__main__":
    main()
