"""Application settings — hardcoded for local dev."""
import os
from pathlib import Path
from pydantic_settings import BaseSettings

ROOT_DIR = Path(__file__).parent.parent

class Settings(BaseSettings):
    fmp_api_key: str = ""
    finnhub_api_key: str = ""
    openai_api_key: str = ""
    cache_dir: str = "/tmp/sollia_cache"
    newsapi_key: str = ""
    alphavantage_key: str = ""

settings = Settings()
