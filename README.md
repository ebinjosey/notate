# Notate

Notate is a simple, open-source Python CLI that exports Apple Books highlights and notes from local macOS data into clean output files.

## What it does

When you run Notate, it:

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

## Run (single-file v1)

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

## File layout

- `notate.py` - Entire CLI and export logic in one file

## Notes

- macOS only (Apple Books local data layout)
- Reads local data only; nothing is uploaded
