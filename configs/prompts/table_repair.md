# Table repair — transcribe a table image into a clean grid

You are looking at a cropped image of ONE table from a scanned contract
page. An OCR heuristic failed to recover its structure. Your job: read the
table from the image and return its true grid.

## Rules

- **headers**: the column header row, left to right. If the table has no
  header row, infer short neutral labels from the content ("Item",
  "Value") — never leave headers empty.
- **rows**: one entry per LOGICAL data row, top to bottom. A logical row
  is one record — when a cell's text wraps over multiple printed lines,
  join the lines with a single space into ONE cell of ONE row.
- Cell text is **verbatim** from the image (fix obvious OCR-style
  spacing like "Supplytemperature" → "Supply temperature", but never
  paraphrase, never invent, never drop symbols like S$, %, °C).
- Every row must have exactly as many cells as there are headers. Use ""
  for genuinely empty cells.
- Merged cells that span several rows: repeat the value in each spanned
  row so every row reads as a complete record.
- If the image is not actually a table (a figure, a paragraph), return
  `{"not_a_table": true, "headers": [], "rows": []}`.

Return the JSON object per the schema.
