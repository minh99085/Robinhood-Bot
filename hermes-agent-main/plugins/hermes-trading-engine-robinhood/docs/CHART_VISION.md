# Chart Vision Integration

Secondary channel: **TradingView chart image → vision → MCP cross-check → Monte Carlo → decision**.

Primary channel remains the structured **TradingView webhook** path (unchanged).

## Architecture

```
Chart PNG/JPEG
    │
    ▼
analyze_tradingview_chart  (Hermes tool / POST /api/chart/analyze)
    │
    ├─ Vision backend (mock | openai | anthropic | google | xai)
    │     → ChartExtractionResult (Pydantic)
    │
    ├─ MCP validation (get_equity_quotes / historicals / portfolio)
    │     → PASSED | DOWNWEIGHTED | REJECTED | SKIPPED
    │
    ├─ Monte-Carlo-Sim chart_vision_pipeline (default 100,000 paths)
    │     → ChartTradeDecision (risk + action + size)
    │
    └─ Audit log (robinhood_audit.jsonl)
              │
              ▼
    Optional human/agent execution via SafeRobinhoodClient
    (review_* + place_* still gated — never bypassed)
```

## Hermes registration

1. Ensure the plugin directory is discoverable (already under `hermes-agent-main/plugins/`).
2. Enable in Hermes config, e.g. `~/.hermes/config.yaml`:

```yaml
plugins:
  enabled:
    - hermes-trading-engine-robinhood
```

3. Or symlink / copy into `~/.hermes/plugins/hermes-trading-engine-robinhood`.

4. Restart Hermes. Tool name: **`analyze_tradingview_chart`**.

### Agent call example

```text
Analyze this TradingView chart and recommend a position.
Use analyze_tradingview_chart with image_path=/data/charts/aapl_1h.png
and ticker_hint=AAPL. Do not place any order until I confirm.
```

### Programmatic

```python
from tools import handle_analyze_tradingview_chart
print(handle_analyze_tradingview_chart({
    "image_path": "chart.png",
    "ticker_hint": "AAPL",
    "mc_paths": 100000,
    "execution_mode": "recommendation_only",
}))
```

## HTTP API (port 8810)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/chart/config` | Effective vision/MC config |
| POST | `/api/chart/extract` | Vision only |
| POST | `/api/chart/analyze` | Full pipeline |

```bash
curl -s http://127.0.0.1:8810/api/chart/config
curl -s -X POST http://127.0.0.1:8810/api/chart/analyze \
  -H "Content-Type: application/json" \
  -d "{\"image_path\":\"/data/charts/aapl.png\",\"ticker_hint\":\"AAPL\",\"mc_paths\":5000}"
```

## Environment

See `.env.example` keys prefixed with `CHART_VISION_*` and `MONTE_CARLO_SIM_PATH`.

| Key | Default | Notes |
|-----|---------|-------|
| `CHART_VISION_PROVIDER` | `mock` | Use `openai` / `anthropic` / `google` / `xai` in prod |
| `CHART_VISION_EXECUTION_MODE` | `recommendation_only` | Safe default |
| `CHART_VISION_MC_PATHS` | `100000` | Production path count |
| `CHART_VISION_REQUIRE_MCP` | `0` | Set `1` to reject when MCP down |
| `MONTE_CARLO_SIM_PATH` | local Monte-Carlo-Sim | Required for MC decision |

## Confidence policy

- Image numbers are **soft**; MCP price/vol are authoritative.
- Material price mismatch or unconfirmed ticker → **REJECTED** → flat.
- Mid confidence → **DOWNWEIGHTED** (smaller size, softer drift).
- Design rationale: Monte-Carlo-Sim `DESIGN_CHART_VISION.md`.

## Safety

- This tool **never** calls `place_equity_order` / `place_option_order`.
- `executable=true` only in `gated_execution` after validation; agent must still use `SafeRobinhoodClient`.
- Webhook path remains primary and higher reliability.

## Tests

```bash
cd hermes-agent-main/plugins/hermes-trading-engine-robinhood
pip install -r requirements.txt -r requirements-dev.txt
# Monte-Carlo-Sim needs pydantic + numpy for full pipeline tests
python -m pytest tests/test_chart_vision.py -q
```

## Evaluation (Chart2CSV-style)

In Monte-Carlo-Sim:

```bash
python chart_vision_scoring.py --dir eval_set/
```

See `chart_vision_scoring.py` for ground-truth layout.
