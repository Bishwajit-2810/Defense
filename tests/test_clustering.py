"""Unit tests for libs/clustering.py — embedding k-means for §5 cluster summaries."""

import sys

sys.path.insert(0, '/home/bk/code/defense')

import numpy as np
import pytest

from libs.clustering import (
    MAX_CLUSTERS,
    choose_k,
    cluster_embeddings,
    parse_pgvector,
)


def test_choose_k_bounds():
    assert choose_k(0) == 1
    assert choose_k(3) == 1                 # below MIN_FOR_CLUSTERING
    assert 2 <= choose_k(50) <= MAX_CLUSTERS
    assert choose_k(10_000) == MAX_CLUSTERS  # clamped


def test_parse_pgvector():
    assert parse_pgvector("[1.0,2.0,3.0]") == [1.0, 2.0, 3.0]
    assert parse_pgvector("[]") is None
    assert parse_pgvector(None) is None
    assert parse_pgvector("garbage") is None


def test_clusters_separate_blobs():
    """Three well-separated blobs in 8-d → k>=2 and members grouped correctly."""
    rng = np.random.default_rng(0)
    centers = [np.zeros(8), np.ones(8) * 5, np.ones(8) * -5]
    pts, truth = [], []
    for ci, c in enumerate(centers):
        for _ in range(20):
            pts.append((c + rng.normal(0, 0.2, 8)).tolist())
            truth.append(ci)

    res = cluster_embeddings(pts)
    assert res.k >= 2
    assert sum(res.sizes) == len(pts)
    assert len(res.representative_indices) == res.k

    # Points sharing a true blob should mostly share a predicted label: every
    # predicted cluster should be dominated by a single true blob.
    for members in res.members:
        if not members:
            continue
        blobs = [truth[i] for i in members]
        dominant = max(set(blobs), key=blobs.count)
        assert blobs.count(dominant) / len(blobs) >= 0.8


def test_single_cluster_when_few_points():
    res = cluster_embeddings([[1.0, 0.0], [0.9, 0.1], [1.1, 0.0]])
    assert res.k == 1
    assert res.sizes == [3]
    assert res.representative_indices[0] in (0, 1, 2)


def test_empty_input():
    res = cluster_embeddings([])
    assert res.k == 0 and res.labels == []


def test_hdbscan_algo_falls_back_to_kmeans_when_unavailable():
    """algo='hdbscan' without the lib installed → graceful k-means fallback,
    still a valid partition covering every point."""
    rng = np.random.default_rng(1)
    centers = [np.zeros(8), np.ones(8) * 5]
    pts = []
    for c in centers:
        for _ in range(15):
            pts.append((c + rng.normal(0, 0.2, 8)).tolist())

    res = cluster_embeddings(pts, algo="hdbscan")
    assert res.k >= 1
    assert sum(res.sizes) == len(pts)            # every point assigned
    assert len(res.labels) == len(pts)
    assert len(res.representative_indices) == res.k
