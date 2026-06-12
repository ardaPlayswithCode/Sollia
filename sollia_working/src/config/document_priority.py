"""
UNIVERSAL DOCUMENT FETCHING PRIORITY LIST
==========================================
All Sollia agents (Agent 1, Agent 2, Agent 3, and future agents)
MUST use this configuration for fetching SEC filings.

This ensures:
1. Consistent fallback behavior across all agents
2. Single source of truth for endpoints
3. Easy updates without modifying each agent
"""

from typing import Dict, List, Any

# ============================================
# 1. 10-K FILINGS (Annual Reports)
# ============================================
TEN_K_SOURCES: List[Dict[str, Any]] = [
    {
        "priority": 1,
        "name": "SEC EDGAR JSON API",
        "endpoint": "https://data.sec.gov/submissions/CIK{CIK}.json",
        "method": "json_api",
        "notes": "Metadata (accession numbers). Filter form='10-K'",
        "timeout": 30
    },
    {
        "priority": 2,
        "name": "SEC EDGAR direct .htm",
        "endpoint": "https://www.sec.gov/Archives/edgar/data/{CIK_int}/{acc_nodash}/{primary_doc}",
        "method": "direct_html",
        "notes": "Full text. MD&A is Item 7",
        "timeout": 30
    },
    {
        "priority": 3,
        "name": "EDGAR full-text search",
        "endpoint": "https://efts.sec.gov/LATEST/search-index?q={TICKER}&dateRange=custom&startdt=YYYY-01-01&enddt=YYYY-12-31&forms=10-K",
        "method": "full_text_search",
        "notes": "Last resort (2016+)",
        "timeout": 45
    }
]

# ============================================
# 2. 10-Q FILINGS (Quarterly Reports)
# ============================================
TEN_Q_SOURCES: List[Dict[str, Any]] = [
    {
        "priority": 1,
        "name": "SEC EDGAR JSON API",
        "endpoint": "https://data.sec.gov/submissions/CIK{CIK}.json",
        "method": "json_api",
        "notes": "Filter form='10-Q'. MD&A is Item 2",
        "timeout": 30
    },
    {
        "priority": 2,
        "name": "SEC EDGAR direct .htm",
        "endpoint": "https://www.sec.gov/Archives/edgar/data/{CIK_int}/{acc_nodash}/{primary_doc}",
        "method": "direct_html",
        "notes": "Shorter docs",
        "timeout": 30
    },
    {
        "priority": 3,
        "name": "SEC EDGAR browse 10-Q",
        "endpoint": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={CIK_int}&type=10-Q&dateb=&owner=exclude&count=40",
        "method": "browse",
        "notes": "Use for older filings not in JSON archive",
        "timeout": 30
    }
]

# ============================================
# 3. 8-K FILINGS (Material Events)
# ============================================
EIGHT_K_SOURCES: List[Dict[str, Any]] = [
    {
        "priority": 1,
        "name": "SEC EDGAR JSON API",
        "endpoint": "https://data.sec.gov/submissions/CIK{CIK}.json",
        "method": "json_api",
        "notes": "Filter form='8-K'. Pre-filter by items field (2.02, 1.01, 5.02)",
        "timeout": 30
    },
    {
        "priority": 2,
        "name": "SEC EDGAR browse 8-K",
        "endpoint": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={CIK_int}&type=8-K&dateb=&owner=exclude&count=100",
        "method": "browse",
        "notes": "Bulk discovery",
        "timeout": 30
    },
    {
        "priority": 3,
        "name": "EDGAR full-text search",
        "endpoint": "https://efts.sec.gov/LATEST/search-index?q={TICKER}&forms=8-K",
        "method": "full_text_search",
        "notes": "2016+",
        "timeout": 45
    }
]

# ============================================
# 4. DEF 14A FILINGS (Proxy Statements)
# ============================================
DEF_14A_SOURCES: List[Dict[str, Any]] = [
    {
        "priority": 1,
        "name": "SEC EDGAR JSON API",
        "endpoint": "https://data.sec.gov/submissions/CIK{CIK}.json",
        "method": "json_api",
        "notes": "Filter forms containing '14A'",
        "timeout": 30
    },
    {
        "priority": 2,
        "name": "SEC EDGAR direct .htm",
        "endpoint": "https://www.sec.gov/Archives/edgar/data/{CIK_int}/{acc_nodash}/{primary_doc}",
        "method": "direct_html",
        "notes": "Contains compensation, board elections",
        "timeout": 30
    },
    {
        "priority": 3,
        "name": "SEC EDGAR browse DEF 14A",
        "endpoint": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={CIK_int}&type=DEF+14A&dateb=&owner=exclude&count=40",
        "method": "browse",
        "notes": "Use for definitive proxy",
        "timeout": 30
    }
]

# ============================================
# 5. EARNINGS CALL TRANSCRIPTS
# ============================================
TRANSCRIPT_SOURCES: List[Dict[str, Any]] = [
    {
        "priority": 1,
        "name": "SEC 8-K EX-99.1",
        "endpoint": "multi_step",
        "method": "ex99_extraction",
        "notes": "Multi-step: 8-K list → fetch index.htm → find EX-99.1 → fetch & strip HTML",
        "timeout": 60
    },
    {
        "priority": 2,
        "name": "SEC Atom feed (8-K)",
        "endpoint": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={CIK_int}&type=8-K&output=atom",
        "method": "atom_feed",
        "notes": "XML feed, filter for 2.02",
        "timeout": 30
    },
    {
        "priority": 3,
        "name": "Nasdaq.com earnings",
        "endpoint": "https://www.nasdaq.com/market-activity/stocks/{ticker}/earnings",
        "method": "scrape",
        "notes": "Scrape transcript links. Use as backup",
        "timeout": 30
    }
]

# ============================================
# UNIVERSAL FETCH FUNCTION
# ============================================

def get_source_priority(document_type: str) -> List[Dict[str, Any]]:
    """
    Returns the priority list for a given document type.
    
    Args:
        document_type: "10-K", "10-Q", "8-K", "DEF14A", "transcript"
    
    Returns:
        List of source configs in priority order
    """
    mapping = {
        "10-K": TEN_K_SOURCES,
        "10-Q": TEN_Q_SOURCES,
        "8-K": EIGHT_K_SOURCES,
        "DEF14A": DEF_14A_SOURCES,
        "transcript": TRANSCRIPT_SOURCES
    }
    return mapping.get(document_type, [])

def get_all_priorities() -> Dict[str, List[Dict[str, Any]]]:
    """Returns all document priority mappings"""
    return {
        "10-K": TEN_K_SOURCES,
        "10-Q": TEN_Q_SOURCES,
        "8-K": EIGHT_K_SOURCES,
        "DEF14A": DEF_14A_SOURCES,
        "transcript": TRANSCRIPT_SOURCES
    }

# Universal rate limits
SEC_RATE_LIMIT = 10  # requests per second
USER_AGENT = "Sollia contact@sollia.app"

print("✅ Universal document priority list loaded")
