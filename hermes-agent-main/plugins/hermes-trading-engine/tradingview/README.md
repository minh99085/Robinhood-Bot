# TradingView — INDEX:BTCUSD only

Polymarket BTC up/down windows settle on **Chainlink BTC/USD**. Use TradingView's
**INDEX:BTCUSD** (multi-exchange spot USD index), not Binance BTCUSDT.

## Setup

1. Add `Hermes_BTC_Pulse_v7_IndexTrend.pine` to TradingView.
2. Open **three** charts, all symbol **INDEX:BTCUSD**:
   - 5 minutes
   - 10 minutes
   - 15 minutes
3. Add the indicator to each chart.
4. Create one alert per chart: **Any alert() function call** → webhook
   `http://<vps-ip>/webhooks/tradingview`
5. Alert JSON sends `"symbol":"INDEX:BTCUSD"` and `"timeframe"` from the chart
   (5, 10, or 15).

Each timeframe is stored separately — alerts never overwrite each other. Grok sees
all three via `tradingview_trend` (`confirm_mtf` / `confirm_3tf` in status API).

## Env (VPS)

```
PULSE_TV_FEATURE_SYMBOL=BTCUSD
TRADINGVIEW_ALLOWED_SYMBOLS=BTCUSD,INDEX:BTCUSD
PULSE_TV_MTF_TIMEFRAMES=5,10,15
PULSE_TV_MTF_CONFIRM_WINDOW_S=750
PULSE_TV_MTF_CONFIRM_WINDOW_10M_S=1500
PULSE_TV_MTF_CONFIRM_WINDOW_15M_S=2250
```

Apply via `python3 /opt/Grok-Bot-2/scripts/apply-loop-arch-env.py` then
`docker compose up -d --force-recreate hermes-training`.