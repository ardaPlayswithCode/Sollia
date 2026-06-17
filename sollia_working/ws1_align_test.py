"""
WS1 offline A/B harness for Agent 1 on F 2025. Embeds once (cached to disk +
pickle), then compares alignment quality across approaches and thresholds:

  - FULL            : raw FinBERT embeddings (baseline)
  - STRIP           : boilerplate-opener stripped before embedding
  - FULL+CENTER     : raw, mean-centered (anisotropy correction)
  - STRIP+CENTER    : stripped, mean-centered

False-positive proxy: an aligned pair with cosine >= threshold but near-zero
content-word overlap (Jaccard < 0.06) — high semantic-similarity score yet almost
no shared vocabulary, the signature of boilerplate/anisotropy-driven matches.

Run: cd sollia_working && python3.11 ws1_align_test.py
"""
import os
import re
import sys
import pickle

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from src.agents import corporate_discrepancy as cd

TICKER, YEAR = "F", 2025
PICKLE = os.path.join(os.path.dirname(__file__), ".cache", "ws1_F2025_chunks.pkl")
THRESHOLDS = [0.80, 0.86, 0.90, 0.95]
JACCARD_FP = 0.06

_STOP = set((
    "the a an and or of to in for on at by with from as is are was were be been being this that "
    "these those it its their our we us you your they them he she his her not no any all may will "
    "would could should can our we have has had do does did but if then than over under between "
    "during such other into out up down more most less least which who whom whose what when where "
    "company companys quarter year period three nine six month months also been being report"
).split())


def content_words(text):
    return set(w for w in re.findall(r"[a-z]{4,}", (text or "").lower()) if w not in _STOP)


def jaccard(a, b):
    wa, wb = content_words(a), content_words(b)
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / float(len(wa | wb))


def has_boilerplate_opener(text):
    return bool(cd._BOILERPLATE_OPENER_RE.match(text or ""))


def build_chunks():
    if os.path.exists(PICKLE):
        with open(PICKLE, "rb") as f:
            print("Loaded cached chunks from pickle.")
            return pickle.load(f)
    print("Fetching + cleaning + chunking %s %d ..." % (TICKER, YEAR))
    docs, err = cd.fetch_all_texts(TICKER, YEAR)
    if err:
        print("FETCH ERROR:", err)
        sys.exit(1)
    docs = cd.clean_all(docs)
    docs = cd.chunk_all(docs)
    all_chunks = (docs.get("chunks_10k", []) + docs.get("chunks_10q", []) +
                  docs.get("chunks_def14a", []) + docs.get("chunks_8k", []) +
                  docs.get("chunks_transcript", []))
    print("Total chunks: %d. Embedding (full + stripped)..." % len(all_chunks))
    embed_fn = cd.get_embedder()
    for n, c in enumerate(all_chunks):
        full = c["text"]
        strip = cd._embedding_text(full)
        ef = cd._get_cached_embedding(full) or embed_fn(full)
        cd._cache_embedding(full, ef)
        es = cd._get_cached_embedding(strip) or embed_fn(strip)
        cd._cache_embedding(strip, es)
        c["emb_full"], c["emb_strip"] = ef, es
        if (n + 1) % 50 == 0:
            print("  embedded %d/%d" % (n + 1, len(all_chunks)))
    slim = [{"chunk_id": c["chunk_id"], "text": c["text"], "metadata": c["metadata"],
             "emb_full": c["emb_full"], "emb_strip": c["emb_strip"]} for c in all_chunks]
    os.makedirs(os.path.dirname(PICKLE), exist_ok=True)
    with open(PICKLE, "wb") as f:
        pickle.dump(slim, f)
    return slim


def add_centered(chunks):
    """Mean-center each embedding family (subtract the corpus mean) to remove the
    dominant shared 'finance boilerplate' component that inflates all cosines."""
    for base in ("emb_full", "emb_strip"):
        M = np.array([c[base] for c in chunks], dtype=float)
        mean = M.mean(axis=0)
        for c in chunks:
            c[base + "_c"] = (np.array(c[base], dtype=float) - mean).tolist()


def align(chunks, field, threshold):
    aligned = []
    for da, db, tier in cd.DOCUMENT_PAIRS:
        ca = cd._get_chunks_by_type(chunks, da)
        cb = cd._get_chunks_by_type(chunks, db)
        if not ca or not cb:
            continue
        A = np.array([c[field] for c in ca], dtype=float)
        B = np.array([c[field] for c in cb], dtype=float)
        A = A / np.maximum(np.linalg.norm(A, axis=1, keepdims=True), 1e-10)
        B = B / np.maximum(np.linalg.norm(B, axis=1, keepdims=True), 1e-10)
        sims = A.dot(B.T)
        used = set()
        for i in range(len(ca)):
            row = sims[i]
            for j in np.argsort(row)[::-1]:
                jj = int(j)
                if row[jj] < threshold:
                    break
                if jj not in used:
                    used.add(jj)
                    aligned.append((ca[i], cb[jj], float(row[jj])))
                    break
    return aligned


def fp_count(aligned):
    return sum(1 for a, b, s in aligned if jaccard(a["text"], b["text"]) < JACCARD_FP)


def main():
    chunks = build_chunks()
    add_centered(chunks)
    nb = sum(1 for c in chunks if has_boilerplate_opener(c["text"]))
    print("\n%d chunks; %d (%.0f%%) open with boilerplate. FP proxy = aligned pair "
          "with cosine>=T and content-word Jaccard < %.2f\n" % (len(chunks), nb,
          100.0 * nb / max(1, len(chunks)), JACCARD_FP))

    fields = [("FULL", "emb_full"), ("STRIP", "emb_strip"),
              ("FULL+CENTER", "emb_full_c"), ("STRIP+CENTER", "emb_strip_c")]
    header = "thresh | " + " | ".join("%-22s" % name for name, _ in fields)
    print(header)
    print("-" * len(header))
    for T in THRESHOLDS:
        cells = []
        for name, fld in fields:
            al = align(chunks, fld, T)
            fp = fp_count(al)
            pct = 100.0 * fp / max(1, len(al))
            cells.append("%4d aligned, %3d FP(%2.0f%%)" % (len(al), fp, pct))
        print("%5.2f  | " % T + " | ".join("%-22s" % c for c in cells))


if __name__ == "__main__":
    main()
