"""
core/ — the pure centre. NO I/O, NO network, NO clock, NO framework.

Enforced by atlas/tests/unit/test_architecture.py, which AST-scans every module
here against an allowlist of pure-computation stdlib. The rule exists because the
legacy tree fused fetching, parsing, testing and persistence into single functions,
which is why none of its logic could be tested without a live network.

  domain/  plain immutable data (Proxy, Source, Target, Verdict, Score)
  policy/  pure decision functions (the admission gate, scoring, cooldown)
  ports/   Protocol interfaces the outer layers implement
"""
