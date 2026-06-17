"""Validate that the REAL pipeline align_all_pairs (with mean-centering wired in)
reduces aligned pairs as predicted. Uses the cached pickle — no embedding."""
import os, sys, pickle
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from src.agents import corporate_discrepancy as cd

chunks = pickle.load(open(os.path.join(os.path.dirname(__file__), ".cache", "ws1_F2025_chunks.pkl"), "rb"))


def regroup():
    docs = {"chunks_10k": [], "chunks_10q": [], "chunks_def14a": [], "chunks_8k": [], "chunks_transcript": []}
    for c in chunks:
        dt = c["metadata"]["source_doc_type"]
        cc = {"chunk_id": c["chunk_id"], "text": c["text"],
              "sentences": [s for s in c["text"].split(". ") if s], "metadata": c["metadata"],
              "embedding": list(c["emb_full"])}
        if dt.startswith("10-K"): docs["chunks_10k"].append(cc)
        elif dt.startswith("10-Q"): docs["chunks_10q"].append(cc)
        elif dt.startswith("DEF14A"): docs["chunks_def14a"].append(cc)
        elif dt.startswith("8-K"): docs["chunks_8k"].append(cc)
        elif dt.startswith("EARNINGS_CALL"): docs["chunks_transcript"].append(cc)
    return docs


out = cd.align_all_pairs(regroup())
print("Pipeline aligned_pairs WITH centering   :", len(out["aligned_pairs"]))

_orig = cd._apply_mean_centering
cd._apply_mean_centering = lambda x: None
out2 = cd.align_all_pairs(regroup())
cd._apply_mean_centering = _orig
print("Pipeline aligned_pairs WITHOUT centering:", len(out2["aligned_pairs"]))
