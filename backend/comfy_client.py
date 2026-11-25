import json, time, random, uuid
from pathlib import Path
import requests

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / "workflows" / "wan_t2v_api.json"
CLIPS_DIR = ROOT / "outputs" / "clips"
PORTS = [8000, 8188]  # ComfyUI Desktop usually uses 8000, classic uses 8188


def find_server():
    for port in PORTS:
        url = f"http://127.0.0.1:{port}"
        try:
            requests.get(url + "/system_stats", timeout=3).raise_for_status()
            return url
        except Exception:
            continue
    raise RuntimeError("ComfyUI is not reachable. Is ComfyUI Desktop running?")


def build_workflow(prompt, width=384, height=672, length=33, steps=20,
                   seed=None, negative_extra=""):
    wf = json.loads(WORKFLOW.read_text(encoding="utf-8"))
    ks_id = next(i for i, n in wf.items() if n["class_type"] == "KSampler")
    ks = wf[ks_id]
    ks["inputs"]["seed"] = seed if seed is not None else random.randint(0, 2**48)
    ks["inputs"]["steps"] = steps
    pos_id = ks["inputs"]["positive"][0]
    wf[pos_id]["inputs"]["text"] = prompt
    if negative_extra:
        neg_id = ks["inputs"]["negative"][0]
        wf[neg_id]["inputs"]["text"] = negative_extra + ", " + wf[neg_id]["inputs"]["text"]
    lat_id = next(i for i, n in wf.items() if "EmptyHunyuanLatentVideo" in n["class_type"])
    wf[lat_id]["inputs"].update(width=width, height=height, length=length, batch_size=1)
    return wf


def generate_clip(prompt, out_name=None, **kwargs):
    server = find_server()
    wf = build_workflow(prompt, **kwargs)
    r = requests.post(server + "/prompt",
                      json={"prompt": wf, "client_id": str(uuid.uuid4())})
    if r.status_code != 200:
        raise RuntimeError(f"ComfyUI rejected the workflow: {r.text}")
    prompt_id = r.json()["prompt_id"]
    print("Queued:", prompt_id)

    while True:  # poll until finished
        hist = requests.get(f"{server}/history/{prompt_id}").json()
        if prompt_id in hist:
            break
        time.sleep(3)
    entry = hist[prompt_id]
    if entry.get("status", {}).get("status_str") == "error":
        raise RuntimeError(f"Generation failed: {entry['status']}")

    for node_out in entry["outputs"].values():
        for items in node_out.values():
            if not isinstance(items, list):
                continue
            for f in items:
                if isinstance(f, dict) and str(f.get("filename", "")).endswith((".mp4", ".webm", ".gif")):
                    data = requests.get(server + "/view", params={
                        "filename": f["filename"],
                        "subfolder": f.get("subfolder", ""),
                        "type": f.get("type", "output")}).content
                    CLIPS_DIR.mkdir(parents=True, exist_ok=True)
                    dest = CLIPS_DIR / (out_name or f["filename"])
                    dest.write_bytes(data)
                    return dest
    raise RuntimeError("No video found in ComfyUI output.")


if __name__ == "__main__":
    t = time.time()
    path = generate_clip("a red fox running through a snowy forest, cinematic, tracking camera",
                         out_name="test_clip.mp4")
    print(f"Saved {path} in {time.time() - t:.0f}s")