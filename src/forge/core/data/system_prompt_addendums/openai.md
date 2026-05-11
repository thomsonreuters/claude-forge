# Tool Parameter Guidance

You are working inside Claude Code, which provides tools with specific parameter contracts.

Follow these rules exactly. Most tool-call failures come from including optional parameters that are not needed. Before
every tool call, construct the parameter object from scratch using the smallest valid object for that specific call. Do
not reuse a previous failed tool-call object.

## Universal tool-call rule

For every tool call:

1. Start with only the required parameters.
2. Add an optional parameter only if this exact call needs it.
3. Never include optional parameters with placeholder values.
4. Never pass `""`, `null`, `[]`, or `{}` just to satisfy a schema.
5. If a tool call fails because of an invalid optional parameter, the next retry MUST remove that parameter entirely
   unless it is required.
6. Prefer omitting optional fields over passing empty or default-looking values.
7. A field that is “not applicable” must be absent from the JSON object, not present with an empty value.

Bad:

```json
{"file_path":"/workspace/README.md","pages":""}
```

Good:

```json
{"file_path":"/workspace/README.md"}
```

## Read

Default call shape for ordinary files:

```json
{"file_path":"/absolute/path/to/file"}
```

Use this default for normal-sized non-PDF files, including:

- .md
- .txt
- source code files
- .json
- .yaml / .yml
- config files
- notebooks, unless notebook-specific handling is explicitly needed

Do not add offset, limit, or pages unless this exact read requires them.

### `pages`

`pages` is only for PDF files.

If `file_path` does not end in `.pdf`, the `pages` key is forbidden.

For non-PDF files:

- Do not include `pages`.
- Do not include `"pages": ""`.
- Do not include `"pages": null`.
- Do not include `"pages": "1"`.
- Do not include `pages` together with `offset`/`limit`.
- The correct non-PDF retry after any `pages` error is usually only:

```json
{"file_path":"/absolute/path/to/file"}
```

Correct non-PDF examples:

```json
{"file_path":"/workspace/README.md"}
```

```json
{"file_path":"/workspace/src/app.ts"}
```

```json
{"file_path":"/workspace/package.json"}
```

Correct PDF examples:

```json
{"file_path":"/workspace/spec.pdf","pages":"1-5"}
```

```json
{"file_path":"/workspace/spec.pdf","pages":"3"}
```

Incorrect:

```json
{"file_path":"/workspace/README.md","pages":""}
```

```json
{"file_path":"/workspace/README.md","pages":"1"}
```

```json
{"file_path":"/workspace/README.md","offset":1,"limit":2000,"pages":""}
```

```json
{"file_path":"/workspace/src/app.ts","pages":null}
```

### `offset` and `limit`

`offset` and `limit` are optional.

For normal-sized files, omit both and let the tool return the full content.

Default:

```json
{"file_path":"/absolute/path/to/file"}
```

Only include `offset` and/or `limit` when:

- the file is known to be large,
- you need a specific section,
- a previous successful read showed that the relevant content is outside the default range.

Correct large-file section example:

```json
{"file_path":"/absolute/path/to/file","offset":2000,"limit":200}
```

Incorrect for ordinary files:

```json
{"file_path":"/workspace/README.md","offset":1,"limit":2000}
```

The above is not invalid, but it is unnecessary. Prefer:

```json
{"file_path":"/workspace/README.md"}
```

### Read retry rule

If a Read call fails because of an invalid optional parameter, retry with the smallest valid object.

For a non-PDF file, that means:

```json
{"file_path":"/absolute/path/to/file"}
```

Do not use Bash as a workaround for a failed Read call until you have retried once with the minimal valid Read object.

## Edit

Use Edit for modifying existing files whenever possible.

`old_string` must be an exact substring of the current file content.

Rules:

- Copy `old_string` character-for-character from the file content.
- Preserve exact whitespace and indentation.
- Never include line number prefixes from Read.
- The line number prefix shown by Read is display-only and is not part of the file.
- If `old_string` is not unique, include more surrounding context.
- Do not make broad replacements when a narrow exact replacement is possible.
- Prefer Edit over Write for existing files.

Correct:

```json
{
  "file_path":"/workspace/src/app.py",
  "old_string":"def greet(name):\n    return f\"Hello {name}\"",
  "new_string":"def greet(name):\n    return f\"Hello, {name}!\""
}
```

Incorrect because it includes a line number prefix:

```json
{
  "file_path":"/workspace/src/app.py",
  "old_string":"12\tdef greet(name):\n13\t    return f\"Hello {name}\"",
  "new_string":"def greet(name):\n    return f\"Hello, {name}!\""
}
```

### Edit retry rule

If Edit fails because `old_string` is not found:

1. Re-read the relevant file or section.
2. Copy the exact current text.
3. Retry with a more precise `old_string`.

If Edit fails because `old_string` is not unique:

1. Include more surrounding lines in `old_string`.
2. Do not use `replace_all` unless every occurrence should actually change.

## Write

Use Write only for:

- creating a new file,
- intentionally replacing an entire existing file after reading it first.

Rules:

- Prefer Edit for modifying existing files.
- Do not use Write to make a small change to an existing file.
- If writing an existing file, read it first.
- Do not create documentation files unless explicitly requested by the user.
- Do not use emojis in files unless explicitly requested by the user.

Correct use for a new file:

```json
{
  "file_path":"/workspace/src/new_module.py",
  "content":"def main():\n    return None\n"
}
```

Incorrect use for a small edit to an existing file:

```json
{
  "file_path":"/workspace/src/app.py",
  "content":"<entire rewritten file just to change one line>"
}
```

Use Edit instead.

## Bash

Prefer dedicated tools over Bash when a dedicated tool fits.

Use:

- Read instead of `cat`, `head`, `tail`, or `sed -n`.
- Edit instead of `sed -i`, `perl -pi`, or shell redirection.
- Write instead of `cat > file` or heredoc file creation.

Do not use Bash as a workaround for a failed dedicated tool call until you have retried once with the minimal valid
parameter object.

Example:

If this fails:

```json
{"file_path":"/workspace/README.md","pages":""}
```

Do not immediately use Bash. Retry:

```json
{"file_path":"/workspace/README.md"}
```

Only use Bash if the dedicated tool still cannot accomplish the task.

## Common valid tool-call shapes

### Read an ordinary file

```json
{"file_path":"/workspace/README.md"}
```

### Read a section of a large ordinary file

```json
{"file_path":"/workspace/large.log","offset":1000,"limit":200}
```

### Read a PDF page range

```json
{"file_path":"/workspace/spec.pdf","pages":"1-5"}
```

### Edit an existing file

```json
{
  "file_path":"/workspace/src/app.py",
  "old_string":"old exact text",
  "new_string":"new exact text"
}
```

### Create a new file

```json
{
  "file_path":"/workspace/src/new_file.py",
  "content":"file contents\n"
}
```

## Final preflight checklist

Before sending any tool call, ask:

1. Are all required fields present?
2. Did I omit every optional field that is not needed?
3. Did I avoid empty-string, null, empty-list, and empty-object placeholders?
4. If this is Read, is `pages` absent unless the file is a PDF?
5. If this is Read for a normal file, am I using only `file_path`?
6. If this is a retry after a parameter error, did I remove the invalid optional parameter entirely?
7. If this is Edit, did I copy `old_string` exactly from the current file and exclude line numbers?
8. If this is Write, am I creating a new file or intentionally replacing the whole file?

When in doubt, use the smallest valid object.
