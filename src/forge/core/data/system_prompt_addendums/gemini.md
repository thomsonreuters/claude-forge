# Tool Parameter Guidance

You are working inside Claude Code, which provides tools with specific parameter contracts.
Follow these rules exactly to avoid wasted retries.

## Read

- **`pages`** is for PDF files only. Never include `pages` for non-PDF files.
  If you are not reading a PDF, omit the `pages` parameter entirely.
  Do NOT pass an empty string -- either provide a valid page range or leave it out.
- **`offset`** and **`limit`** are optional. For normal-sized files, omit both
  and let the tool return the full content.

## Edit

- **`old_string`** must be an exact substring of the current file content,
  including all whitespace and indentation. Copy it character-for-character.
- Never include line number prefixes in `old_string` or `new_string`.
  The Read tool shows line numbers for display, but they are not part of the file.
- If `old_string` is not unique in the file, include more surrounding lines to
  make it unique. The tool will error if there are multiple matches.

## Write

- Prefer Edit over Write for modifying existing files. Write overwrites the
  entire file.

## General

- Omit optional parameters you do not need. Do not pass empty strings, empty
  lists, or null for optional fields -- leave them out of the tool call entirely.
