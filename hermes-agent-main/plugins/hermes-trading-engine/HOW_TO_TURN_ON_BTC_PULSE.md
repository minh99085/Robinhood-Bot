# How to turn on BTC 5‑min Pulse (simple guide)

This is written for **non‑coders**. Just follow the steps in order.
Everything here is **paper money only** (pretend money). It can never
spend real money, never touch a wallet, and never place a real order.

---

## What you need to know first (30 seconds)

There are **two different "BTC Pulse" things**. They have **two different switches**:

| What you see | Where it lives | The switch to turn it ON |
|---|---|---|
| The **"BTC 5MIN PULSE" box on the dashboard** | the `hermes-trading-engine` container | `HTE_BTC_PULSE_PAPER_ENABLED=1` |
| The **background training experiment** (in the logs) | the `hermes-training` container | `BTC_PULSE_ENABLED=1` |

If you want the **dashboard box to place pretend bets**, use the **first** switch.
You can turn on both at the same time. Both are pretend money.

---

## Step 1 — Open the right folder

Open PowerShell and go to the project folder (the one that has the file
named `docker-compose.yml`):

```powershell
cd C:\hermes-agent-cursor\hermes-agent-main\plugins\hermes-trading-engine
```

---

## Step 2 — Create or open the settings file named `.env`

The settings file must be named exactly `.env` and live in the folder from Step 1.

If you don't have one yet, make one by copying the example:

```powershell
copy .env.example .env
```

If you already have a `.env`, that's fine — just open it to edit:

```powershell
notepad .env
```

---

## Step 3 — Turn the switches ON

In that `.env` file, make sure these lines exist and are set to these exact values.
If a line is already there but says `=0`, change it to `=1` as shown.
Add any missing lines at the bottom.

```
# Turn on the dashboard BTC 5MIN PULSE box (pretend bets)
HTE_BTC_PULSE_PAPER_ENABLED=1

# Turn on the background training experiment (optional, also pretend)
BTC_PULSE_ENABLED=1
BTC_PULSE_PAPER_ONLY=1
BTC_PULSE_ISOLATED_LEARNING=1
BTC_PULSE_LIVE_ENABLED=0
BTC_AUTOTRADE_ENABLED=0
```

Save the file and close Notepad.

> Tip: keep `BTC_PULSE_LIVE_ENABLED=0` and `BTC_AUTOTRADE_ENABLED=0`.
> If you ever set those to `1`, the app will refuse to start on purpose
> (that is the safety guard — it is doing its job).

---

## Step 4 — Restart so the new settings take effect

Run these two commands, one at a time:

```powershell
docker compose down
docker compose up -d --build
```

(`down` stops it, `up -d --build` rebuilds and starts it fresh in the background.)

---

## Step 5 — Check it worked

### Check the settings were read
```powershell
docker compose config | Select-String "BTC_PULSE"
```
You should see `HTE_BTC_PULSE_PAPER_ENABLED: "1"` and `BTC_PULSE_ENABLED: "1"`.
If they say `"0"`, your `.env` was not saved correctly — go back to Step 3.

### Watch the live logs
```powershell
docker compose logs -f hermes-training
```
Look for a line like:
```
btc_pulse: frozen=False ticks=12 decisions=3 paper_trades=1 ...
```
Press `Ctrl + C` to stop watching the logs (this does NOT stop the app).

### Open the dashboard in your browser
Go to: **http://localhost:8800**

- The **"BTC 5MIN PULSE"** box should be active.
- When a round has a good pretend bet, the **TRADES** number goes up and the
  pretend P&L changes.

---

## "It still says NO TRADE" — what that means

A "no trade" round is **usually normal, not a bug**. The bot only places a
pretend bet when the odds are actually in its favor for that 5‑minute round.
If the price looks like a coin flip, it correctly **skips** the round. It will
bet on the next round that has a real edge.

If you want it to bet **more often** (more action, slightly less picky),
add this line to `.env`, then repeat Step 4:

```
HTE_AGGRESSIVENESS=balanced
```

---

## Quick "turn it OFF" later

Open `.env`, set these back to `0`, then run Step 4 again:

```
HTE_BTC_PULSE_PAPER_ENABLED=0
BTC_PULSE_ENABLED=0
```

---

## Make training learn faster (Feedback Accelerator)

This makes the bot **learn about 10x faster** by studying almost every market it
sees — not just the ones it bets on. It is still **pretend money only**, and it
does **not** make the bot take risky bets. Safety rules stay exactly as strict.

### Step A — Open your settings file
```powershell
cd C:\hermes-agent-cursor\hermes-agent-main\plugins\hermes-trading-engine
notepad .env
```

### Step B — Add these lines at the bottom, then save
```
FEEDBACK_ACCELERATOR_ENABLED=1
EXPLORATION_ENABLED=1
EXPLORATION_TINY_SIZE_ENABLED=1
EXPLORATION_COUNTS_FOR_READINESS=0
SHADOW_DECISION_LOGGING_ENABLED=1
NO_TRADE_LABELING_ENABLED=1
ACTIVE_LEARNING_ENABLED=1
```

(If you don't want to type, run this instead — it adds them for you:)
```powershell
Add-Content .env "`nFEEDBACK_ACCELERATOR_ENABLED=1`nEXPLORATION_ENABLED=1`nEXPLORATION_TINY_SIZE_ENABLED=1`nEXPLORATION_COUNTS_FOR_READINESS=0`nSHADOW_DECISION_LOGGING_ENABLED=1`nNO_TRADE_LABELING_ENABLED=1`nACTIVE_LEARNING_ENABLED=1"
```

### Step C — Restart so it takes effect
```powershell
docker compose down
docker compose up -d --build
```

### Step D — Check it is on
```powershell
docker compose logs --tail 80 hermes-training | Select-String "feedback_accel|Feedback Accelerator"
```
You should see a line like:
```
feedback_accel: target x10 decisions/tick=1000 candidates=120 exploration=True tiny=True counts_for_readiness=False
```

### What you will and won't see
- You **will** see the number of decisions and learning samples go up a lot.
- You **may** see a few more tiny pretend trades, but only when it's safe.
- A "no trade" round is still **normal** — the bot only bets when the odds are good.
- Calibration and learning quality should improve within about **1 to 3 days**.

### Turn it OFF later
Open `.env`, set `FEEDBACK_ACCELERATOR_ENABLED=0`, then run Step C again.

---

## Turn on the market‑news scanner (optional)

This makes the bot read real news headlines about each market and feed them to
Grok as **read‑only advice**. It never trades, never spends money.

### Step A — Add these lines to `.env`
```
NEWS_SCANNER_ENABLED=1
NEWS_PROVIDER_MODE=live_read_only
```
(`live_read_only` pulls free Google News headlines — no API key needed.
Use `offline_cache` instead if you want it on but with no internet calls.)

### Step B — Restart
```powershell
docker compose up -d --build
```

### Step C — Check it's scanning
```powershell
docker compose exec hermes-training python -c "import json,os;from pathlib import Path;d=json.loads((Path(os.getenv('HTE_DATA_DIR','/data'))/'polymarket_training.json').read_text());n=d.get('news',{});print('enabled=',n.get('news_scanner_enabled'),'markets_scanned=',n.get('news_markets_scanned'),'items_used=',n.get('news_items_used'),'provider=',n.get('news_provider_mode'))"
```
- `enabled= True` and `markets_scanned` going up = it's running.
- With `live_read_only`, `items_used` should become greater than 0 as it finds headlines.
- With `offline_cache`, `items_used` stays 0 (it runs but has no news source) — that's normal.

---

## If something looks wrong, copy me these 3 outputs

1. `docker compose config | Select-String "BTC_PULSE"`
2. `docker compose logs --tail 50 hermes-training`
3. What the dashboard box shows (a screenshot is fine)

That tells me exactly what to fix.
