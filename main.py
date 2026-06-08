import os
import re
import logging
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from concurrent.futures import ThreadPoolExecutor, as_completed
from fastapi import FastAPI, HTTPException
import uvicorn

# ───────────────── 🛠️ LOGGING & CONFIGURATION ─────────────────
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
logger = logging.getLogger("GuidelyAPI")

# Environment Variables (Inhe Railway Variables mein add kar lena)
BASE_URL = os.getenv("GUIDELY_BASE_URL", "https://mobapi.guidely.in")
DRM_BASE_URL = os.getenv("GUIDELY_DRM_URL", "https://guidely.in/blog/drmplayer")
API_KEY = os.getenv("GUIDELY_API_KEY", "85a1364c-0419-42d5-b4c9-5dbe71549743")
AUTH_TOKEN = os.getenv("GUIDELY_AUTH_TOKEN", "384561026d1529e5a9fbc852f029fe160457c89b12a4c32afb568aee2a86d145ce48b14c1b5b4d9ac9a9d2942423d9ebf08a4bb3ab53687008f6137f5aa62df8")
DEVICE_ID = os.getenv("GUIDELY_DEVICE_ID", "PQ3B.190801.04221524")
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "5"))

# ───────────────── 🌐 GLOBAL SESSION (Memory Leak Fix) ─────────────────
session = requests.Session()
retry = Retry(total=3, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504], allowed_methods=["GET"])
adapter = HTTPAdapter(pool_connections=50, pool_maxsize=50, max_retries=retry)
session.mount("http://", adapter)
session.mount("https://", adapter)
session.headers.update({
    "User-Agent": "Dart/3.5 (dart:io)",
    "platform": "Android",
    "api-key": API_KEY,
    "auth": AUTH_TOKEN,
    "Content-Type": "application/json; charset=utf-8",
    "accept-encoding": "gzip",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache"
})

# ───────────────── 🛠️ CORE EXTRACTION LOGIC ─────────────────
def _extract_token(iframe):
    if not iframe or not isinstance(iframe, str): return None
    m = re.search(r'(?:access_token|token|id)=([a-zA-Z0-9\-_.]+)', iframe)
    return m.group(1) if m else None

def _get_m3u8(token):
    if not token: return None
    endpoints = [
        {"url": f"{DRM_BASE_URL}/new-main.php", "params": {"token": token, "device_id": DEVICE_ID}, "headers": {"User-Agent": "Dart/3.5 (dart:io)", "Content-Type": "application/json"}},
        {"url": f"{DRM_BASE_URL}/tpstream-player.php", "params": {"token": token}, "headers": {"User-Agent": "Mozilla/5.0", "Referer": "https://guidely.in/"}},
        {"url": f"{DRM_BASE_URL}/player.php", "params": {"token": token, "device_id": DEVICE_ID}, "headers": {"User-Agent": "Dart/3.5 (dart:io)"}}
    ]
    for ep in endpoints:
        try:
            r = session.get(ep["url"], params=ep["params"], headers=ep["headers"], timeout=(5, 10))
            if r.status_code == 200:
                try:
                    data = r.json()
                    if data.get("status") and isinstance(data.get("data"), dict):
                        file_url = data["data"].get("file_url")
                        if file_url: return file_url
                except: continue
        except: continue
    return None

def _process_session(s):
    if not isinstance(s, dict): return None
    cat = (s.get("catgname") or "General").strip()
    title = (s.get("name") or "No Title").strip()
    pdf = s.get("urlpdf") if isinstance(s.get("urlpdf"), str) else None
    
    vid_raw = None
    vld = s.get("video_link_data")
    if isinstance(vld, dict): vid_raw = vld.get("video_id")
    if not vid_raw: vid_raw = _extract_token(s.get("iframe"))
    if not vid_raw and s.get("video_link"):
        m = re.search(r'/(\d+)$', s.get("video_link", ""))
        if m: vid_raw = m.group(1)
        
    m3u8 = _get_m3u8(vid_raw) if vid_raw else None
    if not m3u8 and not pdf: return None
    return {"cat": cat, "title": title, "video": m3u8, "pdf": pdf}

def _fetch_batches_sync():
    res = session.get(f"{BASE_URL}/video-class", timeout=10)
    if res.status_code != 200: return []
    data = res.json()
    products = data.get("data", {}).get("products", []) if isinstance(data.get("data"), dict) else data.get("products", [])
    if not isinstance(products, list): return []
    return [{"id": str(b.get("id")), "title": b.get("title", "Unknown"), "price": b.get("price", "0"), "image": b.get("image", "")} for b in products if isinstance(b, dict)]

def _extract_batch_sync(bid):
    res = session.get(f"{BASE_URL}/live-video-class-new/{bid}", timeout=20)
    if res.status_code != 200: raise Exception(f"Batch API Error: {res.status_code}")
    data = res.json()
    
    items = []
    for key in ["live", "upcmg", "prevs", "recorded", "classes", "sessions"]:
        lst = data.get("data", {}).get(key) if isinstance(data.get("data"), dict) else data.get(key)
        if isinstance(lst, list): items.extend(lst)
    if not items: raise Exception("No items found in this batch")
    
    valid = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(_process_session, item): i for i, item in enumerate(items)}
        for future in as_completed(futures):
            try:
                res = future.result()
                if res: valid.append(res)
            except Exception as e:
                logger.warning(f"Item processing error: {e}")
                
    # Group by subject
    grouped = {}
    for r in valid:
        cat = r["cat"]
        if cat not in grouped: grouped[cat] = []
        grouped[cat].append({"title": r["title"], "video": r["video"], "pdf": r["pdf"]})
        
    return {
        "total": len(valid),
        "video": sum(1 for r in valid if r["video"]),
        "pdf": sum(1 for r in valid if r["pdf"]),
        "subjects": grouped
    }

# ───────────────── 🚀 FASTAPI ROUTES (ENDPOINTS) ─────────────────
app = FastAPI(title="Guidely Extractor API", description="Your own API for Guidely extraction")

@app.get("/")
def root():
    return {"status": "running", "message": "Guidely API is ready!", "endpoints": ["/allbatch", "/batch/{batch_id}", "/drm/{token}"]}

@app.get("/allbatch")
def all_batch():
    """Fetches all available batches"""
    try:
        batches = _fetch_batches_sync()
        return {"success": True, "count": len(batches), "data": batches}
    except Exception as e:
        logger.error(f"Error in /allbatch: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/batch/{batch_id}")
def get_batch(batch_id: str):
    """Fetches all videos and PDFs for a specific batch"""
    try:
        result = _extract_batch_sync(batch_id)
        return {"success": True, "batch_id": batch_id, "data": result}
    except Exception as e:
        logger.error(f"Error in /batch/{batch_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/drm/{token}")
def get_drm(token: str):
    """Fetches a single M3U8 link for a video token"""
    try:
        url = _get_m3u8(token)
        if not url:
            return {"success": False, "message": "No DRM link found for this token"}
        return {"success": True, "data": {"file_url": url}}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ───────────────── 🏃 RUN SERVER ─────────────────
if __name__ == "__main__":
    # Railway automatically provides a PORT environment variable
    port = int(os.getenv("PORT", 8000))
    logger.info(f"🚀 Starting Guidely API server on port {port}")
    uvicorn.run(app, host="0.0.0.0", port=port)
