"""
QQQ Defined-Risk Options Research & Paper-Trading Platform
===========================================================
Pipeline:
  Market Data → Signal Screening → Strategy Research → Backtesting
  → Independent Validation → Risk Engine → Trade Proposal → Human Approval
  → Paper Broker

Hard guarantees (enforced in code, not by agents):
  * The risk engine is deterministic and has no override path.
  * There is NO live-order code path. QQQ_ENV accepts only "research" or "paper".
  * Every entry and exit is a proposal that needs explicit human approval.
  * Any data, API or risk-control failure results in NO TRADE.
"""
