import re

_UNITS = {"h": 3600, "m": 60, "s": 1}


def parse_duration(text: str) -> int:
    total = 0
    for value, unit in re.findall(r"(\d+)([hms])", text):
        total += int(value) * _UNITS[unit]
    return total
