"""Application settings — hardcoded for local dev."""
import os
from pathlib import Path
from pydantic_settings import BaseSettings

ROOT_DIR = Path(__file__).parent.parent

class Settings(BaseSettings):
    # NOTE: Sollia's only live data sources are SEC EDGAR (filings) and Groq/
    # OpenRouter (LLM). The keys below are unused; fmp_api_key was a reference to
    # a forbidden dead source (FMP) and has been removed per the API priority spec.
    openai_api_key: str = ""
    cache_dir: str = "/tmp/sollia_cache"

settings = Settings()
