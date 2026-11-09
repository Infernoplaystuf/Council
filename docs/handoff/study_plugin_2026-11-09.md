# Study plugin (council.study), 2026-11-09

Andy asked for something like Google's NotebookLM: load sources, get a
literature review, then make study documents (flash cards, quizzes). Built as
a built-in plugin on the plugin foundation (PR #68), OFF until ticked in the
Settings tab, in four steps (one commit each).

## Services Council now offers plugins (`council_core/plugin_services.py`)
- `sources`: a notebook is a named Connections graph (`kg_graphs`) over chosen
  folders, made with `use_in_answers=False` so studying never changes the
  Council's answers. `build` is Connections' rebuild; `passages` come from
  `KnowledgeGraph.text_passages` (new; every passage of free text, unlike
  `text_chunks`); `search` is BM25 (no model, cached per notebook until a
  document changes); `context` reads the cited lines back from the file.
- `models`: US-made local models only (Ollama + GGUF, `local_models` origin
  must be "US"); `chat(model)` refuses any other.
- `PluginContext.service(name)` gives only what the manifest's `needs` names.

## The plugin (`council_plugins/study/`)
- `engine/review.py`: outline (JSON schema) -> each section written from
  numbered passages -> sentences kept only when they cite a passage the
  section was shown and most content words are in it; the rest are listed as
  left out, with why.
- `engine/study_docs.py`: flash cards, quiz (choice/short, answer key),
  study guide, glossary per section; items dropped (counted by reason) when
  not backed by their cited passage.
- `engine/practice.py`: Leitner boxes (1..5; due after 0/1/3/7/16 days),
  sessions repeat missed cards; quiz runs, retake missed, scores kept.
- `engine/export.py`: Word (python-docx), printable HTML -> PDF with Qt's
  QPdfWriter, Anki tab-separated import.
- Saved per notebook in `<app dir>/plugin_data/council.study/`:
  `<id>.review.json`, `<id>.study.json`, `<id>.practice.json`, settings.json.

## Not done / next
- Not tried with a real model yet: prompts were tested with fake replies.
  A small model may produce few supported sentences; check the "left out"
  lists on a real run.
- The support check is word overlap, not meaning: it cannot catch a sentence
  that reverses its source.
- No audio overview (NotebookLM's podcast); left out on purpose.
