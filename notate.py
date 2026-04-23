#!/usr/bin/env python3
"""Notate: simple Apple Books highlights exporter (single-file v1)."""

from __future__ import annotations

import html
import os
import posixpath
import re
import sqlite3
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote
from xml.etree import ElementTree


# ============================================================================
# Constants and simple models
# ============================================================================

APPLE_BOOKS_ROOT = Path("~/Library/Containers/com.apple.iBooksX/Data/Documents").expanduser()
BKLIBRARY_DIR = APPLE_BOOKS_ROOT / "BKLibrary"
ANNOTATION_DIR = APPLE_BOOKS_ROOT / "AEAnnotation"
EXPORTS_DIR_NAME = "exports"
SCRIPT_ROOT = Path(__file__).resolve().parent

TRUSTED_BOOK_PATH_ROOTS = (
    APPLE_BOOKS_ROOT,
    Path("~/Library/Mobile Documents/iCloud~com~apple~iBooks/Documents").expanduser(),
    Path("~/Library/Containers/com.apple.BKAgentService/Data/Documents/iBooks/Books").expanduser(),
)

MAX_TEXT_FILE_BYTES = 2 * 1024 * 1024
MAX_XML_BYTES = 2 * 1024 * 1024
MAX_ZIP_MEMBER_BYTES = 2 * 1024 * 1024
MAX_ZIP_EXPANSION_RATIO = 200

CFI_SPINE_PATTERN = re.compile(r"/6/(\d+)(?:\[([^\]]+)])?")
INT_PATTERN = re.compile(r"\d+")
ORDERED_LIST_LEAD_PATTERN = re.compile(r"^(\d+)([.)])(\s+)")
UNORDERED_LIST_LEAD_PATTERN = re.compile(r"^([*+-])(\s+)")
ANSI_ESCAPE_PATTERN = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")


@dataclass(frozen=True)
class Book:
    asset_id: str
    title: str
    path: str | None
    highlight_count: int


@dataclass(frozen=True)
class Annotation:
    highlight_text: str
    note_text: str
    location: str
    range_start: int | None
    range_end: int | None


@dataclass(frozen=True)
class Highlight:
    text: str
    note: str | None
    chapter_index: int | None
    chapter_title: str
    location: str
    order_key: tuple[int, ...]


@dataclass(frozen=True)
class ChapterGroup:
    chapter_index: int | None
    title: str
    highlights: list[Highlight]


@dataclass(frozen=True)
class ManifestItem:
    href: str
    media_type: str
    properties: str


def warn(message: str) -> None:
    print(f"[notate] {message}", file=sys.stderr)


def sanitize_terminal_text(text: str) -> str:
    no_ansi = ANSI_ESCAPE_PATTERN.sub("", text)
    return "".join(ch for ch in no_ansi if ch.isprintable() or ch in {" ", "\t"})


def is_trusted_book_path(path: Path) -> bool:
    if path.suffix.lower() != ".epub":
        return False

    try:
        resolved_path = path.resolve(strict=False)
    except OSError:
        return False

    for root in TRUSTED_BOOK_PATH_ROOTS:
        try:
            resolved_root = root.resolve(strict=False)
        except OSError:
            continue
        if resolved_root in resolved_path.parents or resolved_path == resolved_root:
            return True
    return False


def is_safe_relative_path(path: str) -> bool:
    if not path:
        return False
    if path.startswith("/"):
        return False

    normalized = posixpath.normpath(path)
    if normalized in {"", "."}:
        return False
    if normalized.startswith("../"):
        return False
    return True


# ============================================================================
# Database discovery and reading
# ============================================================================

def discover_databases() -> tuple[Path, Path]:
    library_db = latest_sqlite(BKLIBRARY_DIR, "BKLibrary-*.sqlite")
    annotation_db = latest_sqlite(ANNOTATION_DIR, "AEAnnotation*_local.sqlite")
    return library_db, annotation_db


def latest_sqlite(directory: Path, pattern: str) -> Path:
    if not directory.exists():
        raise FileNotFoundError(f"Missing Apple Books directory: {directory}")

    candidates = sorted(directory.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        raise FileNotFoundError(f"No matching database in {directory} for {pattern}")
    return candidates[0]


def open_books_connection(library_db: Path, annotation_db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{library_db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("ATTACH DATABASE ? AS ann", (str(annotation_db),))
    return conn


def list_books_with_highlights(conn: sqlite3.Connection) -> list[Book]:
    rows = conn.execute(
        """
        SELECT
            b.ZASSETID AS asset_id,
            COALESCE(NULLIF(TRIM(b.ZTITLE), ''), b.ZASSETID) AS title,
            b.ZPATH AS book_path,
            COUNT(*) AS highlight_count
        FROM ZBKLIBRARYASSET b
        JOIN ann.ZAEANNOTATION a
          ON a.ZANNOTATIONASSETID = b.ZASSETID
        WHERE IFNULL(a.ZANNOTATIONDELETED, 0) = 0
          AND IFNULL(TRIM(a.ZANNOTATIONSELECTEDTEXT), '') <> ''
        GROUP BY b.ZASSETID, b.ZTITLE, b.ZPATH
        ORDER BY LOWER(title)
        """
    ).fetchall()

    return [
        Book(
            asset_id=row["asset_id"],
            title=row["title"],
            path=row["book_path"],
            highlight_count=row["highlight_count"],
        )
        for row in rows
    ]


def get_annotations_for_book(conn: sqlite3.Connection, asset_id: str) -> list[Annotation]:
    rows = conn.execute(
        """
        SELECT
            IFNULL(ZANNOTATIONSELECTEDTEXT, '') AS highlight_text,
            IFNULL(ZANNOTATIONNOTE, '') AS note_text,
            IFNULL(ZANNOTATIONLOCATION, '') AS location,
            ZPLLOCATIONRANGESTART AS range_start,
            ZPLLOCATIONRANGEEND AS range_end
        FROM ann.ZAEANNOTATION
        WHERE ZANNOTATIONASSETID = ?
          AND IFNULL(ZANNOTATIONDELETED, 0) = 0
          AND (
                IFNULL(TRIM(ZANNOTATIONSELECTEDTEXT), '') <> ''
             OR IFNULL(TRIM(ZANNOTATIONNOTE), '') <> ''
          )
        """,
        (asset_id,),
    ).fetchall()

    return [
        Annotation(
            highlight_text=row["highlight_text"],
            note_text=row["note_text"],
            location=row["location"],
            range_start=row["range_start"],
            range_end=row["range_end"],
        )
        for row in rows
    ]


# ============================================================================
# Parsing and chapter organization
# ============================================================================

def organize_highlights_by_chapter(book: Book, annotations: list[Annotation]) -> list[ChapterGroup]:
    chapter_lookup = load_chapter_lookup(book.path)
    parsed: list[Highlight] = []

    for annotation in annotations:
        highlight_text = clean_text(annotation.highlight_text)
        note_text = clean_text(annotation.note_text)
        if not highlight_text and not note_text:
            continue

        chapter_index, item_id = chapter_index_from_location(
            annotation.location, annotation.range_start
        )
        chapter_title = resolve_chapter_title(chapter_lookup, chapter_index, item_id)
        order_key = reading_order_key(chapter_index, annotation.range_start, annotation.location)

        parsed.append(
            Highlight(
                text=highlight_text,
                note=note_text or None,
                chapter_index=chapter_index,
                chapter_title=chapter_title,
                location=annotation.location,
                order_key=order_key,
            )
        )

    parsed.sort(key=lambda h: h.order_key)

    grouped: dict[tuple[int, str], list[Highlight]] = {}
    ordered_keys: list[tuple[int, str]] = []
    for highlight in parsed:
        chapter_sort = highlight.chapter_index if highlight.chapter_index is not None else 1_000_000
        key = (chapter_sort, highlight.chapter_title)
        if key not in grouped:
            grouped[key] = []
            ordered_keys.append(key)
        grouped[key].append(highlight)

    result: list[ChapterGroup] = []
    for chapter_sort, title in ordered_keys:
        result.append(
            ChapterGroup(
                chapter_index=None if chapter_sort == 1_000_000 else chapter_sort,
                title=title,
                highlights=grouped[(chapter_sort, title)],
            )
        )
    return result


def chapter_index_from_location(location: str, range_start: int | None) -> tuple[int | None, str | None]:
    match = CFI_SPINE_PATTERN.search(location or "")
    if match:
        step = int(match.group(1))
        item_id = match.group(2)
        if step >= 2:
            return max((step // 2) - 1, 0), item_id
    if range_start is not None and range_start >= 0:
        return range_start, None
    return None, None


def resolve_chapter_title(
    chapter_lookup: dict[int, str], chapter_index: int | None, item_id: str | None
) -> str:
    if chapter_index is not None and chapter_index in chapter_lookup:
        return chapter_lookup[chapter_index]

    friendly = friendly_item_id(item_id)
    if friendly:
        return friendly

    if chapter_index is not None:
        return f"Chapter {chapter_index + 1}"
    return "Unsorted"


def reading_order_key(chapter_index: int | None, range_start: int | None, location: str) -> tuple[int, ...]:
    chapter_sort = chapter_index if chapter_index is not None else 1_000_000
    range_sort = range_start if range_start is not None else 1_000_000
    location_numbers = tuple(int(v) for v in INT_PATTERN.findall(location or ""))
    if not location_numbers:
        location_numbers = (1_000_000,)
    return (chapter_sort, range_sort, *location_numbers)


def clean_text(value: str) -> str:
    text = html.unescape(value or "").replace("\u00A0", " ")
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    return " ".join(line for line in lines if line)


def friendly_item_id(item_id: str | None) -> str | None:
    if not item_id:
        return None

    lowered = item_id.lower().strip()
    if lowered.startswith("html") and lowered[4:].isdigit():
        return None
    if lowered.startswith("chapter") and lowered[7:].isdigit():
        return None

    cleaned = re.sub(r"[_\-]+", " ", item_id).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.title() if cleaned else None


# ============================================================================
# EPUB chapter title lookup (directory or zip .epub)
# ============================================================================

def load_chapter_lookup(book_path: str | None) -> dict[int, str]:
    if not book_path:
        return {}

    path = Path(book_path).expanduser()
    if not is_trusted_book_path(path):
        warn(f"Skipping untrusted book path from database: {path}")
        return {}
    if not path.exists():
        return {}

    if path.is_dir():
        return load_chapter_lookup_from_directory(path)
    if path.is_file() and zipfile.is_zipfile(path):
        try:
            return load_chapter_lookup_from_zip(path)
        except (zipfile.BadZipFile, OSError) as error:
            warn(f"Skipping invalid EPUB archive {path}: {error}")
            return {}
    return {}


def load_chapter_lookup_from_directory(root: Path) -> dict[int, str]:
    opf_path = find_opf_relative_path_directory(root)
    if not opf_path:
        return {}
    if not is_safe_relative_path(opf_path):
        warn(f"Skipping unsafe OPF path in directory EPUB: {opf_path}")
        return {}
    opf_text = read_file_text(root / Path(opf_path), max_bytes=MAX_XML_BYTES)
    if not opf_text:
        return {}

    def exists(relative_path: str) -> bool:
        if not is_safe_relative_path(relative_path):
            return False
        return (root / Path(relative_path)).exists()

    def read(relative_path: str) -> str:
        if not is_safe_relative_path(relative_path):
            return ""
        return read_file_text(root / Path(relative_path), max_bytes=MAX_XML_BYTES)

    return chapter_lookup_from_opf(opf_path, opf_text, exists, read)


def load_chapter_lookup_from_zip(epub_path: Path) -> dict[int, str]:
    with zipfile.ZipFile(epub_path) as archive:
        opf_path = find_opf_relative_path_zip(archive)
        if not opf_path:
            return {}
        if not is_safe_relative_path(opf_path):
            warn(f"Skipping unsafe OPF path in zip EPUB: {opf_path}")
            return {}
        opf_text = read_zip_text(archive, opf_path, max_bytes=MAX_XML_BYTES)
        if not opf_text:
            return {}

        def exists(relative_path: str) -> bool:
            if not is_safe_relative_path(relative_path):
                return False
            try:
                archive.getinfo(relative_path)
                return True
            except KeyError:
                return False

        def read(relative_path: str) -> str:
            if not is_safe_relative_path(relative_path):
                return ""
            return read_zip_text(archive, relative_path, max_bytes=MAX_XML_BYTES)

        return chapter_lookup_from_opf(opf_path, opf_text, exists, read)


def chapter_lookup_from_opf(
    opf_relative_path: str,
    opf_text: str,
    file_exists_fn,
    read_text_fn,
) -> dict[int, str]:
    manifest, spine_idrefs, toc_id = parse_opf(opf_text)
    if not manifest or not spine_idrefs:
        return {}

    opf_dir = posixpath.dirname(opf_relative_path)
    toc_map: dict[str, str] = {}

    if toc_id and toc_id in manifest:
        toc_path = resolve_href(opf_dir, manifest[toc_id].href)
        if toc_path and file_exists_fn(toc_path):
            toc_map = parse_ncx(read_text_fn(toc_path), opf_dir)

    if not toc_map:
        ncx_item = next(
            (item for item in manifest.values() if item.media_type == "application/x-dtbncx+xml"),
            None,
        )
        if ncx_item:
            ncx_path = resolve_href(opf_dir, ncx_item.href)
            if ncx_path and file_exists_fn(ncx_path):
                toc_map = parse_ncx(read_text_fn(ncx_path), opf_dir)

    if not toc_map:
        nav_item = next((item for item in manifest.values() if "nav" in item.properties.split()), None)
        if nav_item:
            nav_path = resolve_href(opf_dir, nav_item.href)
            if nav_path and file_exists_fn(nav_path):
                toc_map = parse_nav_document(read_text_fn(nav_path), opf_dir)

    if not toc_map:
        return {}

    basename_map = {posixpath.basename(path): title for path, title in toc_map.items() if title}

    chapter_lookup: dict[int, str] = {}
    for chapter_index, idref in enumerate(spine_idrefs):
        item = manifest.get(idref)
        if not item:
            continue
        chapter_path = resolve_href(opf_dir, item.href)
        title = toc_map.get(chapter_path) or basename_map.get(posixpath.basename(chapter_path))
        if title:
            chapter_lookup[chapter_index] = title

    return chapter_lookup


def parse_opf(opf_text: str) -> tuple[dict[str, ManifestItem], list[str], str | None]:
    try:
        root = ElementTree.fromstring(opf_text)
    except ElementTree.ParseError:
        return {}, [], None

    manifest: dict[str, ManifestItem] = {}
    for item in root.findall(".//{*}manifest/{*}item"):
        item_id = item.attrib.get("id")
        href = item.attrib.get("href")
        if not item_id or not href:
            continue
        manifest[item_id] = ManifestItem(
            href=href,
            media_type=item.attrib.get("media-type", ""),
            properties=item.attrib.get("properties", ""),
        )

    spine_idrefs: list[str] = []
    spine = root.find(".//{*}spine")
    toc_id = None
    if spine is not None:
        toc_id = spine.attrib.get("toc")
        for itemref in spine.findall("{*}itemref"):
            idref = itemref.attrib.get("idref")
            if idref:
                spine_idrefs.append(idref)

    return manifest, spine_idrefs, toc_id


def parse_ncx(ncx_text: str, opf_dir: str) -> dict[str, str]:
    if not ncx_text:
        return {}
    try:
        root = ElementTree.fromstring(ncx_text)
    except ElementTree.ParseError:
        return {}

    mapping: dict[str, str] = {}
    for nav_point in root.findall(".//{*}navPoint"):
        content = nav_point.find("./{*}content")
        if content is None:
            continue
        resolved = resolve_href(opf_dir, content.attrib.get("src", ""))
        title = clean_text(nav_point.findtext("./{*}navLabel/{*}text", default=""))
        if resolved and title and resolved not in mapping:
            mapping[resolved] = title
    return mapping


def parse_nav_document(nav_text: str, opf_dir: str) -> dict[str, str]:
    if not nav_text:
        return {}
    try:
        root = ElementTree.fromstring(nav_text)
    except ElementTree.ParseError:
        return {}

    toc_navs = []
    for nav in root.findall(".//{*}nav"):
        nav_type = (
            nav.attrib.get("{http://www.idpf.org/2007/ops}type") or nav.attrib.get("type") or ""
        ).lower()
        if "toc" in nav_type:
            toc_navs.append(nav)

    targets = toc_navs if toc_navs else root.findall(".//{*}nav")
    if not targets:
        targets = [root]

    mapping: dict[str, str] = {}
    for target in targets:
        for link in target.findall(".//{*}a"):
            resolved = resolve_href(opf_dir, link.attrib.get("href", ""))
            title = clean_text(" ".join(link.itertext()))
            if resolved and title and resolved not in mapping:
                mapping[resolved] = title
    return mapping


def find_opf_relative_path_directory(root: Path) -> str | None:
    container_xml = root / "META-INF" / "container.xml"
    if container_xml.exists():
        opf_path = extract_opf_path(read_file_text(container_xml, max_bytes=MAX_XML_BYTES))
        if opf_path:
            return opf_path

    first_opf = next(root.rglob("*.opf"), None)
    if first_opf is None:
        return None
    return first_opf.relative_to(root).as_posix()


def find_opf_relative_path_zip(archive: zipfile.ZipFile) -> str | None:
    container_xml = read_zip_text(archive, "META-INF/container.xml", max_bytes=MAX_XML_BYTES)
    if container_xml:
        opf_path = extract_opf_path(container_xml)
        if opf_path:
            return opf_path

    for name in archive.namelist():
        if name.lower().endswith(".opf"):
            return name
    return None


def extract_opf_path(container_xml: str) -> str | None:
    if not container_xml:
        return None
    try:
        root = ElementTree.fromstring(container_xml)
    except ElementTree.ParseError:
        return None

    for rootfile in root.findall(".//{*}rootfile"):
        full_path = rootfile.attrib.get("full-path", "").strip()
        if full_path:
            normalized = posixpath.normpath(full_path)
            if is_safe_relative_path(normalized):
                return normalized
            warn(f"Skipping unsafe OPF full-path in container.xml: {full_path}")
    return None


def resolve_href(base_dir: str, href: str) -> str:
    clean_href = unquote((href or "").split("#", 1)[0]).strip()
    if not clean_href:
        return ""

    if clean_href.startswith("/"):
        candidate = posixpath.normpath(clean_href.lstrip("/"))
    elif base_dir:
        candidate = posixpath.normpath(posixpath.join(base_dir, clean_href))
    else:
        candidate = posixpath.normpath(clean_href)

    if not is_safe_relative_path(candidate):
        return ""
    return candidate


def read_file_text(path: Path, max_bytes: int = MAX_TEXT_FILE_BYTES) -> str:
    try:
        size = path.stat().st_size
        if size > max_bytes:
            warn(f"Skipping oversized file ({size} bytes): {path}")
            return ""

        with path.open("rb") as file_obj:
            data = file_obj.read(max_bytes + 1)
        if len(data) > max_bytes:
            warn(f"Skipping oversized file while reading: {path}")
            return ""
        return data.decode("utf-8", errors="ignore")
    except OSError:
        return ""


def read_zip_text(archive: zipfile.ZipFile, relative_path: str, max_bytes: int = MAX_ZIP_MEMBER_BYTES) -> str:
    try:
        info = archive.getinfo(relative_path)
    except KeyError:
        return ""

    if info.file_size > max_bytes:
        warn(f"Skipping oversized archive member ({info.file_size} bytes): {relative_path}")
        return ""

    if info.compress_size > 0 and info.file_size / info.compress_size > MAX_ZIP_EXPANSION_RATIO:
        warn(f"Skipping high-expansion archive member: {relative_path}")
        return ""

    try:
        with archive.open(info, "r") as file_obj:
            data = file_obj.read(max_bytes + 1)
        if len(data) > max_bytes:
            warn(f"Skipping oversized archive member while reading: {relative_path}")
            return ""
        return data.decode("utf-8", errors="ignore")
    except OSError:
        return ""


# ============================================================================
# Output formatting
# ============================================================================

def format_output(book_title: str, chapter_groups: list[ChapterGroup], output_format: str) -> str:
    if output_format == "notion":
        return format_notion(book_title, chapter_groups)
    if output_format == "obsidian":
        return format_obsidian(book_title, chapter_groups)
    return format_plain_text(book_title, chapter_groups)


def format_notion(book_title: str, chapter_groups: list[ChapterGroup]) -> str:
    lines = [f"# {book_title}", ""]
    for chapter in chapter_groups:
        lines.append(f"## {chapter.title}")
        lines.append("")
        for h in chapter.highlights:
            if h.text:
                lines.append(f"- {escape_markdown_list_lead(h.text)}")
                if h.note:
                    lines.append(f"  - Note: {h.note}")
            elif h.note:
                lines.append(f"- Note: {h.note}")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def escape_markdown_list_lead(text: str) -> str:
    """Prevent nested list parsing without showing escape characters."""
    if ORDERED_LIST_LEAD_PATTERN.match(text) or UNORDERED_LIST_LEAD_PATTERN.match(text):
        # Invisible separator keeps rendered text unchanged while breaking list-marker parsing.
        return "\u2060" + text
    return text


def format_obsidian(book_title: str, chapter_groups: list[ChapterGroup]) -> str:
    lines = [f"# {book_title}", ""]
    for chapter in chapter_groups:
        lines.append(f"## {chapter.title}")
        lines.append("")
        for h in chapter.highlights:
            if h.text:
                lines.append(f"> {h.text}")
                if h.note:
                    lines.append(">")
                    lines.append(f"> Note: {h.note}")
            elif h.note:
                lines.append(f"> Note: {h.note}")
            lines.append("")
    return "\n".join(lines).strip() + "\n"


def format_plain_text(book_title: str, chapter_groups: list[ChapterGroup]) -> str:
    lines = [book_title, "=" * len(book_title), ""]
    for chapter in chapter_groups:
        lines.append(chapter.title)
        lines.append("-" * len(chapter.title))
        lines.append("")
        for h in chapter.highlights:
            if h.text:
                lines.append(f"- {h.text}")
                if h.note:
                    lines.append(f"  Note: {h.note}")
            elif h.note:
                lines.append(f"- Note: {h.note}")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


# ============================================================================
# Simple CLI flow
# ============================================================================

def prompt_for_book(books: list[Book]) -> Book:
    print("Books with highlights:")
    for idx, book in enumerate(books, start=1):
        safe_title = sanitize_terminal_text(book.title)
        print(f"{idx}. {safe_title} ({book.highlight_count} highlights)")

    while True:
        choice = input("\nSelect by number or title text: ").strip()
        if not choice:
            print("Please enter a number or title text.")
            continue

        if choice.isdigit():
            selected = int(choice)
            if 1 <= selected <= len(books):
                return books[selected - 1]
            print("That number is out of range.")
            continue

        matches = [book for book in books if choice.lower() in book.title.lower()]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            print("Multiple books match that text. Be more specific or pick a number:")
            for book in matches[:10]:
                print(f"- {sanitize_terminal_text(book.title)}")
            continue
        print("No matching book found. Try again.")


def prompt_for_format() -> tuple[str, str]:
    print("\nChoose output format:")
    print("1 = Notion")
    print("2 = Obsidian")
    print("3 = Plain Text")

    mapping = {
        "1": ("notion", "md"),
        "2": ("obsidian", "md"),
        "3": ("plain_text", "txt"),
    }

    while True:
        choice = input("Enter 1, 2, or 3: ").strip()
        if choice in mapping:
            return mapping[choice]
        print("Please enter 1, 2, or 3.")


def write_output_file(book_title: str, output_format: str, extension: str, content: str) -> Path:
    base = slugify(book_title)
    exports_dir = SCRIPT_ROOT / EXPORTS_DIR_NAME
    if exports_dir.exists() and exports_dir.is_symlink():
        raise RuntimeError(f"Refusing to write to symlinked exports directory: {exports_dir}")

    exports_dir.mkdir(parents=True, exist_ok=True)
    if exports_dir.is_symlink():
        raise RuntimeError(f"Refusing to write to symlinked exports directory: {exports_dir}")

    counter = 1
    data = content.encode("utf-8")
    while True:
        candidate = exports_dir / f"{base}-{output_format}.{extension}"
        if counter > 1:
            candidate = exports_dir / f"{base}-{output_format}-{counter}.{extension}"
        try:
            fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            with os.fdopen(fd, "wb") as output_file:
                output_file.write(data)
            return candidate
        except FileExistsError:
            counter += 1


def slugify(text: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return slug or "book"


def main() -> int:
    try:
        library_db, annotation_db = discover_databases()
        conn = open_books_connection(library_db, annotation_db)
    except FileNotFoundError as error:
        print(f"Apple Books database not found: {error}", file=sys.stderr)
        return 1
    except Exception as error:
        print(f"Could not open Apple Books database: {error}", file=sys.stderr)
        return 1

    try:
        books = list_books_with_highlights(conn)
        if not books:
            print("No books with highlights were found.")
            return 0

        selected_book = prompt_for_book(books)
        output_format, extension = prompt_for_format()

        annotations = get_annotations_for_book(conn, selected_book.asset_id)
        chapter_groups = organize_highlights_by_chapter(selected_book, annotations)
        if not chapter_groups:
            print("No highlight content found for that book.")
            return 0

        content = format_output(selected_book.title, chapter_groups, output_format)
        output_path = write_output_file(selected_book.title, output_format, extension, content)
        count = sum(len(group.highlights) for group in chapter_groups)

        print("")
        print(f"Export complete: {output_path}")
        print(f"Entries exported: {count}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
