# Technical Lessons

This public document intentionally excludes account history, wallet identifiers, balances, transaction links, private infrastructure, and operator details.

## Safety and execution

- Default to synthetic demo or dry-run behavior. Live execution must require a separate explicit opt-in, a valid network selection, and validated credentials.
- Treat exchange state as unknown on read failures. Unknown position or order state must block new exposure rather than be interpreted as flat.
- Cancel and verify resting entry orders before replacements. A cancel/fill race can otherwise create duplicate exposure.
- Size entries from an explicit risk budget and stop distance, subject to a notional cap. Do not infer risk from available margin alone.
- Place exchange-native protective stops after a verified fill, verify their acceptance, and keep software exit thresholds consistent with the stop selected at entry.
- A guardian is a second execution authority. Its close actions therefore need an independent opt-in in addition to the main live-trading opt-in.

## Data and research

- Use only completed candles and causally prior events in backtests and paper evaluation.
- Include fees, slippage, funding, and adverse selection before assessing a candidate signal.
- Freeze parameters and manifests for prospective tests. Report an insufficient sample rather than extrapolating.
- Strategy logs are sensitive operational data. Keep them local and expose them only through authenticated endpoints.

## Operations and privacy

- Keep account-derived balances, positions, holdings, PnL, and logs behind authentication. Public dashboards should use clearly labeled synthetic data.
- Bind services to localhost by default. Requiring an explicit host override makes network exposure visible in deployment configuration.
- Keep secrets in environment variables or an OS keyring, never tracked files. Scan the current tree before release and scan Git history separately offline.
- Autonomous review and deployment should be proposal-only or manually triggered; code must not assume access to a contributor's infrastructure.
