"""Russian spoken number & duration parser for offline voice commands."""

from __future__ import annotations

import re

_UNITS: dict[str, int] = {
    "ноль": 0, "нуль": 0, "нуля": 0,
    "один": 1, "одна": 1, "одну": 1, "одно": 1, "одного": 1, "одной": 1,
    "два": 2, "две": 2, "двух": 2,
    "три": 3, "трех": 3,
    "четыре": 4, "четырех": 4,
    "пять": 5, "пяти": 5,
    "шесть": 6, "шести": 6,
    "семь": 7, "семи": 7,
    "восемь": 8, "восьми": 8,
    "девять": 9, "девяти": 9,
}

_TEENS: dict[str, int] = {
    "десять": 10, "десяти": 10,
    "одиннадцать": 11, "одиннадцати": 11,
    "двенадцать": 12, "двенадцати": 12,
    "тринадцать": 13, "тринадцати": 13,
    "четырнадцать": 14, "четырнадцати": 14,
    "пятнадцать": 15, "пятнадцати": 15,
    "шестнадцать": 16, "шестнадцати": 16,
    "семнадцать": 17, "семнадцати": 17,
    "восемнадцать": 18, "восемнадцати": 18,
    "девятнадцать": 19, "девятнадцати": 19,
}

_TENS: dict[str, int] = {
    "двадцать": 20, "двадцати": 20,
    "тридцать": 30, "тридцати": 30,
    "сорок": 40, "сорока": 40,
    "пятьдесят": 50, "пятидесяти": 50,
    "шестьдесят": 60, "шестидесяти": 60,
    "семьдесят": 70, "семидесяти": 70,
    "восемьдесят": 80, "восьмидесяти": 80,
    "девяносто": 90, "девяноста": 90,
}

_HUNDREDS: dict[str, int] = {
    "сто": 100, "ста": 100, "сотку": 100, "сотка": 100,
    "двести": 200, "двухсот": 200,
    "триста": 300, "трехсот": 300,
    "четыреста": 400, "четырехсот": 400,
    "пятьсот": 500, "пятисот": 500,
    "шестьсот": 600, "шестисот": 600,
    "семьсот": 700, "семисот": 700,
    "восемьсот": 800, "восьмисот": 800,
    "девятьсот": 900, "девятисот": 900,
}

_SPECIAL_VOLUME: dict[str, int] = {
    "максимум": 100,
    "максимальную": 100,
    "максимальный": 100,
    "полную": 100,
    "полный": 100,
    "половину": 50,
    "половина": 50,
    "полтинник": 50,
    "минимум": 5,
    "минимальную": 5,
    "минимальный": 5,
}

_HOUR_WORDS = {"час", "часа", "часов", "часик", "часика"}
_MIN_WORDS = {"минута", "минуту", "минуты", "минут", "минутку", "минутки", "минуток"}
_SEC_WORDS = {"секунда", "секунду", "секунды", "секунд"}

_NON_WORD_RE = re.compile(r"[^\w\s]+", re.UNICODE)


def _tokenize(text: str) -> list[str]:
    cleaned = text.lower().replace("ё", "е")
    cleaned = _NON_WORD_RE.sub(" ", cleaned)
    return [tok for tok in cleaned.split() if tok]


def _consume_number(tokens: list[str], idx: int) -> tuple[int | None, int]:
    """Consume a Russian cardinal number (0..999) or integer digits starting at idx."""
    n = len(tokens)
    if idx >= n:
        return None, idx

    tok = tokens[idx]
    if tok.isdigit():
        return int(tok), idx + 1

    total = 0
    matched = False
    pos = idx

    # Optional hundreds
    if pos < n and tokens[pos] in _HUNDREDS:
        total += _HUNDREDS[tokens[pos]]
        pos += 1
        matched = True

    # Optional teens OR tens + optional units
    if pos < n and tokens[pos] in _TEENS:
        total += _TEENS[tokens[pos]]
        pos += 1
        matched = True
    else:
        if pos < n and tokens[pos] in _TENS:
            total += _TENS[tokens[pos]]
            pos += 1
            matched = True
        if pos < n and tokens[pos] in _UNITS:
            # Don't merge "ноль" onto a nonzero number like "двадцать ноль"
            if not (matched and _UNITS[tokens[pos]] == 0):
                total += _UNITS[tokens[pos]]
                pos += 1
                matched = True

    if not matched:
        return None, idx
    return total, pos


def parse_number(text: str, *, allow_volume_aliases: bool = False) -> int | None:
    """Extract the first Russian spoken number (or volume alias) from text."""
    tokens = _tokenize(text)
    if allow_volume_aliases:
        for tok in tokens:
            if tok in _SPECIAL_VOLUME:
                return _SPECIAL_VOLUME[tok]

    idx = 0
    while idx < len(tokens):
        val, next_idx = _consume_number(tokens, idx)
        if val is not None:
            return val
        idx += 1
    return None


def format_duration_ru(total_seconds: int) -> str:
    if total_seconds < 60:
        return f"{total_seconds} сек"
    total_minutes = round(total_seconds / 60)
    hours, minutes = divmod(total_minutes, 60)
    if hours and minutes:
        return f"{hours} ч {minutes} мин"
    if hours:
        return f"{hours} ч"
    return f"{minutes} мин"


def parse_duration_seconds(text: str) -> tuple[int, str] | None:
    """Parse Russian sleep-timer durations into (seconds, human_label).

    Supports:
      - "через тридцать минут" -> (1800, "30 мин")
      - "через полчаса" -> (1800, "30 мин")
      - "через полтора часа" -> (5400, "1 ч 30 мин")
      - "через час двадцать" -> (4800, "1 ч 20 мин")
      - "через два часа пятнадцать минут" -> (8100, "2 ч 15 мин")
      - "через минуту" -> (60, "1 мин")
      - "через час" -> (3600, "1 ч")
      - "через сорок пять" (bare number after "через") -> (2700, "45 мин")
    """
    tokens = _tokenize(text)
    if not tokens:
        return None

    # Prefer parsing the portion after "через" / "на" if present
    start = 0
    for marker in ("через", "на"):
        if marker in tokens:
            start = tokens.index(marker) + 1
            break
    sub = tokens[start:]
    if not sub:
        return None

    total_seconds = 0
    found_any = False
    saw_hours = False
    idx = 0
    n = len(sub)

    while idx < n:
        tok = sub[idx]

        # "полчаса"
        if tok == "полчаса":
            total_seconds += 30 * 60
            found_any = True
            idx += 1
            continue

        # "пол часа"
        if tok == "пол" and idx + 1 < n and sub[idx + 1] in _HOUR_WORDS:
            total_seconds += 30 * 60
            found_any = True
            idx += 2
            continue

        # "полтора часа" / "полторы минуты"
        if tok in {"полтора", "полторы"}:
            if idx + 1 < n and sub[idx + 1] in _HOUR_WORDS:
                total_seconds += 90 * 60
                found_any = True
                saw_hours = True
                idx += 2
                continue
            if idx + 1 < n and sub[idx + 1] in _MIN_WORDS:
                total_seconds += 90
                found_any = True
                idx += 2
                continue

        # Bare "час" / "минуту" without preceding number ("через час", "через минуту")
        if tok in _HOUR_WORDS:
            total_seconds += 3600
            found_any = True
            saw_hours = True
            idx += 1
            continue
        if tok in _MIN_WORDS:
            total_seconds += 60
            found_any = True
            idx += 1
            continue

        # Number + optional "с половиной" + unit
        num, next_idx = _consume_number(sub, idx)
        if num is not None:
            half = False
            if (
                next_idx + 1 < n
                and sub[next_idx] == "с"
                and sub[next_idx + 1] in {"половиной", "половина"}
            ):
                half = True
                next_idx += 2

            if next_idx < n and sub[next_idx] in _HOUR_WORDS:
                total_seconds += num * 3600 + (1800 if half else 0)
                found_any = True
                saw_hours = True
                idx = next_idx + 1
                continue
            if next_idx < n and sub[next_idx] in _MIN_WORDS:
                total_seconds += num * 60 + (30 if half else 0)
                found_any = True
                idx = next_idx + 1
                continue
            if next_idx < n and sub[next_idx] in _SEC_WORDS:
                total_seconds += num
                found_any = True
                idx = next_idx + 1
                continue

            # Bare number right after hours ("час двадцать") or right after "через" ("через сорок пять")
            if saw_hours or start > 0:
                total_seconds += num * 60
                found_any = True
                idx = next_idx
                continue

            idx = next_idx
            continue

        idx += 1

    if not found_any or total_seconds <= 0:
        return None
    return total_seconds, format_duration_ru(total_seconds)
