"""Local media path recovery helpers."""

from __future__ import annotations

import glob
import os
from pathlib import Path
import re
from urllib.parse import unquote, urlparse


CONTROL_PATH_ESCAPES = {
    "\n": r"\n",
    "\r": r"\r",
    "\t": r"\t",
    "\a": r"\a",
    "\b": r"\b",
    "\f": r"\f",
    "\v": r"\v",
}
DRIVE_RELATIVE_RE = re.compile(r"^([A-Za-z]):([^\\/].*)$", re.S)
TENCENT_FILES_RE = re.compile(r"Tencent Files[\\/]+(.+)$", re.I)


def is_recoverable_local_media_reference(source: str) -> bool:
    """Return True for local path shapes that urlparse may mistake for schemes."""

    source = str(source or "").strip().strip("\"'")
    if not source:
        return False
    return bool(DRIVE_RELATIVE_RE.match(source) or re.match(r"^[A-Za-z]:[\\/]", source))


def resolve_trusted_local_media_path(
    source: str,
    *,
    name: str = "",
    suffixes: set[str] | frozenset[str] | None = None,
) -> Path | None:
    """Resolve a trusted local media source, repairing common OneBot/NTQQ path damage."""

    suffixes = {item.lower() for item in (suffixes or set())}
    candidates = _local_path_candidates(source, name=name)
    for candidate in candidates:
        if _is_allowed_file(candidate, suffixes):
            return candidate

    filename = _candidate_filename(source, name=name)
    if not filename:
        return None

    for candidate in _tencent_file_candidates(source, filename):
        if _is_allowed_file(candidate, suffixes):
            return candidate
    return None


def _local_path_candidates(source: str, *, name: str = "") -> list[Path]:
    texts = _source_text_variants(source)
    candidates: list[Path] = []
    seen: set[str] = set()
    for text in texts:
        for candidate_text in _path_text_candidates(text):
            key = candidate_text.lower() if os.name == "nt" else candidate_text
            if key in seen:
                continue
            seen.add(key)
            candidates.append(Path(candidate_text))
    if name:
        for text in texts:
            parent = Path(text).parent
            if str(parent) not in {"", "."}:
                candidates.append(parent / name)
    return candidates


def _source_text_variants(source: str) -> list[str]:
    source = str(source or "").strip().strip("\"'")
    if not source:
        return []
    variants = [source]
    if source.startswith("file://"):
        decoded = _decode_file_uri(source)
        if decoded:
            variants.append(decoded)
    unquoted = unquote(source)
    if unquoted != source:
        variants.append(unquoted)
    repaired_controls = _repair_control_path_escapes(source)
    if repaired_controls != source:
        variants.append(repaired_controls)
        unquoted_repaired = unquote(repaired_controls)
        if unquoted_repaired != repaired_controls:
            variants.append(unquoted_repaired)
    return _dedupe_text(variants)


def _decode_file_uri(source: str) -> str:
    parsed = urlparse(source)
    if parsed.scheme.lower() != "file":
        return ""
    netloc = unquote(parsed.netloc or "")
    path = unquote(parsed.path or "")
    if re.fullmatch(r"[A-Za-z]:", netloc):
        return f"{netloc}{path}"
    if netloc and path:
        return f"//{netloc}{path}"
    if re.match(r"^/[A-Za-z]:[\\/]", path):
        return path[1:]
    return path


def _path_text_candidates(text: str) -> list[str]:
    candidates = [text]
    match = DRIVE_RELATIVE_RE.match(text)
    if match:
        candidates.append(f"{match.group(1)}:\\{match.group(2)}")
        candidates.append(f"{match.group(1)}:/{match.group(2)}")
    if "\\" in text:
        candidates.append(text.replace("\\", "/"))
    if "/" in text:
        candidates.append(text.replace("/", "\\"))
    return _dedupe_text(candidates)


def _repair_control_path_escapes(source: str) -> str:
    repaired = source
    for char, replacement in CONTROL_PATH_ESCAPES.items():
        repaired = repaired.replace(char, replacement)
    return repaired


def _candidate_filename(source: str, *, name: str = "") -> str:
    for value in (name, *_source_text_variants(source)):
        value = str(value or "").strip()
        if not value:
            continue
        filename = Path(value).name
        if filename and filename not in {".", ".."}:
            return filename
        parts = re.split(r"[\\/]+", value)
        for part in reversed(parts):
            if part:
                return part
    return ""


def _tencent_file_candidates(source: str, filename: str) -> list[Path]:
    tails = _tencent_relative_tails(source)
    roots = _tencent_file_roots()
    candidates: list[Path] = []
    for root in roots:
        for tail in tails:
            candidates.append(root / tail)
    if filename:
        for root in roots:
            candidates.extend(_search_first_matches(root, filename))
    return candidates


def _tencent_relative_tails(source: str) -> list[Path]:
    tails: list[Path] = []
    for text in _source_text_variants(source):
        match = TENCENT_FILES_RE.search(text)
        if not match:
            continue
        tail = match.group(1).strip().strip("\\/")
        if tail:
            tails.append(Path(tail))
    return _dedupe_paths(tails)


def _tencent_file_roots() -> list[Path]:
    roots: list[Path] = []
    home = Path.home()
    userprofile = Path(os.environ.get("USERPROFILE") or home)
    for base in (home, userprofile):
        roots.append(base / "Documents" / "Tencent Files")
        roots.append(base / "Tencent Files")
    if os.name == "nt":
        for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
            root = Path(f"{letter}:\\")
            roots.append(root / "Documents" / "Tencent Files")
            roots.append(root / "Tencent Files")
    return [root for root in _dedupe_paths(roots) if root.exists()]


def _search_first_matches(root: Path, filename: str, *, limit: int = 3) -> list[Path]:
    matches: list[Path] = []
    try:
        iterator = root.rglob(glob.escape(filename))
        for path in iterator:
            matches.append(path)
            if len(matches) >= limit:
                break
    except OSError:
        return matches
    return matches


def _is_allowed_file(path: Path, suffixes: set[str]) -> bool:
    try:
        if not path.is_file():
            return False
    except OSError:
        return False
    return not suffixes or path.suffix.lower() in suffixes


def _dedupe_text(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not value:
            continue
        key = value.lower() if os.name == "nt" else value
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def _dedupe_paths(values: list[Path]) -> list[Path]:
    result: list[Path] = []
    seen: set[str] = set()
    for value in values:
        key = str(value).lower() if os.name == "nt" else str(value)
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result
