---
description: Generate a local visual profiler for a Claude Code session
argument-hint: "[session number | transcript path]"
---

Use the token-coach:visual-review-coach subagent to create a visual review for the
session specified by "$ARGUMENTS". With no argument, review the current session.

The helper scripts are at ${CLAUDE_PLUGIN_ROOT}/scripts/session_stats.py and
${CLAUDE_PLUGIN_ROOT}/scripts/visual_review.py. Tell the subagent both paths.

When it finishes, show its short completion message, including the local HTML path.
