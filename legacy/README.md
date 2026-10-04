# Quarantined pre-contract experiments

These modules are preserved for historical comparison and research. The operational
graph, API, provider registry and dashboard do not import them.

- `agents/base.py`: shadowed duplicate base-agent contract.
- `agents/intelligence/risk_agent.py`: superseded dictionary-based risk agent.
- `agents/mappo/`: synthetic reinforcement-learning environment and training experiment.
  Its generated demand is an experimental training assumption, never business data.
- `optimization/*_optimizer.py`: superseded permissive dictionary APIs. The original
  inventory symbolic-expression bug was repaired before archival. Production entry
  points use `optimization.engine` and canonical contracts.
- `core/schemas/`: superseded permissive contracts. Their former public module
  paths now re-export the canonical contracts, rather than competing definitions.

Do not wire this directory into operational execution. Existing ST-GNN training,
historical AIS recorder, and actual provider adapters remain outside this archive.
