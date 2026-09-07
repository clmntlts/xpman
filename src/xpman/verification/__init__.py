"""Integration verification: cross-check an xpman ``events.csv`` against the BioSemi ``.bdf`` it was
recorded into, and emit one self-contained interactive HTML report.

See :mod:`xpman.verification.integration_report` for the report generator (``generate_report`` and
its CLI) and :mod:`xpman.verification.gui` for the file-picker desktop front-end packaged as the
``xpman-verify`` app. Imports are kept out of this ``__init__`` on purpose so ``python -m
xpman.verification.integration_report`` runs without a re-import warning.
"""
