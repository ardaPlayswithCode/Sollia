import sys, re, json
sys.path.insert(0, '/Users/ardaturgut/Desktop/SMALL SOLLIA TEST FOLDER ,LAYER BY LAYER/sollia_working')
from src.pipeline.orchestrator import run_analysis
from src.schemas import SolliaAnalysisResult, AgentLogMessage
from datetime import datetime

result = None
logs = []
for e in run_analysis('NVDA', run_forensic=True, run_narrative=True):
    if isinstance(e, SolliaAnalysisResult):
        result = e
    elif isinstance(e, AgentLogMessage):
        logs.append(e)

if result:
    export = {
        'ticker': result.ticker, 'company': result.company_name,
        'score': round(result.s_final, 1), 'signal': result.signal,
        'signal_color': result.signal_color,
        'summary': result.executive_summary, 'timestamp': datetime.now().isoformat(),
        'agents_run': result.agents_run,
        'track1_score': result.track1_score,
        'corroboration': result.corroboration,
        'forensic': {}, 'narrative': {},
        'log': [{'speaker': l.speaker, 'message': l.message} for l in logs],
    }
    if result.forensic:
        f = result.forensic
        export['forensic'] = {
            's1': f.s1, 'discrepancy_label': f.discrepancy_label,
            'conclusion': f.conclusion, 'ceo': f.ceo_name,
            'ceo_start_date': f.ceo_start_date,
            'historical_start': f.historical_start,
            'historical_end': f.historical_end,
            'baseline_mean': f.baseline_mean,
            'baseline_stddev': f.baseline_stddev,
            'current_gap': f.current_gap,
            'sigma_1': f.sigma_1,
            'discrepancy_metric': f.discrepancy_metric,
            'direct_contradiction': f.direct_contradiction,
            'contradiction_detail': f.contradiction_detail,
            'fallback_used': f.fallback_used,
            'fallback_detail': f.fallback_detail,
            'quarterly_pairs': [{
                'fiscal_period': p.fiscal_period,
                'transcript_date': p.transcript_date,
                'filing_date': p.filing_date,
                'gap': p.gap,
                'rolling_sigma': p.rolling_sigma,
                'tone': {
                    'hedging_density': p.tone.hedging_density if p.tone else None,
                    'confidence_ratio': p.tone.confidence_ratio if p.tone else None,
                    'sentence_length_mean': p.tone.sentence_length_mean if p.tone else None,
                    'sentence_length_variance': p.tone.sentence_length_variance if p.tone else None,
                    'forward_looking_density': p.tone.forward_looking_density if p.tone else None,
                    'overall_sentiment': p.tone.overall_sentiment if p.tone else None,
                    'raw_score': p.tone.raw_score if p.tone else None,
                },
                'reality': {
                    'revenue_direction': p.reality.revenue_direction if p.reality else None,
                    'revenue_magnitude': p.reality.revenue_magnitude if p.reality else None,
                    'raw_score': p.reality.raw_score if p.reality else None,
                    'context_snippet': p.reality.context_snippet if p.reality else None,
                }
            } for p in (f.quarterly_pairs or [])],
            'top_signals': [{
                'rank': s.rank, 'severity': s.severity, 'severity_color': s.severity_color,
                'fiscal_period': s.fiscal_period,
                'description': s.description,
                'transcript_citation': s.transcript_citation,
                'filing_citation': s.filing_citation,
                'transcript_url': s.transcript_url,
                'htm_link': s.htm_link,
            } for s in (f.top_signals or [])]
        }
    if result.narrative:
        n = result.narrative
        export['narrative'] = {
            's2': n.s2, 'sentiment_label': n.sentiment_label,
            'conclusion': n.conclusion,
            'historical_start': n.historical_start,
            'historical_end': n.historical_end,
            'total_articles': n.total_articles,
            'filtered_articles': n.filtered_articles,
            'baseline_mean': n.baseline_mean,
            'baseline_stddev': n.baseline_stddev,
            'test_weighted_sentiment': n.test_weighted_sentiment,
            'sigma_2': n.sigma_2,
            'sentiment_metric': n.sentiment_metric,
            'providers_used': n.providers_used,
            'top_signals': [{
                'rank': s.rank, 'headline': s.headline,
                'url': s.url, 'source': s.source, 'date': s.date,
                'sentiment_score': s.sentiment_score,
            } for s in (n.top_signals or [])]
        }

    disc_rows = ""
    # Format helper for US dates
    def _us_date(d):
        try:
            from datetime import datetime as _dt
            return _dt.strptime(d[:10], "%Y-%m-%d").strftime("%m-%d-%Y")
        except:
            return d

    if result.forensic and result.forensic.top_signals:
        for s in result.forensic.top_signals:
            q = s.fiscal_period
            disc_rows += f'<tr><td><span style="color:{s.severity_color};font-weight:bold;font-size:13px;">{s.severity}</span></td><td style="font-size:14px;">{s.description}</td><td style="color:#CBD5E1;font-size:13px;">{_us_date(s.fiscal_period)}: "{s.transcript_citation}"</td><td style="font-size:13px;"><a href="{s.transcript_url}" style="color:#3B82F6;text-decoration:underline;">Trans</a></td><td style="color:#CBD5E1;font-size:13px;">{_us_date(s.fiscal_period)}: "{s.filing_citation}"</td><td style="font-size:13px;"><a href="{s.htm_link}" style="color:#3B82F6;text-decoration:underline;">10-K</a></td></tr>'
    
    disc_label = result.forensic.discrepancy_label.replace('\u2014', '-') if result.forensic else ""
    sent_rows = ""
    if result.narrative and result.narrative.top_signals:
        for s in result.narrative.top_signals:
            sent_rows += f'<tr><td style="font-size:14px;"><a href="{s.url}" style="color:#F8FAFC;">{s.headline[:80]}</a></td><td style="color:#CBD5E1;font-size:13px;">{s.source}</td><td style="color:#CBD5E1;font-size:13px;">{s.date}</td></tr>'
    sent_label = result.narrative.sentiment_label.replace('\u2014', '-') if result.narrative else ""
    log_html = "".join(f'<p style="font-size:14px;color:#CBD5E1;margin:3px 0;">[{l.speaker}] {l.message}</p>' for l in logs[-25:])

    html = f'''<html><head><title>Sollia - {result.ticker}</title><meta charset="UTF-8">
<style>body{{background:#0F1117;color:#F8FAFC;font-family:-apple-system,Arial,sans-serif;max-width:1100px;margin:auto;padding:48px 24px;}}
.card{{background:#1A1D2E;border-radius:14px;padding:28px;margin:24px 0;}}
h1{{font-size:96px;color:{result.signal_color};margin:0;text-align:center;font-weight:800;}}
h2{{color:{result.signal_color};margin:0 0 8px;text-align:center;font-size:24px;font-weight:700;}}
h3{{color:#F8FAFC;margin:0 0 14px;font-size:22px;font-weight:700;}}
p{{color:#CBD5E1;line-height:1.6;font-size:15px;}}
table{{width:100%;border-collapse:collapse;margin-top:14px;}}
th{{text-align:left;color:#CBD5E1;font-size:13px;text-transform:uppercase;letter-spacing:.5px;padding:10px 8px;border-bottom:1px solid #2a2d3e;font-weight:600;}}
td{{padding:10px 8px;border-bottom:1px solid #1f2233;font-size:14px;vertical-align:top;}}
a{{color:#93C5FD;text-decoration:none;}}a:hover{{text-decoration:underline;}}
.json-section{{margin-top:28px;}}.json-header{{color:#F8FAFC;font-size:18px;font-weight:bold;margin-bottom:10px;}}
.json-btns{{display:flex;gap:10px;}}.json-btn{{background:#1A1D2E;color:#F8FAFC;border:1px solid #2a2d3e;border-radius:10px;padding:12px 24px;cursor:pointer;font-size:14px;font-weight:600;}}.json-btn:hover{{background:#2a2d3e;}}
.json-viewer{{display:none;background:#0a0c14;border-radius:10px;padding:18px;margin-top:10px;max-height:400px;overflow:auto;}}
.json-viewer pre{{color:#22C55E;font-size:12px;margin:0;white-space:pre-wrap;}}</style>
<script>var d={json.dumps(export)};
function viewJSON(){{var v=document.getElementById('jv');var p=document.getElementById('jp');
if(v.style.display==='none'||v.style.display===''){{v.style.display='block';p.textContent=JSON.stringify(d,null,2);}}else{{v.style.display='none';}}}}
function downloadJSON(){{var b=new Blob([JSON.stringify(d,null,2)],{{type:'application/json'}});var a=document.createElement('a');a.href=URL.createObjectURL(b);a.download='sollia_{result.ticker.lower()}.json';a.click();}}</script></head>
<body><h1>{int(round(result.s_final))}%</h1><h2>{result.signal}</h2>
<p style="text-align:center;color:#CBD5E1;font-size:16px;">{result.company_name} &middot; {datetime.now().strftime('%b %d, %Y %I:%M %p')}</p>
<p style="text-align:center;margin-bottom:32px;">{result.executive_summary}</p>

<div class="card"><h3>CEO vs 10-K</h3><p><b>{disc_label}</b></p><table><tr><th>Sev</th><th>Finding</th><th>Transcript</th><th></th><th>10-K Filing</th><th></th></tr>{disc_rows}</table><p style="color:#CBD5E1;margin-top:14px;font-size:14px;">{result.forensic.conclusion if result.forensic else ''}</p></div>

<div class="card"><h3>News Sentiment</h3><p><b>{sent_label}</b></p><table><tr><th>Headline</th><th>Source</th><th>Date</th></tr>{sent_rows}</table><p style="color:#CBD5E1;margin-top:14px;font-size:14px;">{result.narrative.conclusion if result.narrative else ''}</p></div>

<div class="card"><h3>Agent Log</h3>{log_html}</div>

<div class="json-section"><div class="json-header">JSON</div><div class="json-btns"><button class="json-btn" onclick="viewJSON()">View</button><button class="json-btn" onclick="downloadJSON()">Download</button></div><div id="jv" class="json-viewer"><pre id="jp"></pre></div></div>
</body></html>'''
    
    with open('/Users/ardaturgut/Desktop/small sollia test folder ,layer by layer/sollia_result.html', 'w') as f:
        f.write(html)
    print(f"DONE: {int(round(result.s_final))}% {result.signal}")
