"""Content quality evaluation study package.

Modules:
    config       load YAML config and price table, resolve the API key
    tokens       count or estimate token usage
    bookkeeping  separate append-only cost and token log
    poe_client   thin wrapper over the Poe OpenAI-compatible endpoint
    data         real dataset acquisition and sampling
    assignment   connected, balanced rater-pair design
    personas     the 10 biased simulated human raters
    evaluate     the judge pipeline and its cached CSV database
    normalize    two-way additive rater-bias removal
    montecarlo   random-human resampling that preserves disagreement
    metrics      correlation, ICC, the headline ratio, bootstrap, P/R/F1
"""
