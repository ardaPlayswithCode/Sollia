"""
Shared LLM Configuration for ALL Sollia Agents
"""

import os
import time
import hashlib
import json
import requests
from typing import Tuple, Dict

# LLM PROVIDER PRIORITY LIST
LLM_PROVIDERS = [
    {"name": "Groq-70B", "url": "https://api.groq.com/openai/v1/chat/completions",
     "model": "llama-3.3-70b-versatile", "key_env": "GROQ_API_KEY"},
    {"name": "Groq-8B", "url": "https://api.groq.com/openai/v1/chat/completions",
     "model": "llama-3.1-8b-instant", "key_env": "GROQ_API_KEY"},
    {"name": "OpenRouter", "url": "https://openrouter.ai/api/v1/chat/completions",
     "model": "meta-llama/llama-3.1-70b-instruct", "key_env": "OPENROUTER_API_KEY"},
]

# Rate limiting
RATE_LIMIT = 0.15
MAX_RETRIES = 3
RETRY_DELAY = 1.0

# Cache
LLM_CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".llm_cache")
os.makedirs(LLM_CACHE_DIR, exist_ok=True)

_last_request_time = 0
_request_lock = None

# Threading lock removed — using simple time-based rate limiting

def call_llm(prompt: str, max_tokens: int = 400, temperature: float = 0.0, provider_idx: int = 0) -> Tuple[str, Dict]:
    cache_key = hashlib.sha256(f"{prompt}_{max_tokens}_{temperature}".encode()).hexdigest()[:16]
    cache_path = os.path.join(LLM_CACHE_DIR, f"{cache_key}.json")
    
    if os.path.exists(cache_path):
        try:
            with open(cache_path, 'r') as f:
                cached = json.load(f)
                return cached['response'], {'cached': True, 'provider': cached.get('provider', 'unknown')}
        except:
            pass
    
    global _last_request_time
    elapsed = time.time() - _last_request_time
    if elapsed < RATE_LIMIT:
        time.sleep(RATE_LIMIT - elapsed)
    _last_request_time = time.time()
    
    for idx in range(provider_idx, len(LLM_PROVIDERS)):
        provider = LLM_PROVIDERS[idx]
        api_key = os.environ.get(provider["key_env"], "")
        if not api_key:
            continue
        
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
        if provider["name"] == "OpenRouter":
            headers["HTTP-Referer"] = "https://sollia.app"
            headers["X-Title"] = "Sollia"
        
        payload = {"model": provider["model"], "messages": [{"role": "user", "content": prompt}], "temperature": temperature, "max_tokens": max_tokens}
        
        for retry in range(MAX_RETRIES):
            try:
                response = requests.post(provider["url"], headers=headers, json=payload, timeout=60)
                if response.status_code == 200:
                    data = response.json()
                    result = data["choices"][0]["message"]["content"].strip()
                    with open(cache_path, 'w') as f:
                        json.dump({'response': result, 'provider': provider["name"], 'model': provider["model"]}, f)
                    return result, {'cached': False, 'provider': provider["name"], 'model': provider["model"], 'retries': retry}
                elif response.status_code == 429:
                    time.sleep(RETRY_DELAY * (retry + 1))
                    continue
                else:
                    break
            except:
                if retry == MAX_RETRIES - 1:
                    break
                time.sleep(RETRY_DELAY)
    
    raise Exception(f"All LLM providers failed for prompt: {prompt[:100]}...")

def get_primary_model() -> str:
    return LLM_PROVIDERS[0]["model"]
