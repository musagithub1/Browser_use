"""Build bounded verbatim text candidates; the model only selects IDs."""

import re


_OPERATIONS = ("press|click|open|launch|save|scroll|create|make|type|write|dictate|enter|insert|"
               "append|add|search|read|summarize|compose|draft|remember|forget|close|select|visit|navigate")
_SEQUENCE = re.compile(r"(?:(?<!\w)and\s+then\b|(?<!\w)and\b|(?<!\w)then\b|;\s*(?:(?:and\s+)?then\b)?)\s*(?=(?:"
                       + _OPERATIONS + r")\b)", re.I)
_FIELD = r"(?:search\s*(?:box|field|bar)|text\s*(?:box|field)|input\s*(?:box|field))"
# A field may have a named owner ("Google Chrome's search bar"). Do not
# consume a preceding destination preposition as part of that owner's name:
# "Hello in the morning into the search bar" still dictates "Hello in the morning".
_FIELD_OWNER = r"(?:(?!(?:into|in|to)\b)[\w][\w'’.-]*\s+){0,8}"
_DESTINATION = re.compile(
    r"\s+(?:into|in|to)\s+(?:(?:a|an|the|my|same|that|this|existing|current|previous|last|new)\s+)*"
    r"(?:" + _FIELD_OWNER + _FIELD + r"|note|document|editor)\s*[.!?]?\s*$", re.I)


def _quoted_ranges(text):
    """Return complete literal ranges and a mask for syntax outside quotes."""
    ranges = []
    masked = list(text)
    opener = None
    closing = None
    start = 0
    pairs = {'"': '"', "“": "”", "'": "'", "‘": "’"}
    for index, character in enumerate(text):
        if character not in pairs and character != closing:
            continue
        slashes = 0
        previous = index - 1
        while previous >= 0 and text[previous] == "\\":
            slashes += 1
            previous -= 1
        if slashes % 2:
            continue
        between_word_characters = (index > 0 and index + 1 < len(text)
                                   and text[index - 1].isalnum() and text[index + 1].isalnum())
        if opener is not None:
            if character == closing and not (character in {"'", "’"} and between_word_characters):
                ranges.append((start, index + 1, text[start + 1:index]))
                masked[start:index + 1] = " " * (index + 1 - start)
                opener = closing = None
        elif character in pairs:
            if character in {"'", "‘"} and index > 0 and text[index - 1].isalnum():
                continue
            opener, closing, start = character, pairs[character], index
    if opener is not None:
        masked[start:] = " " * (len(text) - start)
    return ranges, "".join(masked)


def _clauses(text):
    _, masked = _quoted_ranges(text)
    position = 0
    for match in _SEQUENCE.finditer(masked):
        clause = text[position:match.start()].strip()
        if clause:
            yield clause
        position = match.end()
    clause = text[position:].strip()
    if clause:
        yield clause


def text_candidates(text):
    values = []

    def add(value, *, literal=False):
        if not literal:
            value = _DESTINATION.sub("", value.strip()).strip()
        if value.strip() and value not in values and len(values) < 80:
            values.append(value)

    quotes, _ = _quoted_ranges(text)
    for _, _, content in quotes:
        add(content, literal=True)
    clauses = list(_clauses(text))
    patterns = [
        r"\b(?:containing|that says|saying|with the text|with text|contents?\s*:)\s+",
        r"\b(?:type|write|dictate|enter|insert|append|add)\s+",
        r"\b(?:about|on the topic of)\s+",
        r"\b(?:search(?:\s+(?:the\s+)?web)?\s+for)\s+",
        r"\b(?:called|titled|named)\s+",
    ]
    for pattern in patterns:
        for clause in clauses:
            literal_ranges, masked = _quoted_ranges(clause)
            # Match prefix syntax in original text but ignore matches inside
            # literals. Matching on the mask would consume a quoted payload
            # as whitespace and lose its exact start.
            for match in re.finditer(pattern, clause, re.I):
                if not masked[match.start():match.start() + 1].strip():
                    continue
                payload_start = match.end()
                literal = next((content for start, _, content in literal_ranges if start == payload_start), None)
                if literal is not None:
                    add(literal, literal=True)
                else:
                    add(clause[payload_start:])
    if re.search(r"\b(?:create|make|compose|draft|write|append|add)\b", text, re.I):
        # Broad drafting requests may have no quoted payload, e.g. "create a
        # shopping list". Keep the full instruction as a selectable candidate.
        add(text)
    return {f"s{i}": value for i, value in enumerate(values)}


def _trim_url_punctuation(value):
    previous = None
    while value != previous:
        previous = value
        value = value.rstrip(".,!?;:")
        for opening, closing in (("(", ")"), ("[", "]"), ("{", "}")):
            while value.endswith(closing) and value.count(closing) > value.count(opening):
                value = value[:-1]
    return value


def url_candidates(text):
    normalized = re.sub(r"\s+dot\s+", ".", text, flags=re.I)
    pattern = (r"https?://[^\s<>\"'“”]+|(?<![\w@])(?:[a-z0-9-]+\.)+[a-z]{2,}"
               r"(?::[0-9]{1,5})?(?:[/?#][^\s<>\"'“”]*)?")
    values = []
    masked = list(normalized)
    for match in re.finditer(pattern, normalized, re.I):
        value = _trim_url_punctuation(match.group())
        if not re.match(r"https?://", value, re.I):
            # Do not reinterpret an explicitly unsupported scheme as HTTPS.
            if re.search(r"[a-z][a-z0-9+.-]*://$", normalized[:match.start()], re.I):
                continue
            value = "https://" + value
        else:
            scheme, remainder = value.split("://", 1)
            value = scheme.casefold() + "://" + remainder
        if value not in values and len(values) < 80:
            values.append(value)
        masked[match.start():match.end()] = " " * (match.end() - match.start())
    sites = {"google": "https://google.com", "youtube": "https://youtube.com", "wikipedia": "https://wikipedia.org", "github": "https://github.com", "duckduckgo": "https://duckduckgo.com"}
    outside_urls = "".join(masked)
    # An installed browser's name is not an additional destination website.
    outside_urls = re.sub(r"\bgoogle\s+chrome\b", "", outside_urls, flags=re.I)
    for word, url in sites.items():
        if url not in values and len(values) < 80 and re.search(r"\b" + word + r"\b", outside_urls, re.I):
            values.append(url)
    return {f"u{i}": url for i, url in enumerate(values)}
