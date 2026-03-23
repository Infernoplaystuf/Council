from fastapi import FastAPI
from pydantic import BaseModel
import httpx, time, json, os

APP_DIR  = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(APP_DIR, "workers.json")

with open(CFG_PATH, "r", encoding="utf-8") as f:
    CFG = json.load(f)

WORKERS    = CFG.get("workers", [])
JUDGE_URL  = (CFG.get("judge_url", "") or "").strip()
JUDGE_MODE = CFG.get("judge_model", "stub").strip().lower()
TIMEOUT_S  = int(CFG.get("timeout_s", 60))

app = FastAPI(title="Council Coordinator (Desktop)")

class Job(BaseModel):
    prompt: str
    max_tokens: int = 256

class JudgeInput(BaseModel):
    options: list[str]
    criteria: str = "clarity, relevance, correctness"

def choose_worker() -> str:
    if not WORKERS:
        raise RuntimeError("No workers configured.")
    return WORKERS[int(time.time()) % len(WORKERS)]

@app.get("/health")
def health():
    return {
        "ok": True,
        "workers": WORKERS,
        "judge": {"mode": JUDGE_MODE, "url": JUDGE_URL or None}
    }

@app.get("/apothecary")
def apothecary():
    status = []
    for w in WORKERS:
        rec = {"worker": w, "ok": False}
        try:
            with httpx.Client(timeout=TIMEOUT_S) as cli:
                r = cli.get(f"{w}/health")
                rec["ok"] = (r.status_code == 200)
                rec["detail"] = r.json() if rec["ok"] else {"status_code": r.status_code}
        except Exception as e:
            rec["error"] = str(e)
        status.append(rec)
    return {"ok": True, "workers": status, "ts": time.time()}

@app.post("/draft")
def draft(job: Job):
    target = choose_worker()
    try:
        with httpx.Client(timeout=TIMEOUT_S) as cli:
            r = cli.post(f"{target}/generate", json=job.dict())
            r.raise_for_status()
            out = r.json()
    except Exception as e:
        out = {"ok": False, "error": str(e), "worker": target}
    try:
        os.makedirs(os.path.join(APP_DIR, "logs"), exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        with open(os.path.join(APP_DIR, "logs", f"{ts}_draft.json"), "w", encoding="utf-8") as f:
            json.dump({"job": job.dict(), "out": out, "worker": target}, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    return out

def judge_stub(options: list[str]) -> dict:
    choice = max((o for o in options if o.strip()), key=len, default="")
    return {"choice": choice, "policy": "length-as-proxy"}

def judge_via_local_llm(options: list[str], criteria: str) -> dict:
    if not JUDGE_URL:
        return {"choice": "", "policy": "local-llm", "error": "JUDGE_URL not configured"}
    prompt = (
        "You are a strict impartial judge.\n"
        f"Criteria: {criteria}\n"
        "Given the numbered options below, return a JSON object with keys "
        '"index" (integer of the best option) and "rationale" (brief string).\n\n' +
        "\n".join(f"Option {i}: {opt}" for i, opt in enumerate(options))
    )
    try:
        with httpx.Client(timeout=TIMEOUT_S) as cli:
            r = cli.post(JUDGE_URL, json={"prompt": prompt})
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        return {"choice": "", "policy": "local-llm", "error": str(e)}
    idx = None
    if isinstance(data, dict) and "index" in data:
        try:
            idx = int(data["index"])
        except Exception:
            idx = None
    if idx is None:
        text = data.get("text") if isinstance(data, dict) else str(data)
        for i in range(len(options)):
            if f"Option {i}" in str(text) or f"{i}." in str(text):
                idx = i; break
    if idx is None:
        idx = 0
    return {"choice": options[idx], "policy": "local-llm", "raw": data}

@app.post("/judge")
def judge(inp: JudgeInput):
    if JUDGE_MODE == "local":
        return judge_via_local_llm(inp.options, inp.criteria)
    return judge_stub(inp.options)
from typing import Optional

class Task(BaseModel):
    worker_url: str
    kind: str            # "apt", "pip", "fetch", "models", "restart"
    payload: dict = {}

REG_PATH = os.path.join(APP_DIR, "registry.json")
if not os.path.exists(REG_PATH):
    with open(REG_PATH, "w", encoding="utf-8") as f: f.write("{}")

def _save_registry(entry: dict):
    try:
        with open(REG_PATH, "r+", encoding="utf-8") as f:
            reg = json.load(f)
            w = entry.get("worker")
            if w not in reg: reg[w] = {}
            if entry.get("kind") == "fetch" and entry.get("ok"):
                arr = reg[w].get("models", [])
                arr = [e for e in arr if e.get("filename") != entry.get("filename")]
                arr.append({
                    "filename": entry.get("filename"),
                    "bytes": entry.get("bytes"),
                    "sha256": entry.get("sha256"),
                    "ts": entry.get("ts", time.time())
                })
                reg[w]["models"] = arr
            reg[w]["ts"] = time.time()
            f.seek(0); f.truncate()
            json.dump(reg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

@app.get("/apothecary/registry")
def apoth_registry():
    try:
        with open(REG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

@app.post("/apothecary/task")
def apoth_task(task: Task):
    paths = {
        "apt":     "apoth/install",
        "pip":     "apoth/pip",
        "fetch":   "apoth/fetch_model",
        "models":  "apoth/models",
        "restart": "apoth/restart_worker",
    }
    if task.kind not in paths:
        return {"ok": False, "error": f"unknown task kind: {task.kind}"}
    url = task.worker_url.rstrip("/") + "/" + paths[task.kind]
    try:
        with httpx.Client(timeout=TIMEOUT_S) as cli:
            if task.kind == "models":
                r = cli.get(url)
            elif task.kind == "restart":
                r = cli.post(url)
            else:
                r = cli.post(url, json=task.payload)
            r.raise_for_status()
            out = r.json()
    except Exception as e:
        return {"ok": False, "error": str(e), "worker": task.worker_url, "kind": task.kind}
    if task.kind == "fetch":
        entry = {"worker": task.worker_url, "kind": "fetch", "ok": out.get("ok"),
                 "filename": out.get("filename"), "bytes": out.get("bytes"),
                 "sha256": out.get("sha256"), "ts": time.time()}
        _save_registry(entry)
    return {"ok": True, "worker": task.worker_url, "kind": task.kind, "result": out}