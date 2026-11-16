"""Ownership checks for server-side stored objects (files, responses, completions).

The gateway can serve several API keys. Objects created by a key must not be
listable, downloadable, continued or deleted by another key. ``admin`` sees
everything; entries saved before ownership existed (``owner is None``) belong
to the legacy single-key identity.
"""

from __future__ import annotations


def visible(entry_owner: str | None, current: str | None) -> bool:
    if current is None or current == "admin":
        # Internal callers (background cleanup/tests) and the admin key.
        return True
    if entry_owner is None:
        return current == "legacy"
    return entry_owner == current
