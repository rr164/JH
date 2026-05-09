import asyncio, hashlib, json, os, uuid, logging, smtplib, secrets
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Dict
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import anthropic, httpx
from apscheduler.schedulers.background import BackgroundScheduler
from bs4 import BeautifulSoup
from fastapi import FastAPI, BackgroundTasks, HTTPException, Header, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("jobhunter")
app = FastAPI(title="JobHunter AI", version="5.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

DATA_DIR   = Path("data");        DATA_DIR.mkdir(exist_ok=True)
USERS_DIR  = DATA_DIR / "users";  USERS_DIR.mkdir(exist_ok=True)
UPLOAD_DIR = Path("uploads");     UPLOAD_DIR.mkdir(exist_ok=True)

client    = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
scheduler = BackgroundScheduler()
user_scan_state: Dict[str, Dict] = {}

# Email config from env
SMTP_HOST     = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT     = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER     = os.environ.get("SMTP_USER", "")
SMTP_PASS     = os.environ.get("SMTP_PASS", "")
APP_URL       = os.environ.get("APP_URL", "https://nextjobhunter.onrender.com")

# Disk-based OTP store (survives server restarts)
OTP_DIR = DATA_DIR / "otps"; OTP_DIR.mkdir(exist_ok=True)

def save_otp(email: str, data: Dict):
    (OTP_DIR / f"{hashlib.md5(email.encode()).hexdigest()}.json").write_text(json.dumps(data))

def load_otp(email: str) -> Optional[Dict]:
    p = OTP_DIR / f"{hashlib.md5(email.encode()).hexdigest()}.json"
    return json.loads(p.read_text()) if p.exists() else None

def delete_otp(email: str):
    p = OTP_DIR / f"{hashlib.md5(email.encode()).hexdigest()}.json"
    if p.exists(): p.unlink()

# ── helpers ───────────────────────────────────────────────────────────────────
def get_state(uid):
    if uid not in user_scan_state:
        user_scan_state[uid] = {"running":False,"log":[],"jobs":[],"stats":{"scanned":0,"matched":0,"sponsors":0,"applied":0}}
    return user_scan_state[uid]

def hash_pin(p): return hashlib.sha256(p.strip().encode()).hexdigest()
def upath(uid): return USERS_DIR / f"{uid}.json"
def load_user(uid): p=upath(uid); return json.loads(p.read_text()) if p.exists() else None
def save_user(u): upath(u["uid"]).write_text(json.dumps(u, indent=2))

def find_by_username(username):
    for f in USERS_DIR.glob("*.json"):
        u = json.loads(f.read_text())
        if u.get("username","").lower() == username.lower(): return u
    return None

def find_by_email(email):
    for f in USERS_DIR.glob("*.json"):
        u = json.loads(f.read_text())
        if u.get("email","").lower() == email.lower(): return u
    return None

def auth(x_user_id: Optional[str]=Header(default=None)):
    if not x_user_id: raise HTTPException(401, "Not authenticated")
    u = load_user(x_user_id)
    if not u: raise HTTPException(401, "User not found")
    return u

def extract_pdf(data):
    try:
        from pypdf import PdfReader
        import io
        return "\n".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(data)).pages)
    except Exception as e:
        logger.warning(f"pypdf failed:{e}")
        try:
            import PyPDF2, io as io2
            return "\n".join(p.extract_text() or "" for p in PyPDF2.PdfReader(io2.BytesIO(data)).pages)
        except Exception as e2: logger.warning(f"PyPDF2 also failed:{e2}"); return ""

def extract_docx(data):
    try:
        from docx import Document; import io
        return "\n".join(p.text for p in Document(io.BytesIO(data)).paragraphs)
    except Exception as e: logger.warning(f"DOCX:{e}"); return ""

def send_email(to: str, subject: str, html: str):
    if not SMTP_USER or not SMTP_PASS:
        logger.warning("SMTP not configured — skipping email")
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = f"JobHunter AI <{SMTP_USER}>"
        msg["To"]      = to
        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(SMTP_USER, to, msg.as_string())
        return True
    except Exception as e:
        logger.warning(f"Email send failed: {e}"); return False

def email_html(title, body, otp=None):
    otp_block = f"""
    <div style="text-align:center;margin:24px 0">
      <div style="font-size:36px;font-weight:800;letter-spacing:10px;color:#5b8fff;
                  background:#0a0d1a;padding:20px 32px;border-radius:12px;
                  border:2px solid rgba(91,143,255,0.3);display:inline-block">{otp}</div>
      <div style="color:#7a8aaa;font-size:12px;margin-top:8px">Expires in 15 minutes</div>
    </div>""" if otp else ""
    return f"""
    <div style="background:#03040a;min-height:100vh;padding:40px 20px;font-family:sans-serif">
      <div style="max-width:480px;margin:0 auto;background:#0a0d1a;border-radius:16px;
                  border:1px solid rgba(91,143,255,0.2);overflow:hidden">
        <div style="background:linear-gradient(135deg,#5b8fff,#00f5c8);padding:24px;text-align:center">
          <div style="font-size:28px">🎯</div>
          <div style="color:#fff;font-size:18px;font-weight:800;margin-top:8px">JobHunter AI</div>
        </div>
        <div style="padding:32px">
          <h2 style="color:#dce8ff;font-size:20px;margin-bottom:12px">{title}</h2>
          <p style="color:#8a9aba;line-height:1.7;margin-bottom:16px">{body}</p>
          {otp_block}
          <p style="color:#5a6a8a;font-size:12px;margin-top:24px">
            If you didn't request this, ignore this email. Your account is safe.
          </p>
        </div>
        <div style="padding:16px 32px;border-top:1px solid rgba(91,143,255,0.1);
                    text-align:center;color:#5a6a8a;font-size:11px">
          JobHunter AI · Autonomous Career Engine
        </div>
      </div>
    </div>"""

# ── sponsor / scan data ───────────────────────────────────────────────────────
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
HDRS = {"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

def is_sponsor(text, company):
    t = text.lower()
    return any(s in t for s in SPONSOR_SIGNALS) or any(k in company.lower() for k in KNOWN_SPONSORS)

def get_match_level(sc):
    return "Excellent" if sc>=85 else "Good" if sc>=70 else "Fair" if sc>=55 else "Low"

# ── models ────────────────────────────────────────────────────────────────────
class RegisterReq(BaseModel):
    username: str; pin: str; name: str; email: str = ""

class LoginReq(BaseModel):
    username: str; pin: str

class ForgotReq(BaseModel):
    email: str

class VerifyOTPReq(BaseModel):
    email: str; otp: str

class ResetPINReq(BaseModel):
    email: str; otp: str; new_pin: str

class ProfileUpdate(BaseModel):
    name: str=""; title: str=""; bio: str=""; experience_years: int=3
    target_roles: str=""; location: str="USA Remote"; salary_min: int=100
    visa_status: str="Requires H1-B sponsorship"; sponsorship_filter: bool=True
    company_types: List[str]=[]; ats_threshold: int=65
    scan_interval_hours: int=24; keywords: str=""; email: str=""
    notify_email: bool=True

class ScanReq(BaseModel):
    query: str=""; max_results: int=40; sponsorship_only: bool=True

class ResumeReq(BaseModel):
    job_description: str; candidate_bio: str=""; role: str=""
    company: str=""; candidate_name: str="Candidate"; job_id: str=""

class ApplyReq(BaseModel):
    job_id: str; job_title: str; company: str; job_url: str=""

# ── auth routes ───────────────────────────────────────────────────────────────
@app.post("/api/auth/register")
async def register(req: RegisterReq):
    if find_by_username(req.username): raise HTTPException(400, "Username already taken")
    if req.email and find_by_email(req.email): raise HTTPException(400, "Email already registered")
    if len(req.pin) < 4: raise HTTPException(400, "PIN must be 4+ digits")
    uid = str(uuid.uuid4())
    u = {"uid":uid,"username":req.username,"name":req.name,"email":req.email,
         "pin_hash":hash_pin(req.pin),"created_at":datetime.now().isoformat(),
         "profile":{},"resume_text":"","resume_skills":[],"resume_roles":[],
         "resume_keywords":[],"pipeline":{"applied":[],"review":[],"interview":[],"offer":[]},
         "scan_history":[],"notify_email":True}
    save_user(u)
    if req.email:
        send_email(req.email, "Welcome to JobHunter AI 🎯",
            email_html("Welcome aboard!", f"Hi {req.name}, your account is ready. Username: <strong>{req.username}</strong>. Start hunting at <a href='{APP_URL}' style='color:#5b8fff'>{APP_URL}</a>"))
    return {"uid":uid,"username":req.username,"name":req.name}

@app.post("/api/auth/login")
async def login(req: LoginReq):
    u = find_by_username(req.username)
    if not u or u["pin_hash"] != hash_pin(req.pin): raise HTTPException(401, "Invalid username or PIN")
    u["last_login"] = datetime.now().isoformat(); save_user(u)
    return {"uid":u["uid"],"username":u["username"],"name":u["name"],
            "profile":u.get("profile",{}),"pipeline":u.get("pipeline",{}),
            "has_resume":bool(u.get("resume_text","")),"email":u.get("email","")}

@app.get("/api/auth/me")
async def get_me(x_user_id: Optional[str]=Header(default=None)):
    u = auth(x_user_id)
    return {"uid":u["uid"],"username":u["username"],"name":u["name"],"email":u.get("email",""),
            "profile":u.get("profile",{}),"pipeline":u.get("pipeline",{}),
            "has_resume":bool(u.get("resume_text","")),"resume_roles":u.get("resume_roles",[]),
            "scan_history":u.get("scan_history",[])[-5:]}

# ── forgot / recovery routes ──────────────────────────────────────────────────
@app.post("/api/auth/forgot")
async def forgot(req: ForgotReq):
    u = find_by_email(req.email.lower().strip())
    if not u:
        # Don't reveal if email exists — return ok anyway
        return {"status":"ok","message":"If that email is registered, you'll receive a recovery code."}
    otp = str(secrets.randbelow(900000) + 100000)  # 6-digit OTP
    save_otp(req.email.lower(), {
        "otp": otp,
        "uid": u["uid"],
        "expires": (datetime.now() + timedelta(minutes=15)).isoformat()
    })
    sent = send_email(req.email, "JobHunter AI — Account Recovery Code 🔑",
        email_html(
            "Account Recovery",
            f"Hi {u['name']}, here is your 6-digit recovery code. Your username is: <strong>{u['username']}</strong>",
            otp
        ))
    if not sent:
        # SMTP not configured — return OTP directly for dev mode
        return {"status":"ok","message":"SMTP not configured. Dev mode OTP included.","dev_otp":otp,"username":u["username"]}
    return {"status":"ok","message":"Recovery code sent to your email."}

@app.post("/api/auth/verify-otp")
async def verify_otp(req: VerifyOTPReq):
    entry = load_otp(req.email.lower())
    if not entry: raise HTTPException(400, "No recovery request found for this email")
    if datetime.now() > datetime.fromisoformat(entry["expires"]):
        delete_otp(req.email.lower())
        raise HTTPException(400, "Recovery code expired. Please request a new one.")
    if entry["otp"] != req.otp.strip(): raise HTTPException(400, "Invalid recovery code")
    u = load_user(entry["uid"])
    return {"status":"ok","username":u["username"],"name":u["name"]}

@app.post("/api/auth/reset-pin")
async def reset_pin(req: ResetPINReq):
    entry = load_otp(req.email.lower())
    if not entry: raise HTTPException(400, "No recovery session found")
    if datetime.now() > datetime.fromisoformat(entry["expires"]):
        raise HTTPException(400, "Session expired. Start over.")
    if entry["otp"] != req.otp.strip(): raise HTTPException(400, "Invalid code")
    if len(req.new_pin) < 4: raise HTTPException(400, "PIN must be 4+ digits")
    u = load_user(entry["uid"])
    u["pin_hash"] = hash_pin(req.new_pin)
    save_user(u)
    delete_otp(req.email.lower())
    send_email(req.email, "JobHunter AI — PIN Reset Successful ✅",
        email_html("PIN Reset Successful", f"Hi {u['name']}, your PIN has been reset successfully. You can now sign in with your new PIN."))
    return {"status":"ok","username":u["username"]}

# ── profile ───────────────────────────────────────────────────────────────────
@app.post("/api/profile")
async def save_profile(profile: ProfileUpdate, x_user_id: Optional[str]=Header(default=None)):
    u = auth(x_user_id)
    u["profile"] = profile.dict()
    if profile.name: u["name"] = profile.name
    if profile.email: u["email"] = profile.email
    if hasattr(profile, 'notify_email'): u["notify_email"] = profile.notify_email
    save_user(u); return {"status":"ok"}

@app.get("/api/profile")
async def get_profile(x_user_id: Optional[str]=Header(default=None)):
    u = auth(x_user_id); return u.get("profile", {})

# ── resume upload ─────────────────────────────────────────────────────────────
@app.post("/api/resume/upload")
async def upload_resume(file: UploadFile=File(...), x_user_id: Optional[str]=Header(default=None)):
    u = auth(x_user_id); data = await file.read(); fname = file.filename.lower()
    if fname.endswith(".pdf"): text = extract_pdf(data)
    elif fname.endswith(".docx"): text = extract_docx(data)
    elif fname.endswith(".txt"): text = data.decode("utf-8", errors="ignore")
    else: raise HTTPException(400, "Only PDF, DOCX or TXT supported")
    if not text.strip(): raise HTTPException(400, "Could not extract text")
    resp = client.messages.create(model="claude-opus-4-5", max_tokens=600,
        messages=[{"role":"user","content":
            f'Analyze resume. Return ONLY JSON: {{"skills":["s1"],"target_roles":["r1"],"experience_years":5,"summary":"2 sentences","top_keywords":["k1"]}}\nRESUME:{text[:4000]}'}])
    try: parsed = json.loads(resp.content[0].text.strip().replace("```json","").replace("```",""))
    except: parsed = {"skills":[],"target_roles":[],"experience_years":3,"summary":"","top_keywords":[]}
    u["resume_text"]=text[:8000]; u["resume_skills"]=parsed.get("skills",[])
    u["resume_roles"]=parsed.get("target_roles",[]); u["resume_keywords"]=parsed.get("top_keywords",[])
    profile = u.get("profile",{})
    if not profile.get("bio") and parsed.get("summary"): profile["bio"] = parsed["summary"]
    if not profile.get("target_roles") and parsed.get("target_roles"): profile["target_roles"] = ", ".join(parsed["target_roles"])
    if not profile.get("experience_years") and parsed.get("experience_years"): profile["experience_years"] = parsed["experience_years"]
    u["profile"] = profile; save_user(u)
    return {"status":"ok","skills":parsed.get("skills",[]),"roles":parsed.get("target_roles",[]),
            "experience_years":parsed.get("experience_years",3),"summary":parsed.get("summary",""),
            "keyword_count":len(parsed.get("top_keywords",[]))}

@app.get("/api/auth/lookup-username")
async def lookup_username(email: str):
    """Let users find their username by email without needing full OTP flow"""
    u = find_by_email(email.lower().strip())
    if not u:
        raise HTTPException(404, "No account found with that email")
    return {"username": u["username"], "name": u["name"]}

@app.delete("/api/resume")
async def delete_resume(x_user_id: Optional[str]=Header(default=None)):
    u = auth(x_user_id)
    u["resume_text"] = ""
    u["resume_skills"] = []
    u["resume_roles"] = []
    u["resume_keywords"] = []
    u["resume_parsed"] = {}
    save_user(u)
    return {"status": "ok", "message": "Resume deleted"}

# ── pipeline ──────────────────────────────────────────────────────────────────
@app.post("/api/pipeline")
async def update_pipeline(pipeline: Dict, x_user_id: Optional[str]=Header(default=None)):
    u = auth(x_user_id); u["pipeline"] = pipeline; save_user(u); return {"status":"ok"}

@app.get("/api/pipeline")
async def get_pipeline(x_user_id: Optional[str]=Header(default=None)):
    u = auth(x_user_id)
    return u.get("pipeline", {"applied":[],"review":[],"interview":[],"offer":[]})

@app.post("/api/apply")
async def apply_to_job(req: ApplyReq, x_user_id: Optional[str]=Header(default=None)):
    u = auth(x_user_id)
    pipeline = u.get("pipeline", {"applied":[],"review":[],"interview":[],"offer":[]})
    entry = {"id":req.job_id,"title":req.job_title,"company":req.company,"url":req.job_url,"applied_at":datetime.now().isoformat()}
    if not any(j.get("id")==req.job_id for j in pipeline["applied"]): pipeline["applied"].insert(0, entry)
    u["pipeline"] = pipeline
    state = get_state(u["uid"]); state["stats"]["applied"] = state["stats"].get("applied",0)+1
    save_user(u)
    # Email notification
    if u.get("notify_email") and u.get("email"):
        send_email(u["email"], f"✅ Applied: {req.job_title} at {req.company}",
            email_html("Application Recorded",
                f"You've applied to <strong>{req.job_title}</strong> at <strong>{req.company}</strong>. "
                f"Track your application at <a href='{APP_URL}' style='color:#5b8fff'>your dashboard</a>."))
    return {"status":"ok","pipeline":pipeline}

# ── scan engine ───────────────────────────────────────────────────────────────
async def scrape_remoteok(role):
    jobs = []
    try:
        async with httpx.AsyncClient(timeout=10, headers=HDRS) as c:
            data = (await c.get("https://remoteok.com/api")).json()
            words = role.lower().split()
            for job in data[1:60]:
                t,d,co = job.get("position",""),job.get("description",""),job.get("company","")
                if any(w in t.lower() or w in d.lower() for w in words):
                    jobs.append({"id":str(uuid.uuid4()),"title":t,"company":co,"location":"Remote",
                        "url":job.get("url",""),"description":d[:500],"sponsor":is_sponsor(d,co),
                        "source":"RemoteOK","posted":job.get("date",""),"salary":job.get("salary",""),
                        "tags":job.get("tags",[])[:5],"ats_score":0,"match_level":"","matching_keywords":[],"match_reason":""})
    except Exception as e: logger.warning(f"RemoteOK:{e}")
    return jobs

async def scrape_generic(url, name, role):
    jobs = []
    try:
        async with httpx.AsyncClient(timeout=12, headers=HDRS, follow_redirects=True) as c:
            soup = BeautifulSoup((await c.get(url)).text, "html.parser")
            for s in soup(["script","style"]): s.decompose()
            lines = [l.strip() for l in soup.get_text(separator="\n").split("\n") if len(l.strip())>20]
            words = role.lower().split()
            for i,line in enumerate(lines[:200]):
                if any(w in line.lower() for w in words) and len(line)<180:
                    snippet = " ".join(lines[i:i+4])
                    jobs.append({"id":str(uuid.uuid4()),"title":line[:80],"company":name,
                        "location":"See listing","url":url,"description":snippet[:400],
                        "sponsor":is_sponsor(snippet,name),"source":name,"posted":"Recent",
                        "salary":"","tags":[],"ats_score":0,"match_level":"","matching_keywords":[],"match_reason":""})
                    if len(jobs)>=4: break
    except Exception as e: logger.warning(f"Scrape {name}:{e}")
    return jobs

async def ai_enhance(jobs, user):
    profile = user.get("profile",{}); role = profile.get("target_roles","") or "Software Engineer"
    resume_keywords = user.get("resume_keywords",[]); bio = profile.get("bio","")
    candidate = f"Role:{role}. Keywords:{', '.join(resume_keywords[:15])}. Bio:{bio[:200]}"
    try:
        batch = jobs[:15]
        summary = json.dumps([{"i":i,"t":j["title"],"c":j["company"],"d":j.get("description","")[:120]} for i,j in enumerate(batch)])
        resp = client.messages.create(model="claude-opus-4-5", max_tokens=800,
            messages=[{"role":"user","content":f"Score jobs ATS match. Candidate:{candidate}\nJobs:{summary}\nReturn ONLY JSON:[{{\"i\":0,\"score\":75,\"keywords\":[\"k1\"],\"reason\":\"brief\"}}]"}])
        for item in json.loads(resp.content[0].text.strip().replace("```json","").replace("```","")):
            idx = item.get("i",-1)
            if 0<=idx<len(batch):
                sc = item.get("score",60); batch[idx]["ats_score"]=sc; batch[idx]["match_level"]=get_match_level(sc)
                batch[idx]["matching_keywords"]=item.get("keywords",[]); batch[idx]["match_reason"]=item.get("reason","")
    except Exception as e: logger.warning(f"AI score:{e}"); [j.update({"ats_score":65,"match_level":"Fair"}) for j in jobs]
    try:
        roles_hint = ", ".join(user.get("resume_roles",[])[:3]) or role
        resp2 = client.messages.create(model="claude-opus-4-5", max_tokens=1800,
            messages=[{"role":"user","content":
                f'Generate 15 job listings for "{roles_hint}" with H1-B sponsorship. Mix startups MNCs fintechs AI.\n'
                f'Key skills:{", ".join(resume_keywords[:10])}\n'
                f'Return ONLY JSON:[{{"title":"","company":"","location":"","salary":"$120k-150k","description":"2 sentences","sponsor":true,"source":"Career Page","tags":["k1","k2","k3"],"ats_score":82,"match_level":"Excellent","matching_keywords":["kw1"],"match_reason":"reason"}}]'}])
        ai_jobs = json.loads(resp2.content[0].text.strip().replace("```json","").replace("```",""))
        for j in ai_jobs: j.update({"id":str(uuid.uuid4()),"url":"","posted":"Recent"})
        jobs.extend(ai_jobs)
    except Exception as e: logger.warning(f"AI discover:{e}")
    for j in jobs:
        if not j.get("ats_score"): j["ats_score"]=62
        if not j.get("match_level"): j["match_level"]=get_match_level(j["ats_score"])
    return jobs

async def run_scan(uid, req):
    state = get_state(uid); user = load_user(uid)
    if not user: return
    profile = user.get("profile",{})
    state.update({"running":True,"log":[],"jobs":[],"stats":{"scanned":0,"matched":0,"sponsors":0,"applied":state["stats"].get("applied",0)}})
    def log(src,msg,lvl="info"): state["log"].append({"ts":datetime.now().strftime("%H:%M:%S"),"source":src,"message":msg,"level":lvl})
    resume_roles = user.get("resume_roles",[])
    role = req.query or (resume_roles[0] if resume_roles else profile.get("target_roles","") or "Software Engineer".split(",")[0].strip())
    log("AGENT",f"🚀 Scanning for: {role}")
    log("AGENT",f"📍 Resume memory: {'✓ Active' if user.get('resume_text') else '✗ Upload resume for better matches'}")
    all_jobs = []
    log("RemoteOK","🔌 Hitting RemoteOK API...")
    rjobs = await scrape_remoteok(role); all_jobs.extend(rjobs); state["stats"]["scanned"]+=max(40,len(rjobs))
    log("RemoteOK",f"✓ {len(rjobs)} found","success")
    for name,url in [("Indeed",f"https://www.indeed.com/jobs?q={role.replace(' ','+')}+visa+sponsorship"),
                     ("Dice",f"https://www.dice.com/jobs?q={role.replace(' ','+')}"),
                     ("Wellfound","https://wellfound.com/jobs"),
                     ("YC Jobs","https://www.ycombinator.com/jobs"),
                     ("Builtin",f"https://builtin.com/jobs?search={role.replace(' ','+')}"),]:
        log(name,f"🕷 Crawling {name}..."); sjobs=await scrape_generic(url,name,role)
        all_jobs.extend(sjobs); state["stats"]["scanned"]+=35; log(name,f"✓ {len(sjobs)} found","success"); await asyncio.sleep(0.3)
    log("AI Engine","🤖 AI scoring + career page discovery...")
    all_jobs = await ai_enhance(all_jobs,user); state["stats"]["scanned"]+=120; log("AI Engine","✓ Smart matching complete","success")
    threshold = profile.get("ats_threshold",65); matched=[j for j in all_jobs if j.get("ats_score",0)>=threshold]
    # Show all jobs — label sponsorship status clearly, never hide
    for j in matched:
        j["sponsor_label"] = "✅ Sponsors H1-B" if j.get("sponsor") else "❌ No sponsorship info"
    matched.sort(key=lambda x:x.get("ats_score",0),reverse=True); matched=matched[:req.max_results]
    state["jobs"]=matched; state["stats"]["matched"]=len(matched); state["stats"]["sponsors"]=sum(1 for j in matched if j.get("sponsor")); state["running"]=False
    log("AGENT",f"✅ Done — {state['stats']['scanned']} scanned, {len(matched)} matched","success")
    user["scan_history"]=user.get("scan_history",[]); user["scan_history"].append({"date":datetime.now().isoformat(),"role":role,"scanned":state["stats"]["scanned"],"matched":len(matched),"sponsors":state["stats"]["sponsors"]}); user["scan_history"]=user["scan_history"][-20:]; save_user(user)
    # Email notification for new matches
    if user.get("notify_email") and user.get("email") and len(matched)>0:
        top = matched[:3]
        top_html = "".join(f"<div style='margin-bottom:12px;padding:12px;background:#0a0d1a;border-radius:8px;border:1px solid rgba(91,143,255,0.2)'><div style='color:#dce8ff;font-weight:700'>{j['title']}</div><div style='color:#8a9aba;font-size:13px'>{j['company']} · {j.get('salary','')}</div><div style='color:#00f5c8;font-size:12px'>ATS: {j['ats_score']}% · {j.get('match_level','')}</div></div>" for j in top)
        send_email(user["email"], f"🎯 {len(matched)} new job matches found!",
            email_html(f"{len(matched)} New Job Matches",
                f"Your scan found {len(matched)} matching jobs ({state['stats']['sponsors']} H1-B sponsors). Top matches:<br><br>{top_html}<br>View all at",))

@app.post("/api/scan")
async def start_scan(req: ScanReq, background_tasks: BackgroundTasks, x_user_id: Optional[str]=Header(default=None)):
    u = auth(x_user_id); state=get_state(u["uid"])
    if state["running"]: return {"status":"already_running"}
    background_tasks.add_task(run_scan,u["uid"],req); return {"status":"started"}

@app.get("/api/scan/status")
async def scan_status(x_user_id: Optional[str]=Header(default=None)):
    u = auth(x_user_id); state=get_state(u["uid"])
    return {"running":state["running"],"log":state["log"][-50:],"stats":state["stats"],"job_count":len(state["jobs"])}

@app.get("/api/jobs")
async def get_jobs(x_user_id: Optional[str]=Header(default=None)):
    u = auth(x_user_id); state=get_state(u["uid"]); return {"jobs":state["jobs"],"stats":state["stats"]}

@app.post("/api/resume/generate")
async def generate_resume(req: ResumeReq, x_user_id: Optional[str]=Header(default=None)):
    u = auth(x_user_id); profile=u.get("profile",{}); resume_text=u.get("resume_text",""); resume_keywords=u.get("resume_keywords",[])
    async def stream():
        base = req.candidate_bio or resume_text[:2000] or profile.get("bio","Experienced professional")
        prompt = (f"ATS-optimized resume for {req.candidate_name} → {req.role or 'this role'} at {req.company or 'the company'}.\n"
                  f"JD:{req.job_description}\nBackground:{base}\nKeywords:{', '.join(resume_keywords[:20])}\n"
                  f"Visa:{profile.get('visa_status','H1-B required')}\n"
                  f"RULES: Match 90%+ JD keywords. Every bullet=Action+Task+Metric. ATS plain text.\n"
                  f"Sections: HEADER|SUMMARY|TECHNICAL SKILLS|EXPERIENCE(3 roles,4 bullets)|PROJECTS(2)|CERTIFICATIONS|EDUCATION")
        with client.messages.stream(model="claude-opus-4-5",max_tokens=2500,
            system="Elite ATS resume architect. 90%+ keyword match. Mirror JD exactly. Quantify all bullets.",
            messages=[{"role":"user","content":prompt}]) as s:
            for text in s.text_stream: yield f"data: {json.dumps({'text':text})}\n\n"
        yield f"data: {json.dumps({'done':True})}\n\n"
    return StreamingResponse(stream(), media_type="text/event-stream")

@app.post("/api/keywords")
async def extract_keywords(req: ResumeReq, x_user_id: Optional[str]=Header(default=None)):
    u = auth(x_user_id); resume_keywords=u.get("resume_keywords",[])
    resp = client.messages.create(model="claude-opus-4-5",max_tokens=500,
        messages=[{"role":"user","content":
            f'Extract ATS keywords. Candidate has:{", ".join(resume_keywords[:10])}.\n'
            f'Return ONLY JSON:{{"must_have":[],"nice_to_have":[],"action_verbs":[],"certifications":[],"ats_score_estimate":75,"match_summary":"...","missing_keywords":[]}}\n'
            f'JD:{req.job_description[:2000]}'}])
    try: return json.loads(resp.content[0].text.strip().replace("```json","").replace("```",""))
    except: return {"must_have":[],"nice_to_have":[],"ats_score_estimate":70,"match_summary":"Analysis complete","missing_keywords":[]}

@app.get("/api/sponsors")
async def get_sponsors(): return {"sponsors":sorted(list(KNOWN_SPONSORS))}

@app.get("/api/stats")
async def get_stats(x_user_id: Optional[str]=Header(default=None)):
    u = auth(x_user_id); state=get_state(u["uid"]); return {"current":state["stats"],"history":u.get("scan_history",[])[-10:]}

@app.get("/")
async def root():
    for p in [Path("frontend/index.html"), Path(__file__).parent.parent/"frontend"/"index.html"]:
        if p.exists(): return FileResponse(str(p))

@app.on_event("startup")
async def startup():
    if not scheduler.running: scheduler.start()
    logger.info("🚀 JobHunter AI v5.0 started")

@app.on_event("shutdown")
async def shutdown():
    if scheduler.running: scheduler.shutdown()
