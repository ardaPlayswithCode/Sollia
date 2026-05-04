"""Full data models for Sollia analysis pipeline."""
from __future__ import annotations
from datetime import datetime
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class AgentLogMessage(BaseModel):
    speaker: str = ""
    timestamp: datetime = Field(default_factory=datetime.now)
    message: str = ""


class TranscriptResult(BaseModel):
    ticker: str = ""
    company_name: str = ""
    fiscal_period: str = ""
    fiscal_period_end: str = ""
    date: str = ""
    content: str = ""
    source: str = ""
    is_proxy: bool = False
    speaker_sections: List[Dict[str, str]] = Field(default_factory=list)
    source_url: str = ""


class FilingResult(BaseModel):
    ticker: str = ""
    company_name: str = ""
    cik: str = ""
    filing_type: str = "10-K"
    period_of_report: str = ""
    filing_date: str = ""
    accession_number: str = ""
    htm_url: str = ""
    mda_text: str = ""
    full_text: str = ""
    ceo_name: str = ""


class SolliaToneMetrics(BaseModel):
    hedging_density: float = 0.0
    confidence_ratio: float = 0.0
    sentence_length_mean: float = 0.0
    sentence_length_variance: float = 0.0
    forward_looking_density: float = 0.0
    overall_sentiment: float = 0.0
    raw_score: float = 0.0


class SolliaRealityMetrics(BaseModel):
    revenue_direction: int = 0
    revenue_magnitude: float = 0.0
    raw_score: float = 0.0
    context_snippet: str = ""


class SolliaQuarterlyPair(BaseModel):
    fiscal_period: str = ""
    transcript_date: str = ""
    filing_date: str = ""
    tone: Optional[SolliaToneMetrics] = None
    reality: Optional[SolliaRealityMetrics] = None
    gap: float = 0.0
    rolling_sigma: float = 0.0


class SolliaDiscrepancySignal(BaseModel):
    rank: int = 0
    fiscal_period: str = ""
    severity: str = "LOW"
    severity_color: str = "#22C55E"
    description: str = ""
    transcript_citation: str = ""
    filing_citation: str = ""
    htm_link: str = ""
    transcript_url: str = ""


class SolliaSentimentSignal(BaseModel):
    rank: int = 0
    headline: str = ""
    url: str = ""
    source: str = ""
    date: str = ""
    sentiment_score: float = 0.0
    weighted_contribution: float = 0.0
    relevance: str = ""


class SolliaForensicResult(BaseModel):
    ticker: str = ""
    ceo_name: str = ""
    ceo_start_date: str = ""
    historical_start: str = ""
    historical_end: str = ""
    quarterly_pairs: List[SolliaQuarterlyPair] = Field(default_factory=list)
    baseline_mean: float = 0.0
    baseline_stddev: float = 1.0
    current_gap: float = 0.0
    sigma_1: float = 0.0
    s1: float = 50.0
    direct_contradiction: bool = False
    contradiction_detail: str = ""
    top_signals: List[SolliaDiscrepancySignal] = Field(default_factory=list)
    discrepancy_metric: float = 0.0
    discrepancy_label: str = ""
    conclusion: str = ""
    log_messages: List[AgentLogMessage] = Field(default_factory=list)
    fallback_used: bool = False
    fallback_detail: str = ""


class SolliaNarrativeResult(BaseModel):
    ticker: str = ""
    historical_start: str = ""
    historical_end: str = ""
    total_articles: int = 0
    filtered_articles: int = 0
    baseline_mean: float = 0.0
    baseline_stddev: float = 1.0
    test_weighted_sentiment: float = 0.0
    sigma_2: float = 0.0
    s2: float = 50.0
    top_signals: List[SolliaSentimentSignal] = Field(default_factory=list)
    sentiment_metric: float = 0.0
    sentiment_label: str = ""
    conclusion: str = ""
    log_messages: List[AgentLogMessage] = Field(default_factory=list)
    providers_used: List[str] = Field(default_factory=list)


class Headline(BaseModel):
    headline: str = ""
    url: str = ""
    source: str = ""
    date: str = ""
    sentiment: float = 0.0
    weight: float = 1.0


class SolliaAnalysisResult(BaseModel):
    ticker: str = ""
    company_name: str = ""
    timestamp: datetime = Field(default_factory=datetime.now)
    s_final: float = 50.0
    signal: str = "HOLD"
    signal_color: str = "#F59E0B"
    agents_run: List[str] = Field(default_factory=list)
    forensic: Optional[SolliaForensicResult] = None
    narrative: Optional[SolliaNarrativeResult] = None
    executive_summary: str = ""
    track1_score: float = 50.0
    corroboration: float = 0.0
    track1_detail: Optional[Dict[str, Any]] = None
    track2_detail: Optional[Dict[str, Any]] = None
    log_messages: List[AgentLogMessage] = Field(default_factory=list)
    export_data: Optional[Dict[str, Any]] = None
