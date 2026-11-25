import json, random, threading, queue, time, uuid
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import uvicorn

from comfy_client import CLIPS_DIR, ROOT
from make_video import split_scenes, make_video, FINAL_DIR

OUT = ROOT / "outputs"
HISTORY = OUT / "history.json"
FRONT = ROOT / "frontend"
for d in (OUT, CLIPS_DIR, FINAL_DIR):
    d.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="AI Video Studio")
jobs: dict = {}
jobq: "queue.Queue[str]" = queue.Queue()


def save_history():
    HISTORY.write_text(json.dumps(jobs, indent=2), encoding="utf-8")


def load_history():
    if HISTORY.exists():
        for k, v in json.loads(HISTORY.read_text(encoding="utf-8")).items():
            if v["status"] in ("queued", "running"):
                v["status"], v["message"] = "error", "Interrupted (app was closed)"
            jobs[k] = v


class Req(BaseModel):
    text: str
    narration: str = ""
    scenes: int = 4
    width: int = 384
    height: int = 672
    length: int = 33
    slow: float = 1.5
    avoid: str = ""
    voice: str = "female"
    captions: bool = True
    cooldown: int = 30


def worker():
    while True:
        jid = jobq.get()
        job = jobs[jid]
        try:
            job["status"], job["started"] = "running", time.time()

            def progress(done, total, msg):
                job["done"], job["message"] = done, msg
                save_history()

            out = make_video(
                job["prompts"], jid, narrations=job["narrations"], voice=job["voice"],
                captions=job["captions"], slow=job["slow"], cooldown=job["cooldown"],
                progress=progress,
                gen=dict(width=job["width"], height=job["height"], length=job["length"],
                         seed=job["seed"], negative_extra=job["avoid"]))
            job["video"] = f"/outputs/final/{out.name}"
            job["status"], job["message"] = "done", "Finished"
        except Exception as e:
            job["status"], job["message"] = "error", str(e)[:600]
        job["finished"] = time.time()
        save_history()


@app.post("/api/generate")
def generate(req: Req):
    lines = [l.strip() for l in req.text.splitlines() if l.strip()]
    if not lines:
        raise HTTPException(400, "Enter a prompt")
    prompts = lines if len(lines) > 1 else split_scenes(lines[0], req.scenes)
    narrations = [l.strip() for l in req.narration.splitlines()]
    jid = time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:4]
    jobs[jid] = dict(id=jid, status="queued", message="Waiting in queue", prompts=prompts,
                     narrations=narrations, total=len(prompts), done=0, video=None,
                     created=time.time(), width=req.width, height=req.height,
                     length=req.length, slow=req.slow, avoid=req.avoid, voice=req.voice,
                     captions=req.captions, cooldown=req.cooldown,
                     seed=random.randint(0, 2**48))
    save_history()
    jobq.put(jid)
    return jobs[jid]


@app.get("/api/jobs/{jid}")
def get_job(jid: str):
    if jid not in jobs:
        raise HTTPException(404, "Job not found")
    return jobs[jid]


@app.get("/api/history")
def history():
    return sorted(jobs.values(), key=lambda j: j["created"], reverse=True)


@app.get("/")
def index():
    return FileResponse(FRONT / "index.html")


app.mount("/outputs", StaticFiles(directory=OUT), name="outputs")

if __name__ == "__main__":
    load_history()
    threading.Thread(target=worker, daemon=True).start()
    uvicorn.run(app, host="127.0.0.1", port=8080)