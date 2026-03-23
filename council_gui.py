import json
import threading
import queue
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext, filedialog
from urllib import request, error
from urllib.parse import urljoin
from functools import partial

# --------- HTTP helpers (stdlib only; no requests needed) ---------
def http_get(url, timeout=30):
    req = request.Request(url, method="GET")
    with request.urlopen(req, timeout=timeout) as resp:
        data = resp.read().decode("utf-8", errors="replace")
        ctype = resp.headers.get_content_type()
        if ctype == "application/json":
            return json.loads(data)
        return data

def http_post_json(url, payload, timeout=60):
    body = json.dumps(payload).encode("utf-8")
    req = request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    with request.urlopen(req, timeout=timeout) as resp:
        data = resp.read().decode("utf-8", errors="replace")
        ctype = resp.headers.get_content_type()
        if ctype == "application/json":
            return json.loads(data)
        return data

# --------- Worker thread wrapper to prevent UI blocking ---------
class JobThread(threading.Thread):
    def __init__(self, fn, args=(), kwargs=None, outq=None):
        super().__init__(daemon=True)
        self.fn = fn
        self.args = args
        self.kwargs = kwargs or {}
        self.outq = outq

    def run(self):
        try:
            result = self.fn(*self.args, **self.kwargs)
            if self.outq:
                self.outq.put(("ok", result))
        except Exception as e:
            if self.outq:
                self.outq.put(("err", str(e)))

# --------- GUI ---------
class CouncilConsole(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Council Console — Judge & Apothecary")
        self.geometry("980x700")
        self.minsize(900, 600)

        self.queue = queue.Queue()

        # Top bar: Coordinator URL
        top = ttk.Frame(self)
        top.pack(fill="x", padx=10, pady=8)

        ttk.Label(top, text="Coordinator URL:").pack(side="left")
        self.coord_var = tk.StringVar(value="http://127.0.0.1:3000/")
        self.coord_entry = ttk.Entry(top, textvariable=self.coord_var, width=45)
        self.coord_entry.pack(side="left", padx=6)

        self.btn_health = ttk.Button(top, text="Health", command=self.on_health)
        self.btn_health.pack(side="left", padx=4)

        self.lbl_status = ttk.Label(top, text="status: idle")
        self.lbl_status.pack(side="right")

        # Notebook tabs
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=10, pady=8)

        self.tab_apoth = ttk.Frame(self.nb)
        self.tab_draft = ttk.Frame(self.nb)
        self.tab_judge = ttk.Frame(self.nb)
        self.tab_config = ttk.Frame(self.nb)
        self.tab_logs = ttk.Frame(self.nb)

        self.nb.add(self.tab_apoth, text="Apothecary")
        self.nb.add(self.tab_draft, text="Draft")
        self.nb.add(self.tab_judge, text="Judge")
        self.nb.add(self.tab_config, text="Config")
        self.nb.add(self.tab_logs, text="Logs")

        self.build_apothecary_tab()
        self.build_draft_tab()
        self.build_judge_tab()
        self.build_config_tab()
        self.build_logs_tab()

        # Poll worker thread queue
        self.after(150, self.process_queue)

    # ------------- UI builders -------------
    def build_apothecary_tab(self):
        frm = self.tab_apoth

        btn_row = ttk.Frame(frm)
        btn_row.pack(fill="x", padx=8, pady=6)

        ttk.Button(btn_row, text="Poll Workers", command=self.on_apothecary).pack(side="left")

        self.tree = ttk.Treeview(frm, columns=("ok","worker","detail"), show="headings", height=18)
        self.tree.heading("ok", text="OK")
        self.tree.heading("worker", text="Worker URL")
        self.tree.heading("detail", text="Detail")
        self.tree.column("ok", width=60, anchor="center")
        self.tree.column("worker", width=280)
        self.tree.column("detail", width=560)
        self.tree.pack(fill="both", expand=True, padx=8, pady=4)

        self.apoth_json = scrolledtext.ScrolledText(frm, height=10)
        self.apoth_json.pack(fill="both", expand=False, padx=8, pady=6)

    def build_draft_tab(self):
        frm = self.tab_draft

        upper = ttk.Frame(frm)
        upper.pack(fill="x", padx=8, pady=6)
        ttk.Label(upper, text="Prompt:").pack(side="left")
        self.prompt_var = tk.StringVar()
        self.prompt_entry = ttk.Entry(upper, textvariable=self.prompt_var, width=80)
        self.prompt_entry.pack(side="left", padx=6)

        ttk.Label(upper, text="max_tokens:").pack(side="left", padx=8)
        self.max_tok = tk.IntVar(value=256)
        ttk.Spinbox(upper, from_=1, to=4096, textvariable=self.max_tok, width=8).pack(side="left")

        ttk.Button(upper, text="Send Draft", command=self.on_draft).pack(side="left", padx=8)

        self.draft_out = scrolledtext.ScrolledText(frm, height=22)
        self.draft_out.pack(fill="both", expand=True, padx=8, pady=6)

    def build_judge_tab(self):
        frm = self.tab_judge

        top = ttk.Frame(frm)
        top.pack(fill="x", padx=8, pady=6)

        ttk.Label(top, text="Criteria:").pack(side="left")
        self.criteria_var = tk.StringVar(value="clarity, relevance, correctness")
        ttk.Entry(top, textvariable=self.criteria_var, width=50).pack(side="left", padx=6)

        ttk.Button(top, text="Judge Options", command=self.on_judge).pack(side="left", padx=8)

        mid = ttk.Frame(frm)
        mid.pack(fill="both", expand=True, padx=8, pady=4)

        left = ttk.Frame(mid)
        left.pack(side="left", fill="both", expand=True)

        ttk.Label(left, text="Options (one per line):").pack(anchor="w")
        self.options_txt = scrolledtext.ScrolledText(left, height=14)
        self.options_txt.pack(fill="both", expand=True, pady=4)

        right = ttk.Frame(mid)
        right.pack(side="left", fill="both", expand=True)

        ttk.Label(right, text="Judge Result:").pack(anchor="w")
        self.judge_out = scrolledtext.ScrolledText(right, height=14)
        self.judge_out.pack(fill="both", expand=True, pady=4)

    def build_config_tab(self):
        frm = self.tab_config

        info = ttk.Label(frm, text=(
            "Edit workers.json below. The Coordinator reads this file at start.\n"
            "After saving, restart the Coordinator process to apply changes."
        ))
        info.pack(anchor="w", padx=8, pady=4)

        btns = ttk.Frame(frm)
        btns.pack(fill="x", padx=8, pady=4)

        ttk.Button(btns, text="Open workers.json", command=self.on_open_workers).pack(side="left")
        ttk.Button(btns, text="Save workers.json", command=self.on_save_workers).pack(side="left", padx=6)
        ttk.Button(btns, text="Load from Coordinator /health", command=self.on_load_from_health).pack(side="left", padx=6)

        self.cfg_txt = scrolledtext.ScrolledText(frm, height=20)
        self.cfg_txt.pack(fill="both", expand=True, padx=8, pady=6)

    def build_logs_tab(self):
        frm = self.tab_logs
        ttk.Label(frm, text="Log / JSON output:").pack(anchor="w", padx=8, pady=4)
        self.logs = scrolledtext.ScrolledText(frm, height=28)
        self.logs.pack(fill="both", expand=True, padx=8, pady=6)

    # ------------- actions -------------
    def coordinator_base(self):
        base = self.coord_var.get().strip()
        if not base.endswith("/"):
            base += "/"
        return base

    def log(self, text):
        self.logs.insert("end", text.rstrip() + "\n")
        self.logs.see("end")

    def set_status(self, text):
        self.lbl_status.config(text=f"status: {text}")

    def process_queue(self):
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "ok":
                    # payload must be a dict from our dispatch wrapper
                    self.handle_result(payload)
                else:
                    messagebox.showerror("Error", payload)
                    self.set_status("error")
        except queue.Empty:
            pass
        self.after(150, self.process_queue)

    def dispatch(self, name, fn, *args, **kwargs):
        self.set_status(name)
        outq = self.queue
        def wrapper(*a, **kw):
            res = fn(*a, **kw)
            return {"name": name, "result": res}
        JobThread(wrapper, args=args, kwargs=kwargs, outq=outq).start()

    # ---- button handlers ----
    def on_health(self):
        base = self.coordinator_base()
        self.dispatch("health", http_get, urljoin(base, "health"))

    def on_apothecary(self):
        base = self.coordinator_base()
        self.dispatch("apothecary", http_get, urljoin(base, "apothecary"))

    def on_draft(self):
        base = self.coordinator_base()
        prompt = self.prompt_var.get().strip()
        if not prompt:
            messagebox.showwarning("Input needed", "Enter a prompt.")
            return
        payload = {"prompt": prompt, "max_tokens": int(self.max_tok.get())}
        self.dispatch("draft", http_post_json, urljoin(base, "draft"), payload)

    def on_judge(self):
        base = self.coordinator_base()
        raw = self.options_txt.get("1.0", "end").strip()
        options = [line for line in (s.strip() for s in raw.splitlines()) if line]
        if not options:
            messagebox.showwarning("Input needed", "Enter at least one option (one per line).")
            return
        payload = {"options": options, "criteria": self.criteria_var.get().strip()}
        self.dispatch("judge", http_post_json, urljoin(base, "judge"), payload)

    def on_open_workers(self):
        try:
            with open("workers.json", "r", encoding="utf-8") as f:
                txt = f.read()
        except Exception as e:
            messagebox.showerror("Open workers.json", str(e))
            return
        self.cfg_txt.delete("1.0", "end")
        self.cfg_txt.insert("1.0", txt)

    def on_save_workers(self):
        try:
            txt = self.cfg_txt.get("1.0", "end").strip()
            # validate JSON
            obj = json.loads(txt)
            # write without BOM
            with open("workers.json", "w", encoding="utf-8") as f:
                f.write(json.dumps(obj, ensure_ascii=False, indent=2))
            messagebox.showinfo("Saved", "workers.json saved. Restart Coordinator to apply changes.")
        except Exception as e:
            messagebox.showerror("Save workers.json", str(e))

    def on_load_from_health(self):
        base = self.coordinator_base()
        def pull():
            data = http_get(urljoin(base, "health"))
            return data
        self.dispatch("load_health", pull)

    # ---- result demux ----
    def handle_result(self, payload):
        name = payload.get("name")
        result = payload.get("result")

        if name == "health":
            self.log(json.dumps(result, indent=2, ensure_ascii=False))
            messagebox.showinfo("Health", "Coordinator responded.")
        elif name == "apothecary":
            # populate table
            for item in self.tree.get_children():
                self.tree.delete(item)
            workers = result.get("workers", []) if isinstance(result, dict) else []
            for rec in workers:
                ok = rec.get("ok")
                wrk = rec.get("worker")
                detail = rec.get("detail") or rec.get("error") or ""
                if isinstance(detail, dict):
                    detail = json.dumps(detail, ensure_ascii=False)
                self.tree.insert("", "end", values=(str(ok), wrk, detail))
            self.apoth_json.delete("1.0","end")
            self.apoth_json.insert("1.0", json.dumps(result, indent=2, ensure_ascii=False))
            self.log("[Apothecary] polled workers.")
        elif name == "draft":
            self.draft_out.delete("1.0", "end")
            try:
                if isinstance(result, dict):
                    text = result.get("text") or json.dumps(result, ensure_ascii=False, indent=2)
                else:
                    text = str(result)
            except Exception:
                text = str(result)
            self.draft_out.insert("1.0", text)
            self.log("[Draft] completed.")
        elif name == "judge":
            self.judge_out.delete("1.0", "end")
            self.judge_out.insert("1.0", json.dumps(result, indent=2, ensure_ascii=False))
            self.log("[Judge] completed.")
        elif name == "load_health":
            # show what /health says (includes workers list, judge mode)
            self.cfg_txt.delete("1.0", "end")
            self.cfg_txt.insert("1.0", json.dumps(result, indent=2, ensure_ascii=False))
            self.log("[Config] Loaded from /health.")
        self.set_status("idle")


if __name__ == "__main__":
    app = CouncilConsole()
    app.mainloop()
