# token-coach

`/token-coach:review` adds a token-efficiency coach to Claude Code.

Run it at the end of a session, or on a past one. A subagent reads the session
transcript, measures where tokens went, and writes a short review. It looks at:

- context growth
- cache hit ratio
- repeated file reads
- large tool results
- vague or corrected prompts
- simple tasks you could run yourself (git commands, test runs, one-line edits) and an upper-bound token saving
- plugins that might help, such as caveman or code-review-graph (suggested only when the session data supports it)

The review contains before-and-after rewrites of your own prompts, a few habits to
change, and a range of estimated savings.

## Visual Review

`/token-coach:visual-review` creates one polished, self-contained HTML report for a
session. It keeps the same coaching analysis while making the evidence easier to scan:
session metrics, request-token growth, token distribution, tool activity, measured
inefficiencies, recommendations, and prompt rewrites when supported by the session.

The report is written to `~/.claude/token-coach/reports/<session-id>.html`, outside
the project, so it never dirties your working tree; open it directly in a browser.
It works offline, uses no server or analytics, and does not upload transcript
information. Charts are rendered by local Python-generated SVG, not by the LLM. The regular `/token-coach:review` behavior is unchanged.

Plain Python does the measuring, so the numbers are exact. Claude does the
interpreting. Savings are what-if estimates from the transcript, not measurements,
and the review reports what you would pay in tokens, not dollars.

## What's in the box

```
.claude-plugin/marketplace.json        # marketplace listing
token-coach/
├── .claude-plugin/plugin.json         # plugin manifest
├── commands/review.md                 # the /token-coach:review slash command
├── agents/efficiency-coach.md         # subagent that interprets the numbers
└── scripts/session_stats.py           # measures a session transcript (Python 3, stdlib only)
```

## Requirements

- Claude Code
- `python3` on your `PATH` (no third-party packages)

## Install

```
/plugin marketplace add sajjadkarimi/token-coach
/plugin install token-coach@token-coach
/reload-plugins
```

**Updating** after the repo changes:

```
/plugin marketplace update token-coach
/plugin update token-coach
/reload-plugins
```

## Usage

| Command | What it does |
| --- | --- |
| `/token-coach:review` | Review the current (newest) session |
| `/token-coach:review list` | List recent sessions, newest = 1 |
| `/token-coach:review 3` | Review session number 3 from the list |
| `/token-coach:review path/to/transcript.jsonl` | Review a specific transcript file |
| `/token-coach:visual-review` | Generate a visual HTML review for the current session |
| `/token-coach:visual-review 3` | Generate one for session number 3 |
| `/token-coach:visual-review path/to/transcript.jsonl` | Generate one for a specific transcript |

Typical flow: finish your work, run `/token-coach:review`, read the review, apply one or
two of the habits next session.

### Running the script directly

From a clone of this repo:

```bash
python3 token-coach/scripts/session_stats.py             # newest session, JSON
python3 token-coach/scripts/session_stats.py --list      # recent sessions
python3 token-coach/scripts/session_stats.py --session 2
python3 token-coach/scripts/session_stats.py path/to.jsonl
python3 token-coach/scripts/visual_review.py path/to.jsonl --output token-coach-visual-review.html
```

## Tests

```bash
python3 -m unittest discover -s tests
```

The fixture in `tests/fixtures/` is a small synthetic transcript, so format changes in
Claude Code show up as failing tests.

## Notes and limits

- Transcripts are read from `~/.claude/projects`. Claude Code deletes old sessions
  after its retention period, so very old sessions can't be reviewed.
- Tool-result sizes are estimated at about 4 characters per token.
- The transcript format is an internal detail of Claude Code and may change; if the
  script breaks after an update, the review will say so rather than invent numbers.
- Nothing is modified: the coach only reads transcripts.

## License

See [LICENSE](LICENSE).
