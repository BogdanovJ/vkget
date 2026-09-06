DEFAULT_STOP_WORDS = [
    "teaser",
    "тизер",
    "trailer",
    "трейлер",
    "preview",
    "promo",
    "анонс",
    "#shorts",
]

def normalize_words(extra: str = "") -> list[str]:
    words = list(DEFAULT_STOP_WORDS)
    words.extend(line.strip() for line in extra.splitlines() if line.strip())
    return words

def rejection_reason(title: str, duration: int | None, min_duration: int, extra_words: str = ""):
    if duration is not None and duration < min_duration:
        return f"duration {duration}s < minimum {min_duration}s"

    lowered = (title or "").casefold()
    for word in normalize_words(extra_words):
        if word.casefold() in lowered:
            return f'stop word "{word}"'
    return None
