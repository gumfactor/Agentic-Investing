"""Provider-agnostic constituent-source adapters.

See docs/plans/01b2-constituent-source-contract.md ("Provider-agnostic
design"). Any provider implements ``ConstituentProvider`` from ``base.py``;
the import pipeline and runtime API never reference a specific provider by
name.
"""
