def normalize_email(value: str) -> str:
    cleaned = value.strip().lower()
    local, separator, domain = cleaned.partition("@")
    if not separator or not local or "." not in domain:
        raise ValueError(f"{value!r} is not a valid email address")
    return cleaned
