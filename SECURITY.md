# Security Policy

## Reporting

Please use the repository host's private security-advisory feature. Do not open
a public issue containing credentials, wallet identifiers, private endpoints,
account data, or exploitable details. Maintainers should acknowledge reports on
a best-effort basis; this project has no paid support or response-time SLA.

## Safe operation

The default process is a synthetic dashboard and cannot trade. Operational API
routes require `OPERATIONAL_API_TOKEN`; public routes return synthetic data.
The server binds to `127.0.0.1` unless `API_HOST` is explicitly changed.

Live trading is high risk and requires all of the following: `DEMO_MODE=false`,
`HL_ENABLE_LIVE_TRADING=true`, the explicit `--live` runtime flag, a valid
`HL_NETWORK`, and valid signing credentials. Guardian close actions additionally
require `GUARDIAN_ENABLE_LIVE_ACTIONS=true` and the launcher's explicit
`--live-actions` runtime flag. Do not expose the API directly to
the internet; use a TLS reverse proxy, network access controls, and a strong
operational token.

Never commit `.env`, keys, tokens, account logs, or generated datasets. Public
CI has no weekly account-review/upload job; account-derived review is available
only through the explicitly opted-in local `weekly_review.sh` path.

## History/privacy release gate

**The current Git history is not publishable.** `python scripts/privacy_scan.py`
checks the tracked checkout, all local refs and blobs, and commit/tag metadata;
it reports categories and locations without printing matched values. A release
must remain blocked until this full-history scan passes after either creating a
fresh clean public history (preferred) or completing a coordinated history
rewrite and re-cloning it. Separately inspect/remediate remote refs, forks,
release assets, caches, and CI artifacts because rewriting local Git objects does
not remove those copies.
