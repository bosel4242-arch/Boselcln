
import os, json, time, uuid, hashlib, base64, subprocess, threading
import requests
from fastapi import FastAPI, Request, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

SERVER_KEY = os.environ.get("MIDTRANS_SERVER_KEY", "")
IS_PROD = os.environ.get("MIDTRANS_PROD", "true").lower() == "true"
ALLOWED = os.environ.get("ALLOWED_ORIGIN", "*")
SNAP_URL = ("https://app.midtrans.com" if IS_PROD else "https://app.sandbox.midtrans.com") + "/snap/v1/transactions"
PLANS = {"monthly": (15000, 31)}
DB_FILE, CLIP_DIR = "/tmp/orders.json", "/tmp/clips"
os.makedirs(CLIP_DIR, exist_ok=True)

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=[ALLOWED] if ALLOWED != "*" else ["*"],
                   allow_methods=["*"], allow_headers=["*"])
lock = threading.Lock()
jobs = {}

def load():
    try: return json.load(open(DB_FILE))
    except Exception: return {}
def save(d): json.dump(d, open(DB_FILE, "w"))

def is_active(order_id):
    o = load().get(order_id)
    return bool(o and o.get("paid") and o["expires"] > time.time())

@app.get("/")
def root(): return {"ok": True}

@app.post("/api/create-transaction")
async def create_tx(req: Request):
    if not SERVER_KEY: raise HTTPException(500, "MIDTRANS_SERVER_KEY belum diset")
    plan = (await req.json()).get("plan")
    if plan not in PLANS: raise HTTPException(400, "Paket tidak valid")
    price, days = PLANS[plan]
    order_id = "KK-" + uuid.uuid4().hex[:12]
    auth = base64.b64encode((SERVER_KEY + ":").encode()).decode()
    r = requests.post(SNAP_URL, headers={"Authorization": "Basic " + auth, "Content-Type": "application/json"},
        json={"transaction_details": {"order_id": order_id, "gross_amount": price},
              "item_details": [{"id": plan, "price": price, "quantity": 1, "name": "KlipKilat All Access " + plan}]}, timeout=30)
    if r.status_code >= 300: raise HTTPException(502, "Midtrans: " + r.text[:200])
    with lock:
        d = load(); d[order_id] = {"paid": False, "amount": price, "days": days, "expires": 0}; save(d)
    return {"token": r.json()["token"], "order_id": order_id}

@app.post("/api/notify")
async def notify(req: Request):
    b = await req.json()
    raw = b.get("order_id", "") + b.get("status_code", "") + b.get("gross_amount", "") + SERVER_KEY
    if hashlib.sha512(raw.encode()).hexdigest() != b.get("signature_key"): raise HTTPException(403, "signature salah")
    with lock:
        d = load(); o = d.get(b["order_id"])
        if o and b.get("transaction_status") in ("settlement", "capture") and b.get("fraud_status", "accept") == "accept":
            o["paid"] = True; o["expires"] = time.time() + o["days"] * 86400; save(d)
    return {"ok": True}

@app.get("/api/status/{order_id}")
def status(order_id: str):
    o = load().get(order_id)
    return {"active": is_active(order_id), "expires": o["expires"] if o else 0}

def run_job(job_id, url, count):
    j = jobs[job_id]
    try:
        src = f"{CLIP_DIR}/{job_id}_src.mp4"
        subprocess.run(["yt-dlp", "-f", "bv*[height<=720]+ba/b[height<=720]", "--merge-output-format", "mp4",
                        "--no-playlist", "-o", src, url], check=True, timeout=900)
        dur = float(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                    "-of", "csv=p=0", src]).decode().strip())
        seg = min(40, dur / count)
        out = []
        for i in range(count):
            start = max(0, (dur / (count + 1)) * (i + 1) - seg / 2)
            fn = f"{job_id}_{i+1}.mp4"
            subprocess.run(["ffmpeg", "-y", "-ss", str(start), "-t", str(seg), "-i", src,
                "-vf", "crop=ih*9/16:ih,scale=720:1280", "-c:v", "libx264", "-preset", "veryfast",
                "-c:a", "aac", f"{CLIP_DIR}/{fn}"], check=True, timeout=600)
            out.append({"id": i + 1, "title": f"Klip {i+1}", "duration": int(seg), "viralScore": 0,
                        "hookText": "", "file": fn})
        os.remove(src)
        j.update(status="done", clips=out)
    except Exception as e:
        j.update(status="error", error=str(e)[:200])

@app.post("/api/clip")
async def clip(req: Request, x_order_id: str = Header(default="")):
    b = await req.json()
    paid = is_active(x_order_id)
    count = min(int(b.get("count", 3)), 5 if paid else 1)
    job_id = uuid.uuid4().hex[:10]
    jobs[job_id] = {"status": "running"}
    threading.Thread(target=run_job, args=(job_id, b.get("url", ""), count), daemon=True).start()
    return {"job_id": job_id, "paid": paid}

@app.get("/api/job/{job_id}")
def job(job_id: str): return jobs.get(job_id, {"status": "error", "error": "job tidak ada"})

@app.get("/api/download/{fn}")
def download(fn: str, x_order_id: str = Header(default="")):
    if not is_active(x_order_id): raise HTTPException(402, "Butuh All Access untuk download")
    path = os.path.join(CLIP_DIR, os.path.basename(fn))
    if not os.path.exists(path): raise HTTPException(404, "File tidak ada")
    return FileResponse(path, media_type="video/mp4", filename=os.path.basename(fn))
