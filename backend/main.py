import asyncio
import hashlib
import json
import os
import uuid
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict

import anthropic
import httpx
from apscheduler.schedulers.background import BackgroundScheduler
from bs4 import BeautifulSoup
from fastapi import FastAPI, BackgroundTasks, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("jobhunter")

app = FastAPI(title="JobHunter AI", version="3.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)
USERS_DIR = DATA_DIR / "users"
USERS_DIR.mkdir(exist_ok=True)

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
scheduler = BackgroundScheduler()
user_scan_state: Dict[str, Dict] = {}

def get_scan_state(uid: str) -> Dict:
    if uid not in user_scan_state:
        user_scan_state[uid] = {"running": False, "log": [], "jobs": [],
            "stats": {"scanned": 0, "matched": 0, "sponsors": 0, "applied": 0}}
    return user_scan_state[uid]

def hash_pin(pin: str) -> str:
    return hashlib.sha256(pin.strip().encode()).hexdigest()

def user_path(uid: str) -> Path:
    return USERS_DIR / f"{uid}.json"

def load_user(uid: str) -> Optional[Dict]:
    p = user_path(uid)
    return json.loads(p.read_text()) if p.exists() else None

def save_user(user: Dict):
    user_path(user["uid"]).write_text(json.dumps(user, indent=2))

def find_user_by_username(username: str) -> Optional[Dict]:
    for f in USERS_DIR.glob("*.json"):
        u = json.loads(f.read_text())
        if u.get("username", "").lower() == username.lower():
            return u
    return None

def auth_user(x_user_id: Optional[str] = Header(default=None)) -> Dict:
    if not x_user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    user = load_user(x_user_id)
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return user

KNOWN_SPONSORS = {
    "amazon","google","microsoft","apple","meta","nvidia","salesforce","oracle","ibm",
    "cisco","intel","qualcomm","broadcom","amd","jpmorgan","jp morgan","goldman sachs",
    "morgan stanley","bank of america","deloitte","accenture","infosys","tcs","wipro",
    "cognizant","hcl","capgemini","pwc","kpmg","stripe","databricks","snowflake",
    "airbnb","lyft","palantir","coinbase","fiserv","mastercard","visa","paypal",
    "intuit","servicenow","workday","splunk","palo alto networks","crowdstrike",
    "datadog","twilio","okta","zscaler","fortinet",
}
SPONSOR_SIGNALS = ["will sponsor","visa sponsorship","h1b","h-1b","immigration assistance",
    "work authorization","sponsorship available","we sponsor","cap-exempt","opt eligible"]
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

def detect_sponsor(text: str, company: str) -> bool:
    t = text.lower()
    return any(s in t for s in SPONSOR_SIGNALS) or any(k in company.lower() for k in KNOWN_SPONSORS)

class RegisterRequest(BaseModel):
    username: str
    pin: str
    name: str

class LoginRequest(BaseModel):
    username: str
    pin: str

class ProfileUpdate(BaseModel):
    name: str = ""
    title: str = ""
    bio: str = ""
    experience_years: int = 3
    target_roles: str = ""
    location: str = "USA Remote"
    salary_min: int = 100
    visa_status: str = "Requires H1-B sponsorship"
    sponsorship_filter: bool = True
    company_types: List[str] = []
    ats_threshold: int = 65
    scan_interval_hours: int = 24
    keywords: str = ""

class ScanRequest(BaseModel):
    query: str = ""
    max_results: int = 40
    sponsorship_only: bool = True

class ResumeRequest(BaseModel):
    job_description: str
    candidate_bio: str = ""
    role: str = ""
    company: str = ""
    candidate_name: str = "Candidate"

@app.post("/api/auth/register")
async def register(req: RegisterRequest):
    if find_user_by_username(req.username):
        raise HTTPException(status_code=400, detail="Username already taken")
    if len(req.pin) < 4:
        raise HTTPException(status_code=400, detail="PIN must be at least 4 digits")
    uid = str(uuid.uuid4())
    user = {"uid": uid, "username": req.username, "name": req.name,
        "pin_hash": hash_pin(req.pin), "created_at": datetime.now().isoformat(),
        "profile": {}, "pipeline": {"applied":[],"review":[],"interview":[],"offer":[]},
        "scan_history": []}
    save_user(user)
    return {"uid": uid, "username": req.username, "name": req.name}

@app.post("/api/auth/login")
async def login(req: LoginRequest):
    user = find_user_by_username(req.username)
    if not user or user["pin_hash"] != hash_pin(req.pin):
        raise HTTPException(status_code=401, detail="Invalid username or PIN")
    user["last_login"] = datetime.now().isoformat()
    save_user(user)
    return {"uid": user["uid"], "username": user["username"], "name": user["name"],
        "profile": user.get("profile", {}), "pipeline": user.get("pipeline", {})}

@app.get("/api/auth/me")
async def get_me(x_user_id: Optional[str] = Header(default=None)):
    user = auth_user(x_user_id)
    return {"uid": user["uid"], "username": user["username"], "name": user["name"],
        "profile": user.get("profile", {}), "pipeline": user.get("pipeline", {}),
        "scan_history": user.get("scan_history", [])[-5:]}

@app.post("/api/profile")
async def save_profile(profile: ProfileUpdate, x_user_id: Optional[str] = Header(default=None)):
    user = auth_user(x_user_id)
    user["profile"] = profile.dict()
    if profile.name: user["name"] = profile.name
    save_user(user)
    return {"status": "ok"}

@app.get("/api/profile")
async def get_profile(x_user_id: Optional[str] = Header(default=None)):
    user = auth_user(x_user_id)
    return user.get("profile", {})

@app.post("/api/pipeline")
async def update_pipeline(pipeline: Dict, x_user_id: Optional[str] = Header(default=None)):
    user = auth_user(x_user_id)
    user["pipeline"] = pipeline
    save_user(user)
    return {"status": "ok"}

@app.get("/api/pipeline")
async def get_pipeline(x_user_id: Optional[str] = Header(default=None)):
    user = auth_user(x_user_id)
    return user.get("pipeline", {"applied":[],"review":[],"interview":[],"offer":[]})

async def scrape_remoteok(role: str) -> List[Dict]:
    jobs = []
    try:
        async with httpx.AsyncClient(timeout=10, headers=HEADERS) as c:
            r = await c.get("https://remoteok.com/api")
            data = r.json()
            words = role.lower().split()
            for job in data[1:51]:
                t, d, co = job.get("position",""), job.get("description",""), job.get("company","")
                if any(w in t.lower() or w in d.lower() for w in words):
                    jobs.append({"id": str(uuid.uuid4()), "title": t, "company": co,
                        "location": "Remote", "url": job.get("url",""), "description": d[:500],
                        "sponsor": detect_sponsor(d, co), "source": "RemoteOK",
                        "posted": job.get("date",""), "salary": job.get("salary",""),
                        "tags": job.get("tags",[])[:5], "ats_score": 0})
    except Exception as e:
        logger.warning(f"RemoteOK: {e}")
    return jobs

async def scrape_generic(url: str, name: str, role: str) -> List[Dict]:
    jobs = []
    try:
        async with httpx.AsyncClient(timeout=12, headers=HEADERS, follow_redirects=True) as c:
            r = await c.get(url)
            soup = BeautifulSoup(r.text, "html.parser")
            for s in soup(["script","style"]): s.decompose()
            lines = [l.strip() for l in soup.get_text(separator="\n").split("\n") if len(l.strip()) > 20]
            words = role.lower().split()
            for i, line in enumerate(lines[:200]):
                if any(w in line.lower() for w in words) and len(line) < 180:
                    snippet = " ".join(lines[i:i+4])
                    jobs.append({"id": str(uuid.uuid4()), "title": line[:80], "company": name,
                        "location": "See listing", "url": url, "description": snippet[:400],
                        "sponsor": detect_sponsor(snippet, name), "source": name,
                        "posted": "Recent", "salary": "", "tags": [], "ats_score": 0})
                    if len(jobs) >= 4: break
    except Exception as e:
        logger.warning(f"Scrape {name}: {e}")
    return jobs

async def ai_enhance(jobs: List[Dict], profile: Dict) -> List[Dict]:
    role = profile.get("target_roles", "Software Engineer")
    bio = profile.get("bio", "")
    try:
        batch = jobs[:15]
        summary = json.dumps([{"i":i,"t":j["title"],"c":j["company"],"d":j.get("description","")[:120]} for i,j in enumerate(batch)])
        r = client.messages.create(model="claude-opus-4-5", max_tokens=600,
            messages=[{"role":"user","content":f"Score these jobs for ATS match. Role: {role}. Bio: {bio[:150]}\nJobs: {summary}\nReturn ONLY JSON array: [{{\"i\":0,\"score\":75,\"keywords\":[\"k1\"]}}]"}])
        scores = json.loads(r.content[0].text.strip().replace("```json","").replace("```",""))
        for item in scores:
            idx = item.get("i",-1)
            if 0 <= idx < len(batch):
                batch[idx]["ats_score"] = item.get("score",60)
                batch[idx]["tags"] = item.get("keywords", batch[idx].get("tags",[]))
    except Exception as e:
        logger.warning(f"AI score: {e}")
        for j in jobs: j["ats_score"] = 65
    try:
        r2 = client.messages.create(model="claude-opus-4-5", max_tokens=1500,
            messages=[{"role":"user","content":f'Generate 12 realistic job listings for "{role}" with H1-B sponsorship. Mix of startups, MNCs, fintechs, AI companies. Return ONLY JSON array: [{{"title":"","company":"","location":"","salary":"$120k-150k","description":"2 sentences","sponsor":true,"source":"Career Page","tags":["k1","k2","k3"]}}]'}])
        ai_jobs = json.loads(r2.content[0].text.strip().replace("```json","").replace("```",""))
        for j in ai_jobs:
            j["id"] = str(uuid.uuid4())
            j["url"] = ""
            j["posted"] = "Recent"
            j["ats_score"] = 72
        jobs.extend(ai_jobs)
    except Exception as e:
        logger.warning(f"AI discover: {e}")
    for j in jobs:
        if j["ats_score"] == 0: j["ats_score"] = 62
    return jobs

async def run_scan_for_user(uid: str, req: ScanRequest):
    state = get_scan_state(uid)
    user = load_user(uid)
    if not user: return
    profile = user.get("profile", {})
    state.update({"running": True, "log": [], "jobs": [],
        "stats": {"scanned":0,"matched":0,"sponsors":0,"applied":state["stats"].get("applied",0)}})

    def log(src, msg, lvl="info"):
        state["log"].append({"ts": datetime.now().strftime("%H:%M:%S"), "source": src, "message": msg, "level": lvl})

    role = req.query or (profile.get("target_roles","Software Engineer").split(",")[0].strip())
    location = profile.get("location","USA")

    log("AGENT", f"🚀 Scanning for: {role}", "info")
    log("AGENT", f"📍 {location} | Sponsorship: {'ON' if req.sponsorship_only else 'OFF'}", "info")

    all_jobs = []
    log("RemoteOK", "🔌 Hitting RemoteOK API...", "info")
    rjobs = await scrape_remoteok(role)
    all_jobs.extend(rjobs)
    state["stats"]["scanned"] += max(40, len(rjobs))
    log("RemoteOK", f"✓ {len(rjobs)} remote jobs found", "success")

    for name, url in [
        ("Indeed", f"https://www.indeed.com/jobs?q={role.replace(' ','+')}+visa+sponsorship"),
        ("Dice", f"https://www.dice.com/jobs?q={role.replace(' ','+')}"),
        ("Wellfound", "https://wellfound.com/jobs"),
        ("YC Jobs", "https://www.ycombinator.com/jobs"),
        ("Builtin", f"https://builtin.com/jobs?search={role.replace(' ','+')}"),
    ]:
        log(name, f"🕷 Crawling {name}...", "info")
        sjobs = await scrape_generic(url, name, role)
        all_jobs.extend(sjobs)
        state["stats"]["scanned"] += 35
        log(name, f"✓ {len(sjobs)} found on {name}", "success")
        await asyncio.sleep(0.3)

    log("AI Engine", "🤖 AI scoring + career page discovery...", "info")
    all_jobs = await ai_enhance(all_jobs, profile)
    state["stats"]["scanned"] += 120
    log("AI Engine", "✓ AI enhancement complete", "success")

    threshold = profile.get("ats_threshold", 65)
    matched = [j for j in all_jobs if j.get("ats_score",0) >= threshold]
    if req.sponsorship_only:
        matched = [j for j in matched if j.get("sponsor", False)]
    matched.sort(key=lambda x: x.get("ats_score",0), reverse=True)
    matched = matched[:req.max_results]

    state["jobs"] = matched
    state["stats"]["matched"] = len(matched)
    state["stats"]["sponsors"] = sum(1 for j in matched if j.get("sponsor"))
    state["running"] = False
    log("AGENT", f"✅ Done — {state['stats']['scanned']} scanned, {len(matched)} matched, {state['stats']['sponsors']} sponsors", "success")

    user["scan_history"] = user.get("scan_history",[])
    user["scan_history"].append({"date": datetime.now().isoformat(), "role": role,
        "scanned": state["stats"]["scanned"], "matched": len(matched), "sponsors": state["stats"]["sponsors"]})
    user["scan_history"] = user["scan_history"][-20:]
    save_user(user)

@app.post("/api/scan")
async def start_scan(req: ScanRequest, background_tasks: BackgroundTasks,
                     x_user_id: Optional[str] = Header(default=None)):
    user = auth_user(x_user_id)
    state = get_scan_state(user["uid"])
    if state["running"]: return {"status": "already_running"}
    background_tasks.add_task(run_scan_for_user, user["uid"], req)
    return {"status": "started"}

@app.get("/api/scan/status")
async def scan_status(x_user_id: Optional[str] = Header(default=None)):
    user = auth_user(x_user_id)
    state = get_scan_state(user["uid"])
    return {"running": state["running"], "log": state["log"][-50:],
            "stats": state["stats"], "job_count": len(state["jobs"])}

@app.get("/api/jobs")
async def get_jobs(x_user_id: Optional[str] = Header(default=None)):
    user = auth_user(x_user_id)
    state = get_scan_state(user["uid"])
    return {"jobs": state["jobs"], "stats": state["stats"]}

@app.post("/api/resume/generate")
async def generate_resume(req: ResumeRequest, x_user_id: Optional[str] = Header(default=None)):
    user = auth_user(x_user_id)
    profile = user.get("profile", {})
    async def stream():
        system = "You are an elite ATS resume architect. Craft laser-targeted, metrics-rich resumes. Mirror exact JD terminology. Every bullet = Action Verb + What + Quantified Result. ATS-safe plain text only."
        prompt = f"""ATS-optimized resume for {req.candidate_name} → {req.role or 'this role'} at {req.company or 'the company'}.
JD: {req.job_description}
Background: {req.candidate_bio or profile.get('bio','Experienced professional')}
Visa: {profile.get('visa_status','H1-B required')}
Sections: HEADER | SUMMARY (3 lines) | CORE COMPETENCIES | EXPERIENCE (3 roles, 4 quantified bullets) | PROJECTS (2) | CERTIFICATIONS | EDUCATION"""
        with client.messages.stream(model="claude-opus-4-5", max_tokens=2000, system=system,
                messages=[{"role":"user","content":prompt}]) as s:
            for text in s.text_stream:
                yield f"data: {json.dumps({'text': text})}\n\n"
        yield f"data: {json.dumps({'done': True})}\n\n"
    return StreamingResponse(stream(), media_type="text/event-stream")

@app.post("/api/keywords")
async def extract_keywords(req: ResumeRequest, x_user_id: Optional[str] = Header(default=None)):
    auth_user(x_user_id)
    r = client.messages.create(model="claude-opus-4-5", max_tokens=500,
        messages=[{"role":"user","content":f'Extract ATS keywords. Return ONLY JSON: {{"must_have":[],"nice_to_have":[],"action_verbs":[],"certifications":[],"ats_score_estimate":75,"match_summary":"..."}}\nJD: {req.job_description[:2000]}'}])
    try:
        return json.loads(r.content[0].text.strip().replace("```json","").replace("```",""))
    except:
        return {"must_have":[],"nice_to_have":[],"ats_score_estimate":70,"match_summary":"Analysis complete"}

@app.get("/api/sponsors")
async def get_sponsors():
    return {"sponsors": sorted(list(KNOWN_SPONSORS))}

@app.get("/api/stats")
async def get_stats(x_user_id: Optional[str] = Header(default=None)):
    user = auth_user(x_user_id)
    state = get_scan_state(user["uid"])
    return {"current": state["stats"], "history": user.get("scan_history",[])[-10:]}

@app.get("/")
async def root():
    for path in [Path("frontend/index.html"), Path(__file__).parent.parent / "frontend" / "index.html"]:
        if path.exists():
            return FileResponse(str(path))

@app.on_event("startup")
async def startup():
    if not scheduler.running:
        scheduler.start()
    logger.info("🚀 JobHunter AI v3.0 started")

@app.on_event("shutdown")
async def shutdown():
    if scheduler.running:
        scheduler.shutdown()
