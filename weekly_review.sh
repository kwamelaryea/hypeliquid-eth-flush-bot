#!/bin/bash
# Optional local, proposal-only review. It never deploys, commits, pushes, or
# contacts an account endpoint unless the operator supplies the required env.
set -euo pipefail

if [[ "${ENABLE_EXTERNAL_REVIEW:-false}" != "true" ]]; then
  echo "External review disabled. Set ENABLE_EXTERNAL_REVIEW=true deliberately."
  exit 0
fi
if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
  echo "External review requested but model API credential is not configured." >&2
  exit 1
fi

cd "$(dirname "$0")"
export LOCAL_PRIVATE_REVIEW=true
python3 weekly_review_agent.py
