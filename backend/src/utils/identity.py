import uuid

_NAMESPACE = uuid.UUID("6f3c1f0e-8a0a-5f4e-9e0c-2d71b9a4c531")


def window_uid(study_code: str, window_code: str) -> str:
    """Stable source identity for a window.

    The identity is derived from the study and window code, never from the
    database id, so two offline branches that independently reuse row ids or
    numbering agreement can still be matched to the same source record.
    Duplicate numbering across libraries does not collide because the study
    code is part of the key.
    """
    return "win:" + uuid.uuid5(_NAMESPACE, f"{study_code}\x00{window_code}").hex


def package_uid() -> str:
    return "pkg-" + uuid.uuid4().hex


def branch_slug(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in value.strip())
    return cleaned.strip("-.") or "branch"
