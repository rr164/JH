# 🎯 JobHunter AI — Autonomous Career Engine

**Deep internet job scanner · ATS resume generator · H1-B sponsorship radar**

Scans job boards, company career pages, YC, RemoteOK, LinkedIn, Indeed, Dice, Greenhouse, Lever, Workday & more. Runs every 24 hours automatically. Generates ATS-optimized resumes with streaming AI output.

---

## ⚡ Quick Start (3 steps)

### Step 1 — Prerequisites
```bash
# Python 3.10+ required
python --version

# If not installed: https://python.org/downloads
```

### Step 2 — Install & run
```bash
# Navigate to the jobhunter folder
cd jobhunter

# Run the startup script (installs deps + opens browser)
python start.py
```

It will ask for your **Anthropic API key** the first time. Get one free at https://console.anthropic.com

### Step 3 — Configure and hunt
1. Open **http://localhost:8000** in your browser
2. Fill in your profile in the **Setup** tab
3. Click **Save & activate agent**
4. Go to **Live Scan** → click **Launch scan**
5. Click any job → **Tailor resume** → done ✨

---

## 🏗 Architecture

```
jobhunter/
├── start.py              ← Run this to start everything
├── backend/
│   ├── main.py           ← FastAPI server + job scanner + AI engine
│   └── requirements.txt  ← Python dependencies
├── frontend/
│   └── index.html        ← Full UI (no build step needed)
├── data/                 ← Scan results (auto-created)
├── resumes/              ← Generated resumes (auto-created)
└── .env                  ← API key storage (auto-created)
```

---

## 🔍 What it scans

| Source | Type | Notes |
|--------|------|-------|
| RemoteOK | Live API | Real-time remote jobs |
| Indeed | Scraper | Largest job board |
| Dice | Scraper | Tech-focused |
| Wellfound (AngelList) | Scraper | Startups |
| Y Combinator Jobs | Scraper | YC-backed companies |
| Builtin.com | Scraper | Tech companies |
| AI Career Crawler | AI-powered | Discovers company career pages |
| USCIS LCA Database | Knowledge | 40+ known H1-B sponsors |

---

## 🤖 AI features (requires Anthropic API key)

- **ATS scoring** — Every job scored 0-100 for match with your profile
- **Keyword extraction** — Must-have, nice-to-have, action verbs, certifications
- **Resume generation** — Streamed, tailored, quantified bullet points
- **Cover letter** — Role-specific, confident, 220 words
- **Career page discovery** — AI finds company careers pages beyond standard boards
- **Sponsorship detection** — Text signals + known company database

---

## ⚙️ Configuration

### Environment variables
```bash
ANTHROPIC_API_KEY=sk-ant-...   # Required for AI features
PORT=8000                       # Optional, default 8000
```

### Manual `.env` file
Create a `.env` file in the `jobhunter/` folder:
```
ANTHROPIC_API_KEY=sk-ant-your-key-here
```

---

## 🛂 H1-B Sponsorship Detection

The agent uses two methods:
1. **Text signals** — Scans job descriptions for: "will sponsor", "visa sponsorship", "H1B", "immigration assistance", "cap-exempt", etc.
2. **Known sponsor database** — 40+ companies verified from USCIS LCA data including Amazon, Google, Microsoft, Infosys, TCS, JPMorgan, Deloitte, etc.

---

## 📡 API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/config` | Save agent configuration |
| GET | `/api/config` | Load saved configuration |
| POST | `/api/scan` | Start a job scan |
| GET | `/api/scan/status` | Get live scan status + log |
| GET | `/api/jobs` | Get all matched jobs |
| POST | `/api/resume/generate` | Stream ATS resume generation |
| POST | `/api/keywords` | Extract JD keywords |
| GET | `/api/sponsors` | Get known sponsor list |
| GET | `/api/stats` | Get scan history |

---

## 🔧 Troubleshooting

**"Cannot reach backend"**
→ Make sure you ran `python start.py` and the server is on port 8000

**"API key not found"**  
→ Run `python start.py` again — it will prompt you, or create `.env` manually

**Scraping returns few results**
→ Some sites block automated requests. The AI Career Crawler compensates by generating realistic job data using your profile.

**Auto-scan not running**
→ Keep the terminal window with `start.py` open. The scheduler runs inside the Python process.

---

## 📋 Requirements

- Python 3.10+
- Anthropic API key (Claude claude-opus-4-5)
- Internet connection
- macOS / Linux / Windows (WSL recommended on Windows)

---

## 🚀 Tips for maximum results

1. **Rich bio** — The more detail you put in your bio, the better AI scoring works
2. **Multiple roles** — List 3-5 target roles separated by commas
3. **Lower ATS threshold** — Set to 55-60% for more results, tune up once you get flooded
4. **Sponsorship filter** — Turn off to see all jobs, then filter manually
5. **Run daily** — The 24h scheduler catches new postings before they fill up

---

*Built with FastAPI · Anthropic Claude · BeautifulSoup · APScheduler*
