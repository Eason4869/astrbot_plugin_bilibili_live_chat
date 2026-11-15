# Natural Low-Frequency Echo Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to implement inline with TDD and one final independent review.

**Goal:** Restore natural unwoken participation while preserving bounded speech and model usage.
**Architecture:** Separate successful participation timestamps from attempt throttling in Conversation. Share conservative question detection between selection and validation; bypass text-only judging for vision and clarify the existing generation prompt.
**Tech Stack:** Python, unittest, existing AstrBot interfaces, native WebUI.
**Spec:** ../specs/2026-11-15-natural-echo-design.md

## Global Constraints

- Preserve saved switches, probability, success interval, per-account limits, wake priority and preview isolation.
- Unsuccessful attempt cooldown: min(60, configured echo interval), shared across sources.
- Echo remains event driven, 2–15 characters, optional silence; no new dependencies.

## Review Focus

- Model rejection, invalid output, failure and timeout must not consume the full successful-participation interval.
- High-volume mixed-source input must not trigger a model on every event or create response bursts.
- Successful wake and preview replies must continue to suppress immediate echoes.
- Vision judging must not invent descriptions or discard frames before visual generation.
- Statement particles must be accepted without admitting explicit questions into echo replies.

## Task 1: Fix candidate selection and pacing

- [x] Add tests in tests/test_natural_echo.py for statements, questions, retry bounds and successful-reply intervals; run and confirm failures.
- [x] Modify conversation.py to share question detection and track attempt cooldown separately from last successful participation.
- [x] Verify targeted tests and existing quiet-dialogue tests.

## Task 2: Fix judgment and generation

- [x] Add asynchronous tests for rejected/silent/invalid/failed attempts, visual judgment bypass and echo prompt semantics; confirm failures.
- [x] Modify decision.py, prompts.py and main.py; preserve session send and preview paths.
- [x] Run the complete test suite and review failure/timeout/cross-source paths.

## Task 3: Explain and validate

- [x] Update README, schema hints, WebUI text and changelog to reflect successful intervals, bounded retry and visual judgment.
- [x] Run full unittest suite, Ruff, JavaScript syntax and available WebUI smoke checks.
- [x] Obtain independent code review; fix material findings with failing regressions, then deliver local changes without publishing.

## Progress

- Baseline: 133 tests passed. Sandboxed Windows asyncio hangs; outside-sandbox offline test execution works.
- Work proceeds on codex/natural-low-frequency-echo in the clean existing checkout. User's explicit repair instruction authorizes implementation; no extra design approval is needed for these scoped fixes.
- RED: new tests reproduced silent/rejected/failed retry starvation, text-only visual rejection, statement filtering and missing unwoken prompt semantics.
- GREEN: 143 tests passed including continuous 15-minute mixed-source input (silent: 16 attempts / 0 sends; successful: 4 attempts / 4 sends).
- Browser smoke passed all existing desktop/mobile interactions. JavaScript syntax checks passed. Ruff found three test-only style issues (loop closure binding and pairwise iteration), corrected before final validation.
- Independent review found one Important issue: elliptical questions such as “你呢 / 主播呢” passed the relaxed particle filter. Verified with seven failing cases, then added clause-aware filtering while preserving “我还在呢 / 我喜欢主播呢 / 你怎么这么厉害呢”.
- Review boundary: actual Bilibili/model behavior and visual-provider compatibility remain unverified; no external account/model calls were authorized or performed.
- Final review fix: elliptical-question regression RED → GREEN; full suite 144/144, Ruff clean. No deferred code-review findings. Produced a local 0.8.1 ZIP and verified its extracted tests and sources.
- Follow-up authorization: reuse the prior Git identity, commit and push the repair, wait for CI success, then publish 0.8.1. Preserve the validated behavior and configuration throughout release preparation.
