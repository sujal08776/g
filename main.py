import os
import re
import logging
import asyncio
import httpx
from fastapi import FastAPI, HTTPException
import uvicorn

# ───────────────── 🛠️ LOGGING & CONFIGURATION ─────────────────
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
logger = logging.getLogger("GuidelyAPI")

BASE_URL = os.getenv("GUIDELY_BASE_URL", "https://mobapi.guidely.in")
DRM_BASE_URL = os.getenv("GUIDELY_DRM_URL", "https://guidely.in/blog/drmplayer")
API_KEY = os.getenv("GUIDELY_API_KEY", "85a1364c-0419-42d5-b4c9-5dbe71549743")
AUTH_TOKEN = os.getenv("GUIDELY_AUTH_TOKEN", "384561026d1529e5a9fbc852f029fe160457c89b12a4c32afb568aee2a86d145ce48b14c1b5b4d9ac9a9d2942423d9ebf08a4bb3ab53687008f6137f5aa62df8")
DEVICE_ID = os.getenv("GUIDELY_DEVICE_ID", "PQ3B.190801.04221524")

# ───────────────── 🌐 GLOBAL ASYNC HTTP CLIENT ─────────────────
client = httpx.AsyncClient(
    timeout=httpx.Timeout(15.0, connect=5.0),
    limits=httpx.Limits(max_keepalive_connections=50, max_connections=100),
    headers={
        "User-Agent": "Dart/3.5 (dart:io)",
        "platform": "Android",
        "api-key": API_KEY,
        "auth": AUTH_TOKEN,
        "Content-Type": "application/json; charset=utf-8",
        "accept-encoding": "gzip",
    }
)

# ───────────────── 🛠️ CORE EXTRACTION LOGIC ─────────────────
def _extract_token(iframe):
    if not iframe or not isinstance(iframe, str): return None
    m = re.search(r'(?:access_token|token|id)=([a-zA-Z0-9\-_.]+)', iframe)
    return m.group(1) if m else None

async def _fetch_drm_url(ep):
    try:
        r = await client.get(ep["url"], params=ep["params"], headers=ep["headers"])
        if r.status_code == 200:
            data = r.json()
            if data.get("status") and isinstance(data.get("data"), dict):
                return data["data"].get("file_url")
    except Exception:
        pass
    return None

async def _get_m3u8(token):
    """🔥 CONCURRENT FALLBACK: Tries all 3 DRM endpoints simultaneously"""
    if not token: return None
    endpoints = [
        {"url": f"{DRM_BASE_URL}/new-main.php", "params": {"token": token, "device_id": DEVICE_ID}, "headers": {"User-Agent": "Dart/3.5 (dart:io)", "Content-Type": "application/json"}},
        {"url": f"{DRM_BASE_URL}/tpstream-player.php", "params": {"token": token}, "headers": {"User-Agent": "Mozilla/5.0", "Referer": "https://guidely.in/"}},
        {"url": f"{DRM_BASE_URL}/player.php", "params": {"token": token, "device_id": DEVICE_ID}, "headers": {"User-Agent": "Dart/3.5 (dart:io)"}}
    ]
    tasks = [_fetch_drm_url(ep) for ep in endpoints]
    for coro in asyncio.as_completed(tasks):
        try:
            result = await coro
            if result: return result
        except Exception:
            continue
    return None

# 🔥 Helper to fetch raw items from main API
async def _get_batch_items(bid):
    res = await client.get(f"{BASE_URL}/live-video-class-new/{bid}")
    if res.status_code != 200: raise Exception(f"Batch API Error: {res.status_code}")
    data = res.json()
    
    items = []
    for key in ["live", "upcmg", "prevs", "recorded", "classes", "sessions"]:
        lst = data.get("data", {}).get(key) if isinstance(data.get("data"), dict) else data.get(key)
        if isinstance(lst, list): items.extend(lst)
    if not items: raise Exception("No items found in this batch")
    return items

# 🔥 1. PDF EXTRACTION (Instant - No extra API calls)
async def _extract_pdfs(bid):
    items = await _get_batch_items(bid)
    pdfs = []
    for s in items:
        if not isinstance(s, dict): continue
        cat = (s.get("catgname") or "General").strip()
        title = (s.get("name") or "No Title").strip()
        pdf = s.get("urlpdf") if isinstance(s.get("urlpdf"), str) else None
        if pdf:
            pdfs.append({"cat": cat, "title": title, "pdf": pdf})
            
    grouped = {}
    for r in pdfs:
        cat = r["cat"]
        if cat not in grouped: grouped[cat] = []
        grouped[cat].append({"title": r["title"], "pdf": r["pdf"]})
        
    return {"total": len(pdfs), "subjects": grouped}

# 🔥 2. VIDEO TOKENS EXTRACTION (INSTANT - No DRM calls!)
async def _extract_video_tokens(bid):
    """🚀 Sirf tokens return karega, m3u8 nahi. Isliye instant response!"""
    items = await _get_batch_items(bid)
    tokens = []
    
    for s in items:
        if not isinstance(s, dict): continue
        cat = (s.get("catgname") or "General").strip()
        title = (s.get("name") or "No Title").strip()
        
        # Token extract karo (bina DRM call kiye)
        vid_raw = None
        vld = s.get("video_link_data")
        if isinstance(vld, dict): vid_raw = vld.get("video_id")
        if not vid_raw: vid_raw = _extract_token(s.get("iframe"))
        if not vid_raw and s.get("video_link"):
            m = re.search(r'/(\d+)$', s.get("video_link", ""))
            if m: vid_raw = m.group(1)
            
        if vid_raw:
            tokens.append({"cat": cat, "title": title, "token": vid_raw})
    
    # Group by subject
    grouped = {}
    for r in tokens:
        cat = r["cat"]
        if cat not in grouped: grouped[cat] = []
        grouped[cat].append({"title": r["title"], "token": r["token"]})
        
    return {"total": len(tokens), "subjects": grouped}

# 🔥 UPDATED: Fetch Batches with REAL Image Priority
async def _fetch_batches():
    res = await client.get(f"{BASE_URL}/video-class")
    if res.status_code != 200: return []
    data = res.json()
    products = data.get("data", {}).get("products", []) if isinstance(data.get("data"), dict) else data.get("products", [])
    if not isinstance(products, list): return []
    
    batches = []
    for b in products:
        if isinstance(b, dict):
            # 🎯 Asli Image Priority: set_card_background_image -> set_app_image -> image
            real_image = b.get("set_card_background_image") or b.get("set_app_image") or b.get("image", "")
            
            batches.append({
                "id": str(b.get("id")),
                "title": b.get("title", "Unknown"),
                "sub_title": b.get("sub_title", ""),
                "price": b.get("price", "0"),
                "discount": b.get("discount", "0"),
                "image": real_image,
                "share_link": b.get("share_link", "")
            })
    return batches

# ───────────────── 🚀 FASTAPI ROUTES ─────────────────
app = FastAPI(title="Guidely Extractor API")

@app.on_event("shutdown")
async def shutdown_event():
    await client.aclose()

@app.get("/")
def root():
    return {
        "status": "running", 
        "message": "Guidely API is ready!",
        "endpoints": [
            "/allbatch", 
            "/batch/{id}/pdfs", 
            "/batch/{id}/videos", 
            "/drm/{token}"
        ]
    }

@app.get("/allbatch")
async def all_batch():
    try:
        batches = await _fetch_batches()
        return {"success": True, "count": len(batches), "data": batches}
    except Exception as e:
        logger.error(f"Error in /allbatch: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# 🔥 PDF Endpoint (Instant Response)
@app.get("/batch/{batch_id}/pdfs")
async def get_batch_pdfs(batch_id: str):
    try:
        result = await _extract_pdfs(batch_id)
        return {"success": True, "batch_id": batch_id, "type": "pdfs", "data": result}
    except Exception as e:
        logger.error(f"Error in /batch/{batch_id}/pdfs: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# 🔥 VIDEO TOKENS Endpoint (INSTANT - Sirf tokens, m3u8 nahi!)
@app.get("/batch/{batch_id}/videos")
async def get_batch_videos(batch_id: str):
    try:
        result = await _extract_video_tokens(batch_id)
        return {"success": True, "batch_id": batch_id, "type": "video_tokens", "data": result}
    except Exception as e:
        logger.error(f"Error in /batch/{batch_id}/videos: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# 🔥 Individual DRM Token to M3U8 Converter
@app.get("/drm/{token}")
async def get_drm(token: str):
    try:
        url = await _get_m3u8(token)
        if not url:
            return {"success": False, "message": "No DRM link found for this token"}
        return {"success": True, "data": {"file_url": url}}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    logger.info(f"🚀 Starting Guidely API server on port {port}")
    uvicorn.run(app, host="0.0.0.0", port=port)
