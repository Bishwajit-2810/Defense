"""Pre-fetch the Stage-2 cheap voters into the local HF cache, and prove they vote.

`MODEL_STUB_MODE=true` means "download nothing", so Stage 2 skips any head whose
weights are not already cached (`_get_pipeline` -> `classifier_skipped_not_cached`)
and that head silently abstains for the life of the process. This script is the
other half of that bargain: it does the downloading, once, deliberately.

It does two things the plain `huggingface-cli download` does not:

* **Checks each head can actually vote.** A checkpoint is only useful here if it
  is a sentiment head whose labels survive `worker._map_sentiment_label`. A base
  encoder (`ElectraForPreTraining`, `BertForMaskedLM`) loads fine and then emits
  `LABEL_0`/`LABEL_1`, which maps to None — the model burns a gigabyte of disk
  and never casts a vote. Four of this roster's original five entries were that,
  or did not exist on the Hub at all, so the check is not hypothetical.
* **Runs each head on a real code-mixed probe** (Bangla, English, Banglish) and
  prints its verdicts, so "downloaded" is not mistaken for "working".

Usage
-----
    uv run python deploy/prefetch_classifiers.py           # fetch + verify
    uv run python deploy/prefetch_classifiers.py --check   # verify only, no download
    uv run python deploy/prefetch_classifiers.py --device cpu

If a download sits at 0 bytes
-----------------------------
Observed on this network: `huggingface_hub` opens a connection for a large LFS
file and never receives a byte. It is not a timeout — the process blocks in
`socket.connect`/read indefinitely, `HF_HUB_DOWNLOAD_TIMEOUT` does not cover it,
and nothing is logged. curl on the same URL streams normally, so the fallback is

    curl -L -C - --speed-limit 20000 --speed-time 30 -o <file> \
      https://huggingface.co/<repo>/resolve/main/model.safetensors

placed into the cache as `blobs/<etag>` with a `snapshots/<rev>/<name>` symlink.
Fetch the small files (`config.json`, `tokenizer.json`, ...) the same way — a
repo with weights but no `tokenizer.json` fails with "Couldn't instantiate the
backend tokenizer ... need sentencepiece or tiktoken", which reads as a missing
dependency and is really a missing file. `--check` reports both cases.
"""

from __future__ import annotations

import argparse
import socket
import sys
import time
from pathlib import Path

# huggingface.co resolves to a set of CloudFront IPs, and at least one of them
# blackholes TCP from some networks. `socket.create_connection` with no timeout
# then hangs forever on that address — `HF_HUB_DOWNLOAD_TIMEOUT` does not cover
# the connect, so the symptom is a prefetch that sits at 0 bytes indefinitely
# with no error. A default timeout makes the connect fail fast and Python moves
# on to the next address in the DNS result.
socket.setdefaulttimeout(30)

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from defense.libs.common.config import get_settings  # noqa: E402
from defense.libs.ensemble import CHEAP_SOURCES  # noqa: E402
from defense.services.workers.stage2_llm.worker import (  # noqa: E402
    _is_cached,
    _map_sentiment_label,
)

#: One line per script the corpus actually contains. If a head cannot label
#: these it is not useful on this data, whatever its leaderboard score.
PROBE: list[tuple[str, str]] = [
    ("bn", "এই সিদ্ধান্তটা খুবই ভালো হয়েছে, ধন্যবাদ।"),
    ("bn", "একদম বাজে কাজ হয়েছে, মেনে নেওয়া যায় না।"),
    ("en", "This is a genuinely good decision, well handled."),
    ("en", "Absolute disgrace, they should be ashamed."),
    ("banglish", "eta khub baje decision, mene nite parlam na"),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="verify only, never download")
    ap.add_argument("--device", default=None, help="cpu | cuda (default: STAGE2_CLASSIFIER_DEVICE)")
    args = ap.parse_args()

    settings = get_settings()
    roster = settings.stage2_classifier_roster
    device_pref = (args.device or settings.stage2_classifier_device or "auto").lower()

    unknown = [n for n, _ in roster if n not in CHEAP_SOURCES]
    if unknown:
        print(f"FATAL: voter names not in ensemble.CHEAP_SOURCES: {unknown}")
        print("       Their votes would be collected and then ignored by should_escalate.")
        return 2

    print(f"roster: {len(roster)} heads, device={device_pref}\n")

    from transformers import pipeline  # noqa: PLC0415 — heavy, and optional

    device = 0 if device_pref == "cuda" else -1
    ok = 0
    for name, model_id in roster:
        cached = _is_cached(model_id)
        if args.check and not cached:
            print(f"  {name:24} MISSING   {model_id}")
            continue
        t0 = time.time()
        try:
            pipe = pipeline("sentiment-analysis", model=model_id, device=device)
        except Exception as exc:
            print(f"  {name:24} LOAD-FAIL {model_id}\n{' ' * 28}{type(exc).__name__}: {str(exc)[:120]}")
            continue
        load_s = time.time() - t0

        t0 = time.time()
        raw = pipe([t for _, t in PROBE], truncation=True)
        infer_ms = (time.time() - t0) * 1000 / len(PROBE)

        mapped = [_map_sentiment_label(r.get("label", "")) for r in raw]
        voted = sum(1 for m in mapped if m)
        verdict = "OK" if voted == len(PROBE) else ("PARTIAL" if voted else "NEVER-VOTES")
        flag = "" if cached else "  (downloaded)"
        print(
            f"  {name:24} {verdict:11} {voted}/{len(PROBE)} votes  "
            f"load {load_s:5.1f}s  {infer_ms:5.1f} ms/comment{flag}"
        )
        print(f"  {'':24} {model_id}")
        for (lang, text), r, m in zip(PROBE, raw, mapped):
            print(
                f"  {'':24}   {lang:8} {str(r.get('label')):16} -> "
                f"{m or 'DROPPED (unmappable label)'}  ({r.get('score', 0):.2f})  {text[:34]}"
            )
        print()
        del pipe
        if voted == len(PROBE):
            ok += 1

    print(f"{ok}/{len(roster)} heads can vote on every probe line.")
    return 0 if ok == len(roster) else 1


if __name__ == "__main__":
    raise SystemExit(main())
