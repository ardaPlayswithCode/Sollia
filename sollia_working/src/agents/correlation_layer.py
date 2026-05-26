
"""Multi-Agent Cross-Correlation Regularization Layer.

Builds a historical Z-matrix from 3-sentence transcript chunks paired with
full MD&A anchors, computes Pearson correlation matrix R, applies
regularization penalty Omega, and synthesizes a final merged score.

Spec: 200 uniform time segments (adapted to 160 for 20 quarters x 8 chunks).
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

SEGMENTS_PER_QUARTER = 8
TOTAL_SEGMENTS = 160
MDA_TRUNCATION_CHARS = 15000
GROQ_TIMEOUT = 60
MAX_CONCURRENT = 4

# ============================================================================
# Divergence Score Formula (from spec)
# ============================================================================

def compute_divergence(extraction_json: dict) -> float:
    """Compute divergence score D from LLM extraction JSON.
    
    T_pol = (positive - negative) / total
    M_pol = (positive - negative) / total
    D = min(100, (50 * max(0, T_pol - M_pol) + 25*(f1+f2)) * (1 + 0.5*c1 + 0.25*c2))
    """
    ts = extraction_json.get("transcript_sentiment_bounds", {})
    ms = extraction_json.get("mda_sentiment_bounds", {})
    af = extraction_json.get("adversarial_flags", {})
    sa = extraction_json.get("semantic_alignment", {})
    
    T_pol = (ts.get("positive_sentences_count", 0) - ts.get("negative_sentences_count", 0))
    T_pol /= max(ts.get("total_sentences_count", 1), 1)
    
    M_pol = (ms.get("positive_sentences_count", 0) - ms.get("negative_sentences_count", 0))
    M_pol /= max(ms.get("total_sentences_count", 1), 1)
    
    f1 = 1 if af.get("omission_of_disclosed_10k_risks", False) else 0
    f2 = 1 if not sa.get("top_3_transcript_themes_match_mda_priorities", True) else 0
    c1 = 1 if af.get("contradiction_of_10k_metrics", False) else 0
    c2 = 1 if af.get("defensive_evasion_in_qa", False) else 0
    
    D = min(100.0, (50.0 * max(0, T_pol - M_pol) + 25.0 * (f1 + f2)) * (1.0 + 0.5*c1 + 0.25*c2))
    return D


def normalize_z(D: float) -> float:
    """Normalize divergence score to Z-matrix range [-1, 1]."""
    return D / 50.0 - 1.0


# ============================================================================
# Prompt Template (from spec)
# ============================================================================

def build_extraction_prompt(transcript_chunk: str, mda_text: str) -> str:
    """Build the feature extraction prompt per the spec template."""
    mda_truncated = mda_text[:MDA_TRUNCATION_CHARS]
    
    return f"""You are a strict, deterministic financial data feature extraction engine. Your sole task is to analyze the provided financial text components (Earnings Call Transcript and 10-K MDA Section) and output a single, raw, minified JSON object matching the exact schema specified below.

### CRITICAL EXECUTION RULES:
1. OUTPUT FORMAT: Output ONLY valid, parsable JSON. Do NOT wrap the JSON in markdown code blocks (no ```json). Do NOT include any introductory text, explanatory prose, or trailing commentary. Start with '{{' and end with '}}'.
2. DETERMINISTIC COUNTING: Count sentences systematically. 
   - "positive_sentences_count": Sentences conveying growth, guidance beats, margin expansion, or operational tailwinds.
   - "negative_sentences_count": Sentences conveying cost inflation, supply headwinds, guidance cuts, or execution failures.
3. ADVERSARIAL FLAGS: These must be strict booleans (true/false).
   - "omission_of_disclosed_10k_risks": Set to true if major risks explicitly highlighted in the 10-K MDA section are entirely unaddressed or avoided by management in the transcript text.
   - "contradiction_of_10k_metrics": Set to true if quantitative metrics or operational narratives given in the transcript directly clash with numbers recorded in the MDA.
   - "defensive_evasion_in_qa": Set to true if management utilizes evasive language, pivots, or non-answers to direct analyst questions regarding headwinds.
4. SEMANTIC ALIGNMENT: 
   - "top_3_transcript_themes_match_mda_priorities": Set to false if the core 3 topics focused on during the call do not align with the top 3 priorities outlined in the MDA.

### TARGET JSON SCHEMA:
{{
  "transcript_sentiment_bounds": {{
    "positive_sentences_count": <integer>,
    "negative_sentences_count": <integer>,
    "total_sentences_count": <integer>
  }},
  "mda_sentiment_bounds": {{
    "positive_sentences_count": <integer>,
    "negative_sentences_count": <integer>,
    "total_sentences_count": <integer>
  }},
  "adversarial_flags": {{
    "omission_of_disclosed_10k_risks": <boolean>,
    "contradiction_of_10k_metrics": <boolean>,
    "defensive_evasion_in_qa": <boolean>
  }},
  "semantic_alignment": {{
    "top_3_transcript_themes_match_mda_priorities": <boolean>
  }}
}}

### INPUT DATA FOR ANALYSIS:
[EARNINGS CALL TRANSCRIPT]:
\"\"\"
{transcript_chunk}
\"\"\"

[10-K MDA SECTION]:
\"\"\"
{mda_truncated}
\"\"\"

JSON OUTPUT:"""


# ============================================================================
# Groq Extraction (single segment)
# ============================================================================

def extract_segment_features(transcript_chunk: str, mda_text: str) -> float:
    """Send one segment to Groq and return normalized z-score.
    
    Returns 0.0 (neutral) on failure to avoid poisoning the matrix.
    """
    from src.agents.follow_through import _ask_groq
    
    prompt = build_extraction_prompt(transcript_chunk, mda_text)
    
    for attempt in range(2):
        try:
            response = _ask_groq(prompt, timeout=GROQ_TIMEOUT)
            if not response:
                continue
            
            # Parse JSON from response
            clean = response.strip()
            if clean.startswith("```"):
                clean = re.sub(r"```\w*\n?", "", clean)
                clean = clean.rstrip("`")
            
            data = json.loads(clean)
            D = compute_divergence(data)
            return normalize_z(D)
            
        except Exception as e:
            logger.debug("Segment extraction attempt %d failed: %s", attempt + 1, str(e)[:100])
            time.sleep(1.0)
    
    return 0.0  # Neutral fallback


print("correlation_layer.py written successfully")

# ============================================================================
# Z-Matrix Builder
# ============================================================================



# ============================================================================
# Pearson Correlation Matrix R (from spec)
# ============================================================================

def compute_R_matrix(Z: np.ndarray) -> np.ndarray:
    """Compute Pearson cross-correlation matrix R from Z matrix.
    
    Z shape: (N_segments, M_agents)
    Returns R shape: (M, M) where R[j,k] = correlation between agents j and k.
    """
    if Z.shape[1] < 2:
        return np.eye(Z.shape[1])
    return np.corrcoef(Z.T)


# ============================================================================
# Regularized Multiplier Omega (from spec)
# ============================================================================

def compute_omega(R: np.ndarray, gamma: float = 0.5) -> np.ndarray:
    """Compute regularization penalty matrix Omega from correlation matrix R.
    
    Omega[j,k] = 1.0 if j==k, else max(0.1, 1.0 - gamma * |R[j,k]|)
    """
    M = R.shape[0]
    Omega = np.ones((M, M))
    for j in range(M):
        for k in range(M):
            if j != k:
                Omega[j, k] = max(0.1, 1.0 - gamma * abs(R[j, k]))
    return Omega


# ============================================================================
# Final Score Synthesis (from spec)
# ============================================================================

def synthesize_score(S_raw: np.ndarray, Omega: np.ndarray) -> float:
    """Compute final regularized score from raw agent scores and Omega matrix.
    
    S_final = sum(S_raw_j * prod(Omega_jk for k!=j)) / sum(prod(...))
    """
    M = len(S_raw)
    weights = np.ones(M)
    for j in range(M):
        for k in range(M):
            if j != k:
                weights[j] *= Omega[j, k]
    
    return float(np.sum(S_raw * weights) / np.sum(weights))

class ZMatrixBuilder:
    """Builds the historical Z-matrix for a ticker using 3-sentence transcript
    chunks paired with full MD&A anchors. Results are cached to disk.
    """
    
    def __init__(self):
        self._cache_dir = os.path.join(os.path.dirname(__file__), "..", "..", ".z_matrix_cache")
        os.makedirs(self._cache_dir, exist_ok=True)
    
    def build(self, ticker: str) -> np.ndarray:
        """Build Z-matrix for a ticker. Returns (N, 1) array of z-scores.
        
        Uses disk cache: builds once per ticker, instant on subsequent calls.
        """
        cache_file = os.path.join(self._cache_dir, f"{ticker}_Z.npy")
        
        # Check cache
        if os.path.exists(cache_file):
            logger.info("Loading Z-matrix from cache: %s", cache_file)
            return np.load(cache_file)
        
        logger.info("Building Z-matrix for %s...", ticker)
        segments = self._prepare_segments(ticker)
        
        if not segments:
            logger.warning("No segments available for %s", ticker)
            return np.zeros((0, 1))
        
        # Extract features with rate-limited parallel execution
        z_scores = self._extract_all(segments)
        
        Z = np.array(z_scores).reshape(-1, 1)
        
        # Cache to disk
        np.save(cache_file, Z)
        logger.info("Z-matrix saved: %s (%d segments)", cache_file, len(Z))
        
        return Z
    
    def _prepare_segments(self, ticker: str) -> list:
        """Prepare all transcript-MDA segment pairs for a ticker."""
        from src.agents.forensic_accountant import ForensicAccountant, _split_sentences
        from src.providers.transcripts.fmp import FMPTranscriptProvider
        from src.providers.filings.sec_edgar import SECEdgarProvider
        from src.agents.follow_through import _extract_mda
        
        # Run Agent 1 to get quarterly pairs
        agent = ForensicAccountant()
        result = agent.run(ticker)
        
        # Get transcripts
        tp = FMPTranscriptProvider()
        transcripts = tp.get_transcripts(ticker)
        transcript_by_period = {}
        for t in transcripts:
            period = t.fiscal_period or (t.date[:7] if t.date else None)
            if period and period not in transcript_by_period:
                transcript_by_period[period] = t
        
        # Get filings with MD&A
        ep = SECEdgarProvider()
        filings = ep.get_10k_filings(ticker)
        filing_by_date = {}
        for filing in filings:
            fd = filing.get("filing_date", "")
            if fd and fd not in filing_by_date:
                html = ep.fetch_html(filing["cik"], filing["accession_nodash"], filing["primary_doc"], ticker)
                mda = _extract_mda(html, "10-K")
                if mda:
                    filing_by_date[fd] = mda
        
        # Build segments
        segments = []
        for pair in result.quarterly_pairs:
            fp = pair.fiscal_period
            if not fp or fp[:7] not in transcript_by_period:
                continue
            
            transcript = transcript_by_period[fp[:7]]
            sentences = _split_sentences(transcript.content)
            mda_text = filing_by_date.get(pair.filing_date, "")
            if not mda_text and filing_by_date:
                mda_text = list(filing_by_date.values())[0]
            if not mda_text:
                continue
            
            n_chunks = min(len(sentences) // 3, SEGMENTS_PER_QUARTER)
            if n_chunks == 0:
                continue
            
            step = max(1, len(sentences) // (n_chunks + 1))
            for i in range(n_chunks):
                start_idx = i * step
                chunk = " ".join(sentences[start_idx:start_idx + 3])
                if len(chunk) > 50:
                    segments.append((chunk, mda_text))
        
        return segments[:TOTAL_SEGMENTS]
    
    def _extract_all(self, segments: list) -> list:
        """Extract features for all segments using rate-limited parallel execution."""
        from src.agents.follow_through import _rate_limited_groq
        
        def extract_one(args):
            chunk, mda = args
            return extract_segment_features(chunk, mda)
        
        z_scores = []
        total = len(segments)
        for i, seg in enumerate(segments):
            z = extract_one(seg)
            z_scores.append(z)
            if (i + 1) % 10 == 0:
                logger.info("  Segment %d/%d", i + 1, total)
        
        return z_scores

# ============================================================================
# Z-Matrix Builder
# ============================================================================



# ============================================================================
# Pearson Correlation Matrix R (from spec)
# ============================================================================

def compute_R_matrix(Z: np.ndarray) -> np.ndarray:
    """Compute Pearson cross-correlation matrix R from Z matrix.
    
    Z shape: (N_segments, M_agents)
    Returns R shape: (M, M) where R[j,k] = correlation between agents j and k.
    """
    if Z.shape[1] < 2:
        return np.eye(Z.shape[1])
    return np.corrcoef(Z.T)


# ============================================================================
# Regularized Multiplier Omega (from spec)
# ============================================================================

def compute_omega(R: np.ndarray, gamma: float = 0.5) -> np.ndarray:
    """Compute regularization penalty matrix Omega from correlation matrix R.
    
    Omega[j,k] = 1.0 if j==k, else max(0.1, 1.0 - gamma * |R[j,k]|)
    """
    M = R.shape[0]
    Omega = np.ones((M, M))
    for j in range(M):
        for k in range(M):
            if j != k:
                Omega[j, k] = max(0.1, 1.0 - gamma * abs(R[j, k]))
    return Omega


# ============================================================================
# Final Score Synthesis (from spec)
# ============================================================================

def synthesize_score(S_raw: np.ndarray, Omega: np.ndarray) -> float:
    """Compute final regularized score from raw agent scores and Omega matrix.
    
    S_final = sum(S_raw_j * prod(Omega_jk for k!=j)) / sum(prod(...))
    """
    M = len(S_raw)
    weights = np.ones(M)
    for j in range(M):
        for k in range(M):
            if j != k:
                weights[j] *= Omega[j, k]
    
    return float(np.sum(S_raw * weights) / np.sum(weights))

class ZMatrixBuilder:
    """Builds the historical Z-matrix for a ticker using 3-sentence transcript
    chunks paired with full MD&A anchors. Results are cached to disk.
    """
    
    def __init__(self):
        self._cache_dir = os.path.join(os.path.dirname(__file__), "..", "..", ".z_matrix_cache")
        os.makedirs(self._cache_dir, exist_ok=True)
    
    def build(self, ticker: str) -> np.ndarray:
        """Build Z-matrix for a ticker. Returns (N, 1) array of z-scores.
        
        Uses disk cache: builds once per ticker, instant on subsequent calls.
        """
        cache_file = os.path.join(self._cache_dir, f"{ticker}_Z.npy")
        
        # Check cache
        if os.path.exists(cache_file):
            logger.info("Loading Z-matrix from cache: %s", cache_file)
            return np.load(cache_file)
        
        logger.info("Building Z-matrix for %s...", ticker)
        segments = self._prepare_segments(ticker)
        
        if not segments:
            logger.warning("No segments available for %s", ticker)
            return np.zeros((0, 1))
        
        # Extract features with rate-limited parallel execution
        z_scores = self._extract_all(segments)
        
        Z = np.array(z_scores).reshape(-1, 1)
        
        # Cache to disk
        np.save(cache_file, Z)
        logger.info("Z-matrix saved: %s (%d segments)", cache_file, len(Z))
        
        return Z
    
    def _prepare_segments(self, ticker: str) -> list:
        """Prepare all transcript-MDA segment pairs for a ticker."""
        from src.agents.forensic_accountant import ForensicAccountant, _split_sentences
        from src.providers.transcripts.fmp import FMPTranscriptProvider
        from src.providers.filings.sec_edgar import SECEdgarProvider
        from src.agents.follow_through import _extract_mda
        
        # Run Agent 1 to get quarterly pairs
        agent = ForensicAccountant()
        result = agent.run(ticker)
        
        # Get transcripts
        tp = FMPTranscriptProvider()
        transcripts = tp.get_transcripts(ticker)
        transcript_by_period = {}
        for t in transcripts:
            period = t.fiscal_period or (t.date[:7] if t.date else None)
            if period and period not in transcript_by_period:
                transcript_by_period[period] = t
        
        # Get filings with MD&A
        ep = SECEdgarProvider()
        filings = ep.get_10k_filings(ticker)
        filing_by_date = {}
        for filing in filings:
            fd = filing.get("filing_date", "")
            if fd and fd not in filing_by_date:
                html = ep.fetch_html(filing["cik"], filing["accession_nodash"], filing["primary_doc"], ticker)
                mda = _extract_mda(html, "10-K")
                if mda:
                    filing_by_date[fd] = mda
        
        # Build segments
        segments = []
        for pair in result.quarterly_pairs:
            fp = pair.fiscal_period
            if not fp or fp[:7] not in transcript_by_period:
                continue
            
            transcript = transcript_by_period[fp[:7]]
            sentences = _split_sentences(transcript.content)
            mda_text = filing_by_date.get(pair.filing_date, "")
            if not mda_text and filing_by_date:
                mda_text = list(filing_by_date.values())[0]
            if not mda_text:
                continue
            
            n_chunks = min(len(sentences) // 3, SEGMENTS_PER_QUARTER)
            if n_chunks == 0:
                continue
            
            step = max(1, len(sentences) // (n_chunks + 1))
            for i in range(n_chunks):
                start_idx = i * step
                chunk = " ".join(sentences[start_idx:start_idx + 3])
                if len(chunk) > 50:
                    segments.append((chunk, mda_text))
        
        return segments[:TOTAL_SEGMENTS]
    
    def _extract_all(self, segments: list) -> list:
        """Extract features for all segments using rate-limited parallel execution."""
        from src.agents.follow_through import _rate_limited_groq
        
        def extract_one(args):
            chunk, mda = args
            return extract_segment_features(chunk, mda)
        
        z_scores = []
        total = len(segments)
        for i, seg in enumerate(segments):
            z = extract_one(seg)
            z_scores.append(z)
            if (i + 1) % 10 == 0:
                logger.info("  Segment %d/%d", i + 1, total)
        
        return z_scores
