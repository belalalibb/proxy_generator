"""
adapters/ — implementations of core/ports (aiohttp, sqlite3, filesystem, clock).

Adapters may import core/, never api/ or engine/ (asserted by
test_adapters_do_not_import_api_or_engine).
"""
