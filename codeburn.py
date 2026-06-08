"""
CodeBurn · dashboard de custos de tokens do Claude Code.

Lê os logs de sessão do Claude Code em ~/.claude/projects/, estima o custo
equivalente em tokens (como se fosse cobrança via API) e gera um dashboard HTML.

Importante: usuários Claude Pro/Max pagam assinatura fixa, não por token. Os
valores em dólar servem como proxy de intensidade de uso, para comparar projetos
entre si. Não é uma fatura real.

Uso:
  python codeburn.py                  # gera report.html ao lado deste script
  python codeburn.py --days 7         # só os últimos 7 dias
  python codeburn.py --json out.json  # exporta JSON além do HTML
  python codeburn.py --open           # abre o HTML no navegador ao terminar

Requer Python 3.10 ou mais novo. Sem dependências externas (só biblioteca padrão).
O gráfico usa Chart.js via CDN, então precisa de internet para renderizar os
gráficos (as tabelas funcionam offline).
"""
import os
import sys
import json
import argparse
import re
from datetime import datetime, timezone, timedelta
from collections import defaultdict, Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).parent
PRICING = json.loads((ROOT / "pricing.json").read_text(encoding="utf-8"))
CLAUDE_DIR = Path(os.path.expanduser("~")) / ".claude" / "projects"
CACHE_PATH = ROOT / ".session_map_cache.json"

# -------------------------------------------------------------------------
# EDITE AQUI para agrupar suas sessões por projeto.
# Cada tupla é (regex testado contra o caminho da pasta de trabalho, rótulo).
# A primeira regra que casar vence. Se nenhuma casar, o rótulo vira o próprio
# caminho da pasta. Os exemplos abaixo são genéricos, troque pelos seus.
PROJECT_RULES = [
    (r"[\\/]frontend([\\/]|$)", "Frontend"),
    (r"[\\/]backend([\\/]|$)", "Backend"),
    (r"[\\/]infra([\\/]|$)", "Infra"),
    (r"[\\/]docs([\\/]|$)", "Docs"),
    (r"my-app|myapp", "My App"),
]

PRICE_1M_THRESHOLD = 200_000


PATH_HINT_REGEX = re.compile(r"[Pp]rojects[\\/]([A-Za-z][A-Za-z0-9_\-\.]*)")
# Tokens que aparecem como "projeto" no texto mas não são projetos reais.
# Adicione aqui qualquer ruído que você queira ignorar na classificação.
HINT_BLACKLIST = set()


def classify_project(cwd: str) -> str:
    if not cwd:
        return "unknown"
    for pattern, label in PROJECT_RULES:
        if re.search(pattern, cwd, re.IGNORECASE):
            return label
    return cwd


def extract_text_from_record(rec: dict) -> str:
    """Extrai texto útil de qualquer tipo de record para buscar path hints."""
    msg = rec.get("message")
    if not isinstance(msg, dict):
        return ""
    content = msg.get("content")
    if isinstance(content, str):
        return content[:20000]
    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                inp = block.get("input", {})
                for v in (inp.values() if isinstance(inp, dict) else []):
                    if isinstance(v, str):
                        parts.append(v)
            elif block.get("type") == "tool_result":
                c = block.get("content")
                if isinstance(c, str):
                    parts.append(c[:5000])
                elif isinstance(c, list):
                    for cb in c:
                        if isinstance(cb, dict) and cb.get("type") == "text":
                            parts.append(cb.get("text", "")[:3000])
        return "\n".join(parts)[:30000]
    return ""


def scan_session_projects(path: Path) -> dict[str, str]:
    """Para cada sessionId no arquivo, determina o projeto mais provável via cwd + path hints."""
    cwds_per_session: dict[str, set] = defaultdict(set)
    hints_per_session: dict[str, Counter] = defaultdict(Counter)

    with path.open("r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            sid = rec.get("sessionId")
            if not sid:
                continue
            cwd = rec.get("cwd", "")
            if cwd:
                cwds_per_session[sid].add(cwd)
            text = extract_text_from_record(rec)
            if text:
                for m in PATH_HINT_REGEX.findall(text):
                    if m in HINT_BLACKLIST:
                        continue
                    hints_per_session[sid][m] += 1

    out = {}
    for sid, cwds in cwds_per_session.items():
        deepest = max(cwds, key=len) if cwds else ""
        classified = classify_project(deepest)
        if classified == "unknown" or classified == deepest:
            hints = hints_per_session.get(sid, Counter())
            if hints:
                top = hints.most_common(1)[0][0]
                classified = classify_project(f"Projects/{top}")
        out[sid] = classified
    return out


def model_prices(model: str, total_input_tokens: int) -> tuple[float, float]:
    """Retorna (input_price_per_1m, output_price_per_1m) em USD. Aplica tier 1M se input > 200k."""
    m = PRICING["models"].get(model) or PRICING["models"]["_fallback"]
    if total_input_tokens > PRICE_1M_THRESHOLD and "tier_1m_input" in m:
        return m["tier_1m_input"], m["tier_1m_output"]
    return m["input"], m["output"]


def parse_jsonl(path: Path, since_ts: datetime | None):
    """Itera sobre records assistant com usage válida."""
    with path.open("r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("type") != "assistant":
                continue
            msg = rec.get("message") or {}
            if not isinstance(msg, dict):
                continue
            usage = msg.get("usage")
            if not usage:
                continue
            ts_str = rec.get("timestamp")
            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            except Exception:
                continue
            if since_ts and ts < since_ts:
                continue
            yield {
                "timestamp": ts,
                "model": msg.get("model", "unknown"),
                "cwd": rec.get("cwd", ""),
                "session_id": rec.get("sessionId", ""),
                "input_tokens": usage.get("input_tokens", 0),
                "cache_creation": usage.get("cache_creation_input_tokens", 0),
                "cache_read": usage.get("cache_read_input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "cache_1h": (usage.get("cache_creation") or {}).get("ephemeral_1h_input_tokens", 0),
                "cache_5m": (usage.get("cache_creation") or {}).get("ephemeral_5m_input_tokens", 0),
            }


def compute_cost(rec: dict) -> float:
    """Retorna custo USD do record. Usa:
    input normal: 1x
    cache_read: 0.1x
    cache_5m: 1.25x
    cache_1h: 2x
    output: output_price
    """
    total_input = rec["input_tokens"] + rec["cache_creation"] + rec["cache_read"]
    in_price, out_price = model_prices(rec["model"], total_input)
    cost = 0.0
    cost += (rec["input_tokens"] / 1_000_000) * in_price
    cost += (rec["cache_read"] / 1_000_000) * in_price * 0.1
    cost += (rec["cache_5m"] / 1_000_000) * in_price * 1.25
    cost += (rec["cache_1h"] / 1_000_000) * in_price * 2.0
    cache_uncategorized = rec["cache_creation"] - rec["cache_5m"] - rec["cache_1h"]
    if cache_uncategorized > 0:
        cost += (cache_uncategorized / 1_000_000) * in_price * 1.25
    cost += (rec["output_tokens"] / 1_000_000) * out_price
    return cost


def aggregate(since_days: int | None = None):
    since_ts = None
    if since_days:
        since_ts = datetime.now(timezone.utc) - timedelta(days=since_days)

    by_project = defaultdict(lambda: {"cost": 0.0, "input": 0, "cache_r": 0, "cache_w": 0, "output": 0, "calls": 0})
    by_model = defaultdict(lambda: {"cost": 0.0, "input": 0, "cache_r": 0, "cache_w": 0, "output": 0, "calls": 0})
    by_date = defaultdict(lambda: {"cost": 0.0, "calls": 0})
    by_session = defaultdict(lambda: {"cost": 0.0, "calls": 0, "project": "", "model": "", "start": None, "end": None})
    total = {"cost": 0.0, "input": 0, "cache_r": 0, "cache_w": 0, "output": 0, "calls": 0}

    cache = {}
    if CACHE_PATH.exists():
        try:
            cache = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except Exception:
            cache = {}

    if not CLAUDE_DIR.exists():
        print(f"AVISO: pasta de logs não encontrada: {CLAUDE_DIR}")
        print("Rode o Claude Code ao menos uma vez para gerar logs de sessão.")
        return {
            "total": total, "by_project": {}, "by_model": {}, "by_date": {},
            "by_session": {}, "file_count": 0,
            "since": since_ts.isoformat() if since_ts else None,
        }

    file_count = 0
    for root_dir in CLAUDE_DIR.iterdir():
        if not root_dir.is_dir():
            continue
        for jsonl in sorted(root_dir.glob("*.jsonl")):
            file_count += 1
            cache_key = f"{jsonl}:{jsonl.stat().st_mtime:.0f}:{jsonl.stat().st_size}"
            if cache_key in cache:
                session_project_map = cache[cache_key]
            else:
                session_project_map = scan_session_projects(jsonl)
                cache[cache_key] = session_project_map
            for rec in parse_jsonl(jsonl, since_ts):
                cost = compute_cost(rec)
                project = session_project_map.get(rec["session_id"]) or classify_project(rec["cwd"])
                date = rec["timestamp"].astimezone(timezone.utc).strftime("%Y-%m-%d")

                for store, key in [(by_project, project), (by_model, rec["model"])]:
                    s = store[key]
                    s["cost"] += cost
                    s["input"] += rec["input_tokens"]
                    s["cache_r"] += rec["cache_read"]
                    s["cache_w"] += rec["cache_creation"]
                    s["output"] += rec["output_tokens"]
                    s["calls"] += 1

                by_date[date]["cost"] += cost
                by_date[date]["calls"] += 1

                ss = by_session[rec["session_id"]]
                ss["cost"] += cost
                ss["calls"] += 1
                ss["project"] = project
                ss["model"] = rec["model"]
                if ss["start"] is None or rec["timestamp"] < ss["start"]:
                    ss["start"] = rec["timestamp"]
                if ss["end"] is None or rec["timestamp"] > ss["end"]:
                    ss["end"] = rec["timestamp"]

                total["cost"] += cost
                total["input"] += rec["input_tokens"]
                total["cache_r"] += rec["cache_read"]
                total["cache_w"] += rec["cache_creation"]
                total["output"] += rec["output_tokens"]
                total["calls"] += 1

    # Persiste o cache (limpa entradas antigas)
    valid_keys = set()
    for root_dir in CLAUDE_DIR.iterdir():
        if not root_dir.is_dir():
            continue
        for jsonl in root_dir.glob("*.jsonl"):
            valid_keys.add(f"{jsonl}:{jsonl.stat().st_mtime:.0f}:{jsonl.stat().st_size}")
    cache = {k: v for k, v in cache.items() if k in valid_keys}
    try:
        CACHE_PATH.write_text(json.dumps(cache), encoding="utf-8")
    except Exception:
        pass

    return {
        "total": total,
        "by_project": dict(by_project),
        "by_model": dict(by_model),
        "by_date": dict(by_date),
        "by_session": dict(by_session),
        "file_count": file_count,
        "since": since_ts.isoformat() if since_ts else None,
    }


def fmt_usd(v): return f"${v:,.2f}"
def fmt_brl(v): return f"R$ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
def fmt_n(v): return f"{v:,}".replace(",", ".")
def fmt_m(v): return f"{v/1_000_000:,.2f} M".replace(".", ",") if v >= 1_000_000 else fmt_n(v)


def render_html(agg: dict, outpath: Path):
    usd_brl = PRICING["usd_brl"]
    priced_at = PRICING.get("_updated", "")
    priced_label = f" (preços conferidos em {priced_at})" if priced_at else ""
    total = agg["total"]
    brl_total = total["cost"] * usd_brl

    def _has_data(d):
        return (d.get("input", 0) + d.get("cache_r", 0) + d.get("cache_w", 0) + d.get("output", 0)) > 0

    rows_project = [x for x in sorted(agg["by_project"].items(), key=lambda x: -x[1]["cost"]) if _has_data(x[1])]
    rows_model = [x for x in sorted(agg["by_model"].items(), key=lambda x: -x[1]["cost"]) if _has_data(x[1])]
    rows_date = sorted(agg["by_date"].items())
    rows_session = sorted(agg["by_session"].items(), key=lambda x: -x[1]["cost"])[:30]

    def proj_rows():
        out = []
        for name, d in rows_project:
            pct = (d["cost"] / total["cost"] * 100) if total["cost"] else 0
            out.append(f"""<tr>
              <td>{name}</td>
              <td class="num">{fmt_usd(d['cost'])}</td>
              <td class="num">{fmt_brl(d['cost']*usd_brl)}</td>
              <td class="num">{pct:.1f}%</td>
              <td class="num">{fmt_n(d['calls'])}</td>
              <td class="num">{fmt_m(d['input']+d['cache_r']+d['cache_w'])}</td>
              <td class="num">{fmt_m(d['output'])}</td>
            </tr>""")
        return "\n".join(out)

    def model_rows():
        out = []
        for name, d in rows_model:
            pct = (d["cost"] / total["cost"] * 100) if total["cost"] else 0
            out.append(f"""<tr>
              <td><code>{name}</code></td>
              <td class="num">{fmt_usd(d['cost'])}</td>
              <td class="num">{fmt_brl(d['cost']*usd_brl)}</td>
              <td class="num">{pct:.1f}%</td>
              <td class="num">{fmt_n(d['calls'])}</td>
              <td class="num">{fmt_m(d['cache_r'])}</td>
              <td class="num">{fmt_m(d['cache_w'])}</td>
              <td class="num">{fmt_m(d['output'])}</td>
            </tr>""")
        return "\n".join(out)

    def date_rows():
        out = []
        cum = 0.0
        for date, d in rows_date:
            cum += d["cost"]
            out.append(f"""<tr>
              <td>{date}</td>
              <td class="num">{fmt_usd(d['cost'])}</td>
              <td class="num">{fmt_brl(d['cost']*usd_brl)}</td>
              <td class="num">{fmt_n(d['calls'])}</td>
              <td class="num">{fmt_usd(cum)}</td>
            </tr>""")
        return "\n".join(out)

    def session_rows():
        out = []
        for sid, d in rows_session:
            dur = ""
            if d["start"] and d["end"]:
                delta = d["end"] - d["start"]
                h = int(delta.total_seconds() // 3600)
                m = int((delta.total_seconds() % 3600) // 60)
                dur = f"{h}h{m:02d}"
            start = d["start"].strftime("%Y-%m-%d %H:%M") if d["start"] else ""
            out.append(f"""<tr>
              <td><code style="font-size:10px;">{sid[:8]}</code></td>
              <td>{d['project']}</td>
              <td><code>{d['model']}</code></td>
              <td class="num">{fmt_usd(d['cost'])}</td>
              <td class="num">{fmt_brl(d['cost']*usd_brl)}</td>
              <td class="num">{fmt_n(d['calls'])}</td>
              <td>{start}</td>
              <td class="num">{dur}</td>
            </tr>""")
        return "\n".join(out)

    chart_labels = ",".join(f'"{d}"' for d, _ in rows_date)
    chart_values = ",".join(f"{d['cost']:.4f}" for _, d in rows_date)
    proj_labels = ",".join(f'"{n}"' for n, _ in rows_project[:10])
    proj_values = ",".join(f"{d['cost']:.4f}" for _, d in rows_project[:10])

    since_label = f" (últimos dias desde {agg['since']})" if agg["since"] else " (histórico completo)"

    html = f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<title>CodeBurn · custos Claude Code</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ font-family: 'Inter', -apple-system, 'Segoe UI', sans-serif; background: #0a0a0a; color: #e5e5e5; margin: 0; padding: 24px; max-width: 1400px; margin-left: auto; margin-right: auto; }}
  h1 {{ color: #f5b301; margin: 0 0 4px; font-size: 28px; }}
  .sub {{ color: #888; margin-bottom: 24px; font-size: 13px; }}
  .kpi-grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 32px; }}
  .kpi {{ background: #171717; border: 1px solid #262626; border-radius: 8px; padding: 16px; }}
  .kpi .label {{ color: #888; font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; }}
  .kpi .value {{ font-size: 24px; font-weight: 700; color: #fff; margin-top: 4px; }}
  .kpi .value.amber {{ color: #f5b301; }}
  .kpi .value.green {{ color: #4ade80; }}
  .kpi .sub {{ color: #666; font-size: 11px; margin-top: 2px; }}
  h2 {{ color: #f5b301; font-size: 18px; margin: 32px 0 12px; border-bottom: 1px solid #333; padding-bottom: 6px; }}
  table {{ width: 100%; border-collapse: collapse; background: #171717; border-radius: 8px; overflow: hidden; font-size: 13px; table-layout: auto; }}
  th {{ background: #1f1f1f; color: #f5b301; text-align: center; padding: 10px 12px; font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; border-bottom: 1px solid #333; white-space: nowrap; }}
  th:first-child {{ text-align: left; }}
  td {{ padding: 8px 12px; border-bottom: 1px solid #222; white-space: nowrap; }}
  tr:last-child td {{ border-bottom: none; }}
  tr:hover td {{ background: #1a1a1a; }}
  td.num {{ text-align: right; font-variant-numeric: tabular-nums; font-family: 'JetBrains Mono', monospace; }}
  td.pct {{ text-align: center; font-variant-numeric: tabular-nums; font-family: 'JetBrains Mono', monospace; }}
  code {{ background: #262626; padding: 2px 6px; border-radius: 3px; font-size: 11px; color: #a5f3fc; }}
  .chart-wrap {{ background: #171717; border: 1px solid #262626; border-radius: 8px; padding: 16px; margin-bottom: 16px; }}
  canvas {{ max-height: 280px; }}
  .grid-2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
  .footer {{ color: #555; font-size: 11px; text-align: center; margin-top: 40px; }}
</style>
</head>
<body>
<h1>CodeBurn</h1>
<div class="sub">Custo de tokens Claude Code{since_label}. Gerado em {datetime.now().strftime('%Y-%m-%d %H:%M')}. {agg['file_count']} arquivos JSONL lidos.</div>
<div style="background: rgba(245, 179, 1, 0.08); border-left: 3px solid #f5b301; padding: 10px 14px; margin-bottom: 20px; font-size: 12px; color: #bbb;">
  <strong style="color: #f5b301;">Nota:</strong> valores em USD são o <strong>equivalente se fosse API billing direto</strong>. Usuários Claude Pro/Max pagam subscription fixa mensal, não por token. Esta ferramenta serve como proxy de <em>intensidade de uso</em>: útil para comparar projetos entre si, não como fatura real.
</div>

<div class="kpi-grid">
  <div class="kpi">
    <div class="label">Custo total (USD)</div>
    <div class="value amber">{fmt_usd(total['cost'])}</div>
    <div class="sub">{fmt_n(total['calls'])} assistant calls</div>
  </div>
  <div class="kpi">
    <div class="label">Custo total (BRL)</div>
    <div class="value green">{fmt_brl(brl_total)}</div>
    <div class="sub">USD→BRL {usd_brl:.2f}</div>
  </div>
  <div class="kpi">
    <div class="label">Input total</div>
    <div class="value">{fmt_m(total['input'] + total['cache_r'] + total['cache_w'])}</div>
    <div class="sub">{fmt_m(total['cache_r'])} cache read · {fmt_m(total['cache_w'])} cache write</div>
  </div>
  <div class="kpi">
    <div class="label">Output total</div>
    <div class="value">{fmt_m(total['output'])}</div>
    <div class="sub">tokens gerados</div>
  </div>
</div>

<div class="grid-2">
  <div class="chart-wrap">
    <h2 style="margin-top:0;border:none;">Custo por dia</h2>
    <canvas id="chart_date"></canvas>
  </div>
  <div class="chart-wrap">
    <h2 style="margin-top:0;border:none;">Top 10 projetos</h2>
    <canvas id="chart_proj"></canvas>
  </div>
</div>

<h2>Por projeto</h2>
<table>
  <thead><tr>
    <th>Projeto</th><th class="num">USD</th><th class="num">BRL</th><th class="num">% total</th>
    <th class="num">Calls</th><th class="num">Input total</th><th class="num">Output</th>
  </tr></thead>
  <tbody>{proj_rows()}</tbody>
</table>

<h2>Por modelo</h2>
<table>
  <thead><tr>
    <th>Modelo</th><th class="num">USD</th><th class="num">BRL</th><th class="num">% total</th>
    <th class="num">Calls</th><th class="num">Cache read</th><th class="num">Cache write</th><th class="num">Output</th>
  </tr></thead>
  <tbody>{model_rows()}</tbody>
</table>

<h2>Por data</h2>
<table>
  <thead><tr>
    <th>Data</th><th class="num">USD</th><th class="num">BRL</th>
    <th class="num">Calls</th><th class="num">Cumulativo USD</th>
  </tr></thead>
  <tbody>{date_rows()}</tbody>
</table>

<h2>Top 30 sessões (por custo)</h2>
<table>
  <thead><tr>
    <th>Session ID</th><th>Projeto</th><th>Modelo principal</th>
    <th class="num">USD</th><th class="num">BRL</th><th class="num">Calls</th>
    <th>Início</th><th class="num">Duração</th>
  </tr></thead>
  <tbody>{session_rows()}</tbody>
</table>

<div class="footer">CodeBurn · pricing configurável em <code>pricing.json</code>{priced_label}.</div>

<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script>
  const tickColor = '#888', gridColor = '#262626';
  const commonPlugins = {{ legend: {{ display: false }}, tooltip: {{ callbacks: {{ label: (ctx) => '$' + ctx.parsed.y.toFixed(2) }} }} }};
  new Chart(document.getElementById('chart_date'), {{
    type: 'bar',
    data: {{ labels: [{chart_labels}], datasets: [{{ label: 'USD', data: [{chart_values}], backgroundColor: '#f5b301' }}] }},
    options: {{
      responsive: true, maintainAspectRatio: false,
      plugins: commonPlugins,
      scales: {{
        x: {{ ticks: {{ color: tickColor }}, grid: {{ color: gridColor }} }},
        y: {{ ticks: {{ color: tickColor, callback: (v) => '$' + v }}, grid: {{ color: gridColor }} }}
      }}
    }}
  }});
  new Chart(document.getElementById('chart_proj'), {{
    type: 'bar',
    data: {{ labels: [{proj_labels}], datasets: [{{ label: 'USD', data: [{proj_values}], backgroundColor: '#4ade80' }}] }},
    options: {{
      responsive: true, maintainAspectRatio: false,
      indexAxis: 'y',
      plugins: {{ legend: {{ display: false }}, tooltip: {{ callbacks: {{ label: (ctx) => '$' + ctx.parsed.x.toFixed(2) }} }} }},
      scales: {{
        x: {{ beginAtZero: true, ticks: {{ color: tickColor, callback: (v) => '$' + v }}, grid: {{ color: gridColor }} }},
        y: {{ ticks: {{ color: tickColor }}, grid: {{ color: gridColor }} }}
      }}
    }}
  }});
</script>
</body>
</html>
"""
    outpath.write_text(html, encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=None, help="Filtrar últimos N dias")
    ap.add_argument("--out", default=str(ROOT / "report.html"), help="Caminho HTML output")
    ap.add_argument("--json", default=None, help="Opcional: export JSON também")
    ap.add_argument("--open", action="store_true", help="Abrir HTML no navegador após gerar")
    args = ap.parse_args()

    print(f"Lendo JSONL de: {CLAUDE_DIR}")
    agg = aggregate(since_days=args.days)
    print(f"  arquivos lidos: {agg['file_count']}")
    print(f"  calls totais: {agg['total']['calls']:,}")
    print(f"  custo total USD: ${agg['total']['cost']:,.2f}")
    print(f"  custo total BRL: R$ {agg['total']['cost']*PRICING['usd_brl']:,.2f}")

    out = Path(args.out)
    render_html(agg, out)
    print(f"\nHTML salvo: {out}")

    if args.json:
        jpath = Path(args.json)
        def dump(obj):
            if isinstance(obj, datetime):
                return obj.isoformat()
            return obj
        jpath.write_text(json.dumps(agg, default=dump, indent=2), encoding="utf-8")
        print(f"JSON salvo: {jpath}")

    if args.open:
        import webbrowser
        webbrowser.open(out.resolve().as_uri())


if __name__ == "__main__":
    main()
