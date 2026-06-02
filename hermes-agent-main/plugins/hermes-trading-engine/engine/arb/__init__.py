"""Cross-exchange arbitrage subsystem (PAPER).

Builds the foundation Section 6 assumed already existed, in Python, wired into
the existing engine/mode/safeguards/Grok/dashboard:

  symbol_map.SymbolMapper      unified symbol -> per-exchange pair format
  feeds.FeedAggregator         per-exchange best bid/ask (public, no key)
  universe.UniverseManager     eligible symbols (price < $120 filter)
  detector.ArbitrageDetector   scans exchanges, computes net spread, finds opps
  gateway.ExchangeGateway      PAPER order placement (simulated fills) + balances
  ledger.ArbLedger             append-only arb-trade ledger + metrics
  execution.ArbExecutionEngine pre-flight -> Grok approval -> dual-leg -> recovery

PAPER ONLY: no real order is ever sent. Live execution would require vetted,
signed exchange adapters behind the existing triple safeguard.
"""
