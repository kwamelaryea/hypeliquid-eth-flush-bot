# ETH Leverage Flush Bot

An experimental Python trading/research bot with a FastAPI dashboard, paper
collection tools, and optional Hyperliquid execution. It is educational
software, not financial advice. No profitability or fitness for live trading is
claimed.

## Safe defaults

- A clean clone starts a credential-free **synthetic demo dashboard**.
- Live trading is disabled; new entries are paused; guardian actions are off.
- Public API responses contain clearly labeled synthetic data, not account data.
- Account status, logs, denials, and fill-derived PnL require an operational token.
- The API binds to `127.0.0.1` unless exposure is explicitly configured.
- Hyperliquid testnet is the default network selection.

## Requirements and setup

Use Python 3.11:

```sh
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env  # optional reference; the app does not auto-load it
```

Export settings in your shell or deployment environment. Do not commit `.env`.

## Credential-free demo/dashboard

```sh
DEMO_MODE=true python hyperliquid_bot.py
```

Open <http://127.0.0.1:8080>. The displayed price, equity, positions, PnL, and
log are synthetic demonstration values. No exchange credentials or network
access are needed. Override `API_PORT` if required.

## Read-only paper collector

The prospective collector uses public OKX market data and submits no orders:

```sh
python prospective_collector.py --once \
  --state-dir ./data/prospective \
  --manifest ./data/prospective-manifest.json \
  --source okx/public-eth-usdt-swap-v1
```

Omit `--once` only when you intentionally want the read-only polling loop. To
run the broader bot against public market data while preserving paper behavior:

```sh
DEMO_MODE=false HL_ENABLE_LIVE_TRADING=false HL_NETWORK=testnet \
  python hyperliquid_bot.py
```

Paper mode still uses external public APIs and may fail safely when offline.
It does not submit orders.

## Optional testnet and live execution

Start on testnet and keep risk controls enabled. Live execution requires all of:

```text
DEMO_MODE=false
HL_ENABLE_LIVE_TRADING=true
HL_NETWORK=testnet        # or mainnet, selected deliberately
HYPERLIQUID_PRIVATE_KEY=...
HYPERLIQUID_ADDRESS=...   # must match the signer when provided
python hyperliquid_bot.py --live
```

**Warning:** the environment setting alone cannot submit orders: the same process
must also receive the explicit `--live` CLI/runtime opt-in. With both opt-ins,
real orders can be submitted on the selected network. Mainnet can lose all allocated funds. Review the code,
exchange permissions, leverage, order limits, and local regulations first.
Missing or mismatched credentials fail closed; live preflight is not weakened.

Guardian close orders are a separate authority and remain disabled unless both
`HL_ENABLE_LIVE_TRADING=true` and `GUARDIAN_ENABLE_LIVE_ACTIONS=true` are set,
along with the valid live configuration above. Service live mode must be launched
as `./start.sh --live`; only then can the launcher pass `--live-actions` to an
explicitly enabled guardian. `start.sh` does not restart-loop a guardian that
exits because it is disabled or misconfigured.

The legacy CCXT runner similarly requires both
`EXCHANGE_ENABLE_LIVE_TRADING=true` and `python run.py --live`, plus its API
credentials. The DEX runner requires both `DEX_ENABLE_LIVE_TRADING=true` and
`python run_dex.py --live`, plus a supported network and wallet key. Calling
`bot.py`, `dex_bot.py`, or their order helpers directly does not set the runtime
opt-in and remains non-live.

## Local/private weekly review

There is no GitHub Actions weekly-review workflow and CI never fetches or uploads
account-derived PnL, trades, logs, or proposals. The proposal generator is a
local/private operator tool only. It requires an explicit local invocation:

```sh
ENABLE_EXTERNAL_REVIEW=true ./weekly_review.sh
```

Configure account endpoints and model credentials only in that private local
environment. Generated `weekly_review_proposal*.md` files are excluded from Git
and Docker contexts; review and handle them as private account artifacts.

## Generic deployment

This public tree intentionally contains no personal deployment workflow. After
creating your own app/config outside the repository, run the checks below, then
use your deployment provider's locally installed and verified CLI. For Fly.io,
a generic manual sequence is `flyctl auth login`, `flyctl launch --no-deploy`
(or configure an existing app outside this tree), then `flyctl deploy`. Do not
commit app identifiers or deployment tokens.

## API and network exposure

Public routes:

- `/` dashboard
- `/health` minimal liveness response
- `/api/public-status` synthetic demo state
- `/api/public-pnl-history` synthetic empty history

`/api/status`, `/api/log`, `/api/denials`, and `/api/pnl-history` require either
`Authorization: Bearer <OPERATIONAL_API_TOKEN>` or `X-API-Token`. They return
503 if no token is configured. Do not expose the app directly to the internet.
To listen beyond localhost, deliberately set `API_HOST=0.0.0.0`, use a strong
random token, terminate TLS at a reverse proxy, and restrict ingress.

## Environment variables

| Variable | Safe default | Purpose |
|---|---|---|
| `DEMO_MODE` | `true` | Serve only the credential-free synthetic dashboard |
| `API_HOST` / `API_PORT` | `127.0.0.1` / `8080` | HTTP listen address |
| `BOT_DATA_DIR` | `./data` | Local logs/state directory |
| `HL_NETWORK` | `testnet` | `testnet` or explicit `mainnet` |
| `HL_ENABLE_LIVE_TRADING` | `false` | Independent Hyperliquid live order opt-in |
| `DEX_ENABLE_LIVE_TRADING` | `false` | Separate DEX swap opt-in; also requires `--live` |
| `PAUSE_NEW_ENTRIES` | `true` | Block new entries while retaining configured exits |
| `GUARDIAN_ENABLE_LIVE_ACTIONS` | `false` | Independent guardian close-order opt-in |
| `OPERATIONAL_API_TOKEN` | unset | Protect account/operations routes |
| `HYPERLIQUID_PRIVATE_KEY` | unset | Live signer credential |
| `HYPERLIQUID_ADDRESS` | unset | Optional signer identity check |
| `DAILY_REVIEW_ENABLED` | `false` | Opt in to local proposal generation |
| `TELEGRAM_NOTIFY` | `false` | Opt in to notifications |

See `.env.example` for optional RPC and research settings.

## Tests and release checks

```sh
python -m unittest discover -s tests -v
python -m compileall -q -x '(^|/)(\.git|\.venv|venv)/' .
sh -n start.sh
bash -n weekly_review.sh
python scripts/check_docker_context.py
python scripts/demo_smoke.py
python scripts/privacy_scan.py
python -m pip check
```

CI also runs `pip-audit` without secrets. If Docker is installed:

```sh
docker build -t eth-flush-bot:local .
docker run --rm -p 127.0.0.1:8080:8080 \
  -e API_HOST=0.0.0.0 eth-flush-bot:local
```

The host publish remains localhost-only; `API_HOST=0.0.0.0` is required inside
the isolated container so the mapped port is reachable.

## Troubleshooting

- **Dashboard not reachable:** confirm the bind address/port and that another
  process is not using the port. Container runs need the host override above.
- **Operational route returns 503:** configure `OPERATIONAL_API_TOKEN`.
- **Operational route returns 401:** send the same token in the bearer or token
  header; do not place it in a URL.
- **Paper preflight reports zero equity:** use `DEMO_MODE=true` for the dashboard
  or configure a paper/testnet account deliberately. The bot will not bypass the
  equity gate.
- **Live startup fails:** verify demo is off, the network value is valid, the
  signer matches the configured address, and the explicit opt-in is present.
- **Public API outage:** collectors need internet access; retry later rather than
  weakening fail-closed checks.

## History/privacy publication gate

**The current Git history is not publishable.** Do not claim publication or
release readiness from a clean working-tree scan. `python scripts/privacy_scan.py`
scans the tracked checkout, every local ref, every Git blob, and commit/tag
metadata without printing matched values. Publication remains blocked until that
full-history command passes in the exact repository intended for release.

Before publishing, either create a fresh public history from a verified clean
snapshot (preferred) or coordinate a complete history rewrite, then re-clone and
run the full scan again. Also inspect remote refs, forks, releases, caches, and CI
artifacts, which a local object scan cannot revoke or erase.

Licensed under the MIT License. See `LICENSE`.
