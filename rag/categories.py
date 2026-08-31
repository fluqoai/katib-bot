"""Category inference for Kateb documents.

Maps a relative file path to one of the four skill categories. The
mapping accepts both the English folder names and the most common
Arabic equivalents so a real-world Drive/Drive-like layout works
without renaming.

The function lives outside `rag.ingest` so the Telegram bot and the
admin dashboard can import it without pulling the rest of the
ingestion stack.
"""
from __future__ import annotations

from pathlib import PurePosixPath


# Folder-name → category. Accepts both dash and underscore forms to be
# forgiving. Arabic equivalents are listed for the common organisational
# patterns Saudi non-profits actually use.
CATEGORY_BY_FOLDER: dict[str, str] = {
    # English
    "templates": "templates",
    "national_regulations": "national_regulations",
    "national-regulations": "national_regulations",
    "regulations": "national_regulations",
    "laws": "national_regulations",
    "internal_policies": "internal_policies",
    "internal-policies": "internal_policies",
    "policies": "internal_policies",
    "examples": "examples",
    # Arabic
    "نماذج": "templates",
    "قوالب": "templates",
    "لوائح": "national_regulations",
    "أنظمة": "national_regulations",
    "سياسات": "internal_policies",
    "أمثلة": "examples",
}


# Map category → Supabase Storage bucket name.
CATEGORY_TO_BUCKET: dict[str, str] = {
    "templates": "templates",
    "national_regulations": "regulations",
    "internal_policies": "policies",
    "examples": "examples",
    "other": "examples",
}


VALID_CATEGORIES: frozenset[str] = frozenset({
    "templates",
    "national_regulations",
    "internal_policies",
    "examples",
    "other",
})


def _category_from_path(path: str) -> str:
    """Infer the category from a relative file path.

    Works on both Windows (`folder\\file`) and Unix (`folder/file`) by
    normalising to forward slashes first.
    """
    normalised = path.replace("\\", "/")
    parts = [p.lower() for p in PurePosixPath(normalised).parts]
    for folder, cat in CATEGORY_BY_FOLDER.items():
        if folder.lower() in parts:
            return cat
    return "other"


__all__ = [
    "CATEGORY_BY_FOLDER",
    "CATEGORY_TO_BUCKET",
    "VALID_CATEGORIES",
    "_category_from_path",
]
