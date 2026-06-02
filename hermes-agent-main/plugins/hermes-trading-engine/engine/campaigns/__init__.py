"""Shared signal-model infrastructure for the Polymarket paper engine.

The controlled paper-campaign orchestrator was removed; this package now only
hosts :mod:`engine.campaigns.signal_models` (``SimulatedSignalModel``,
``ResearchSignalModel``, ``FeedbackCalibrator``, ``SignalResult``,
``build_signal_model``), which the PAPER training pipeline depends on. Import
the submodule directly, e.g. ``from engine.campaigns import signal_models``.
"""
