"""
council_qt.tabs.docs — ask documentation servers; get cited answers and code.

THREE THINGS ON ONE PAGE, BECAUSE THEY ARE ONE JOB
  * which documentation servers to ask (MCP: the bundled one documents any
    installed Python package; add a local command or a URL), each with a
    Test connection that lists its tools;
  * which model answers — the "docs" role — with a picker and a quick
    CAPABILITY CHECK that runs a small grounded benchmark against an invented
    package and says whether this model is fit for the job, so a user can
    IDENTIFY a model that does this well instead of guessing;
  * the question: an answer whose [n] citations are links to the pages the
    Council fetched, a "Write code" mode whose code was checked against those
    pages, and Stop.

ALL THE WORK IS IN council_core
Retrieval, citation rules, code checks (docs_qa), the server registry
(docs_servers) and the check (docs_bench) are toolkit-free and tested without
a display. This file lays out widgets and moves results across the thread
hop, nothing else.

NOTHING RUNS ON THE GUI THREAD THAT CAN WAIT ON A SERVER OR A MODEL
Every action goes through `_start`, one worker at a time, with a Stop that
cancels the MCP request or model call in flight (the same should_stop the
engine and the MCP client poll). Building the tab reads two small JSON files
and starts nothing; the model list is fetched on a worker the first time the
tab is shown.
"""
from __future__ import annotations

import html
import threading
from pathlib import Path
from typing import Callable, List, Optional

from PySide6.QtCore import Qt, QUrl
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox,
                               QGroupBox, QHBoxLayout, QLabel, QLineEdit,
                               QListWidget, QPlainTextEdit, QSplitter,
                               QTextBrowser, QVBoxLayout, QWidget)

from council_core import docs_bench, docs_qa, docs_servers, paths

from .. import theme
from ..view import ViewHelpers

#: The Models tab's registry title, for the "All roles…" link.
MODELS_TAB = "🇺🇸 Models"

KINDS = ("Python packages (bundled)", "Local command (stdio)", "HTTP URL")
PLACEHOLDERS = {
    0: "Python interpreter or conda env name (empty = the Council's own)",
    1: "Command line, e.g.  python -m my_docs_server",
    2: "URL, e.g.  http://127.0.0.1:8000/mcp",
}


class DocsActions:
    """What the Docs tab can ask the application to do."""

    def __init__(self, config_path: Optional[Path] = None,
                 vault_dir: Optional[Path] = None,
                 checks_path: Optional[Path] = None,
                 model_call: Optional[Callable] = None,
                 chat_tools: Optional[Callable] = None,
                 models: Optional[Callable[[], List[dict]]] = None):
        self.config_path = config_path
        self.vault_dir = Path(vault_dir) if vault_dir else paths.vault_dir()
        self.checks_path = checks_path
        self.model_call = model_call
        self.chat_tools = chat_tools
        self._models = models

    # -- servers ---------------------------------------------------------
    def servers(self) -> List[docs_servers.ServerSpec]:
        return docs_servers.load(self.config_path)

    def problem(self) -> str:
        return docs_servers.problem(self.config_path)

    def add_server(self, spec: docs_servers.ServerSpec) -> str:
        docs_servers.add(spec, self.config_path)
        return f"Added {spec.name}."

    def remove_server(self, name: str) -> str:
        docs_servers.remove(name, self.config_path)
        return f"Removed {name}."

    def check_server(self, spec, should_stop=None):
        return docs_servers.check_server(spec, should_stop=should_stop)

    # -- the question ----------------------------------------------------
    def ask(self, question: str, packages: str, write_code: bool,
            use_tools: bool, should_stop=None, progress=None):
        return docs_qa.ask(question, packages=packages, write_code=write_code,
                           model_call=self.model_call,
                           mode="tools" if use_tools else "orchestrated",
                           should_stop=should_stop, progress=progress,
                           chat_tools=self.chat_tools,
                           config_path=self.config_path)

    # -- the model -------------------------------------------------------
    def answering(self):
        """(role, model id) for the docs role as saved right now."""
        from council_core import model_slots
        cfg = model_slots.load(self.vault_dir)
        role = docs_qa.answering_role(cfg)
        return role, docs_qa.role_model(role, self.vault_dir)

    def docs_assigned(self) -> bool:
        """Whether the docs role has a model of its own."""
        from council_core import model_slots
        return docs_qa.DOCS_ROLE in model_slots.load(self.vault_dir).roles

    def models(self) -> List[dict]:
        return (self._models or docs_qa.list_models)()

    def set_docs_model(self, model_id: str) -> str:
        return docs_qa.assign_docs_model(model_id, self.vault_dir)

    def capability_check(self, should_stop=None, progress=None):
        role, model_id = self.answering()
        try:
            origin = next((str(m.get("origin") or "") for m in self.models()
                           if m.get("id") == model_id), "")
        except Exception:                                 # noqa: BLE001
            origin = ""
        report = docs_bench.capability_check(
            self.model_call, should_stop=should_stop, progress=progress,
            model_label=docs_qa.model_label(model_id), origin=origin)
        if report.items and not report.stopped:
            docs_bench.record_check(report, self.checks_path)
        return report

    def past_checks(self) -> List[dict]:
        return docs_bench.load_checks(self.checks_path)


def render_answer(ans: docs_qa.DocsAnswer, muted: str = "#888",
                  error: str = "#d55") -> str:
    """The answer as HTML: [n] become links to the page they cite.

    Plain function, so the citation rendering is testable without a widget —
    and so a link can never point at a page that was not fetched: only the
    numbers in `ans.sources` are linked."""
    esc = html.escape
    valid = {s.n for s in ans.sources}
    parts: List[str] = []
    if ans.error and not ans.ok:
        parts.append(f"<p style='color:{error}'>{esc(ans.error)}</p>")
    if ans.answer:
        body = esc(ans.answer)
        body = docs_qa.CITATION.sub(
            lambda m: (f"<a href='src:{m.group(1)}'>[{m.group(1)}]</a>"
                       if int(m.group(1)) in valid else m.group(0)), body)
        parts.append("<p>" + body.replace("\n", "<br>") + "</p>")
    if ans.sources:
        rows = []
        for s in ans.sources:
            mark = "<b>" if s.n in ans.cited else ""
            unmark = "</b>" if s.n in ans.cited else ""
            rows.append(f"<li>{mark}<a href='src:{s.n}'>[{s.n}] "
                        f"{esc(s.title)}</a>{unmark} "
                        f"<span style='color:{muted}'>— {esc(s.server)}"
                        f"</span></li>")
        parts.append("<p><b>Sources</b> (cited in bold)</p><ul>"
                     + "".join(rows) + "</ul>")
    if ans.code_issues:
        parts.append(f"<p style='color:{error}'>Code problems still open:"
                     "<br>" + "<br>".join(esc(i) for i in ans.code_issues)
                     + "</p>")
    if ans.notes:
        parts.append(f"<p style='color:{muted}'>"
                     + "<br>".join(esc(n) for n in ans.notes) + "</p>")
    return "".join(parts)


def timing_line(ans: docs_qa.DocsAnswer) -> str:
    t = ans.timings
    bits = [f"{t.get('total_s', 0):.1f} s"]
    if "model_s" in t:
        bits.append(f"model {t['model_s']:.1f} s in {ans.model_calls} "
                    f"call(s)")
    if "search_s" in t:
        bits.append(f"search {t['search_s'] * 1000:.0f} ms")
    if ans.constrained is False:
        bits.append("unconstrained output")
    return " — ".join(bits)


class DocsTab(ViewHelpers, QWidget):
    """Servers and model on the left; question, answer, code on the right."""

    def __init__(self, window=None, actions: Optional[DocsActions] = None):
        super().__init__()
        self.window = window
        self.bridge = getattr(window, "bridge", None)
        self.actions = actions or DocsActions()
        self._tokens = theme.tokens("dark")
        self._busy = False
        self._stop = threading.Event()
        self._answer: Optional[docs_qa.DocsAnswer] = None
        self._specs: List[docs_servers.ServerSpec] = []
        self._models_loaded = False
        self._build()
        self.refresh_servers()
        self.refresh_role()
        self.refresh_checks()

    # ------------------------------------------------------------------
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 8)
        blurb = QLabel(
            "Ask a question about a Python package — or ask for code. The "
            "Council searches your documentation servers, hands the pages to "
            "the model, and every [n] links to the page it came from.")
        blurb.setWordWrap(True)
        blurb.setStyleSheet(f"color: {self._tokens['muted_fg']};")
        outer.addWidget(blurb)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(self._left())
        split.addWidget(self._right())
        split.setSizes([380, 700])
        outer.addWidget(split, 1)

        self.status = QLabel("Ready.")
        self.status.setWordWrap(True)
        outer.addWidget(self.status)

    def _left(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)

        box = QGroupBox("Documentation servers")
        bl = QVBoxLayout(box)
        self.server_list = QListWidget()
        self.server_list.setMinimumHeight(70)
        bl.addWidget(self.server_list)
        row = QHBoxLayout()
        self.test_btn = self._button(row, "Test connection", self.on_test)
        self.remove_btn = self._button(row, "Remove", self.on_remove)
        row.addStretch(1)
        bl.addLayout(row)
        self.server_info = QPlainTextEdit()
        self.server_info.setReadOnly(True)
        self.server_info.setMaximumHeight(130)
        self.server_info.setPlaceholderText(
            "Test a server to see its tools.")
        bl.addWidget(self.server_info)
        layout.addWidget(box)

        add = QGroupBox("Add a server")
        al = QVBoxLayout(add)
        self.kind = QComboBox()
        self.kind.addItems(list(KINDS))
        self.kind.currentIndexChanged.connect(self.on_kind_changed)
        al.addWidget(self.kind)
        self.add_name = QLineEdit()
        self.add_name.setPlaceholderText("Name")
        al.addWidget(self.add_name)
        self.add_target = QLineEdit()
        al.addWidget(self.add_target)
        self.add_packages = QLineEdit()
        self.add_packages.setPlaceholderText(
            "Packages, e.g. simplnx (optional)")
        al.addWidget(self.add_packages)
        self.allow_remote = QCheckBox("Allow a server on another computer")
        al.addWidget(self.allow_remote)
        row = QHBoxLayout()
        self.add_btn = self._button(row, "Add", self.on_add)
        row.addStretch(1)
        al.addLayout(row)
        layout.addWidget(add)
        self.on_kind_changed(0)

        mbox = QGroupBox("Answering model (the “docs” role)")
        ml = QVBoxLayout(mbox)
        self.role_label = QLabel("")
        self.role_label.setWordWrap(True)
        ml.addWidget(self.role_label)
        row = QHBoxLayout()
        self.model_combo = QComboBox()
        self.model_combo.setMinimumWidth(180)
        row.addWidget(self.model_combo, 1)
        self.refresh_models_btn = self._button(row, "⟳", self.refresh_models)
        ml.addLayout(row)
        row = QHBoxLayout()
        self.use_model_btn = self._button(row, "Use for docs",
                                          self.on_use_model)
        self.roles_btn = self._button(row, "All roles…", self.on_roles)
        row.addStretch(1)
        ml.addLayout(row)
        row = QHBoxLayout()
        self.check_btn = self._button(row, "Check this model",
                                      self.on_check)
        row.addStretch(1)
        ml.addLayout(row)
        self.check_result = QLabel("")
        self.check_result.setWordWrap(True)
        ml.addWidget(self.check_result)
        self.checks_label = QLabel("")
        self.checks_label.setWordWrap(True)
        self.checks_label.setStyleSheet(
            f"color: {self._tokens['muted_fg']};")
        ml.addWidget(self.checks_label)
        layout.addWidget(mbox)
        layout.addStretch(1)
        return panel

    def _right(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel("Question:"))
        self.question = QPlainTextEdit()
        self.question.setMaximumHeight(80)
        self.question.setPlaceholderText(
            "e.g. “How do I solve a linear system in numpy?” or, with Write "
            "code, “a function that reads a CSV into a DataStructure with "
            "simplnx”")
        layout.addWidget(self.question)
        row = QHBoxLayout()
        row.addWidget(QLabel("Package:"))
        self.package = QLineEdit()
        self.package.setPlaceholderText("e.g. numpy (optional)")
        self.package.setMaximumWidth(180)
        row.addWidget(self.package)
        self.write_code = QCheckBox("Write code")
        row.addWidget(self.write_code)
        self.use_tools = QCheckBox("Native tool calling")
        self.use_tools.setToolTip(
            "Let the model call search/fetch itself. Only for models that do "
            "tool calling well; the default lets the Council retrieve.")
        row.addWidget(self.use_tools)
        row.addStretch(1)
        self.ask_btn = self._button(row, "Ask", self.on_ask)
        self.stop_btn = self._button(row, "Stop", self.on_stop)
        self.stop_btn.setEnabled(False)
        layout.addLayout(row)

        self.answer_view = QTextBrowser()
        self.answer_view.setOpenLinks(False)
        self.answer_view.setOpenExternalLinks(False)
        self.answer_view.anchorClicked.connect(self.on_link)
        layout.addWidget(self.answer_view, 2)

        code_box = QGroupBox("Code")
        cl = QVBoxLayout(code_box)
        self.code = QPlainTextEdit()
        self.code.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.code.setPlaceholderText("Tick “Write code” to get code here.")
        cl.addWidget(self.code)
        row = QHBoxLayout()
        self.copy_btn = self._button(row, "Copy code", self.on_copy)
        self.code_status = QLabel("")
        self.code_status.setWordWrap(True)
        row.addWidget(self.code_status, 1)
        cl.addLayout(row)
        layout.addWidget(code_box, 1)

        src_box = QGroupBox("Source page")
        sl = QVBoxLayout(src_box)
        self.source_view = QPlainTextEdit()
        self.source_view.setReadOnly(True)
        self.source_view.setPlaceholderText("Click a [n] to read the page.")
        sl.addWidget(self.source_view)
        layout.addWidget(src_box, 1)
        return panel

    # -- refreshing ------------------------------------------------------
    def refresh_servers(self) -> None:
        self._specs = self.actions.servers()
        self.server_list.clear()
        for spec in self._specs:
            self.server_list.addItem(spec.describe())
        if self._specs:
            self.server_list.setCurrentRow(0)
        problem = self.actions.problem()
        if problem:
            self.status.setText(problem)

    def refresh_role(self) -> None:
        role, model_id = self.actions.answering()
        label = docs_qa.model_label(model_id)
        if self.actions.docs_assigned():
            text = f"Answers come from the docs role: {label}."
        elif role == docs_qa.DOCS_ROLE:
            text = (f"No model is chosen for docs yet, so the main model "
                    f"answers: {label}.")
        else:
            text = (f"No model is chosen for docs yet, so the {role} role's "
                    f"model answers: {label}.")
        self.role_label.setText(text)

    def refresh_checks(self) -> None:
        rows = self.actions.past_checks()
        if not rows:
            self.checks_label.setText(
                "No model checked yet. A check asks 5 questions about an "
                "invented package, so only reading the docs can pass it.")
            return
        best = {}
        for r in rows:                      # newest first: keep the latest
            best.setdefault(r.get("model") or "?", r)
        ranked = sorted(best.values(), key=lambda r: (
            -float(r.get("rate") or 0), float(r.get("mean_seconds") or 0)))
        lines = ["Checked so far:"]
        for r in ranked[:5]:
            if str(r.get("origin") or "").lower() == "non-us":
                verdict = "non-US, measured only"
            else:
                verdict = "good" if r.get("good") else "weak"
            lines.append(f"• {r.get('model')}: {r.get('passed')}/"
                         f"{r.get('total')}, {r.get('mean_seconds')} s/item "
                         f"— {verdict}")
        self.checks_label.setText("\n".join(lines))

    def refresh_models(self) -> None:
        self._models_loaded = True
        self._start("Looking for local models…", self.actions.models,
                    name="docs-models", then=self._show_models, quiet=True)

    def _show_models(self, models) -> None:
        _role, current = self.actions.answering()
        self.model_combo.clear()
        self.model_combo.addItem("(no model of its own — use the fallback)",
                                 "")
        chosen = 0
        passed = {r.get("model") for r in self.actions.past_checks()
                  if r.get("recommendable")}
        for m in models or []:
            label = str(m.get("name") or docs_qa.model_label(m["id"]))
            backend = m.get("backend") or ""
            origin = str(m.get("origin") or "unknown")
            text = f"{label}  [{backend}]" if backend else label
            if origin.lower() == "non-us":
                # Listed so it can be measured; never marked as a good pick.
                text += "  — non-US: for measurement only"
            elif docs_qa.model_label(m["id"]) in passed:
                text += "  ★ passed the docs check"
            self.model_combo.addItem(text, m["id"])
            if m["id"] == current:
                chosen = self.model_combo.count() - 1
        self.model_combo.setCurrentIndex(chosen)
        self.status.setText(f"{len(models or [])} local model(s) found.")

    def showEvent(self, event) -> None:                    # noqa: N802
        super().showEvent(event)
        if not self._models_loaded:
            self.refresh_models()

    # -- the one road to a worker -----------------------------------------
    def _start(self, status: str, call, *, name: str, then=None,
               quiet: bool = False) -> None:
        """Run `call()` on a worker; hand its result to `then` on the GUI
        thread. One at a time — except quiet jobs (the model list), which do
        not lock the buttons."""
        if self._busy and not quiet:
            self.status.setText("Already working — wait, or press Stop.")
            return
        if not quiet:
            self._busy = True
            self._stop = threading.Event()
            self._set_buttons(False)
        self.status.setText(status)

        def work() -> None:
            try:
                result = call()
                failure = None
            except Exception as exc:                      # noqa: BLE001
                result, failure = None, f"{type(exc).__name__}: {exc}"

            def show() -> None:
                if not quiet:
                    self._busy = False
                    self._set_buttons(True)
                if failure is not None:
                    self.status.setText(failure)
                    return
                if then is not None:
                    then(result)

            self._to_ui(show)

        threading.Thread(target=work, name=name, daemon=True).start()

    def _set_buttons(self, enabled: bool) -> None:
        for b in (self.ask_btn, self.test_btn, self.remove_btn, self.add_btn,
                  self.check_btn, self.use_model_btn):
            b.setEnabled(enabled)
        self.stop_btn.setEnabled(not enabled)

    def _progress(self, text: str) -> None:
        """Called from a worker: hop before touching the label."""
        self._to_ui(lambda: self.status.setText(text))

    # -- servers ---------------------------------------------------------
    def selected_spec(self) -> Optional[docs_servers.ServerSpec]:
        i = self.server_list.currentRow()
        return self._specs[i] if 0 <= i < len(self._specs) else None

    def on_kind_changed(self, index: int) -> None:
        self.add_target.setPlaceholderText(PLACEHOLDERS.get(index, ""))
        self.add_packages.setEnabled(index == 0)
        self.allow_remote.setEnabled(index == 2)

    def spec_from_form(self) -> docs_servers.ServerSpec:
        """The ServerSpec the Add form describes. Raises ValueError."""
        kind = self.kind.currentIndex()
        target = self.add_target.text().strip()
        packages = docs_qa.parse_packages(self.add_packages.text())
        name = self.add_name.text().strip()
        if kind == 0:
            python = target
            if python and not any(c in python for c in "/\\") and \
                    not python.lower().endswith(".exe"):
                import python_envs
                found = python_envs.find_env(python)
                if not found:
                    raise ValueError(f"No conda env called {python!r}.")
                python = found
            name = name or (", ".join(packages) or "Python packages") + (
                f" ({Path(python).parent.name})" if python else "")
            return docs_servers.bundled_spec(name, python=python,
                                             packages=packages)
        if kind == 1:
            command, args = docs_servers.parse_command_line(target)
            return docs_servers.ServerSpec(name=name or command,
                                           transport="stdio",
                                           command=command, args=args)
        return docs_servers.ServerSpec(name=name or target, transport="http",
                                       url=target,
                                       allow_remote=self.allow_remote
                                       .isChecked())

    def on_add(self) -> None:
        try:
            spec = self.spec_from_form()
            message = self.actions.add_server(spec)
        except ValueError as exc:
            self.status.setText(str(exc))
            return
        self.add_name.clear()
        self.add_target.clear()
        self.add_packages.clear()
        self.refresh_servers()
        self.server_list.setCurrentRow(len(self._specs) - 1)
        self.status.setText(message + " Press Test connection to try it.")

    def on_remove(self) -> None:
        spec = self.selected_spec()
        if spec is None:
            self.status.setText("Select a server to remove.")
            return
        try:
            message = self.actions.remove_server(spec.name)
        except ValueError as exc:
            self.status.setText(str(exc))
            return
        self.refresh_servers()
        self.status.setText(message)

    def on_test(self) -> None:
        spec = self.selected_spec()
        if spec is None:
            self.status.setText("Select a server to test.")
            return
        stop = self._stop_flag()
        self._start(f"Connecting to {spec.name}…",
                    lambda: self.actions.check_server(spec, stop),
                    name="docs-test", then=self._show_test)

    def _show_test(self, report) -> None:
        self.server_info.setPlainText("\n".join(report.lines()))
        self.status.setText(report.message)

    # -- the model -------------------------------------------------------
    def on_use_model(self) -> None:
        model_id = self.model_combo.currentData() or ""
        try:
            message = self.actions.set_docs_model(str(model_id))
        except Exception as exc:                          # noqa: BLE001
            self.status.setText(f"Could not save the choice: {exc}")
            return
        self.refresh_role()
        self.status.setText(message)

    def on_roles(self) -> None:
        show = getattr(self.window, "show_tab", None)
        if callable(show):
            show(MODELS_TAB)

    def on_check(self) -> None:
        stop = self._stop_flag()
        self._start("Checking the docs model — 5 questions…",
                    lambda: self.actions.capability_check(
                        should_stop=stop, progress=self._progress),
                    name="docs-check", then=self._show_check)

    def _show_check(self, report) -> None:
        self.check_result.setText("\n".join(report.lines()[:3]))
        self.status.setText("Check finished." if not report.stopped
                            else "Check stopped.")
        self.refresh_checks()

    # -- asking ----------------------------------------------------------
    def _stop_flag(self) -> Callable[[], bool]:
        """A should_stop bound to the job about to start (a new Event is made
        in _start, so read it through self at call time)."""
        return lambda: self._stop.is_set()

    def on_ask(self) -> None:
        question = self.question.toPlainText().strip()
        if not question:
            self.status.setText("Type a question first.")
            return
        packages = self.package.text()
        write_code = self.write_code.isChecked()
        use_tools = self.use_tools.isChecked()
        stop = self._stop_flag()
        self._start("Searching the documentation…",
                    lambda: self.actions.ask(question, packages, write_code,
                                             use_tools, should_stop=stop,
                                             progress=self._progress),
                    name="docs-ask", then=self.show_answer)

    def on_stop(self) -> None:
        self._stop.set()
        self.status.setText("Stopping…")

    def show_answer(self, ans: docs_qa.DocsAnswer) -> None:
        self._answer = ans
        self.answer_view.setHtml(render_answer(
            ans, self._tokens.get("muted_fg", "#888")))
        self.code.setPlainText(ans.code or "")
        if ans.code:
            self.code_status.setText(
                "Checked against the documentation: no problems found."
                if ans.code_ok else
                f"{len(ans.code_issues)} problem(s) remain — see above.")
        else:
            self.code_status.setText("")
        self.source_view.clear()
        if ans.stopped:
            self.status.setText("Stopped.")
        elif ans.error and not ans.ok:
            self.status.setText(ans.error)
        else:
            self.status.setText(("Answered: " if ans.covered else
                                 "Not covered by the documentation: ")
                                + timing_line(ans))

    def on_link(self, url: QUrl) -> None:
        text = url.toString()
        if not text.startswith("src:") or self._answer is None:
            return
        try:
            n = int(text[4:])
        except ValueError:
            return
        src = next((s for s in self._answer.sources if s.n == n), None)
        if src is not None:
            self.source_view.setPlainText(
                f"[{src.n}] {src.title}  ({src.server})\n\n{src.text}")

    def on_copy(self) -> None:
        text = self.code.toPlainText()
        if not text.strip():
            self.status.setText("No code to copy.")
            return
        QApplication.clipboard().setText(text)
        self.status.setText("Code copied.")


def build_docs(window) -> QWidget:
    """Factory for the tab registry."""
    return DocsTab(window)
