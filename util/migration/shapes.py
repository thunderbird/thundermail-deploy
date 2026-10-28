"""Value shapes of the v0.15 dumps, read the way migrate_v016.py reads them.

unwrap_list: pv_list (migrate_v016.py:389-408). unwrap_str: pv_string (:351-369). unwrap_int: pv_int (:371-388).
duration_ms: parse_duration_ms and _DURATION_UNITS_MS (:287-294, :304-315), also accepting a space before the unit.
norm_addr: split_email (:410-418), which lowercases and strips the domain and keeps the local part.
"""
import re

DURATION_UNITS_MS = {"ms": 1, "s": 1000, "m": 60_000, "h": 3_600_000, "d": 86_400_000, "w": 604_800_000}


def unwrap_list(v):
    if v is None:
        return []
    if isinstance(v, list):
        return list(v)
    if isinstance(v, dict):
        for key in ("stringList", "integerList"):
            if isinstance(v.get(key), list):
                return list(v[key])
        if isinstance(v.get("string"), str):
            return [v["string"]]
        if isinstance(v.get("integer"), int):
            return [v["integer"]]
        return []
    return [v]


def unwrap_str(v):
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, dict):
        if isinstance(v.get("string"), str):
            return v["string"]
        if isinstance(v.get("stringList"), list) and v["stringList"]:
            return str(v["stringList"][0])
        if isinstance(v.get("integer"), int):
            return str(v["integer"])
    if isinstance(v, list) and v:
        return unwrap_str(v[0])
    return ""


def unwrap_int(v):
    if v is None:
        return None
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, int):
        return v
    if isinstance(v, str):
        try:
            return int(v)
        except ValueError:
            return None
    if isinstance(v, dict) and isinstance(v.get("integer"), int):
        return v["integer"]
    return None


def strings(v):
    return [s for s in unwrap_list(v) if isinstance(s, str)]


def duration_ms(v):
    s = str(v).strip().lower()
    m = re.fullmatch(r"(-?\d+)\s*(ms|s|m|h|d|w)?", s)
    if not m:
        raise ValueError(f"unparsed duration {v!r}")
    return int(m[1]) * DURATION_UNITS_MS[m[2] or "ms"]


def norm_domain(d):
    return d.strip().lower()


def norm_addr(addr):
    if "@" not in addr:
        return addr
    local, _, domain = addr.rpartition("@")
    return f"{local}@{norm_domain(domain)}"
