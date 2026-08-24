"""
policy/ — pure decision functions. The admission gate lives here (P06).

Kept separate from domain/ so the LIVE != GOOD rule is a testable function of data
rather than a method hidden on an object, and so the thresholds can be calibrated
against engineering/BASELINE.json without touching the data model.
"""
