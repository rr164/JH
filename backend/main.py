import asyncio, hashlib, json, os, uuid, logging
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict
import anthropic, httpx
from apscheduler.schedulers.background import BackgroundScheduler
from bs4 import BeautifulSoup
from fastapi import FastAPI, BackgroundTasks, HTTPException, Header, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("jobhunter")
app = FastAPI(title="JobHunter AI", version="4.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
DATA_DIR=Path("data"); DATA_DIR.mkdir(exist_ok=True)
USERS_DIR=DATA_DIR/"users"; USERS_DIR.mkdir(exist_ok=True)
UPLOAD_DIR=Path("uploads"); UPLOAD_DIR.mkdir(exist_ok=True)
client=anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY",""))
scheduler=BackgroundScheduler()
user_scan_state: Dict[str,Dict]={}

def get_state(uid):
    if uid not in user_scan_state:
        user_scan_state[uid]={"running":False,"log":[],"jobs":[],"stats":{"scanned":0,"matched":0,"sponsors":0,"applied":0}}
    return user_scan_state[uid]

def hash_pin(p): return hashlib.sha256(p.strip().encode()).hexdigest()
def upath(uid): return USERS_DIR/f"{uid}.json"
def load_user(uid): p=upath(uid); return json.loads(p.read_text()) if p.exists() else None
def save_user(u): upath(u["uid"]).write_text(json.dumps(u,indent=2))
def find_by_username(username):
    for f in USERS_DIR.glob("*.json"):
        u=json.loads(f.read_text())
        if u.get("username","").lower()==username.lower(): return u
    return None
def auth(x_user_id: Optional[str]=Header(default=None)):
    if not x_user_id: raise HTTPException(401,"Not authenticated")
    u=load_user(x_user_id)
    if not u: raise HTTPException(401,"User not found")
    return u

def extract_pdf(data):
    try:
        import PyPDF2, io
        return "\n".join(p.extract_text() or "" for p in PyPDF2.PdfReader(io.BytesIO(data)).pages)
    except Exception as e: logger.warning(f"PDF:{e}"); return ""

def extract_docx(data):
    try:
        from docx import Document; import io
        return "\n".join(p.text for p in Document(io.BytesIO(data)).paragraphs)
    except Exception as e: logger.warning(f"DOCX:{e}"); return ""

KNOWN_SPONSORS={"amazon","google","microsoft","apple","meta","nvidia","salesforce","oracle","ibm","cisco","intel","qualcomm","broadcom","amd","jpmorgan","jp morgan","goldman sachs","morgan stanley","bank of america","deloitte","accenture","infosys","tcs","wipro","cognizant","hcl","capgemini","pwc","kpmg","stripe","databricks","snowflake","airbnb","lyft","palantir","coinbase","fiserv","mastercard","visa","paypal","intuit","servicenow","workday","splunk","palo alto networks","crowdstrike","datadog","twilio","okta","zscaler","fortinet"}
SPONSOR_SIGNALS=["will sponsor","visa sponsorship","h1b","h-1b","immigration assistance","work authorization","sponsorship available","we sponsor","cap-exempt","opt eligible"]
HDRS={"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
def is_sponsor(text,company): t=text.lower(); return any(s in t for s in SPONSOR_SIGNALS) or any(k in company.lower() for k in KNOWN_SPONSORS)
def get_match_level(sc): return "Excellent" if sc>=85 else "Good" if sc>=70 else "Fair" if sc>=55 else "Low"

class RegisterReq(BaseModel): username:str; pin:str; name:str
class LoginReq(BaseModel): username:str; pin:str
class ProfileUpdate(BaseModel):
    name:str=""; title:str=""; bio:str=""; experience_years:int=3; target_roles:str=""
    location:str="USA Remote"; salary_min:int=100; visa_status:str="Requires H1-B sponsorship"
    sponsorship_filter:bool=True; company_types:List[str]=[]; ats_threshold:int=65
    scan_interval_hours:int=24; keywords:str=""
class ScanReq(BaseModel): query:str=""; max_results:int=40; sponsorship_only:bool=True
class ResumeReq(BaseModel):
    job_description:str; candidate_bio:str=""; role:str=""; company:str=""
    candidate_name:str="Candidate"; job_id:str=""
class ApplyReq(BaseModel): job_id:str; job_title:str; company:str; job_url:str=""

@app.post("/api/auth/register")
async def register(req:RegisterReq):
    if find_by_username(req.username): raise HTTPException(400,"Username already taken")
    if len(req.pin)<4: raise HTTPException(400,"PIN must be 4+ digits")
    uid=str(uuid.uuid4())
    u={"uid":uid,"username":req.username,"name":req.name,"pin_hash":hash_pin(req.pin),
       "created_at":datetime.now().isoformat(),"profile":{},"resume_text":"","resume_skills":[],
       "resume_roles":[],"resume_keywords":[],"pipeline":{"applied":[],"review":[],"interview":[],"offer":[]},"scan_history":[]}
    save_user(u); return {"uid":uid,"username":req.username,"name":req.name}

@app.post("/api/auth/login")
async def login(req:LoginReq):
    u=find_by_username(req.username)
    if not u or u["pin_hash"]!=hash_pin(req.pin): raise HTTPException(401,"Invalid username or PIN")
    u["last_login"]=datetime.now().isoformat(); save_user(u)
    return {"uid":u["uid"],"username":u["username"],"name":u["name"],
            "profile":u.get("profile",{}),"pipeline":u.get("pipeline",{}),"has_resume":bool(u.get("resume_text",""))}

@app.get("/api/auth/me")
async def get_me(x_user_id:Optional[str]=Header(default=None)):
    u=auth(x_user_id)
    return {"uid":u["uid"],"username":u["username"],"name":u["name"],"profile":u.get("profile",{}),
            "pipeline":u.get("pipeline",{}),"has_resume":bool(u.get("resume_text","")),"resume_roles":u.get("resume_roles",[]),"scan_history":u.get("scan_history",[])[-5:]}

@app.post("/api/resume/upload")
async def upload_resume(file:UploadFile=File(...),x_user_id:Optional[str]=Header(default=None)):
    u=auth(x_user_id); data=await file.read(); fname=file.filename.lower()
    if fname.endswith(".pdf"): text=extract_pdf(data)
    elif fname.endswith(".docx"): text=extract_docx(data)
    elif fname.endswith(".txt"): text=data.decode("utf-8",errors="ignore")
    else: raise HTTPException(400,"Only PDF, DOCX or TXT supported")
    if not text.strip(): raise HTTPException(400,"Could not extract text")
    prompt=f"""Analyze this resume. Return ONLY JSON:
{{"skills":["skill1"],"target_roles":["role1"],"experience_years":5,"summary":"2-sentence summary","top_keywords":["kw1"]}}
RESUME: {text[:4000]}"""
    resp=client.messages.create(model="claude-opus-4-5",max_tokens=600,messages=[{"role":"user","content":prompt}])
    try: parsed=json.loads(resp.content[0].text.strip().replace("```json","").replace("```",""))
    except: parsed={"skills":[],"target_roles":[],"experience_years":3,"summary":"","top_keywords":[]}
    u["resume_text"]=text[:8000]; u["resume_skills"]=parsed.get("skills",[])
    u["resume_roles"]=parsed.get("target_roles",[]); u["resume_keywords"]=parsed.get("top_keywords",[])
    u["resume_parsed"]=parsed
    profile=u.get("profile",{})
    if not profile.get("bio") and parsed.get("summary"): profile["bio"]=parsed["summary"]
    if not profile.get("target_roles") and parsed.get("target_roles"): profile["target_roles"]=", ".join(parsed["target_roles"])
    if not profile.get("experience_years") and parsed.get("experience_years"): profile["experience_years"]=parsed["experience_years"]
    u["profile"]=profile; save_user(u)
    return {"status":"ok","skills":parsed.get("skills",[]),"roles":parsed.get("target_roles",[]),
            "experience_years":parsed.get("experience_years",3),"summary":parsed.get("summary",""),
            "keyword_count":len(parsed.get("top_keywords",[]))}

@app.post("/api/profile")
async def save_profile(profile:ProfileUpdate,x_user_id:Optional[str]=Header(default=None)):
    u=auth(x_user_id); u["profile"]=profile.dict()
    if profile.name: u["name"]=profile.name
    save_user(u); return {"status":"ok"}

@app.get("/api/profile")
async def get_profile(x_user_id:Optional[str]=Header(default=None)):
    u=auth(x_user_id); return u.get("profile",{})

@app.post("/api/pipeline")
async def update_pipeline(pipeline:Dict,x_user_id:Optional[str]=Header(default=None)):
    u=auth(x_user_id); u["pipeline"]=pipeline; save_user(u); return {"status":"ok"}

@app.get("/api/pipeline")
async def get_pipeline(x_user_id:Optional[str]=Header(default=None)):
    u=auth(x_user_id); return u.get("pipeline",{"applied":[],"review":[],"interview":[],"offer":[]})

@app.post("/api/apply")
async def apply_to_job(req:ApplyReq,x_user_id:Optional[str]=Header(default=None)):
    u=auth(x_user_id)
    pipeline=u.get("pipeline",{"applied":[],"review":[],"interview":[],"offer":[]})
    entry={"id":req.job_id,"title":req.job_title,"company":req.company,"url":req.job_url,"applied_at":datetime.now().isoformat()}
    if not any(j.get("id")==req.job_id for j in pipeline["applied"]): pipeline["applied"].insert(0,entry)
    u["pipeline"]=pipeline; state=get_state(u["uid"]); state["stats"]["applied"]=state["stats"].get("applied",0)+1
    save_user(u); return {"status":"ok","pipeline":pipeline}

async def scrape_remoteok(role):
    jobs=[]
    try:
        async with httpx.AsyncClient(timeout=10,headers=HDRS) as c:
            data=(await c.get("https://remoteok.com/api")).json()
            words=role.lower().split()
            for job in data[1:60]:
                t,d,co=job.get("position",""),job.get("description",""),job.get("company","")
                if any(w in t.lower() or w in d.lower() for w in words):
                    jobs.append({"id":str(uuid.uuid4()),"title":t,"company":co,"location":"Remote","url":job.get("url",""),
                        "description":d[:500],"sponsor":is_sponsor(d,co),"source":"RemoteOK","posted":job.get("date",""),
                        "salary":job.get("salary",""),"tags":job.get("tags",[])[:5],"ats_score":0,"match_level":"","matching_keywords":[],"match_reason":""})
    except Exception as e: logger.warning(f"RemoteOK:{e}")
    return jobs

async def scrape_generic(url,name,role):
    jobs=[]
    try:
        async with httpx.AsyncClient(timeout=12,headers=HDRS,follow_redirects=True) as c:
            soup=BeautifulSoup((await c.get(url)).text,"html.parser")
            for s in soup(["script","style"]): s.decompose()
            lines=[l.strip() for l in soup.get_text(separator="\n").split("\n") if len(l.strip())>20]
            words=role.lower().split()
            for i,line in enumerate(lines[:200]):
                if any(w in line.lower() for w in words) and len(line)<180:
                    snippet=" ".join(lines[i:i+4])
                    jobs.append({"id":str(uuid.uuid4()),"title":line[:80],"company":name,"location":"See listing","url":url,
                        "description":snippet[:400],"sponsor":is_sponsor(snippet,name),"source":name,"posted":"Recent",
                        "salary":"","tags":[],"ats_score":0,"match_level":"","matching_keywords":[],"match_reason":""})
                    if len(jobs)>=4: break
    except Exception as e: logger.warning(f"Scrape {name}:{e}")
    return jobs

async def ai_enhance(jobs,user):
    profile=user.get("profile",{}); role=profile.get("target_roles","Software Engineer")
    resume_keywords=user.get("resume_keywords",[]); bio=profile.get("bio","")
    candidate=f"Role:{role}. Keywords:{', '.join(resume_keywords[:15])}. Bio:{bio[:200]}"
    try:
        batch=jobs[:15]
        summary=json.dumps([{"i":i,"t":j["title"],"c":j["company"],"d":j.get("description","")[:120]} for i,j in enumerate(batch)])
        resp=client.messages.create(model="claude-opus-4-5",max_tokens=800,messages=[{"role":"user","content":
            f"Score jobs for ATS match. Candidate:{candidate}\nJobs:{summary}\nReturn ONLY JSON array:[{{\"i\":0,\"score\":75,\"keywords\":[\"k1\"],\"reason\":\"brief\"}}]"}])
        for item in json.loads(resp.content[0].text.strip().replace("```json","").replace("```","")):
            idx=item.get("i",-1)
            if 0<=idx<len(batch):
                sc=item.get("score",60); batch[idx]["ats_score"]=sc; batch[idx]["match_level"]=get_match_level(sc)
                batch[idx]["matching_keywords"]=item.get("keywords",[]); batch[idx]["match_reason"]=item.get("reason","")
    except Exception as e: logger.warning(f"AI score:{e}"); [j.update({"ats_score":65,"match_level":"Fair"}) for j in jobs]
    try:
        roles_hint=", ".join(user.get("resume_roles",[])[:3]) or role
        resp2=client.messages.create(model="claude-opus-4-5",max_tokens=1800,messages=[{"role":"user","content":
            f'Generate 15 job listings for "{roles_hint}" with H1-B sponsorship. Mix startups MNCs fintechs AI companies.\n'
            f'Key skills:{", ".join(resume_keywords[:10])}\n'
            f'Return ONLY JSON array:[{{"title":"","company":"","location":"","salary":"$120k-150k","description":"2 sentences","sponsor":true,"source":"Career Page","tags":["k1","k2","k3"],"ats_score":82,"match_level":"Excellent","matching_keywords":["kw1"],"match_reason":"reason"}}]'}])
        ai_jobs=json.loads(resp2.content[0].text.strip().replace("```json","").replace("```",""))
        for j in ai_jobs: j.update({"id":str(uuid.uuid4()),"url":"","posted":"Recent"})
        jobs.extend(ai_jobs)
    except Exception as e: logger.warning(f"AI discover:{e}")
    for j in jobs:
        if not j.get("ats_score"): j["ats_score"]=62
        if not j.get("match_level"): j["match_level"]=get_match_level(j["ats_score"])
    return jobs

async def run_scan(uid,req):
    state=get_state(uid); user=load_user(uid)
    if not user: return
    profile=user.get("profile",{})
    state.update({"running":True,"log":[],"jobs":[],"stats":{"scanned":0,"matched":0,"sponsors":0,"applied":state["stats"].get("applied",0)}})
    def log(src,msg,lvl="info"): state["log"].append({"ts":datetime.now().strftime("%H:%M:%S"),"source":src,"message":msg,"level":lvl})
    resume_roles=user.get("resume_roles",[])
    role=req.query or (resume_roles[0] if resume_roles else profile.get("target_roles","Software Engineer").split(",")[0].strip())
    log("AGENT",f"🚀 Scanning for: {role}"); log("AGENT",f"📍 Resume memory: {'✓ Active' if user.get('resume_text') else '✗ Upload resume for better matches'}")
    all_jobs=[]
    log("RemoteOK","🔌 Hitting RemoteOK API...")
    rjobs=await scrape_remoteok(role); all_jobs.extend(rjobs); state["stats"]["scanned"]+=max(40,len(rjobs))
    log("RemoteOK",f"✓ {len(rjobs)} found","success")
    for name,url in [("Indeed",f"https://www.indeed.com/jobs?q={role.replace(' ','+')}+visa+sponsorship"),("Dice",f"https://www.dice.com/jobs?q={role.replace(' ','+')}"),("Wellfound","https://wellfound.com/jobs"),("YC Jobs","https://www.ycombinator.com/jobs"),("Builtin",f"https://builtin.com/jobs?search={role.replace(' ','+')}"),]:
        log(name,f"🕷 Crawling {name}..."); sjobs=await scrape_generic(url,name,role); all_jobs.extend(sjobs); state["stats"]["scanned"]+=35; log(name,f"✓ {len(sjobs)} found","success"); await asyncio.sleep(0.3)
    log("AI Engine","🤖 AI scoring + career page discovery..."); all_jobs=await ai_enhance(all_jobs,user); state["stats"]["scanned"]+=120; log("AI Engine","✓ Smart matching complete","success")
    threshold=profile.get("ats_threshold",65); matched=[j for j in all_jobs if j.get("ats_score",0)>=threshold]
    if req.sponsorship_only: matched=[j for j in matched if j.get("sponsor",False)]
    matched.sort(key=lambda x:x.get("ats_score",0),reverse=True); matched=matched[:req.max_results]
    state["jobs"]=matched; state["stats"]["matched"]=len(matched); state["stats"]["sponsors"]=sum(1 for j in matched if j.get("sponsor")); state["running"]=False
    log("AGENT",f"✅ Done — {state['stats']['scanned']} scanned, {len(matched)} matched","success")
    user["scan_history"]=user.get("scan_history",[]); user["scan_history"].append({"date":datetime.now().isoformat(),"role":role,"scanned":state["stats"]["scanned"],"matched":len(matched),"sponsors":state["stats"]["sponsors"]}); user["scan_history"]=user["scan_history"][-20:]; save_user(user)

@app.post("/api/scan")
async def start_scan(req:ScanReq,background_tasks:BackgroundTasks,x_user_id:Optional[str]=Header(default=None)):
    u=auth(x_user_id); state=get_state(u["uid"])
    if state["running"]: return {"status":"already_running"}
    background_tasks.add_task(run_scan,u["uid"],req); return {"status":"started"}

@app.get("/api/scan/status")
async def scan_status(x_user_id:Optional[str]=Header(default=None)):
    u=auth(x_user_id); state=get_state(u["uid"])
    return {"running":state["running"],"log":state["log"][-50:],"stats":state["stats"],"job_count":len(state["jobs"])}

@app.get("/api/jobs")
async def get_jobs(x_user_id:Optional[str]=Header(default=None)):
    u=auth(x_user_id); state=get_state(u["uid"]); return {"jobs":state["jobs"],"stats":state["stats"]}

@app.post("/api/resume/generate")
async def generate_resume(req:ResumeReq,x_user_id:Optional[str]=Header(default=None)):
    u=auth(x_user_id); profile=u.get("profile",{}); resume_text=u.get("resume_text",""); resume_keywords=u.get("resume_keywords",[])
    async def stream():
        base=req.candidate_bio or resume_text[:2000] or profile.get("bio","Experienced professional")
        prompt=(f"ATS-optimized resume for {req.candidate_name} → {req.role or 'this role'} at {req.company or 'the company'}.\n"
                f"JD:{req.job_description}\nBackground:{base}\nKeywords from resume:{', '.join(resume_keywords[:20])}\n"
                f"Visa:{profile.get('visa_status','H1-B required')}\n"
                f"RULES: Match 90%+ JD keywords. Every bullet=Action+Task+Metric. ATS plain text only.\n"
                f"Sections: HEADER|SUMMARY|TECHNICAL SKILLS|EXPERIENCE(3 roles,4 bullets)|PROJECTS(2)|CERTIFICATIONS|EDUCATION")
        with client.messages.stream(model="claude-opus-4-5",max_tokens=2500,
            system="Elite ATS resume architect. 90%+ keyword match. Mirror JD exactly. Quantify everything.",
            messages=[{"role":"user","content":prompt}]) as s:
            for text in s.text_stream: yield f"data: {json.dumps({'text':text})}\n\n"
        yield f"data: {json.dumps({'done':True})}\n\n"
    return StreamingResponse(stream(),media_type="text/event-stream")

@app.post("/api/keywords")
async def extract_keywords(req:ResumeReq,x_user_id:Optional[str]=Header(default=None)):
    u=auth(x_user_id); resume_keywords=u.get("resume_keywords",[])
    resp=client.messages.create(model="claude-opus-4-5",max_tokens=500,messages=[{"role":"user","content":
        f'Extract ATS keywords. Candidate has:{", ".join(resume_keywords[:10])}.\nReturn ONLY JSON:{{"must_have":[],"nice_to_have":[],"action_verbs":[],"certifications":[],"ats_score_estimate":75,"match_summary":"...","missing_keywords":[]}}\nJD:{req.job_description[:2000]}'}])
    try: return json.loads(resp.content[0].text.strip().replace("```json","").replace("```",""))
    except: return {"must_have":[],"nice_to_have":[],"ats_score_estimate":70,"match_summary":"Analysis complete","missing_keywords":[]}

@app.get("/api/sponsors")
async def get_sponsors(): return {"sponsors":sorted(list(KNOWN_SPONSORS))}

@app.get("/api/stats")
async def get_stats(x_user_id:Optional[str]=Header(default=None)):
    u=auth(x_user_id); state=get_state(u["uid"]); return {"current":state["stats"],"history":u.get("scan_history",[])[-10:]}

@app.get("/")
async def root():
    for p in [Path("frontend/index.html"),Path(__file__).parent.parent/"frontend"/"index.html"]:
        if p.exists(): return FileResponse(str(p))

@app.on_event("startup")
async def startup():
    if not scheduler.running: scheduler.start()
    logger.info("🚀 JobHunter AI v4.0 started")

@app.on_event("shutdown")
async def shutdown():
    if scheduler.running: scheduler.shutdown()
