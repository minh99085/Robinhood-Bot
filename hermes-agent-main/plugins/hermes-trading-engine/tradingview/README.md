# TradingView — INDEX:BTCUSD only

Polymarket BTC up/down windows settle on **Chainlink BTC/USD**. Use TradingView's
**INDEX:BTCUSD** (multi-exchange spot USD index), not Binance BTCUSDT.

## Setup

1. Add `Hermes_BTC_Pulse_v7_IndexTrend.pine` to TradingView.
2. Open **four** charts, all symbol **INDEX:BTCUSD**:
   - 1 minute
   - 5 minutes
   - 10 minutes
   - 15 minutes
3. Add the indicator to each chart.
4. Create one alert per chart: **Any alert() function call** → webhook
   `http://<vps-ip>/webhooks/tradingview`
5. Alert JSON sends `"symbol":"INDEX:BTCUSD"` and `"timeframe"` from the chart.

The bot cross-confirms all four timeframes (`confirm_4tf` in status API).

## Env (VPS)

```
PULSE_TV_FEATURE_SYMBOL=BTCUSD
TRADINGVIEW_ALLOWED_SYMBOLS=BTCUSD,INDEX:BTCUSD
```

Apply via `python3 /opt/Grok-Bot-2/scripts/apply-loop-arch-env.py` then
`docker compose up -d --force-recreate hermes-training`.