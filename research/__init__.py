"""Research backtest package.

The former top-level ``backtest`` package was renamed to ``research`` because
it shadowed ``backtest.py`` and broke ``python main.py backtest`` and
``selftest.py``.  The CLI facade remains ``backtest.py``; this package holds
the fuller research engine (``research.backtest``), tuning and horizon scans.
"""
