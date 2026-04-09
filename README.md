# notate

**notate** is a simple, open-source Python CLI that exports Apple Books highlights and notes from your local macOS library into clean, well-formatted output files.

## Demo

<p align="center">
  <img src="https://github.com/user-attachments/assets/b837ff9b-f304-44ce-9ed7-6e6cba43317e" width="100%" />
</p>

## Overview

notate provides a fast and minimal way to extract your Apple Books highlights into formats that are ready to use in tools like **Notion**, **Obsidian**, or plain text workflows. It runs entirely locally and is designed for simplicity, readability, and zero friction.

1. Shows only books that have highlights
2. Lets you select a book by number or title text
3. Asks for output format:
   - `1` = Notion (`.md`)
   - `2` = Obsidian (`.md`)
   - `3` = Plain Text (`.txt`)
4. Writes a clean export file with:
   - Book title at the top
   - Highlights grouped chapter-by-chapter
   - Entries sorted in reading order (location-based), not creation time
   - Files saved in `exports/`

When you run notate, it:

- Displays only books that contain highlights or annotations
- Allows selection by number or partial title match
- Supports multiple output formats:
  - **Notion** (`.md`)
  - **Obsidian** (`.md`)
  - **Plain Text** (`.txt`)
- Generates a clean export file with:
  - Book title at the top
  - Highlights grouped **chapter by chapter**
  - Entries ordered by **reading position (location-based)**, not creation time

## Run

Directly:

```bash
python3 notate.py
```

Or install command-line entrypoint:

```bash
python3 -m pip install .
notate
```

## Data sources

Notate reads Apple Books local SQLite databases:

- `~/Library/Containers/com.apple.iBooksX/Data/Documents/BKLibrary/*.sqlite`
- `~/Library/Containers/com.apple.iBooksX/Data/Documents/AEAnnotation/*.sqlite`

It also reads local EPUB data (including iCloud-synced books that exist locally) to detect chapter names where possible.

## Notes

- macOS only (Apple Books local data layout)
- Reads local data only; nothing is uploaded
