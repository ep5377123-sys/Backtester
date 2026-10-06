"""Statistical studies on the data store, outside the backtest engine (Phase 13).

A study defines a per-day signal and outcome; these modules supply the rest:
- days: the trading days of the store (contract per day, RTH window, scheduled early closes) and their minute bars.
- profile: volume profiles (POC, value area) from trades or from 1-minute bars.
- stats: base rate vs conditional rate, exact one-sided binomial test, per-period gaps, the verdict rule.
- report: a self-contained HTML page for a study's results.
Studies may look ahead freely (forward outcomes): they never feed the engine.
"""
