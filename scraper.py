"""
╔══════════════════════════════════════════════════════════════════════╗
║  JOB AI AGENT V2 — AUTONOMOUS SCRAPER (FIXED)                       ║
║                                                                      ║
║  ARCHITECTURE (Hybrid — Best of Both Worlds):                        ║
║    Step 1: CSS Selectors → extract raw description (fast, reliable)  ║
║    Step 2: AI analyzes description → score, tech, summary, email     ║
║    Step 3: If CSS fails → AI finds description from page text        ║
║    Step 4: If AI partial → escalate to more powerful model           ║
║                                                                      ║
║  AI Model Cascade:                                                   ║
║    FAST:    llama-3.1-8b       → metadata extraction                 ║
║    SMART:   llama-3.3-70b      → if fast gives poor results          ║
║    POWER:   nemotron-70b       → deep reasoning, self-heal           ║
║    ULTRA:   nemotron-ultra-253b→ maximum intelligence                ║
║    SUPREME: nemotron-550b-a55b → last resort, most powerful          ║
║                                                                      ║
║  Databricks: Works on Databricks (requests-first, PW optional)      ║
║  Scheduling: Run daily as Databricks Job                             ║
╚══════════════════════════════════════════════════════════════════════╝
"""

import os, sys, csv, re, time, random, json, requests, hashlib, uuid, logging
from datetime import datetime, date, timedelta
from urllib.parse import urlparse, quote_plus
from collections import defaultdict

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

try:
    from tabulate import tabulate
    HAS_TABULATE = True
except ImportError:
    HAS_TABULATE = False

# ============================================================
# ⚙️  CONFIG
# ============================================================
CONFIG = {
    "output_csv":  "jobs_ai_agent_output.csv",
    "nvidia_api_key": os.getenv("NVIDIA_NIM_API_KEY",
        ""),
    "nvidia_base_url": "https://integrate.api.nvidia.com/v1",

    # AI Model Cascade — ordered from fast to most powerful
    "models": {
        "fast":    "meta/llama-3.1-8b-instruct",
        "smart":   "meta/llama-3.3-70b-instruct",
        "power":   "nvidia/llama-3.1-nemotron-70b-instruct",
        "ultra":   "nvidia/llama-3.1-nemotron-ultra-253b-v1",
        "supreme": "nvidia/nemotron-3-ultra-550b-a55b",
    },
    "thinking_budget": 500,

    "roles": [
        "Data Engineer",
        "Senior Data Engineer",
        "PySpark Engineer",
        "ETL Developer",
        "Analytics Engineer",
        "Machine Learning Engineer",
        "Databricks Engineer",
        "Spark Developer",
    ],

    "max_jobs_per_portal":    25,
    "page_timeout":           30,
    "enable_all_sites":       True,
    "headless":               True,
    "min_description_words":  100,
    "max_retries":            3,
    "retry_delay_min":        2.0,
    "retry_delay_max":        6.0,
    "between_jobs_min":       1.5,
    "between_jobs_max":       3.5,
}

TODAY     = date.today()
YESTERDAY = TODAY - timedelta(days=1)

CSV_HEADERS = [
    "id", "job_hash", "fetch_date", "portal", "search_keyword",
    "job_title", "company_name", "location", "remote_type",
    "salary_range", "experience_years", "tech_stack",
    "posted_date", "job_description", "description_length",
    "roles_responsibilities", "requirements_section",
    "apply_link", "easy_apply_link", "company_career_url",
    "company_website", "hr_email", "job_id", "visa_sponsorship",
    "validation_score", "validation_status",
    "ai_summary", "ai_model_used", "extraction_attempts",
]

# Verified CSS selectors per portal (from real browser inspection)
PORTAL_CSS = {
    "dice.com": [
        "div[data-testid='jobDescriptionHtml']",
        "div[class*='description']",        # VERIFIED: 5193 chars
        "div[class*='job-description']",
    ],
    "linkedin.com": [
        "div.description__text",
        "div[class*='show-more-less-html']",
        "div[class*='description__text']",
    ],
    "indeed.com": [
        "div#jobDescriptionText",
        "div[class*='jobsearch-JobComponent-description']",
        "div[id*='jobDescription']",
    ],
    "glassdoor.com": [
        "div[class*='JobDetails_jobDescription']",
        "div.jobDescriptionContent",
        "div[class*='job-description']",
    ],
    "wellfound.com":    ["div[class*='description']", "section[class*='job-description']"],
    "builtin.com":      ["div.job-description", "div[class*='job-description']"],
    "simplyhired.com":  ["div[data-testid='VJ-section-description']", "div.viewjob-description"],
    "ziprecruiter.com": ["div[class*='job_description']", "div[class*='jobDescription']"],
    "greenhouse.io":    ["div#content", "div.job-post"],
    "lever.co":         ["div.content", "div[class*='posting-description']"],
    "monster.com":      ["div[class*='job-description']", "section[class*='description']"],
}
FALLBACK_CSS = [
    "div#jobDescription", "div.description", "div[class*='description']",
    "section[class*='description']", "article", "main",
]

# JS-heavy sites that need Playwright (not just requests)
NEEDS_JS = {"linkedin.com", "dice.com", "glassdoor.com", "wellfound.com", "ziprecruiter.com"}

# Portal JS wait times (seconds after page load)
PORTAL_WAIT = {
    "dice.com": 5, "linkedin.com": 4, "glassdoor.com": 4,
    "wellfound.com": 5, "ziprecruiter.com": 3, "default": 3,
}

UA_LIST = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
]

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", re.I)
EMAIL_BL = {"example.com","test.com","noreply","sentry.io","amazonaws.com",
            "cloudfront.net","w3.org","schema.org","intercom.io","hubspot.com","wixpress.com"}

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("JobAIAgent")


# ============================================================
# 📧  HELPERS
# ============================================================
def extract_best_email(html: str) -> str:
    emails = EMAIL_RE.findall(html)
    clean, seen = [], set()
    for e in emails:
        e = e.lower().strip(".,;")
        if any(bl in e.split("@")[-1] for bl in EMAIL_BL): continue
        if e not in seen: seen.add(e); clean.append(e)
    if not clean: return ""
    for prefix in ["recruit","talent","hr","hiring","jobs","careers","apply","people"]:
        for e in clean:
            if prefix in e.split("@")[0]: return e
    return clean[0]

def extract_roles(text: str) -> str:
    for pat in [
        r"(?:key\s+)?responsibilities[:\s]*\n(.*?)(?=\n(?:requirements|qualifications|skills|benefits|about|education)|$)",
        r"(?:what you(?:'ll)? do|your role|day.to.day)[:\s]*\n(.*?)(?=\n(?:requirements|qualifications|skills|benefits)|$)",
    ]:
        m = re.search(pat, text, re.I|re.DOTALL)
        if m and len(m.group(1).strip()) > 100: return m.group(1).strip()[:2000]
    return ""

def extract_requirements(text: str) -> str:
    for pat in [
        r"(?:requirements|qualifications|must have|required skills)[:\s]*\n(.*?)(?=\n(?:benefits|about|nice to have|preferred|compensation)|$)",
    ]:
        m = re.search(pat, text, re.I|re.DOTALL)
        if m and len(m.group(1).strip()) > 100: return m.group(1).strip()[:2000]
    return ""


# ============================================================
# 🌐  PAGE FETCHER
# ============================================================
class PageFetcher:
    """Fetch page content. Requests-first (Databricks compatible), Playwright fallback."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "DNT": "1",
        })
        self._pw = None
        self._browser = None
        self._ctx = None
        self._page = None

    def _ua(self): return random.choice(UA_LIST)

    def _get_wait(self, url):
        return next((v for k,v in PORTAL_WAIT.items() if k in url), PORTAL_WAIT["default"])

    def _init_pw(self):
        if not HAS_PLAYWRIGHT: return False
        if self._pw is None:
            try:
                self._pw = sync_playwright().start()
                self._browser = self._pw.chromium.launch(
                    headless=CONFIG["headless"],
                    args=["--no-sandbox","--disable-blink-features=AutomationControlled","--disable-dev-shm-usage"])
                self._ctx = self._browser.new_context(
                    user_agent=self._ua(), viewport={"width":1280,"height":800},
                    extra_http_headers={"Accept-Language":"en-US,en;q=0.9"})
                self._ctx.route("**/*.{png,jpg,jpeg,gif,svg,woff,woff2,ttf,mp4,ico}", lambda r: r.abort())
                self._page = self._ctx.new_page()
                log.info("   🎭 Playwright ready")
                return True
            except Exception as e:
                log.warning(f"   Playwright failed: {e}")
                return False
        return True

    def fetch(self, url: str) -> tuple[str, str, object]:
        """
        Returns (text, html, pw_page_or_None).
        pw_page returned so caller can use CSS selectors directly.
        """
        needs_js = any(s in url for s in NEEDS_JS)

        # Try requests for non-JS sites
        if not needs_js:
            try:
                self.session.headers["User-Agent"] = self._ua()
                resp = self.session.get(url, timeout=CONFIG["page_timeout"])
                if resp.status_code == 200 and len(resp.text) > 500:
                    return resp.text, resp.text, None
            except Exception as e:
                log.debug(f"   Requests failed: {e}")

        # Playwright for JS-heavy sites
        if self._init_pw() and self._page:
            try:
                self._page.goto(url, wait_until="domcontentloaded",
                                timeout=CONFIG["page_timeout"] * 1000)
                wait_secs = self._get_wait(url)
                time.sleep(wait_secs)
                try:
                    self._page.wait_for_selector(
                        "h1, h2, div[class*='description'], article, div#jobDescriptionText",
                        timeout=8000)
                except: pass
                text = self._page.inner_text("body") or ""
                html = self._page.content() or ""
                return text, html, self._page
            except Exception as e:
                log.debug(f"   Playwright fetch failed: {e}")

        return "", "", None

    def fetch_with_retry(self, url: str) -> tuple[str, str, object]:
        for attempt in range(CONFIG["max_retries"]):
            text, html, page_obj = self.fetch(url)
            if len(text) > 200:
                return text, html, page_obj
            if attempt < CONFIG["max_retries"] - 1:
                sleep_t = random.uniform(CONFIG["retry_delay_min"], CONFIG["retry_delay_max"])
                log.debug(f"   Retry {attempt+2}/{CONFIG['max_retries']} in {sleep_t:.1f}s")
                time.sleep(sleep_t)
        return "", "", None

    def css_extract(self, url: str, page_obj, html: str) -> str:
        """
        Extract raw description text using CSS selectors.
        Uses Playwright page object if available, falls back to regex on HTML.
        """
        portal_key = next((k for k in PORTAL_CSS if k in url), None)
        selectors  = (PORTAL_CSS.get(portal_key, []) if portal_key else []) + FALLBACK_CSS

        desc = ""

        # Method 1: Playwright CSS selector (most accurate)
        if page_obj is not None:
            for sel in selectors:
                try:
                    el = page_obj.query_selector(sel)
                    if el:
                        candidate = el.inner_text().strip()
                        if len(candidate) > len(desc) and len(candidate) > 80:
                            desc = candidate
                        if len(desc) > 500:
                            break
                except: continue

            # JS-driven fallback: find largest job-like div
            if len(desc) < 100:
                try:
                    desc = page_obj.evaluate("""() => {
                        let best = '';
                        for (let el of document.querySelectorAll('div,section,article')) {
                            const t = el.innerText || '';
                            if (t.length > best.length && t.length < 15000) {
                                const l = t.toLowerCase();
                                if (l.includes('responsib') || l.includes('qualif') ||
                                    l.includes('experience') || l.includes('skill') ||
                                    l.includes('requirement')) { best = t; }
                            }
                        }
                        return best;
                    }""") or ""
                except: pass

        # Method 2: Regex on raw HTML (for requests-based fetch)
        if len(desc) < 100 and html:
            # Strip scripts/styles
            clean = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL|re.I)
            clean = re.sub(r'<style[^>]*>.*?</style>', '', clean, flags=re.DOTALL|re.I)
            # Extract text from description divs
            for pattern in [
                r'(?:id=["\']jobDescriptionText["\']|data-testid=["\']jobDescriptionHtml["\'])[^>]*>(.*?)</div',
                r'class=["\'][^"\']*description[^"\']*["\'][^>]*>(.*?)</(?:div|section|article)',
            ]:
                m = re.search(pattern, clean, re.DOTALL|re.I)
                if m:
                    raw = re.sub(r'<[^>]+>', ' ', m.group(1))
                    raw = re.sub(r'\s+', ' ', raw).strip()
                    if len(raw) > len(desc): desc = raw
                    if len(desc) > 300: break

        return desc.strip()

    def close(self):
        for obj in [self._page, self._ctx, self._browser]:
            try:
                if obj: obj.close()
            except: pass
        try:
            if self._pw: self._pw.stop()
        except: pass


# ============================================================
# 🧠  AI ENGINE — Cascade with Self-Healing
# ============================================================
class AIEngine:
    """
    Multi-model AI brain.
    - Analyzes already-extracted description text (no JSON escaping issues)
    - Escalates to more powerful models when data is incomplete
    - Self-heals: reasons about WHY data is missing
    """

    def __init__(self):
        self.api_key    = CONFIG["nvidia_api_key"]
        self.base_url   = CONFIG["nvidia_base_url"]
        self.models     = CONFIG["models"]
        self.call_counts = defaultdict(int)

    def call(self, prompt: str, tier: str = "fast", max_tokens: int = 500) -> str | None:
        """Call NVIDIA NIM with specified model tier."""
        model = self.models.get(tier, self.models["fast"])
        self.call_counts[tier] += 1
        for attempt in range(3):
            try:
                resp = requests.post(
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}",
                             "Content-Type": "application/json"},
                    json={"model": model,
                          "messages": [{"role": "user", "content": prompt}],
                          "temperature": 0.1, "max_tokens": max_tokens},
                    timeout=30,
                )
                if resp.status_code == 200:
                    return resp.json()["choices"][0]["message"]["content"].strip()
                elif resp.status_code == 429:
                    time.sleep(5)
            except Exception as e:
                log.debug(f"AI call error [{tier}]: {e}")
                if attempt < 2: time.sleep(random.uniform(1, 3))
        return None

    def analyze(self, description: str, title: str, company: str,
                 location: str, url: str) -> dict:
        """
        CORE AI FUNCTION:
        Given the raw description text (already extracted by CSS),
        AI analyzes it to produce: score, tech_stack, experience,
        remote_type, salary, summary, roles, requirements, visa.
        
        Uses cascade: fast → smart → power → ultra → supreme
        """
        result = {
            "validation_score": 0,
            "validation_status": "Pending",
            "ai_summary": "",
            "roles_summary": "",
            "tech_stack": "",
            "experience_years": "Not specified",
            "remote_type": "Not specified",
            "salary_range": "",
            "visa_sponsorship": False,
            "ai_model_used": "none",
            "extraction_attempts": 0,
        }

        if len(description.split()) < 20:
            result.update({
                "validation_score": 50,
                "validation_status": "Partial",
                "ai_summary": "Insufficient description",
            })
            return result

        desc_sample = description[:3500]

        # Cascade tiers
        tiers = [
            ("fast",   500, ""),
            ("smart",  700, "Previous model gave incomplete result. Think carefully."),
            ("power",  900, f"Use up to {CONFIG['thinking_budget']} tokens to reason before answering."),
            ("ultra", 1200, f"This is a critical extraction. Use all {CONFIG['thinking_budget']} tokens to think step-by-step."),
        ]

        for tier, max_tok, extra_instruction in tiers:
            result["extraction_attempts"] += 1
            prompt = self._build_analysis_prompt(
                desc_sample, title, company, location, url,
                extra_instruction, previous=result
            )
            log.debug(f"   🤖 [{tier}] analyzing...")
            response = self.call(prompt, tier, max_tokens=max_tok)

            if not response:
                log.debug(f"   [{tier}] No response, escalating...")
                continue

            parsed = self._parse_response(response)
            if not parsed:
                log.debug(f"   [{tier}] Parse failed, escalating...")
                continue

            # Merge good data
            for key, val in parsed.items():
                if val and val not in ("", "Not specified", "N/A", "Unknown", "0", 0):
                    result[key] = val

            result["ai_model_used"] = tier
            score = int(result.get("validation_score") or 0)

            # Check quality of extraction
            has_tech    = len(result.get("tech_stack","").split(",")) >= 2
            has_summary = len(result.get("ai_summary","").split()) >= 10
            has_score   = score > 0

            if has_tech and has_summary and has_score:
                log.debug(f"   ✅ [{tier}] Good extraction: score={score}%, tech={result['tech_stack'][:40]}")
                break
            else:
                missing = []
                if not has_tech:    missing.append("tech_stack")
                if not has_summary: missing.append("summary")
                if not has_score:   missing.append("score")
                log.debug(f"   ⚠️ [{tier}] Missing: {missing} — escalating...")

                # Self-heal: ask why
                if tier in ("fast", "smart"):
                    self._self_heal_reason(desc_sample, missing, result, tier)

            time.sleep(0.5)

        # Finalize status
        score = int(result.get("validation_score") or 0)
        result["validation_status"] = ("Valid"   if score >= 70 else
                                       "Partial" if score >= 40 else "Junk")
        return result

    def _build_analysis_prompt(self, desc: str, title: str, company: str,
                                location: str, url: str,
                                extra: str, previous: dict) -> str:
        prev_tech  = previous.get("tech_stack","")
        prev_score = previous.get("validation_score", 0)
        prev_note  = (f"Previous attempt got: tech={prev_tech[:40]}, score={prev_score}. "
                      "Improve on this." if prev_tech or prev_score else "")

        return f"""Analyze this US IT job description. {extra}
{prev_note}

Job Title: {title}
Company: {company}
Location: {location}
URL: {url}

JOB DESCRIPTION TEXT:
{desc}

Return ONLY valid JSON (no markdown, no explanation):
{{
  "validation_score": <integer 0-100, how relevant for a US IT data engineering job seeker>,
  "ai_summary": "<2 clear sentences: what this company does and what this role does day-to-day>",
  "roles_summary": "<bullet points of 3-5 key responsibilities>",
  "tech_stack": "<comma-separated top 10 technologies/frameworks/tools mentioned>",
  "experience_years": "<e.g. '5+ years' or '3-5 years' — from the job description>",
  "remote_type": "<exactly one of: Remote | Hybrid | Onsite | Not specified>",
  "salary_range": "<salary if explicitly mentioned in description, else empty string>",
  "visa_sponsorship": <true if visa or sponsorship mentioned, else false>
}}"""

    def _parse_response(self, response: str) -> dict | None:
        """Parse JSON from AI response robustly."""
        # Try direct JSON parse
        try:
            m = re.search(r'\{[^{}]*\}', response, re.DOTALL)
            if m:
                data = json.loads(m.group())
                if any(k in data for k in ("validation_score", "tech_stack", "ai_summary")):
                    return data
        except: pass

        # Broader search (handles nested braces)
        try:
            start = response.find('{')
            end   = response.rfind('}')
            if start != -1 and end != -1 and end > start:
                data = json.loads(response[start:end+1])
                return data
        except: pass

        # Field-by-field extraction
        result = {}
        for key, pat in [
            ("ai_summary",      r'"ai_summary"\s*:\s*"((?:[^"\\]|\\.)*)"'),
            ("roles_summary",   r'"roles_summary"\s*:\s*"((?:[^"\\]|\\.)*)"'),
            ("tech_stack",      r'"tech_stack"\s*:\s*"([^"]+)"'),
            ("experience_years",r'"experience_years"\s*:\s*"([^"]*)"'),
            ("remote_type",     r'"remote_type"\s*:\s*"([^"]+)"'),
            ("salary_range",    r'"salary_range"\s*:\s*"([^"]*)"'),
        ]:
            m = re.search(pat, response, re.DOTALL)
            if m: result[key] = m.group(1).replace('\\"', '"').strip()

        m = re.search(r'"validation_score"\s*:\s*(\d+)', response)
        if m: result["validation_score"] = int(m.group(1))

        m = re.search(r'"visa_sponsorship"\s*:\s*(true|false)', response, re.I)
        if m: result["visa_sponsorship"] = m.group(1).lower() == "true"

        return result if result else None

    def _self_heal_reason(self, desc: str, missing: list, current: dict, tier: str):
        """AI reasons about WHY data is incomplete — for debugging and self-correction."""
        prompt = f"""A previous AI attempt failed to extract {missing} from this job description.
Tech found so far: {current.get('tech_stack','none')}
Score found so far: {current.get('validation_score', 0)}

Description (first 1500 chars):
{desc[:1500]}

In 2 sentences: Why might {missing} be missing or incorrect?
What specifically in the text should be used to extract them?"""
        reasoning = self.call(prompt, "smart", max_tokens=150)
        if reasoning:
            log.debug(f"   🔍 Self-heal reason: {reasoning[:120]}...")

    def score_jobs_list(self, jobs_text: str, keyword: str, portal: str) -> list[dict]:
        """
        AI extracts structured job listings from a list page.
        Returns list of {title, company, location, url, posted, job_id}.
        """
        prompt = f"""From this {portal} jobs listing page (keyword: "{keyword}"),
extract all job postings. Return ONLY a JSON array, no other text.

Page content:
{jobs_text[:5000]}

JSON array format:
[
  {{"title": "<job title>", "company": "<company>", "location": "<city state>",
    "url": "<full job URL — must start with http>", "posted": "<time ago or date>", "job_id": "<id if visible>"}}
]

Rules:
- Only real job postings, not navigation or ads
- Include max {CONFIG['max_jobs_per_portal']} jobs
- URL must be complete (fix relative URLs by prepending the portal's base domain)
- If no jobs found: []"""

        resp = self.call(prompt, "smart", max_tokens=2000)
        if not resp:
            return []
        try:
            m = re.search(r'\[.*\]', resp, re.DOTALL)
            if m:
                data = json.loads(m.group())
                return [j for j in data if isinstance(j, dict) and j.get("url","").startswith("http")]
        except Exception as e:
            log.debug(f"Job list parse error: {e}")
        return []


# ============================================================
# 🏢  JOB PORTAL SCRAPERS (AI-powered discovery)
# ============================================================
class PortalScraper:
    def __init__(self, fetcher: PageFetcher, ai: AIEngine):
        self.fetcher = fetcher
        self.ai = ai

        self.portals = {
            "LinkedIn":     ("https://www.linkedin.com/jobs/search?keywords={q}&location=United+States&f_TPR=r86400&sortBy=DD",     "https://www.linkedin.com"),
            "Indeed":       ("https://www.indeed.com/jobs?q={q}&l=United+States&fromage=1&sort=date",                               "https://www.indeed.com"),
            "Dice":         ("https://www.dice.com/jobs?q={q}&countryCode=US&radius=30&radiusUnit=mi&page=1&pageSize=20&filters.postedDate=ONE_DAY_AGO", "https://www.dice.com"),
            "Built In":     ("https://builtin.com/jobs?search={q}",                                                                  "https://builtin.com"),
            "Glassdoor":    ("https://www.glassdoor.com/Job/united-states-{slug}-jobs-SRCH_IL.0,13_IN1.htm?fromAge=1",             "https://www.glassdoor.com"),
            "ZipRecruiter": ("https://www.ziprecruiter.com/Jobs/{slug}?days=1&sort=date",                                            "https://www.ziprecruiter.com"),
            "SimplyHired":  ("https://www.simplyhired.com/search?q={q}&l=United+States&fdb=1&sb=dd",                               "https://www.simplyhired.com"),
            "Monster":      ("https://www.monster.com/jobs/search?q={q}&where=United+States&tm=1",                                  "https://www.monster.com"),
            "Wellfound":    ("https://wellfound.com/jobs?q={q}&remote=true",                                                         "https://wellfound.com"),
        }

    def discover(self, portal: str, keyword: str) -> list[dict]:
        if portal not in self.portals:
            return []
        url_tmpl, base_domain = self.portals[portal]
        q    = quote_plus(keyword)
        slug = keyword.replace(" ", "-").lower()
        url  = url_tmpl.format(q=q, slug=slug)

        log.info(f"  🌐 {portal}: {url[:75]}")
        text, html, _ = self.fetcher.fetch_with_retry(url)
        if not text:
            log.warning(f"  ⚠️ {portal}: No content")
            return []

        raw_jobs = self.ai.score_jobs_list(text, keyword, portal)
        jobs = []
        seen = set()
        for j in raw_jobs:
            job_url = j.get("url","")
            if not job_url or job_url in seen: continue
            seen.add(job_url)
            jobs.append({
                "portal":         portal,
                "search_keyword": keyword,
                "job_title":      j.get("title", keyword),
                "company_name":   j.get("company","Unknown"),
                "location":       j.get("location","USA"),
                "apply_link":     job_url,
                "posted_date":    j.get("posted",""),
                "job_id":         j.get("job_id",""),
            })
        log.info(f"  📦 {portal}: {len(jobs)} jobs found")
        return jobs


# ============================================================
# 🔍  FULL JOB DETAIL EXTRACTOR
# ============================================================
class DetailExtractor:
    """
    For each job: fetch page → CSS extract description → AI analyze.
    Self-heals on failure. Uses model cascade.
    """

    def __init__(self, fetcher: PageFetcher, ai: AIEngine):
        self.fetcher = fetcher
        self.ai = ai

    def extract(self, job: dict) -> dict:
        url    = job.get("apply_link","")
        title  = job.get("job_title","")
        portal = job.get("portal","")

        if not url:
            return job

        # ── STEP 1: Fetch page ─────────────────────────────────
        text, html, page_obj = self.fetcher.fetch_with_retry(url)

        if not text:
            log.warning(f"   ⚠️ Could not fetch page")
            job.update({
                "job_description": "", "description_length": 0,
                "validation_score": 50, "validation_status": "Partial",
                "ai_summary": "Page fetch failed", "ai_model_used": "none",
                "extraction_attempts": 0,
            })
            return job

        # ── STEP 2: CSS → Raw description (fast, reliable) ────
        raw_desc = self.fetcher.css_extract(url, page_obj, html)
        desc_words = len(raw_desc.split()) if raw_desc else 0

        # ── STEP 3: If CSS got nothing, AI finds description ──
        if desc_words < 50:
            log.debug(f"   CSS got {desc_words} words — using AI to find description...")
            raw_desc = self._ai_find_description(text, url, title)
            desc_words = len(raw_desc.split()) if raw_desc else 0
            log.debug(f"   AI-found description: {desc_words} words")

        # ── STEP 4: AI analyzes description ───────────────────
        company  = job.get("company_name","")
        location = job.get("location","")

        log.debug(f"   📝 {desc_words} words — AI analyzing...")
        ai_result = self.ai.analyze(raw_desc, title, company, location, url)

        # Extract emails and sub-sections from raw text
        email         = extract_best_email(html)
        roles_resp    = extract_roles(raw_desc)
        requirements  = extract_requirements(raw_desc)

        # Update job record
        job.update({
            "job_description":     raw_desc[:5000],
            "description_length":  desc_words,
            "roles_responsibilities": roles_resp[:2000],
            "requirements_section":   requirements[:2000],
            "hr_email":            email,
            "validation_score":    ai_result.get("validation_score", 0),
            "validation_status":   ai_result.get("validation_status","Partial"),
            "ai_summary":          ai_result.get("ai_summary",""),
            "roles_summary":       ai_result.get("roles_summary",""),
            "tech_stack":          ai_result.get("tech_stack",""),
            "experience_years":    ai_result.get("experience_years",""),
            "remote_type":         ai_result.get("remote_type",""),
            "visa_sponsorship":    ai_result.get("visa_sponsorship",False),
            "ai_model_used":       ai_result.get("ai_model_used",""),
            "extraction_attempts": ai_result.get("extraction_attempts",0),
        })

        # Salary: AI-extracted if not already in job record
        if ai_result.get("salary_range") and not job.get("salary_range","").strip():
            job["salary_range"] = ai_result["salary_range"]

        return job

    def _ai_find_description(self, page_text: str, url: str, title: str) -> str:
        """
        When CSS fails: AI reads the page text and extracts the description.
        Uses plain text response (NO JSON) to avoid escaping issues.
        """
        prompt = f"""From this job listing page text, extract ONLY the job description section.
Output ONLY the job description text — no JSON, no formatting, just the raw description.
If you cannot find it, output: DESCRIPTION NOT FOUND

Job Title: {title}
URL: {url}

Page text:
{page_text[:4000]}

Job description (output only the description text):"""

        for tier in ("smart", "power", "ultra"):
            resp = self.ai.call(prompt, tier, max_tokens=1500)
            if resp and "DESCRIPTION NOT FOUND" not in resp and len(resp.split()) > 30:
                return resp
        return ""


# ============================================================
# 💾  CSV OPERATIONS
# ============================================================
def build_record(job: dict) -> dict:
    r = {h: "" for h in CSV_HEADERS}
    r.update({
        "id":          str(uuid.uuid4()),
        "job_hash":    hashlib.md5(job.get("apply_link","").encode()).hexdigest(),
        "fetch_date":  date.today().isoformat(),
    })
    r.update(job)
    # Coerce types for CSV
    for f in ("description_length","validation_score","extraction_attempts"):
        r[f] = str(r.get(f, 0) or 0)
    for f in ("visa_sponsorship",):
        r[f] = str(r.get(f, False))
    return r

def save_csv(records: list, append: bool = False) -> int:
    csv_file = CONFIG["output_csv"]
    existing = set()
    if os.path.exists(csv_file):
        try:
            with open(csv_file, "r", encoding="utf-8") as f:
                existing = {row.get("job_hash","") for row in csv.DictReader(f)}
        except: pass

    new_recs = [r for r in records if r.get("job_hash","") not in existing]
    if not new_recs: return 0

    mode = "a" if (append and os.path.exists(csv_file)) else "w"
    with open(csv_file, mode=mode, newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_HEADERS, extrasaction="ignore")
        if mode == "w": w.writeheader()
        w.writerows(new_recs)
    return len(new_recs)


# ============================================================
# 📊  SUMMARY
# ============================================================
def print_summary(records: list, ai: AIEngine):
    by_portal = defaultdict(int)
    by_status = defaultdict(int)
    by_model  = defaultdict(int)
    with_desc = w_email = total_words = 0

    for r in records:
        by_portal[r.get("portal","?")] += 1
        by_status[r.get("validation_status","?")] += 1
        by_model[r.get("ai_model_used","?")] += 1
        dlen = int(r.get("description_length",0) or 0)
        if dlen > 50: with_desc += 1; total_words += dlen
        if r.get("hr_email"): w_email += 1

    avg = total_words // max(with_desc, 1)
    total = len(records)

    print("\n" + "="*70)
    print("  🤖 JOB AI AGENT — COMPLETE")
    print("="*70)
    print(f"  📋 Total:          {total}")
    print(f"  📝 With desc:      {with_desc} ({with_desc*100//max(total,1)}%) avg {avg} words")
    print(f"  📧 With email:     {w_email}")
    print(f"  ✅ Valid (70%+):   {by_status.get('Valid',0)}")
    print(f"  ⚠️  Partial:       {by_status.get('Partial',0)}")
    print(f"  ❌ Junk:           {by_status.get('Junk',0)}")
    print(f"\n🧠 AI Model Usage:")
    for m, c in sorted(by_model.items(), key=lambda x:-x[1]):
        print(f"   {m:<10}: {c}")
    print(f"   Total calls: {sum(ai.call_counts.values())}")

    if HAS_TABULATE:
        print("\n🌐 By Portal:")
        print(tabulate([[p,c] for p,c in sorted(by_portal.items(),key=lambda x:-x[1])],
                       headers=["Portal","Jobs"], tablefmt="rounded_outline"))

    print(f"\n📁 CSV: {os.path.abspath(CONFIG['output_csv'])}")
    print("="*70)

    print("\n📋 SAMPLE JOBS (top 5):")
    shown = 0
    for r in records:
        if shown >= 5: break
        if int(r.get("description_length",0) or 0) > 100:
            print(f"\n  🏢 {r.get('company_name','?')[:28]} | {r.get('portal','?')}")
            print(f"  💼 {r.get('job_title','?')[:55]}")
            print(f"  📍 {r.get('location','?')[:35]} | 🏠 {r.get('remote_type','?')}")
            print(f"  💰 {r.get('salary_range','N/A')[:45]}")
            print(f"  🛠️  {r.get('tech_stack','')[:62]}")
            print(f"  📅 {r.get('experience_years','N/A')} | 🤖 {r.get('validation_score','?')}% {r.get('validation_status','?')} [{r.get('ai_model_used','?')}]")
            if r.get('hr_email'): print(f"  📧 {r['hr_email']}")
            print(f"  🔗 {r.get('apply_link','')[:72]}")
            preview = " ".join(str(r.get("job_description","")).split()[:25])
            print(f"  📄 {preview}...")
            shown += 1


# ============================================================
# 🚀  MAIN PIPELINE
# ============================================================
def run_ai_agent():
    log.info("="*70)
    log.info("🤖 JOB AI AGENT V2 — AUTONOMOUS SCRAPER")
    log.info(f"📋 Roles: {len(CONFIG['roles'])} | Portals: {'ALL' if CONFIG['enable_all_sites'] else 'Top 4'}")
    log.info(f"🧠 Models: fast={CONFIG['models']['fast'].split('/')[-1]} → supreme={CONFIG['models']['supreme'].split('/')[-1]}")
    log.info("="*70)

    ai      = AIEngine()
    fetcher = PageFetcher()
    scraper = PortalScraper(fetcher, ai)
    extractor = DetailExtractor(fetcher, ai)

    portals = ["LinkedIn", "Indeed", "Dice", "Built In"]
    if CONFIG["enable_all_sites"]:
        portals += ["Glassdoor", "Wellfound", "ZipRecruiter", "SimplyHired", "Monster"]

    all_jobs = []
    seen = set()

    # ── Phase 1: Discovery ────────────────────────────────────
    log.info("\n═══ PHASE 1: JOB DISCOVERY (AI-POWERED) ═══")
    for role in CONFIG["roles"]:
        log.info(f"\n{'─'*55}\n🔍 Role: {role}\n{'─'*55}")
        for portal in portals:
            try:
                jobs = scraper.discover(portal, role)
                for j in jobs:
                    h = hashlib.md5(j.get("apply_link","").encode()).hexdigest()
                    if h not in seen and j.get("apply_link"):
                        seen.add(h); all_jobs.append(j)
                time.sleep(random.uniform(1.5, 3.0))
            except Exception as e:
                log.warning(f"  ❌ {portal}/{role}: {e}")

    log.info(f"\n📊 Phase 1: {len(all_jobs)} unique jobs")

    # ── Phase 2: Full Detail Extraction ──────────────────────
    log.info("\n═══ PHASE 2: DETAIL EXTRACTION + AI ANALYSIS ═══")
    enriched = []
    for i, job in enumerate(all_jobs):
        title   = job.get("job_title","?")[:40]
        portal  = job.get("portal","?")
        company = job.get("company_name","?")[:22]

        log.info(f"[{i+1:3}/{len(all_jobs)}] {portal:12} | {title} | {company}")
        try:
            job     = extractor.extract(job)
            record  = build_record(job)
            enriched.append(record)

            score  = record.get("validation_score","?")
            status = record.get("validation_status","?")
            dlen   = record.get("description_length","0")
            model  = record.get("ai_model_used","?")
            icon   = {"Valid":"✅","Partial":"⚠️","Junk":"❌"}.get(status,"?")
            log.info(f"         {icon} {score}% | {dlen}w | [{model}] | {record.get('tech_stack','')[:45]}")
            if record.get("hr_email"): log.info(f"         📧 {record['hr_email']}")
        except Exception as e:
            log.warning(f"         ❌ Error: {e}")
            enriched.append(build_record(job))

        time.sleep(random.uniform(CONFIG["between_jobs_min"], CONFIG["between_jobs_max"]))

        if (i+1) % 20 == 0:
            n = save_csv(enriched, append=True)
            log.info(f"  💾 Progress saved: {i+1}/{len(all_jobs)} ({n} new)")

    written = save_csv(enriched, append=True)
    log.info(f"\n💾 Final: {written} new → {CONFIG['output_csv']}")
    fetcher.close()
    print_summary(enriched, ai)
    return enriched


# ── Databricks entry point ────────────────────────────────────
# def run_on_databricks():
#     import subprocess
#     if HAS_PLAYWRIGHT:
#         try:
#             subprocess.run(["playwright","install","chromium","--with-deps"],
#                            capture_output=True, timeout=120)
#         except: pass
#     CONFIG["output_csv"] = "/dbfs/FileStore/jobs_ai_agent_output.csv"
#     return run_ai_agent()


# if __name__ == "__main__":
#     in_db = "DATABRICKS_RUNTIME_VERSION" in os.environ
#     jobs  = run_on_databricks() if in_db else run_ai_agent()
#     valid  = sum(1 for j in jobs if j.get("validation_status")=="Valid")
#     w_desc = sum(1 for j in jobs if int(j.get("description_length",0) or 0)>100)
#     print(f"\n✅ Done! {len(jobs)} total | {valid} valid | {w_desc} with descriptions")

if __name__ == "__main__":
    # 1. Run the scraper
    jobs = run_ai_agent()
    
    # 2. GitHub Actions లో రన్ అవుతోంది కాబట్టి, CSV ని రెపోలో సేవ్ చేస్తాము
    import subprocess
    import os
    
    csv_file = CONFIG["output_csv"] # ఇది "jobs_ai_agent_output.csv"
    
    if os.path.exists(csv_file):
        print("🚀 Pushing updated CSV to GitHub...")
        
        # Git config set cheyyadam
        subprocess.run(["git", "config", "--global", "user.email", "actions@github.com"])
        subprocess.run(["git", "config", "--global", "user.name", "github-actions"])
        
        # File ni add mariyu commit cheyyadam
        subprocess.run(["git", "add", csv_file])
        
        # Commit message lo date add cheste track cheyadaniki easy ga untundi
        from datetime import date
        today_str = date.today().strftime("%Y-%m-%d")
        subprocess.run(["git", "commit", "-m", f"Daily job data update: {today_str}"])
        
        # Push cheyyadam (GH_PAT secret nundi teesukuntundi)
        repo = os.getenv('GITHUB_REPOSITORY')
        token = os.getenv('GH_PAT')
        
        # గమనిక: మీ branch పేరు 'master' అయితే 'main' బదులు 'master' అని పెట్టండి
        push_url = f"https://x-access-token:{token}@github.com/{repo}.git"
        result = subprocess.run(["git", "push", push_url, "HEAD:main"], capture_output=True, text=True)
        
        if result.returncode == 0:
            print("✅ Successfully pushed to GitHub!")
        else:
            print(f"❌ Push failed: {result.stderr}")
    else:
        print("⚠️ CSV file not found. Nothing to push.")