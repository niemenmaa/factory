# Role-Based Skills for Factory Worker Agents

**Date:** 2026-03-01
**Status:** Approved
**Branch:** New branch from `main`

## Overview

Add structured workflow disciplines to Factory's agent system prompts, adapted from the superpowers skill system. No new infrastructure — skills are baked directly into each role's prompt file.

## Motivation

Current agent prompts are thin (~30 lines) and provide only role descriptions + inter-agent communication format. Agents lack structured workflows for common engineering activities (debugging, testing, verification). This leads to inconsistent quality and agents that guess-and-check rather than following disciplined processes.

## Design Decisions

- **Delivery:** Inline in system prompts (no separate skill files, no CLAUDE.md mounting)
- **Adaptation:** Trim and adapt superpowers originals — keep core discipline, remove interactive elements, rewrite for autonomous context
- **Blocking:** Agents try 2-3 alternatives before declaring blocked
- **Reviewer handback:** Reviewer hands issues back to coder via message board, blocks only for architecture concerns

## Skills Per Role

| Role | Skills |
|---|---|
| coder | TDD, Systematic Debugging, Verification Before Completion, Plan Execution |
| coder_revision | Receiving Code Review, Verification Before Completion |
| reviewer | Verification Discipline |
| devops | Systematic Debugging, Verification Before Completion |
| researcher | Research Verification |

## Universal Principles (All Roles)

- **Evidence before assertions** — never claim "done" without running verification commands and reading output
- **Try alternatives before blocking** — attempt 2-3 approaches before declaring blocked
- **Structured blocking** — when truly stuck, output structured JSON for the orchestrator

## Adaptation Rules

### Keep
- Core discipline (TDD red-green-refactor, 4-phase debugging, verification commands)
- Hard rules (no code without failing test, no success claims without evidence)
- Phase/step structure

### Remove
- Skill tool invocations (Skill, EnterPlanMode, ExitPlanMode)
- TodoWrite/TaskCreate references
- Interactive user questions
- References to other skills ("invoke writing-plans next")

### Adapt
- "Ask user" → try alternatives, then output structured block message
- "Dispatch subagent" → use message board to coordinate with other agents
- "Commit frequently" → keep (agents can commit)
- Reviewer "push back if wrong" → hand back to coder via message board with technical reasoning

## Prompt Structures

### Coder (`prompts/coder.md`, ~30 → ~250 lines)

1. Role description (existing)
2. Workflows
   - **TDD:** RED (failing test) → GREEN (minimal implementation) → REFACTOR. Iron law: no code without a failing test.
   - **Systematic Debugging:** Phase 1 (investigate) → Phase 2 (analyze patterns) → Phase 3 (single hypothesis) → Phase 4 (fix with test). Hard stop after 3 failed fixes.
   - **Plan Execution:** Follow task steps sequentially, verify each step, block if unclear after trying interpretation.
   - **Verification Before Completion:** Identify verification command, run fresh, read complete output + exit code, never claim done without evidence.
3. Inter-Agent Communication (existing, enhanced)
4. Blocking Protocol

### Coder Revision (`prompts/coder_revision.md`, ~47 → ~150 lines)

1. Role description (existing)
2. Receiving Review Feedback — read all feedback first, verify each issue against codebase, push back with reasoning if wrong, implement one at a time
3. Verification Before Completion
4. Revision Output Format (existing)
5. Inter-Agent Communication (existing)

### Reviewer (`prompts/reviewer.md`, ~64 → ~120 lines)

1. Role description (existing)
2. Review Discipline — read actual implementation (not just diff), run tests if available, verify claims in comments, note uncertainty level
3. When To Hand Back vs Block — coder-fixable issues via message board, architecture concerns escalate to human
4. Review Output Format (existing, unchanged)
5. Inter-Agent Communication (existing)

### DevOps (`prompts/devops.md`, ~31 → ~120 lines)

1. Role description (existing)
2. Systematic Debugging (same phases as coder, infra-focused)
3. Verification Before Completion — check state before AND after changes
4. Inter-Agent Communication (existing)

### Researcher (`prompts/researcher.md`, ~30 → ~80 lines)

1. Role description (existing)
2. Research Verification — multiple sources per claim, flag contradictions, cite URLs, distinguish facts from speculation
3. Inter-Agent Communication (existing)

## Blocking Protocol (All Roles)

When an agent exhausts alternatives and is truly stuck:

```json
{"type": "blocked", "reason": "Description of what's blocking", "tried": ["approach 1", "approach 2"], "needs": "What input/decision is needed"}
```

Orchestrator parses this and routes to Plane comments / Telegram.

## What Doesn't Change

- `config.py` — no schema changes
- `config.yml` — no new fields
- `runner.py` / `container.py` — prompt loading pipeline unchanged
- `prompts.py` — `load_prompt()` works as-is
- `prompts/private/` override mechanism — still works

## File Changes

| File | Change |
|---|---|
| `prompts/coder.md` | Rewrite with 4 workflow sections |
| `prompts/coder_revision.md` | Add review receiving + verification |
| `prompts/reviewer.md` | Add review discipline |
| `prompts/devops.md` | Add debugging + verification |
| `prompts/researcher.md` | Add research verification |

Five prompt files rewritten. Zero code changes.
