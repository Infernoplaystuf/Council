# Ledger pictures and the clipboard — 2026-10-26

Andy: "it appears that the clip board isn't actually clearing itself to
continue to copy and paste things. I am only getting about 1/3 to 1/2 of the
images read", with the message "the copy Excel saved could not be read, and
the clipboard holds something besides plain text, which copying would
replace".

## The clipboard (excel_open.read_with_excel)

A protected (sensitivity-labelled) workbook: Excel saves a copy, but the copy
is encrypted too, so its pictures are copied out through the clipboard.

- **Before:** the clipboard was used only if it held plain text, and the text
  was put back only if Windows' change counter had not moved. Excel moves the
  counter itself: Windows' clipboard history renders its copy, and Excel puts
  the copy there once more when it closes. So the Ledger took that for the
  user copying, left its last picture on the clipboard, and refused every
  later protected workbook ("holds something besides plain text").
- **Now:**
  - What the user had on the clipboard is kept, every format
    (`ClipSnapshot`, `_clip_snapshot`), unless something there cannot be put
    back as it was (`_keep_plan`: owner-drawn or private formats, a GDI
    object with nothing to make it from; more than 64 MB). OLE's own link to
    the copying program (`DataObject`, `Ole Private Data`) is left out, so a
    paste is made from the formats kept.
  - Whose copy is on the clipboard is told by its owner
    (`_clip_owner_pid`): the hidden Excel's PID is this read's. After its
    first picture, any other owner is the user copying (`_user_copied`), and
    no more pictures are copied, as before. Excel's own changes are not.
  - The user's clipboard is put back while Excel still owns its last copy,
    before the workbook is closed (`_clip_is`). Otherwise the old rule (the
    counter) applies, and never when another program owns the clipboard.
  - When the owner cannot be told (no pywin32), everything behaves as before.
- The knowledge graph's background rebuild still uses the clipboard only
  when it holds plain text (`clipboard_safe()` with no `keeps`).
- The Windows calls (ctypes) were not run on Windows here; the tests fake
  the clipboard.

## The pictures

- `.xlsx`: pictures come from the zip (`excel_open.zip_pictures`), not
  openpyxl's `ws._images`. openpyxl dropped pictures in a group (all but
  one), at a fixed position (`absoluteAnchor`), EMF/WMF ones, and a whole
  sheet's pictures when its drawing held anything it did not understand
  (`mc:AlternateContent`, among others).
- `zip_pictures` reads groups, `mc:AlternateContent` (the first choice that
  holds an anchor), fixed positions (put at A1) and pictures placed in a cell
  (`_cell_pictures`: the cell's `vm` → `xl/metadata.xml` → the rich value →
  `xl/richData/richValueRel.xml` → the picture).
- Through Excel (.xls, .xlsb), from the copy it saves: a picture shape whose
  cell is not where the file anchors it takes another of its sheet's; the
  copy's pictures no shape took (groups, in a cell, fixed position) are added.
  Through the clipboard: a group holding a picture is copied out whole.
  Pictures placed in a cell are not reached that way.
- EMF / WMF drawings are turned into PNG before they are read
  (`source_extract._as_raster`; Pillow draws them only on Windows). One that
  cannot be is saved as it was and said so.

## The count

"Images read" counted a picture with no text in it (a photo) as not read.
Now the summary says `N of M images read (K more had no text in them); not
read: …` with the reasons counted (`ledger_ingest._images_blank`,
`_images_unread`). The tab shows `N / M (+K no text)`, with the reasons in
its tooltip.

Tests: `tests/test_ledger_pictures.py`, and in `tests/test_excel_open.py`
`test_a_protected_workbook_copies_through_the_clipboard_and_puts_yours_back`,
`test_the_user_copying_meanwhile_stops_the_copies_and_keeps_theirs`,
`test_what_a_clipboard_snapshot_can_keep`,
`test_pictures_no_shape_takes_still_come_from_the_copy`.
