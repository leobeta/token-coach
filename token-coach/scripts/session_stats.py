#!/usr/bin/env python3
"""Summarise a Claude Code session transcript for the efficiency-coach subagent.

Usage:
  session_stats.py                    newest session for this project (usually the current one)
  session_stats.py --list [N]         list the N most recent sessions (default 15), newest = 1
  session_stats.py --session K        analyse session K from the --list ordering
  session_stats.py path/to/file.jsonl analyse a specific transcript

Prints JSON (or a table for --list). Token counts come from the API usage counters
in the transcript; tool-result sizes are estimated at ~4 characters per token.
NOTE: the transcript format is an internal detail of Claude Code and may change.
"""
import collections
import datetime
import glob
import json
import os
import re
import sys

CORRECTION = re.compile(
    r"\b(no[, .]|nope|wrong|that'?s not|not what i|i meant|again|undo|revert|"
    r"instead|actually|try again|still (broken|failing|not))", re.I)

USER_SOURCES = ("typed", "suggestion_accepted")
FENCED = re.compile(r"```.*?(```|$)|<pasted_content.*?(</pasted_content[^>]*>|$)", re.S)


def is_correction(text):
    """Match correction wording in what the user typed, not in pasted logs or code."""
    return bool(CORRECTION.search(FENCED.sub(" ", text)[:500]))


# Relative prices, in units of one fresh input token (assumption: typical API ratios).
W_OUT, W_WRITE, W_READ = 5.0, 1.25, 0.1
# "Compaction" what-if: compact once context passes THRESH down to TARGET,
# paying a one-off COST for the summary and re-cache.
THRESH, TARGET, COMPACT_COST = 100_000, 20_000, 40_000


def project_files():
    root = os.path.expanduser("~/.claude/projects")
    slug = re.sub(r"[^A-Za-z0-9]", "-", os.getcwd())
    files = (glob.glob(os.path.join(root, slug, "*.jsonl"))
             or glob.glob(os.path.join(root, "*", "*.jsonl")))
    if not files:
        sys.exit("no transcript found under ~/.claude/projects "
                 "(Claude Code deletes old sessions after a retention period)")
    return sorted(files, key=os.path.getmtime, reverse=True)


def text_of(content):
    if isinstance(content, str):
        return content
    return "\n".join(b.get("text", "") for b in content
                     if isinstance(b, dict) and b.get("type") == "text")


def result_chars(content):
    if isinstance(content, str):
        return len(content)
    return sum(len(b.get("text", "")) for b in content if isinstance(b, dict))


def is_only_tool_results(content):
    return isinstance(content, list) and content and all(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in content)


def real_prompt(e):
    """Return the user's typed text for a transcript entry, or None."""
    if e.get("type") != "user" or e.get("isSidechain") or e.get("isMeta"):
        return None
    # Newer transcripts tag where a message came from; only the user's own count.
    # Older ones have no tag, so those fall through to the text checks below.
    src = e.get("promptSource")
    if src is not None and src not in USER_SOURCES:
        return None
    content = (e.get("message") or {}).get("content")
    txt = text_of(content).strip()
    if (not txt or is_only_tool_results(content)
            or txt.startswith(("<command-", "<local-command", "Caveat:"))):
        return None
    return txt


def first_prompt(path):
    try:
        with open(path) as f:
            for i, line in enumerate(f):
                if i > 300:
                    break
                try:
                    p = real_prompt(json.loads(line))
                except ValueError:
                    continue
                if p:
                    return p.replace("\n", " ")[:70]
    except OSError:
        pass
    return "(no prompt found)"


def list_sessions(limit):
    print(" #  modified          size      first prompt")
    for i, p in enumerate(project_files()[:limit], 1):
        ts = datetime.datetime.fromtimestamp(os.path.getmtime(p))
        print(f"{i:>2}  {ts:%Y-%m-%d %H:%M}  {os.path.getsize(p) // 1024:>6} KB  {first_prompt(p)}")


GIT_RE = re.compile(r"(^|[;&|]\s*)git\s+(status|diff|log|show|add|commit|push|pull|fetch|"
                    r"checkout|switch|branch|stash|merge|rebase|restore|reset)\b")
TEST_RE = re.compile(r"(^|[;&|]\s*)(pytest|python3? -m (pytest|unittest)|npm (run )?test|"
                     r"yarn test|pnpm test|go test|cargo test|mvn\S* .*\btest\b(?![-:])|gradlew? .*\btest\b|"
                     r"dotnet test|jest|vitest|make test|rspec|phpunit)\b")
SMALL_EDIT_CHARS = 200


def command_segments(cmd):
    """Split a shell command into its chained commands. Heredoc bodies are dropped,
    and a pipeline counts as its first command (so `git log | head` is just git)."""
    cmd = cmd.split("<<", 1)[0]
    segs = []
    for part in re.split(r"&&|\|\||;|\n", cmd):
        part = part.split("|", 1)[0].strip()
        if part and not part.startswith("cd "):
            segs.append(part)
    return segs


def diy_category(c):
    """Which simple developer task a tool call was, or None. A chained command only
    counts when every command in it is the same kind of simple task."""
    if c["name"] == "Bash":
        segs = command_segments(c["cmd"])
        if segs and all(GIT_RE.search(x) for x in segs):
            return "git_commands"
        if segs and all(TEST_RE.search(x) for x in segs):
            return "running_tests"
    elif c["name"] in ("Edit", "Write", "MultiEdit") and c["size"] <= SMALL_EDIT_CHARS:
        return "small_edits"
    return None


def wcost(u):
    return (u.get("output_tokens", 0) * W_OUT + u.get("input_tokens", 0)
            + u.get("cache_creation_input_tokens", 0) * W_WRITE
            + u.get("cache_read_input_tokens", 0) * W_READ)


def subagent_output_tokens(path):
    """Output tokens from <session>/subagents/*.jsonl (where newer versions store them)."""
    seen = {}
    for f in glob.glob(os.path.join(os.path.splitext(path)[0], "subagents", "*.jsonl")):
        try:
            with open(f) as fh:
                for line in fh:
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    if e.get("type") != "assistant":
                        continue
                    msg = e.get("message") or {}
                    mid = msg.get("id") or e.get("uuid")
                    out = (msg.get("usage") or {}).get("output_tokens", 0)
                    seen[mid] = max(out, seen.get(mid, 0))   # streamed blocks repeat usage
        except OSError:
            continue
    return sum(seen.values())


def analyse(path):
    asst = collections.OrderedDict()   # message id -> {model, usage}
    tool_uses = {}                     # tool_use_id -> (name, target)
    events = []                        # ("prompt", text) / ("assistant", id)
    results = []                       # (tool, target, est_tokens, turn_index)
    calls = {}                         # tool_use_id -> {name, cmd, size}
    turn_calls = {}                    # message id -> set of tool_use ids
    res_tok = {}                       # tool_use_id -> (est_tokens, turn_index)
    side_tokens = 0
    timestamps = []

    with open(path) as f:
        for line in f:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            msg = e.get("message") or {}
            kind = e.get("type")
            timestamp = e.get("timestamp")
            if kind == "assistant":
                u = msg.get("usage") or {}
                if e.get("isSidechain"):
                    side_tokens += u.get("output_tokens", 0)
                    continue
                mid = msg.get("id") or e.get("requestId") or e.get("uuid")
                cur = asst.get(mid)
                if cur is None:
                    cur = asst[mid] = {"model": msg.get("model"), "usage": {},
                                      "timestamp": timestamp}
                    events.append(("assistant", mid))
                # streamed blocks repeat usage; keep the largest output count seen
                if u.get("output_tokens", 0) >= cur["usage"].get("output_tokens", -1):
                    cur["usage"] = u
                blocks = msg.get("content")
                for b in blocks if isinstance(blocks, list) else []:
                    if b.get("type") == "tool_use":
                        inp = b.get("input", {})
                        target = (inp.get("file_path") or inp.get("command")
                                  or inp.get("pattern") or inp.get("path") or "")
                        tool_uses[b["id"]] = (b.get("name"), str(target)[:120])
                        calls[b["id"]] = {
                            "name": b.get("name"), "cmd": str(inp.get("command") or "")[:400],
                            "size": len(str(inp.get("new_string") or inp.get("content") or ""))}
                        turn_calls.setdefault(mid, set()).add(b["id"])
            elif kind == "user" and not e.get("isSidechain"):
                content = msg.get("content")
                if isinstance(content, list):
                    for b in content:
                        if isinstance(b, dict) and b.get("type") == "tool_result":
                            name, target = tool_uses.get(b.get("tool_use_id"), ("?", ""))
                            k = result_chars(b.get("content", "")) // 4
                            results.append((name, target, k, len(asst)))
                            res_tok[b.get("tool_use_id")] = (k, len(asst))
                p = real_prompt(e)
                if p:
                    events.append(("prompt", p))

    side_tokens += subagent_output_tokens(path)
    total_turns = len(asst)
    tot, models, ctx = collections.Counter(), collections.Counter(), []
    for m in asst.values():
        u = m["usage"]
        tot["input"] += u.get("input_tokens", 0)
        tot["output"] += u.get("output_tokens", 0)
        tot["cache_read"] += u.get("cache_read_input_tokens", 0)
        tot["cache_write"] += u.get("cache_creation_input_tokens", 0)
        models[m["model"]] += 1
        ctx.append(u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0)
                   + u.get("cache_creation_input_tokens", 0))
        if m.get("timestamp"):
            timestamps.append(m["timestamp"])
    total_in = tot["input"] + tot["cache_read"] + tot["cache_write"]
    session_cost = sum(wcost(m["usage"]) for m in asst.values())

    prompts, cur = [], None
    for kind, val in events:
        if kind == "prompt":
            cur = {"text": val, "output": 0, "fresh_input": 0, "turns": 0, "cost": 0.0}
            prompts.append(cur)
        elif cur is not None:
            u = asst[val]["usage"]
            cur["output"] += u.get("output_tokens", 0)
            cur["fresh_input"] += (u.get("input_tokens", 0)
                                   + u.get("cache_creation_input_tokens", 0))
            cur["cost"] += wcost(u)
            cur["turns"] += 1
    for i, p in enumerate(prompts, 1):
        p["n"] = i
    top = sorted(prompts, key=lambda p: -p["cost"])[:5]

    by_tool = collections.defaultdict(lambda: [0, 0])
    for n, _, k, _ in results:
        key = "mcp:" + n.split("__")[1] if (n or "").startswith("mcp__") and n.count("__") >= 2 else n
        by_tool[key][0] += 1
        by_tool[key][1] += k

    reads = collections.Counter(t for n, t, _, _ in results if n == "Read" and t)
    words = sorted(len(p["text"].split()) for p in prompts)
    corrections = [p for p in prompts if is_correction(p["text"])]

    # ---- savings estimates (what-ifs, in input-token-equivalents) -------------
    items = []

    by_file = collections.defaultdict(list)
    for n, t, k, ti in results:
        if n == "Read" and t:
            by_file[t].append((k, ti))
    rep = 0.0
    for lst in by_file.values():
        for k, ti in lst[1:]:   # every read after the first is the "avoidable" one
            rep += k * (W_WRITE + W_READ * max(total_turns - ti, 0))
    if rep:
        items.append({
            "tip": "Avoid re-reading files already in context",
            "est_units": rep, "confidence": "medium",
            "assumption": "Every read after the first of the same file was avoidable. "
                          "Wrong if the file changed in between. Cost counts the re-read "
                          "once plus carrying it in cache for the remaining turns."})

    stake = sum(prompts[i - 1]["cost"] for i, p in enumerate(prompts)
                if i > 0 and is_correction(p["text"]))
    if stake:
        items.append({
            "tip": "Be specific up front so fewer attempts need correcting",
            "est_units": stake, "confidence": "low",
            "assumption": "UPPER BOUND: the full cost of the attempt before each "
                          "correction-style prompt (found by keyword match, so some may "
                          "be false positives). A clearer prompt would still cost something."})

    raw = sum((c - TARGET) * W_READ for c in ctx if c > THRESH)
    if raw > COMPACT_COST:
        items.append({
            "tip": f"/compact or /clear once context passes ~{THRESH // 1000}k tokens",
            "est_units": raw - COMPACT_COST, "confidence": "medium",
            "assumption": f"Context shrinks to ~{TARGET // 1000}k at the threshold; net of "
                          f"~{COMPACT_COST // 1000}k units for the summary. Assumes nothing "
                          "important was lost in compaction."})

    # ---- "do it yourself" what-if: simple tasks a developer could run directly ----
    diy = {}
    for mid, ids in turn_calls.items():
        cats = {i: diy_category(calls[i]) for i in ids}
        if not all(cats.values()):
            continue   # turn also did other work, so it would still have happened
        turn_cost = wcost(asst[mid]["usage"]) / len(ids)
        for i, cat in cats.items():
            k, ti = res_tok.get(i, (0, total_turns))
            d = diy.setdefault(cat, {"calls": 0, "units": 0.0, "examples": []})
            d["calls"] += 1
            d["units"] += turn_cost + k * (W_WRITE + W_READ * max(total_turns - ti, 0))
            if len(d["examples"]) < 3:
                d["examples"].append((calls[i]["cmd"] or calls[i]["name"])[:80])
    diy_total = min(sum(d["units"] for d in diy.values()), session_cost)
    for d in diy.values():
        d["units"] = round(min(d["units"], session_cost))
        d["pct_of_session"] = round(100 * d["units"] / session_cost, 1) if session_cost else 0
    diy_block = {
        "by_category": diy,
        "total_units": round(diy_total),
        "total_pct_of_session": round(100 * diy_total / session_cost, 1) if session_cost else 0,
        "note": "UPPER BOUND. Counts only turns where every tool call was a simple developer "
                "task: the turn's own cost plus carrying its output in context. If the "
                "developer must paste results back (e.g. a failing test) or the next step "
                "needs the output, part of this comes back. Overlaps the other items, so "
                "never add it to combined_pct_range.",
    }

    for it in items:
        it["est_units"] = round(min(it["est_units"], session_cost))   # cannot exceed total
        it["pct_of_session"] = round(100 * it["est_units"] / session_cost, 1) if session_cost else 0
    items.sort(key=lambda i: -i["est_units"])
    # Low-confidence figures are "at stake", not savings: report them separately.
    at_stake = [i for i in items if i["confidence"] == "low"]
    savings = [i for i in items if i["confidence"] != "low"]
    low = min(savings[0]["pct_of_session"], 90) if savings else 0
    high = max(low, min(sum(i["pct_of_session"] for i in savings), 90))

    return {
        "transcript": path,
        "assistant_turns": total_turns,
        "user_prompts": len(prompts),
        "models": dict(models),
        "tokens": dict(tot),
        "cache_hit_ratio": round(tot["cache_read"] / total_in, 3) if total_in else None,
        "context_tokens": {"first": ctx[0] if ctx else 0, "peak": max(ctx, default=0),
                           "last": ctx[-1] if ctx else 0},
        "context_growth": [
            {"turn": i, "request_tokens": value,
             "timestamp": asst[mid].get("timestamp")}
            for i, (mid, value) in enumerate(zip(asst, ctx), 1)
        ],
        "session_timestamps": {"first": timestamps[0] if timestamps else None,
                               "last": timestamps[-1] if timestamps else None},
        "subagent_output_tokens": side_tokens,
        "largest_tool_results": [
            {"tool": n, "target": t, "est_tokens": k}
            for n, t, k, _ in sorted(results, key=lambda r: -r[2])[:5]],
        "tool_result_totals": [
            {"tool": t, "calls": v[0], "est_tokens": v[1]}
            for t, v in sorted(by_tool.items(), key=lambda kv: -kv[1][1])[:8]],
        "tool_call_totals": [
            {"tool": tool, "calls": count}
            for tool, count in sorted(collections.Counter(
                call["name"] for call in calls.values() if call["name"]
            ).items(), key=lambda item: (-item[1], item[0]))[:12]],
        "files_read_3plus_times": {k: v for k, v in reads.items() if v >= 3},
        "prompt_stats": {
            "median_words": words[len(words) // 2] if words else 0,
            "very_short_prompts_4_words_or_less": sum(1 for w in words if w <= 4),
            "correction_style_prompts": len(corrections),
            "pasted_large_over_2000_chars": sum(1 for p in prompts if len(p["text"]) > 2000),
        },
        "costliest_prompts": [
            {"n": p["n"], "turns": p["turns"], "output": p["output"],
             "fresh_input": p["fresh_input"], "cost_units": round(p["cost"]),
             "text": p["text"][:300]} for p in top],
        "correction_examples": [{"n": p["n"], "text": p["text"][:200]}
                                for p in corrections[:5]],
        "estimated_savings": {
            "unit": "input-token-equivalents (output=5x, cache write=1.25x, cache read=0.1x)",
            "session_total_units": round(session_cost),
            "items": savings,
            "combined_pct_range": [low, round(high, 1)],
            "combined_pct_text": ("no clear savings found" if not high
                                  else f"about {low:g}%" if round(high, 1) == low
                                  else f"roughly {low:g}-{round(high, 1):g}%"),
            "at_stake_not_savings": at_stake,
            "do_it_yourself": diy_block,
            "note": "Items overlap, so do not add them: the real figure likely lies between "
                    "the largest single item and the capped sum. 'at_stake' items are what a "
                    "failed attempt cost, not what a better prompt would have saved, and are "
                    "excluded from the range. All figures are what-if estimates, not measurements.",
        },
    }


def main():
    args = sys.argv[1:]
    if args and args[0] == "--list":
        return list_sessions(int(args[1]) if len(args) > 1 else 15)
    if args and args[0] == "--session":
        files = project_files()
        k = int(args[1])
        if not 1 <= k <= len(files):
            sys.exit(f"session {k} out of range (1-{len(files)}); try --list")
        path = files[k - 1]
    elif args:
        path = args[0]
        if not os.path.exists(path):
            sys.exit(f"transcript not found: {path}")
    else:
        path = project_files()[0]
    print(json.dumps(analyse(path), indent=2))


if __name__ == "__main__":
    main()
