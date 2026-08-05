"""Write the text-analysable corpus: every post except the image-only ones.

Why this exists
---------------
"Multimodal text+image sentiment fusion" is claim #2 of the project, but the
image term has never contributed a non-zero value in any configuration that can
be run (PROJECT_ASSESSMENT §5.2): the dataset's 69 ``photoUrls`` are all
relative object-storage keys, and the objects are not in MinIO. With the image
and OCR terms out of scope for now, a **null-caption PHOTO post has no text at
all** — there is nothing left for Stage 1 to analyse, and routing it produces a
confidently-empty result rather than a measurement.

PHOTO_TEXT posts are unaffected: they carry a real caption, so they analyse
correctly as text-only once the image term is gone. Only the 7 PHOTO posts are
excluded.

The source corpus is never modified — the images may come back, and with them
OCR. This writes a filtered copy that ``eval/`` and the ingestion path can point
at, and prints exactly what it dropped so the exclusion is reportable rather
than invisible.

Usage
-----
    python -m eval.make_text_corpus                 # → posts_text_only.json
    python -m eval.make_text_corpus --out other.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "posts_with_details.json"
DEFAULT_OUT = ROOT / "posts_text_only.json"


def has_text(post: dict) -> bool:
    """True when the post carries a caption Stage 1 can analyse."""
    return bool((post.get("caption") or "").strip())


def filter_posts(posts: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split into (kept, dropped). Dropped = image-only, i.e. no caption."""
    kept = [p for p in posts if has_text(p)]
    dropped = [p for p in posts if not has_text(p)]
    return kept, dropped


def _comment_count(post: dict) -> int:
    return len(post.get("comments") or [])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    posts = json.loads(args.source.read_text())
    if isinstance(posts, dict):
        posts = posts.get("posts") or posts.get("data") or []

    kept, dropped = filter_posts(posts)
    args.out.write_text(json.dumps(kept, ensure_ascii=False, indent=2))

    by_type: dict[str, int] = {}
    for p in dropped:
        t = p.get("postType") or "UNKNOWN"
        by_type[t] = by_type.get(t, 0) + 1

    print(f"source:  {args.source.name}  ({len(posts)} posts, "
          f"{sum(_comment_count(p) for p in posts):,} comments)")
    print(f"written: {args.out.name}  ({len(kept)} posts, "
          f"{sum(_comment_count(p) for p in kept):,} comments)")
    print(f"dropped: {len(dropped)} image-only posts (no caption) "
          f"carrying {sum(_comment_count(p) for p in dropped):,} comments")
    for t, n in sorted(by_type.items()):
        print(f"           {t}: {n}")
    for p in dropped:
        print(f"           - {p.get('id')}  photoUrls={len(p.get('photoUrls') or [])}"
              f"  comments={_comment_count(p)}")


if __name__ == "__main__":
    main()
