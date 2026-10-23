"""
local_chatbot.py — a chat window for a local model: Ollama, or a .gguf file
loaded with llama-cpp-python.

WHAT IT DOES
Pick a backend. "Ollama" lists the models the local Ollama server has
(127.0.0.1 only); "GGUF file" loads a .gguf you browse to; "Echo" needs
nothing and repeats you back, so the window can be tried with no model at
all. Type in the box and press Ctrl+Enter or Send; the reply streams in word
by word and Stop cuts it off. The system prompt, temperature and reply
length apply to the next message. New chat clears the conversation; Save
writes it as Markdown or JSON.

SHOWS
  * a QObject worker moved to a QThread, talking to the window only through
    Signals (token / finished / failed) — the UI never blocks on the model
  * streaming text into a QTextBrowser with a QTextCursor at the end, and
    chat "bubbles" from simple HTML
  * a Stop flag the worker checks between tokens (cooperative cancel)
  * backends behind one small interface, each imported lazily so the app
    starts without either package installed and says what is missing
  * QSplitter sidebar | conversation, QComboBox driven by another QComboBox
  * QPlainTextEdit input with a QShortcut (Ctrl+Return) to send
  * QProgressBar in busy mode (range 0..0) while the model works
  * QDoubleSpinBox / QSpinBox for sampling settings, QFileDialog for a .gguf
  * keeping the conversation as a list of {"role", "content"} dicts, the
    shape both Ollama and llama.cpp take

RUN
    python examples/qt_apps/local_chatbot.py

REQUIRES  ollama, llama_cpp
          Both optional: ollama for the Ollama backend, llama_cpp
          (pip install llama-cpp-python) for .gguf files. Each is imported
          only when its backend is picked, and says so when it is missing.
"""
from __future__ import annotations

import html
import json
import sys
from pathlib import Path
from typing import Dict, Iterator, List, Optional

from PySide6.QtCore import QObject, Qt, QThread, Signal, Slot
from PySide6.QtGui import QKeySequence, QShortcut, QTextCursor
from PySide6.QtWidgets import (QApplication, QComboBox, QDoubleSpinBox,
                               QFileDialog, QFormLayout, QHBoxLayout, QLabel,
                               QMainWindow, QMessageBox, QPlainTextEdit,
                               QProgressBar, QPushButton, QSpinBox,
                               QSplitter, QTextBrowser, QVBoxLayout, QWidget)

OLLAMA_HOST = "http://127.0.0.1:11434"
Message = Dict[str, str]


# ============================================================
# Backends: list models, stream a reply
# ============================================================

class Backend:
    name = "?"

    def models(self) -> List[str]:
        return []

    def stream(self, model: str, messages: List[Message], temperature: float,
               max_tokens: int) -> Iterator[str]:
        raise NotImplementedError


class EchoBackend(Backend):
    """No model: answers with the user's own words, for trying the UI."""
    name = "Echo (no model)"

    def models(self) -> List[str]:
        return ["echo"]

    def stream(self, model, messages, temperature, max_tokens):
        last = messages[-1]["content"] if messages else ""
        for word in f"You said: {last}".split(" "):
            yield word + " "


class OllamaBackend(Backend):
    name = "Ollama"

    def __init__(self):
        import ollama                       # imported here: optional package
        self.client = ollama.Client(host=OLLAMA_HOST)

    def models(self) -> List[str]:
        listed = self.client.list()
        return sorted(m.model for m in listed.models)

    def stream(self, model, messages, temperature, max_tokens):
        for chunk in self.client.chat(
                model=model, messages=messages, stream=True,
                options={"temperature": temperature,
                         "num_predict": max_tokens}):
            yield chunk["message"]["content"]


class GgufBackend(Backend):
    """One .gguf file, loaded once. Loading is slow, so the worker does it."""
    name = "GGUF file"

    def __init__(self, path: str, n_ctx: int = 4096):
        from llama_cpp import Llama           # optional package
        self.path = path
        self.llm = Llama(model_path=path, n_ctx=n_ctx, n_gpu_layers=-1,
                         verbose=False)

    def models(self) -> List[str]:
        return [Path(self.path).name]

    def stream(self, model, messages, temperature, max_tokens):
        for chunk in self.llm.create_chat_completion(
                messages=messages, stream=True, temperature=temperature,
                max_tokens=max_tokens):
            piece = chunk["choices"][0]["delta"].get("content")
            if piece:
                yield piece


# ============================================================
# The worker: runs in its own QThread
# ============================================================

class ChatWorker(QObject):
    token = Signal(str)
    finished = Signal(str)          # the whole reply
    failed = Signal(str)

    def __init__(self, backend: Backend, model: str, messages: List[Message],
                 temperature: float, max_tokens: int):
        super().__init__()
        self.backend, self.model = backend, model
        self.messages = list(messages)
        self.temperature, self.max_tokens = temperature, max_tokens
        self.stop_requested = False

    @Slot()
    def run(self) -> None:
        parts: List[str] = []
        try:
            for piece in self.backend.stream(self.model, self.messages,
                                             self.temperature,
                                             self.max_tokens):
                if self.stop_requested:
                    parts.append(" …[stopped]")
                    break
                parts.append(piece)
                self.token.emit(piece)
        except Exception as exc:                         # noqa: BLE001
            self.failed.emit(f"{type(exc).__name__}: {exc}")
            return
        self.finished.emit("".join(parts))


class LoadWorker(QObject):
    """Builds a backend off the UI thread (a GGUF load takes seconds)."""
    loaded = Signal(object, list)
    failed = Signal(str)

    def __init__(self, kind: str, path: str = ""):
        super().__init__()
        self.kind, self.path = kind, path

    @Slot()
    def run(self) -> None:
        try:
            if self.kind == OllamaBackend.name:
                backend: Backend = OllamaBackend()
            elif self.kind == GgufBackend.name:
                backend = GgufBackend(self.path)
            else:
                backend = EchoBackend()
            self.loaded.emit(backend, backend.models())
        except ImportError as exc:
            pkg = "ollama" if self.kind == OllamaBackend.name else \
                "llama-cpp-python"
            self.failed.emit(f"{exc}. Install it with: pip install {pkg}")
        except Exception as exc:                         # noqa: BLE001
            self.failed.emit(f"{type(exc).__name__}: {exc}")


# ============================================================
# The window
# ============================================================

BUBBLE = ('<div style="margin:6px 0; padding:8px; border-radius:8px; '
          'background:{bg};"><b>{who}</b><br>{text}</div>')


class ChatWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Local chatbot")
        self.resize(980, 680)
        self.backend: Optional[Backend] = None
        self.history: List[Message] = []
        self.thread: Optional[QThread] = None
        self.worker: Optional[QObject] = None

        split = QSplitter(Qt.Horizontal)
        split.addWidget(self._sidebar())
        split.addWidget(self._conversation())
        split.setStretchFactor(1, 1)
        split.setSizes([280, 700])
        self.setCentralWidget(split)

        self.busy = QProgressBar(maximumWidth=140, textVisible=False)
        self.busy.setRange(0, 0)
        self.busy.hide()
        self.statusBar().addPermanentWidget(self.busy)
        QShortcut(QKeySequence("Ctrl+Return"), self, activated=self.send)
        self.connect_backend()

    def _sidebar(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        self.kind = QComboBox()
        self.kind.addItems([EchoBackend.name, OllamaBackend.name,
                            GgufBackend.name])
        self.kind.currentTextChanged.connect(self._kind_changed)
        form.addRow("Backend", self.kind)

        row = QHBoxLayout()
        self.model = QComboBox()
        self.model.setMinimumContentsLength(14)
        row.addWidget(self.model, 1)
        self.refresh = QPushButton("↻", toolTip="List the models again")
        self.refresh.setFixedWidth(32)
        self.refresh.clicked.connect(self.connect_backend)
        row.addWidget(self.refresh)
        form.addRow("Model", row)

        self.browse = QPushButton("Choose .gguf file…")
        self.browse.clicked.connect(self.choose_gguf)
        self.browse.hide()
        form.addRow(self.browse)

        self.temperature = QDoubleSpinBox(minimum=0.0, maximum=2.0,
                                          singleStep=0.1, value=0.7)
        form.addRow("Temperature", self.temperature)
        self.max_tokens = QSpinBox(minimum=16, maximum=8192, singleStep=64,
                                   value=512)
        form.addRow("Max reply tokens", self.max_tokens)

        form.addRow(QLabel("System prompt"))
        self.system_prompt = QPlainTextEdit("You are a helpful assistant. Answer "
                                     "briefly.")
        self.system_prompt.setMaximumHeight(110)
        form.addRow(self.system_prompt)

        new = QPushButton("New chat")
        new.clicked.connect(self.new_chat)
        save = QPushButton("Save chat…")
        save.clicked.connect(self.save_chat)
        form.addRow(new)
        form.addRow(save)
        return w

    def _conversation(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        self.view = QTextBrowser(openExternalLinks=False)
        v.addWidget(self.view, 1)
        self.input = QPlainTextEdit(placeholderText="Message (Ctrl+Enter "
                                                    "to send)")
        self.input.setMaximumHeight(110)
        v.addWidget(self.input)
        row = QHBoxLayout()
        row.addStretch(1)
        self.stop_btn = QPushButton("Stop", enabled=False)
        self.stop_btn.clicked.connect(self.stop)
        self.send_btn = QPushButton("Send", default=True)
        self.send_btn.clicked.connect(self.send)
        row.addWidget(self.stop_btn)
        row.addWidget(self.send_btn)
        v.addLayout(row)
        return w

    # ---------------------------------------------------------- backends
    def _kind_changed(self, kind: str) -> None:
        self.browse.setVisible(kind == GgufBackend.name)
        self.refresh.setVisible(kind != GgufBackend.name)
        if kind != GgufBackend.name:
            self.connect_backend()
        else:
            self.backend = None
            self.model.clear()
            self.statusBar().showMessage("Choose a .gguf file.")

    def choose_gguf(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "GGUF model", "",
                                              "GGUF models (*.gguf)")
        if path:
            self.connect_backend(path)

    def connect_backend(self, path: str = "") -> None:
        kind = self.kind.currentText()
        self.statusBar().showMessage(f"Connecting to {kind}…")
        worker = LoadWorker(kind, path if isinstance(path, str) else "")
        worker.loaded.connect(self._backend_ready)
        worker.failed.connect(self._backend_failed)
        self._start(worker)

    @Slot(object, list)
    def _backend_ready(self, backend: Backend, models: List[str]) -> None:
        self.backend = backend
        self.model.clear()
        self.model.addItems(models)
        self.statusBar().showMessage(
            f"{backend.name}: {len(models)} model(s)" if models
            else f"{backend.name} has no models")

    @Slot(str)
    def _backend_failed(self, why: str) -> None:
        self.backend = None
        self.model.clear()
        self.statusBar().showMessage("Not connected")
        QMessageBox.warning(self, "Could not connect", why)

    # ---------------------------------------------------------- chatting
    def send(self) -> None:
        text = self.input.toPlainText().strip()
        if not text or self.thread is not None:
            return
        if self.backend is None or not self.model.currentText():
            self.statusBar().showMessage("No model is ready.")
            return
        self.input.clear()
        self.history.append({"role": "user", "content": text})
        self._bubble("You", text, "#dbeafe")
        self._bubble("Assistant", "", "#f1f5f9")
        messages = ([{"role": "system",
                      "content": self.system_prompt.toPlainText().strip()}]
                    + self.history)
        worker = ChatWorker(self.backend, self.model.currentText(), messages,
                            self.temperature.value(), self.max_tokens.value())
        worker.token.connect(self._token)
        worker.finished.connect(self._reply_done)
        worker.failed.connect(self._reply_failed)
        self.send_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self._start(worker)

    def stop(self) -> None:
        if isinstance(self.worker, ChatWorker):
            self.worker.stop_requested = True

    def _bubble(self, who: str, text: str, bg: str) -> None:
        body = html.escape(text).replace("\n", "<br>")
        self.view.append(BUBBLE.format(who=who, text=body, bg=bg))

    @Slot(str)
    def _token(self, piece: str) -> None:
        cur = self.view.textCursor()
        cur.movePosition(QTextCursor.End)
        cur.insertText(piece)
        self.view.setTextCursor(cur)
        self.view.ensureCursorVisible()

    @Slot(str)
    def _reply_done(self, reply: str) -> None:
        self.history.append({"role": "assistant", "content": reply})
        self.statusBar().showMessage(f"{len(reply)} characters")

    @Slot(str)
    def _reply_failed(self, why: str) -> None:
        self.history.pop()                      # the unanswered user turn
        self._token(f"[error: {why}]")
        self.statusBar().showMessage("The model failed; see the reply.")

    def new_chat(self) -> None:
        self.history = []
        self.view.clear()

    def save_chat(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Save chat", "chat.md", "Markdown (*.md);;JSON (*.json)")
        if not path:
            return
        if path.endswith(".json"):
            text = json.dumps(self.history, indent=2)
        else:
            text = "\n\n".join(f"**{m['role']}:** {m['content']}"
                               for m in self.history)
        Path(path).write_text(text, encoding="utf-8")
        self.statusBar().showMessage(f"Saved {path}")

    # ---------------------------------------------------------- threads
    def _start(self, worker: QObject) -> None:
        """Run ``worker.run`` on a fresh QThread; one job at a time."""
        if self.thread is not None:
            return
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        for sig in ("finished", "failed", "loaded"):
            if hasattr(worker, sig):
                getattr(worker, sig).connect(thread.quit)
        thread.finished.connect(self._thread_done)
        self.thread, self.worker = thread, worker
        self.busy.show()
        thread.start()

    def _thread_done(self) -> None:
        self.thread.deleteLater()
        self.worker.deleteLater()
        self.thread = self.worker = None
        self.busy.hide()
        self.send_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)

    def closeEvent(self, event) -> None:
        self.stop()
        if self.thread is not None:
            self.thread.quit()
            self.thread.wait(3000)
        super().closeEvent(event)


def make_window(argv: Optional[List[str]] = None) -> ChatWindow:
    return ChatWindow()


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    win = make_window()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
