# Long-Only HyperLiquid Baseline

Status: research implementation only. New live entries remain paused. Live execution requires an explicit `HL_ENABLE_LIVE_TRADING=true` opt-in and is not enabled here.

## Decision

The bot is not being tuned into a larger multi-signal system. The recovery baseline is one ETH HyperLiquid perpetual, isolated 1x, one position maximum, long-only, fixed-risk sizing, and after-cost entry approval.

No feature is called alpha without chronological out-of-sample or prospective evidence.

## Retained

- HyperLiquid perpetual execution.
- Exchange-native reduce-only protective stop, placed only after a verified fill.
- Verified-flat close semantics.
- Independent guardian and startup/account identity checks.
- Fee, funding, transfer, and equity reconciliation.
- Completed-candle data only.
- Frozen manifests and chronological research splits.

## Disabled or removed from the baseline

- All shorts, including cascade-riding short paths.
- Uniswap/spot execution from the deployment path.
- Laddering, averaging down, reversals, and concurrent positions.
- Markov and VPIN as trading gates until independently validated.
- Canary cascade detector as an entry or sizing signal; telemetry only.
- Macro data as alpha; it may only veto entries when healthy and fail-closed.
- Automatic market fallback after a post-only timeout.
- Trailing stops and dynamic parameter mutation in the first validation pass.
- Side-agnostic or duplicate-prone liquidation clusters.

## Candidate long signal

The only candidate strategy is liquidation exhaustion with strict causal ordering:

1. Completed 5-minute candles only.
2. A normalized, side-aware long-liquidation event is observed before the signal.
3. Liquidation burst and exhaustion/reclaim conditions are evaluated using only prior completed data.
4. Higher-timeframe alignment is required: 1h EMA-50 above EMA-200 and price above EMA-200.
5. RSI is recorded as context, not treated as standalone alpha.
6. Feed age, feed health, spread, funding, and expected costs must all be known.

The current Hypothesis B result is rejected for promotion: its prospective sample has negative net PnL and profit factor below the existing gate.

## Economics

Before an order:

```text
expected gross move > fees + slippage + funding + adverse-selection buffer
expected net move / stop distance >= 1.5
```

Costs must be measured from HyperLiquid fills. Post-only is an execution option, not an assumed rebate. A timeout must cancel and abort rather than silently become a taker order.

## Sizing

- Isolated 1x.
- Initial research risk: 0.25% to 0.50% of equity per trade.
- Initial notional cap: 25% of equity; 10% for tiny-live validation.
- Quantity is the lower of fixed-risk quantity and notional-cap quantity.
- No entry if the computed size is below exchange minimums or if equity is below the floor.

## Entry kill switches

Pause new entries on stale or unhealthy price, funding, liquidation, or candle feeds; unknown order state; missing or rejected protective stop; slippage above budget; daily loss/drawdown breach; three consecutive losses; equity below floor; reconciliation residual; unexpected side or size; or unknown strategy attribution.

Existing positions remain under protective risk management.

## Promotion gates

### Accelerated validation path

The shortened path does not weaken safety or imply profitability:

- Historical chronological replay first, targeting 200+ closed trades across at least three regimes when the available data supports it. If data cannot support that sample, report the shortfall rather than extrapolating.
- Then a minimum of 30 closed prospective paper trades over 2–4 weeks with frozen parameters and an immutable manifest.
- Require positive after-cost expectancy and PF >= 1.20 in the historical holdouts and prospective sample, with complete decision, denial, order, fill, and reconciliation records.
- Hard stop and no live promotion if after-cost expectancy is non-positive or any PF gate fails; do not extend the sample by cherry-picking trades.

### Tiny live

- Explicit human approval.
- Isolated 1x ETH perp.
- 0.1% to 0.25% equity risk and 10% notional cap.
- Automatic entry pause on the first execution mismatch or unexplained fee drag.
- No scaling until live economics reproduce paper economics.
