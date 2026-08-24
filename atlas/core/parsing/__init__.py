"""Pure candidate parsing. No I/O, no network — safe to run in every gate."""
from atlas.core.parsing.candidates import (
    PARSER_NAMES, ParseResult, parse_adjacent, parse_body, parse_html_table,
    parse_json_path, valid_ip, valid_port,
)

__all__ = [
    "PARSER_NAMES", "ParseResult", "parse_body", "parse_adjacent",
    "parse_json_path", "parse_html_table", "valid_ip", "valid_port",
]
