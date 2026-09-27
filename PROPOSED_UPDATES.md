# Technical Roadmap

This roadmap lists research and reliability ideas, not expected-return claims.
Every strategy change requires chronological out-of-sample and prospective
paper validation including fees, slippage, and funding.

## Reliability

- Prefer WebSocket market streams with bounded REST fallback and stale-data gates.
- Keep post-only order replacement cancel-and-verify semantics.
- Expand deterministic replay tests for disconnects, partial fills, and unknown state.
- Record structured, privacy-safe metrics without publishing account data.

## Research

- Compare static and ATR-based stop policies under identical causal datasets.
- Evaluate funding and spread as vetoes, not assumed sources of edge.
- Keep model training offline with frozen time splits and explicit leakage tests.
- Reject variants that do not pass after-cost holdout and prospective gates.

## Operations

- Keep network exposure, notifications, reviews, guardian actions, and deployments
  disabled or local by default.
- Require human review for parameter proposals and releases.
- Maintain dependency, privacy, and container checks in CI without secrets.
