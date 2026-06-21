"""Option chain archive snapshotter daemon.

Long-running service that snapshots SPX/NDX/RUT/VIX option chains into
TimescaleDB every ~10 minutes during RTH.  Runs as a Docker compose service
(``xenon-option-chain-snapshotter``) against the separate ``option_chain``
database.

Entry point:  ``python -m xenon.option_chain_snapshotter``
CLI alias:    ``xenon-option-chain-snapshotter``
"""
