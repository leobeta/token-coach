#!/usr/bin/env python3
"""Render a local, self-contained visual Token Coach session review."""
import argparse
import datetime
import html
import json
import os
import sys

# Reports live outside the user's project so they never dirty its working tree.
REPORTS_DIRECTORY = os.path.join(os.path.expanduser("~"), ".claude", "token-coach", "reports")

SCRIPT_DIRECTORY = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIRECTORY not in sys.path:
    sys.path.insert(0, SCRIPT_DIRECTORY)
import session_stats


MAX_INSIGHT_ITEMS = 12
MAX_TEXT_LENGTH = 2000
PALETTE = ("#7dd3fc", "#a78bfa", "#34d399", "#fbbf24", "#fb7185", "#94a3b8")
# The coach recommends /compact past this request size; the chart draws it as a reference.
COMPACT_THRESHOLD_TOKENS = 100_000
# A turn whose request shrinks by this share (and at least DROP_MIN_TOKENS) looks like a compaction.
DROP_RATIO = 0.6
DROP_MIN_TOKENS = 20_000
# Idle gaps longer than the default prompt-cache TTL make the next turn re-create the cache.
IDLE_GAP_SECONDS = 5 * 60
LINE_WIDTH, LINE_HEIGHT, LINE_PAD = 700, 180, 18


def text(value, limit=MAX_TEXT_LENGTH):
    """Convert untrusted content to safe, bounded display text."""
    return html.escape(str(value or "")[:limit], quote=True)


def number(value):
    return f"{int(value):,}"


def parse_timestamp(value):
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def session_duration(stats):
    timestamps = stats.get("session_timestamps", {})
    start, end = (parse_timestamp(timestamps.get(key)) for key in ("first", "last"))
    if not start or not end or end < start:
        return None
    seconds = int((end - start).total_seconds())
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m" if hours else f"{minutes}m {seconds}s"


def token_rows(stats):
    labels = (("Input", "input"), ("Output", "output"), ("Cache read", "cache_read"),
              ("Cache creation", "cache_write"))
    tokens = stats.get("tokens", {})
    return [{"label": label, "value": tokens[key]} for label, key in labels if tokens.get(key)]


def safe_list(value):
    return value[:MAX_INSIGHT_ITEMS] if isinstance(value, list) else []


def insights_from_file(path):
    """Read a bounded optional coach handoff. Invalid content is deliberately ignored."""
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {key: safe_list(payload.get(key)) for key in
            ("findings", "recommendations", "prompt_improvements", "positives")}


def report_data(stats, insights):
    """Create the renderer's compact intermediate representation."""
    context = stats.get("context_growth", [])
    return {
        "session": {
            "transcript": stats.get("transcript", ""),
            "turns": stats.get("assistant_turns", 0),
            "prompts": stats.get("user_prompts", 0),
            "duration": session_duration(stats),
        },
        "tokens": token_rows(stats),
        "cache_hit_ratio": stats.get("cache_hit_ratio"),
        "context_growth": [point for point in context
                           if isinstance(point, dict) and point.get("request_tokens") is not None],
        "tool_activity": stats.get("tool_call_totals", []),
        "tool_payloads": stats.get("tool_result_totals", []),
        "repeated_reads": stats.get("files_read_3plus_times", {}),
        "large_results": stats.get("largest_tool_results", []),
        "prompt_stats": stats.get("prompt_stats", {}),
        "costliest_prompts": stats.get("costliest_prompts", []),
        "correction_prompts": [item.get("n") for item in stats.get("correction_examples", [])
                               if isinstance(item, dict)],
        "estimated_savings": stats.get("estimated_savings", {}),
        "insights": insights,
    }


def line_y(value, peak):
    return LINE_HEIGHT - LINE_PAD - (LINE_HEIGHT - 2 * LINE_PAD) * value / peak


def line_x(fraction):
    return LINE_PAD + (LINE_WIDTH - 2 * LINE_PAD) * fraction


def compaction_turns(values):
    """Indexes where the request size falls sharply from the previous turn."""
    return [index for index in range(1, len(values))
            if values[index - 1] - values[index] >= DROP_MIN_TOKENS
            and values[index] <= values[index - 1] * DROP_RATIO]


def line_svg(values, fractions, label, extra=""):
    """Shared line chart: threshold reference, compaction drops and the peak marker."""
    peak = max(max(values), COMPACT_THRESHOLD_TOKENS if max(values) >= COMPACT_THRESHOLD_TOKENS * 0.8 else 0) or 1
    coords = [f"{line_x(fraction):.1f},{line_y(value, peak):.1f}" for value, fraction in zip(values, fractions)]
    marks = ""
    if peak >= COMPACT_THRESHOLD_TOKENS:
        y = line_y(COMPACT_THRESHOLD_TOKENS, peak)
        marks += (f'<line x1="{LINE_PAD}" y1="{y:.1f}" x2="{LINE_WIDTH - LINE_PAD}" y2="{y:.1f}" class="threshold"/>'
                  f'<text x="{LINE_WIDTH - LINE_PAD}" y="{y - 5:.1f}" class="threshold-label">~100k: /compact suggested</text>')
    for index in compaction_turns(values):
        x, y = line_x(fractions[index]), line_y(values[index], peak)
        marks += f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5" class="drop"><title>Likely compaction at turn {index + 1}</title></circle>'
    top = values.index(max(values))
    x, y = line_x(fractions[top]), line_y(values[top], peak)
    marks += (f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5" class="peak"><title>Peak</title></circle>'
              f'<text x="{x:.1f}" y="{max(y - 9, 10):.1f}" class="peak-label">peak {number(values[top])}</text>')
    bottom = LINE_HEIGHT - LINE_PAD
    return (f'<svg class="line-chart" viewBox="0 0 {LINE_WIDTH} {LINE_HEIGHT}" role="img" aria-label="{text(label)}">'
            f'{extra}<line x1="{LINE_PAD}" y1="{bottom}" x2="{LINE_WIDTH - LINE_PAD}" y2="{bottom}" class="grid"/>'
            f'<polyline points="{" ".join(coords)}" />{marks}</svg>')


def line_legend(values):
    drops = len(compaction_turns(values))
    items = ['<li><i class="peak"></i>Peak</li>']
    if max(values) >= COMPACT_THRESHOLD_TOKENS:
        items.append('<li><i class="threshold"></i>100k reference</li>')
    if drops:
        items.append(f'<li><i class="drop"></i>Likely compaction ({drops})</li>')
    return f'<ul class="chart-legend">{"".join(items)}</ul>'


def svg_line(points):
    if len(points) < 2:
        return ""
    values = [point["request_tokens"] for point in points]
    fractions = [index / (len(values) - 1) for index in range(len(values))]
    labels = f"Turn 1 · {number(values[0])} tokens"
    labels += f"<span>Turn {len(values)} · {number(values[-1])} tokens</span>"
    return (f'<div class="chart-note">{labels}</div>'
            f'{line_svg(values, fractions, "Request tokens by assistant turn")}{line_legend(values)}')


def svg_time_line(points):
    """The same series on a wall-clock axis, shading idle gaps longer than the cache TTL."""
    timed = [(parse_timestamp(point.get("timestamp")), point["request_tokens"]) for point in points]
    timed = [(moment, value) for moment, value in timed if moment]
    if len(timed) < 2:
        return ""
    start, end = timed[0][0], timed[-1][0]
    span = (end - start).total_seconds()
    if span <= 0:
        return ""
    fractions = [(moment - start).total_seconds() / span for moment, _ in timed]
    values = [value for _, value in timed]
    gaps, extra = 0, ""
    for index in range(1, len(timed)):
        idle = (timed[index][0] - timed[index - 1][0]).total_seconds()
        if idle > IDLE_GAP_SECONDS:
            gaps += 1
            x1, x2 = line_x(fractions[index - 1]), line_x(fractions[index])
            extra += (f'<rect x="{x1:.1f}" y="{LINE_PAD}" width="{max(x2 - x1, 1):.1f}" '
                      f'height="{LINE_HEIGHT - 2 * LINE_PAD}" class="gap"><title>Idle {int(idle // 60)} min</title></rect>')
    labels = f'{start.astimezone().strftime("%H:%M")}<span>{end.astimezone().strftime("%H:%M")}</span>'
    legend = f'<ul class="chart-legend"><li><i class="gap"></i>Idle over {IDLE_GAP_SECONDS // 60} min ({gaps})</li></ul>'
    return (f'<div class="chart-note">{labels}</div>'
            f'{line_svg(values, fractions, "Request tokens over time", extra)}{legend}')


def donut(rows):
    total = sum(row["value"] for row in rows)
    if not total:
        return ""
    offset, segments = 0, []
    for index, row in enumerate(rows):
        percent = row["value"] / total * 100
        segments.append(f'<circle cx="60" cy="60" r="45" pathLength="100" '
                        f'stroke="{PALETTE[index % len(PALETTE)]}" stroke-dasharray="{percent} {100 - percent}" '
                        f'stroke-dashoffset="{-offset}" />')
        offset += percent
    legend = "".join(f'<li><i style="background:{PALETTE[index % len(PALETTE)]}"></i>{text(row["label"])} <b>{number(row["value"])}</b></li>'
                     for index, row in enumerate(rows))
    return f'<div class="donut-wrap"><svg class="donut" viewBox="0 0 120 120" role="img" aria-label="Token distribution">{"".join(segments)}<text x="60" y="57">{number(total)}</text><text x="60" y="72">tokens</text></svg><ul class="legend">{legend}</ul></div>'


def bars(rows, label, value_key, unit):
    rows = [row for row in rows if row.get(value_key)]
    if not rows:
        return ""
    peak = max(row[value_key] for row in rows) or 1
    body = "".join(
        f'<div class="bar-row"><span title="{text(row.get(label, ""))}">{text(row.get(label, ""), 80)}</span>'
        f'<div class="bar"><i style="width:{row[value_key] / peak * 100:.1f}%"></i></div>'
        f'<b>{number(row[value_key])} {unit}</b></div>' for row in rows[:8])
    return f'<div class="bars">{body}</div>'


def prompt_cost_bars(prompts, corrections):
    rows = [row for row in prompts if isinstance(row, dict) and row.get("cost_units")]
    if not rows:
        return ""
    peak = max(row["cost_units"] for row in rows) or 1
    body = ""
    for row in rows[:8]:
        correction = row.get("n") in corrections
        label = f'#{row.get("n", "?")} · {row.get("text", "")}'
        detail = f'{number(row.get("turns", 0))} turns · {number(row.get("output", 0))} output'
        body += (f'<div class="bar-row"><span title="{text(label)}">{text(label, 80)}</span>'
                 f'<div class="bar"><i class="{"alt" if correction else ""}" style="width:{row["cost_units"] / peak * 100:.1f}%"></i></div>'
                 f'<b>{number(row["cost_units"])} units</b><small>{detail}{" · correction" if correction else ""}</small></div>')
    legend = '<ul class="chart-legend"><li><i class="bar-main"></i>Prompt</li><li><i class="bar-alt"></i>Correction-style prompt</li></ul>'
    return f'<div class="bars">{body}</div>{legend}'


def savings_bars(savings):
    rows = [(item, False) for item in safe_list(savings.get("items")) if isinstance(item, dict)]
    rows += [(item, True) for item in safe_list(savings.get("at_stake_not_savings")) if isinstance(item, dict)]
    rows = [(item, at_stake) for item, at_stake in rows if item.get("tip") and item.get("pct_of_session") is not None]
    if not rows:
        return ""
    body = ""
    for item, at_stake in rows:
        percent = max(0.0, min(float(item["pct_of_session"]), 100.0))
        tag = "at stake, not savings" if at_stake else f'{item.get("confidence", "unknown")} confidence'
        body += (f'<div class="bar-row"><span title="{text(item.get("assumption", ""))}">{text(item["tip"], 80)}</span>'
                 f'<div class="bar"><i class="{"alt" if at_stake else ""}" style="width:{percent:.1f}%"></i></div>'
                 f'<b>{percent:.1f}%</b><small>{text(tag, 40)}</small></div>')
    combined = savings.get("combined_pct_text")
    note = f'<p class="chart-foot">Combined estimate: {text(combined, 80)}. Items overlap and do not add up.</p>' if combined else ""
    return f'<div class="bars">{body}</div>{note}'


def tool_cost_bars(rows):
    rows = [row for row in rows if isinstance(row, dict) and row.get("calls")]
    if not rows:
        return ""
    peak_calls = max(row["calls"] for row in rows) or 1
    peak_tokens = max(row.get("est_tokens", 0) for row in rows) or 1
    body = ""
    for row in rows[:8]:
        tokens = row.get("est_tokens", 0)
        body += (f'<div class="bar-row dual"><span>{text(row.get("tool", ""), 80)}</span>'
                 f'<div class="dual-bars"><div class="bar"><i style="width:{row["calls"] / peak_calls * 100:.1f}%"></i></div>'
                 f'<div class="bar"><i class="alt" style="width:{tokens / peak_tokens * 100:.1f}%"></i></div></div>'
                 f'<b>{number(row["calls"])} calls<br>{number(tokens)} tokens</b>'
                 f'<small>~{number(tokens / row["calls"])} tokens/call</small></div>')
    legend = '<ul class="chart-legend"><li><i class="bar-main"></i>Calls</li><li><i class="bar-alt"></i>Est. result tokens</li></ul>'
    return f'<div class="bars">{body}</div>{legend}'


def prompt_profile(stats):
    cards = (("Median prompt length", stats.get("median_words"), "words"),
             ("Very short prompts", stats.get("very_short_prompts_4_words_or_less"), "4 words or fewer"),
             ("Correction-style prompts", stats.get("correction_style_prompts"), "keyword match"),
             ("Large pastes", stats.get("pasted_large_over_2000_chars"), "over 2,000 characters"))
    body = "".join(metric(label, number(value), note) for label, value, note in cards if value is not None)
    return f'<div class="metrics">{body}</div>' if body else ""


def metric(label, value, note=""):
    return f'<article class="metric"><span>{text(label)}</span><strong>{text(value)}</strong><small>{text(note)}</small></article>'


def section(title, subtitle, content, extra=""):
    if not content:
        return ""
    return f'<section class="panel {extra}"><header><h2>{text(title)}</h2><p>{text(subtitle)}</p></header>{content}</section>'


def finding_cards(insights):
    cards = []
    for item in insights.get("findings", []):
        if not isinstance(item, dict) or not item.get("title"):
            continue
        severity = str(item.get("severity", "Observation")).upper()
        cards.append(f'''<article class="finding"><span class="badge">{text(severity, 20)}</span>
          <h3>{text(item["title"], 160)}</h3><p>{text(item.get("why", ""))}</p>
          <div><b>Recommendation</b><p>{text(item.get("recommendation", ""))}</p></div></article>''')
    return "".join(cards)


def recommendation_list(insights):
    items = []
    for item in insights.get("recommendations", []):
        if isinstance(item, dict):
            body, rank = item.get("text"), item.get("priority")
        else:
            body, rank = item, None
        if body:
            prefix = f'<span class="badge">{text(rank, 20)}</span>' if rank else ""
            items.append(f'<li>{prefix}{text(body)}</li>')
    return f'<ol class="recommendations">{"".join(items)}</ol>' if items else ""


def prompt_cards(insights):
    cards = []
    for item in insights.get("prompt_improvements", []):
        if not isinstance(item, dict) or not item.get("original") or not item.get("better"):
            continue
        cards.append(f'''<article class="prompt"><div><span>Original</span><pre>{text(item["original"])}</pre></div>
        <div><span>Better</span><pre>{text(item["better"])}</pre></div>
        <p>{text(item.get("why", ""))}</p></article>''')
    return "".join(cards)


def deterministic_observations(data):
    cards = []
    for filename, reads in list(data["repeated_reads"].items())[:5]:
        cards.append(f'<article class="observation"><b>{text(filename, 160)}</b><span>{number(reads)} reads</span><p>Repeated complete-file reads are a measurable context-growth risk; changes between reads are unknown.</p></article>')
    for item in data["large_results"][:3]:
        if item.get("est_tokens", 0):
            cards.append(f'<article class="observation"><b>{text(item.get("tool", "Tool"))}</b><span>~{number(item["est_tokens"])} result tokens</span><p>{text(item.get("target", ""), 160)}</p></article>')
    return "".join(cards)


def render(data):
    session = data["session"]
    metrics = [metric("Assistant turns", number(session["turns"])), metric("User prompts", number(session["prompts"]))]
    total = sum(row["value"] for row in data["tokens"])
    if total:
        metrics.insert(0, metric("Accounted token components", number(total), "input + output + cache components"))
    if data["cache_hit_ratio"] is not None:
        metrics.append(metric("Cache read ratio", f'{data["cache_hit_ratio"] * 100:.1f}%'))
    if session["duration"]:
        metrics.append(metric("Session duration", session["duration"], "assistant-event timestamps"))
    overview = f'<div class="metrics">{"".join(metrics)}</div>'
    charts = section("Request-token growth", "Per assistant turn: input + cache-read + cache-creation tokens. This is request usage, not context-window occupancy.", svg_line(data["context_growth"]))
    charts += section("Request tokens over time", "The same series on the clock; shaded spans are idle gaps after which the prompt cache had likely expired.", svg_time_line(data["context_growth"]))
    charts += section("Token distribution", "Only API usage components present in the transcript.", donut(data["tokens"]))
    activity = section("Tool activity", "What Claude spent tool calls doing.", bars(data["tool_activity"], "tool", "calls", "calls"))
    payloads = section("Largest tool-result payloads", "Estimated from returned text at approximately four characters per token.", bars(data["tool_payloads"], "tool", "est_tokens", "est. tokens"))
    payloads += section("Calls vs. result tokens", "Each bar is scaled to its own maximum; tokens per call shows which tool is expensive per use.", tool_cost_bars(data["tool_payloads"]))
    payloads += section("Costliest prompts", "Weighted cost units (output = 5x input, cache read = 0.1x) per prompt, with correction-style prompts highlighted.", prompt_cost_bars(data["costliest_prompts"], data["correction_prompts"]))
    payloads += section("Estimated savings by habit", "What-if estimates from the transcript, not measurements. Hover a tip for its assumption.", savings_bars(data["estimated_savings"]))
    payloads += section("Prompt profile", "How your prompts were written in this session.", prompt_profile(data["prompt_stats"]))
    observations = section("Measured inefficiencies", "Counts and estimated payload sizes, not claimed token savings.", deterministic_observations(data))
    findings = section("Coach findings", "Structured interpretation of this session.", finding_cards(data["insights"]))
    recommendations = section("Recommendations", "Ranked by the coach only where it supplied a priority.", recommendation_list(data["insights"]))
    prompts = section("Prompt improvements", "Your original wording compared with a more specific alternative.", prompt_cards(data["insights"]))
    unavailable = ""
    if not data["context_growth"]:
        unavailable += "<li>Context-growth data unavailable.</li>"
    if data["cache_hit_ratio"] is None:
        unavailable += "<li>Cache data unavailable.</li>"
    notes = f'<ul class="notes">{unavailable}</ul>' if unavailable else ""
    return f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Token Coach · Visual Review</title><style>
:root{{color-scheme:dark;--bg:#0b1020;--panel:#141c30;--muted:#95a4bd;--ink:#edf4ff;--line:#283653;--accent:#7dd3fc}}*{{box-sizing:border-box}}body{{margin:0;background:linear-gradient(135deg,#0b1020,#111b31);color:var(--ink);font:15px/1.55 ui-sans-serif,system-ui,sans-serif}}main{{max-width:1180px;margin:auto;padding:48px 24px 80px}}h1{{font-size:clamp(2rem,5vw,3.6rem);letter-spacing:-.05em;margin:4px 0}}h2{{margin:0;font-size:1.2rem}}h3{{margin:9px 0}}p{{color:var(--muted);margin:.35rem 0}}.eyebrow,.badge,.prompt span{{color:var(--accent);font-size:.72rem;font-weight:800;letter-spacing:.1em;text-transform:uppercase}}.path{{color:var(--muted);font-family:ui-monospace,monospace;overflow-wrap:anywhere}}.metrics{{display:grid;grid-template-columns:repeat(auto-fit,minmax(165px,1fr));gap:12px;margin:28px 0}}.metric,.panel,.finding,.observation,.prompt{{border:1px solid var(--line);background:rgba(20,28,48,.86);border-radius:14px}}.metric{{padding:15px}}.metric span,.metric small{{color:var(--muted);display:block;font-size:.78rem}}.metric strong{{display:block;font-size:1.55rem;margin:4px 0}}.panel{{padding:22px;margin:18px 0}}.panel header{{margin-bottom:18px}}.line-chart{{width:100%;height:auto;overflow:visible}}.line-chart .grid{{stroke:var(--line)}}.line-chart polyline{{fill:none;stroke:var(--accent);stroke-width:3;stroke-linecap:round;stroke-linejoin:round}}.chart-note{{display:flex;justify-content:space-between;color:var(--muted);font-size:.8rem}}.donut-wrap{{display:flex;align-items:center;gap:28px;flex-wrap:wrap}}.donut{{width:155px}}.donut circle{{fill:none;stroke-width:14;transform:rotate(-90deg);transform-origin:center}}.donut text{{fill:var(--ink);font-size:12px;text-anchor:middle}}.donut text+text{{fill:var(--muted);font-size:8px}}.legend{{list-style:none;margin:0;padding:0;min-width:210px}}.legend li{{display:flex;gap:8px;padding:4px 0;color:var(--muted)}}.legend i{{height:10px;width:10px;border-radius:100%;margin-top:7px}}.legend b{{color:var(--ink);margin-left:auto}}.bar-row{{display:grid;grid-template-columns:minmax(90px,180px) 1fr auto;gap:12px;align-items:center;margin:10px 0}}.bar-row span{{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--muted)}}.bar{{height:9px;background:#0d1527;border-radius:9px;overflow:hidden}}.bar i{{display:block;height:100%;background:linear-gradient(90deg,var(--accent),#a78bfa);border-radius:9px}}.bar-row b{{font-size:.8rem}}.bar-row small{{grid-column:2/-1;color:var(--muted);font-size:.74rem;margin-top:-6px}}.bar i.alt{{background:linear-gradient(90deg,#fb7185,#fbbf24)}}.dual-bars{{display:grid;gap:4px}}.line-chart .threshold{{stroke:#fbbf24;stroke-dasharray:6 5}}.line-chart .threshold-label,.line-chart .peak-label{{fill:var(--muted);font-size:11px;text-anchor:end}}.line-chart .peak-label{{text-anchor:middle;fill:var(--ink)}}.line-chart .peak{{fill:var(--accent)}}.line-chart .drop{{fill:#fb7185}}.line-chart .gap{{fill:rgba(167,139,250,.16)}}.chart-legend{{list-style:none;display:flex;flex-wrap:wrap;gap:16px;margin:10px 0 0;padding:0;color:var(--muted);font-size:.8rem}}.chart-legend i{{display:inline-block;width:10px;height:10px;border-radius:100%;margin-right:6px}}.chart-legend .peak,.chart-legend .bar-main{{background:var(--accent)}}.chart-legend .drop,.chart-legend .bar-alt{{background:#fb7185}}.chart-legend .threshold{{background:#fbbf24;border-radius:2px;height:3px;vertical-align:middle}}.chart-legend .gap{{background:rgba(167,139,250,.45);border-radius:2px}}.chart-foot{{font-size:.8rem}}.finding,.observation{{padding:16px;margin:10px 0}}.finding div{{border-top:1px solid var(--line);margin-top:12px;padding-top:10px}}.finding div b{{font-size:.75rem;color:var(--accent);text-transform:uppercase}}.observation{{display:grid;grid-template-columns:1fr auto;gap:2px 12px}}.observation p{{grid-column:1/-1}}.observation span{{color:var(--accent);font-size:.8rem}}.recommendations{{padding-left:22px}}.recommendations li{{padding:10px 0 10px 8px;color:var(--muted)}}.recommendations .badge{{display:block;margin-bottom:4px}}.prompt{{display:grid;grid-template-columns:1fr 1fr;gap:16px;padding:16px;margin:12px 0}}.prompt p{{grid-column:1/-1}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:#0c1425;border:1px solid var(--line);border-radius:8px;padding:12px;color:#dbeafe;margin:6px 0 0;font:12px/1.5 ui-monospace,monospace}}.notes{{color:var(--muted)}}@media(max-width:620px){{main{{padding:28px 15px}}.prompt{{grid-template-columns:1fr}}.bar-row{{grid-template-columns:100px 1fr}}.bar-row b{{grid-column:2}}}}
</style><main><div class="eyebrow">Local, offline visual profiler</div><h1>Token Coach review</h1><div class="path">{text(session["transcript"])}</div>{overview}{notes}{charts}{activity}{payloads}{observations}{findings}{recommendations}{prompts}<footer><p>Generated locally from Claude Code transcript data. No data is uploaded. Tool-result token figures are estimates; savings estimates, if present in coach findings, are what-if calculations rather than measurements.</p></footer></main></html>'''


def resolve_session(arguments):
    if arguments.session:
        files = session_stats.project_files()
        if not 1 <= arguments.session <= len(files):
            raise ValueError(f"session {arguments.session} out of range (1-{len(files)})")
        return files[arguments.session - 1]
    if arguments.transcript:
        return arguments.transcript
    return session_stats.project_files()[0]


def default_output(transcript):
    session = os.path.splitext(os.path.basename(transcript))[0]
    return os.path.join(REPORTS_DIRECTORY, f"{session}.html")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("transcript", nargs="?", help="Claude Code transcript JSONL path")
    parser.add_argument("--session", type=int, help="session number from the recent-session list")
    parser.add_argument("--output", help=f"HTML path (default: {REPORTS_DIRECTORY}/<session>.html)")
    parser.add_argument("--insights", help="optional JSON produced by the visual-review coach")
    arguments = parser.parse_args()
    try:
        transcript = resolve_session(arguments)
        stats = session_stats.analyse(transcript)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    output = arguments.output or default_output(transcript)
    os.makedirs(os.path.dirname(os.path.abspath(output)), exist_ok=True)
    with open(output, "w", encoding="utf-8") as handle:
        handle.write(render(report_data(stats, insights_from_file(arguments.insights))))
    print(os.path.abspath(output))


if __name__ == "__main__":
    main()
