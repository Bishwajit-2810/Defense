"""Embedding clustering for cluster-level summarization (architecture §5).

The cost lever: instead of summarizing N posts with N LLM calls, cluster their
embeddings and summarize one representative slice per cluster — a handful of LLM
calls for the whole corpus.

Pure-numpy k-means (deterministic, seeded — no heavy sklearn/hdbscan dependency,
no network). k is chosen by a sqrt heuristic and clamped. The caller fetches the
``analysis_results.embedding`` vectors from pgvector and passes them here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

# Hard cap so a runaway campaign can't ask the LLM to summarize 100s of clusters.
MAX_CLUSTERS = 8
MIN_FOR_CLUSTERING = 4  # below this, clustering is meaningless — one cluster


@dataclass
class ClusterResult:
    k: int
    labels: list[int]                        # cluster id per input vector
    sizes: list[int]                         # member count per cluster id
    representative_indices: list[int]        # input index closest to each centroid

    # convenience: members[cluster_id] -> list of input indices
    members: list[list[int]] = field(default_factory=list)


def choose_k(n: int, max_k: int = MAX_CLUSTERS) -> int:
    """Pick a cluster count: ~sqrt(n/2), clamped to [2, max_k] and <= n."""
    if n < MIN_FOR_CLUSTERING:
        return 1
    k = round(math.sqrt(n / 2.0))
    return max(2, min(k, max_k, n))


def _kmeans(
    x: np.ndarray,
    k: int,
    iters: int = 50,
    seed: int = 42,
) -> tuple[np.ndarray, np.ndarray]:
    """Deterministic Lloyd's k-means. Returns (labels, centroids).

    Vectors are L2-normalized first, so Euclidean distance on the unit sphere is
    monotonic with cosine distance — appropriate for sentence embeddings.
    """
    rng = np.random.default_rng(seed)

    # k-means++ style seeding (deterministic via the seeded rng).
    n = x.shape[0]
    first = int(rng.integers(0, n))
    centroids = [x[first]]
    for _ in range(1, k):
        d2 = np.min(
            np.stack([np.sum((x - c) ** 2, axis=1) for c in centroids], axis=0),
            axis=0,
        )
        total = float(d2.sum())
        if total <= 0:
            centroids.append(x[int(rng.integers(0, n))])
            continue
        probs = d2 / total
        nxt = int(rng.choice(n, p=probs))
        centroids.append(x[nxt])
    cent = np.stack(centroids)

    labels = np.zeros(n, dtype=int)
    for _ in range(iters):
        # assign
        dists = np.stack([np.sum((x - c) ** 2, axis=1) for c in cent], axis=1)
        new_labels = np.argmin(dists, axis=1)
        if np.array_equal(new_labels, labels) and _ > 0:
            labels = new_labels
            break
        labels = new_labels
        # update
        for ci in range(k):
            mask = labels == ci
            if mask.any():
                cent[ci] = x[mask].mean(axis=0)
            # empty cluster: leave centroid where it is
    return labels, cent


def _hdbscan_labels(x: np.ndarray) -> "list[int] | None":
    """Try HDBSCAN (architecture §5). Returns labels (noise→its own singleton
    clusters) or None when the library isn't installed. Density-based, so no k."""
    try:
        import hdbscan  # type: ignore
    except Exception:
        try:
            from sklearn.cluster import HDBSCAN as _SKHDBSCAN  # type: ignore
        except Exception:
            return None
        clusterer = _SKHDBSCAN(min_cluster_size=max(2, x.shape[0] // 20))
        raw = clusterer.fit_predict(x)
    else:
        clusterer = hdbscan.HDBSCAN(min_cluster_size=max(2, x.shape[0] // 20))
        raw = clusterer.fit_predict(x)

    # Reassign noise (-1) points to fresh singleton cluster ids so every post
    # belongs somewhere (the report summarizes all clusters).
    labels: list[int] = []
    next_id = int(max(raw)) + 1 if len(raw) and max(raw) >= 0 else 0
    for v in raw:
        if int(v) < 0:
            labels.append(next_id)
            next_id += 1
        else:
            labels.append(int(v))
    return labels


def cluster_embeddings(
    vectors: list[list[float]],
    max_k: int = MAX_CLUSTERS,
    seed: int = 42,
    algo: str | None = None,
) -> ClusterResult:
    """Cluster embedding vectors and pick a representative member per cluster.

    ``algo`` selects the algorithm (env ``CLUSTER_ALGO`` when omitted):
    ``"kmeans"`` (default, pure-numpy) or ``"hdbscan"`` (density-based; needs the
    ``hdbscan`` or scikit-learn extra — falls back to k-means if unavailable).

    Returns a single all-members cluster when there are too few points to
    cluster meaningfully (``< MIN_FOR_CLUSTERING``).
    """
    import os

    algo = (algo or os.getenv("CLUSTER_ALGO", "kmeans")).lower()
    n = len(vectors)
    if n == 0:
        return ClusterResult(k=0, labels=[], sizes=[], representative_indices=[], members=[])

    x = np.asarray(vectors, dtype=np.float64)
    # L2-normalize (guard zero vectors).
    norms = np.linalg.norm(x, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    x = x / norms

    # HDBSCAN path (density-based, picks its own cluster count).
    if algo == "hdbscan" and n >= MIN_FOR_CLUSTERING:
        hl = _hdbscan_labels(x)
        if hl is not None:
            return _result_from_labels(x, hl)
        # else fall through to k-means

    k = choose_k(n, max_k)
    if k <= 1:
        # Single cluster: representative = closest to the mean.
        center = x.mean(axis=0)
        rep = int(np.argmin(np.sum((x - center) ** 2, axis=1)))
        return ClusterResult(
            k=1, labels=[0] * n, sizes=[n],
            representative_indices=[rep], members=[list(range(n))],
        )

    labels, _cent = _kmeans(x, k, seed=seed)
    return _result_from_labels(x, [int(v) for v in labels])


def _result_from_labels(x: np.ndarray, labels: list[int]) -> ClusterResult:
    """Build a ClusterResult from arbitrary integer labels.

    Cluster ids are renumbered to a contiguous 0..k-1 range (HDBSCAN can emit
    sparse/non-contiguous ids); the representative of each cluster is the member
    closest to that cluster's centroid (mean).
    """
    # Map original label -> contiguous id, preserving first-seen order.
    order: dict[int, int] = {}
    for lab in labels:
        if lab not in order:
            order[lab] = len(order)
    k = len(order)

    members: list[list[int]] = [[] for _ in range(k)]
    for idx, lab in enumerate(labels):
        members[order[lab]].append(idx)

    sizes = [len(m) for m in members]
    reps: list[int] = []
    for ci in range(k):
        member_idx = np.array(members[ci])
        centroid = x[member_idx].mean(axis=0)
        d = np.sum((x[member_idx] - centroid) ** 2, axis=1)
        reps.append(int(member_idx[int(np.argmin(d))]))

    return ClusterResult(
        k=k,
        labels=[order[lab] for lab in labels],
        sizes=sizes,
        representative_indices=reps,
        members=members,
    )


def parse_pgvector(literal: str | None) -> list[float] | None:
    """Parse a pgvector text literal ``[0.1,0.2,...]`` into a float list."""
    if not literal:
        return None
    s = literal.strip()
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1]
    if not s:
        return None
    try:
        return [float(p) for p in s.split(",")]
    except ValueError:
        return None
