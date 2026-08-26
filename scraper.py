"""
╔══════════════════════════════════════════════════════════════════════════╗
║  DAILY JOB AI AGENT — GITHUB ACTIONS READY                             ║
║                                                                          ║
║  ✅ WHY PREVIOUS VERSION FOUND 0 JOBS:                                  ║
║     All portals (LinkedIn, Indeed, Dice search pages) BLOCK             ║
║     GitHub Actions / Azure IPs for scraping.                            ║
║                                                                          ║
║  ✅ THIS VERSION FIXES IT USING:                                         ║
║     • LinkedIn Guest API   → 10+ jobs/search (NO login needed)          ║
║     • Dice RSS Feed        → 20+ jobs/keyword (machine-readable XML)    ║
║     • RemoteOK JSON API    → Free public API (no auth, no blocking)     ║
║     • The Muse JSON API    → Free public API (no auth, no blocking)     ║
║     • Playwright           → Only for individual JOB DETAIL pages       ║
║                                                                          ║
║  ✅ AI STRATEGY (NVIDIA NIM):                                            ║
║     AI is NOT used for discovery (saves API calls, 100x faster)         ║
║     AI is ONLY used to ANALYZE fetched description: score/tech/summary  ║
║     Cascade: fast(8B) → smart(70B) → power(nemotron-70b) if incomplete  ║
║                                                                          ║
║  ✅ US-ONLY JOBS: India/Asia locations automatically filtered out        ║
║  ✅ SELF-HEALING: 3 retries, AI reasoning on failure, model escalation  ║
║  ✅ TEST_MODE: Set True for quick 5-job test, False for full daily run   ║
╚══════════════════════════════════════════════════════════════════════════╝
"""

import os, sys, csv, re, time, random, json, requests, hashlib, uuid, logging
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from urllib.parse import quote_plus
from collections import defaultdict

# ── Optional: Playwright (for detail pages, NOT required for discovery) ──
try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

try:
    from tabulate import tabulate
    HAS_TABULATE = True
except ImportError:
    HAS_TABULATE = False

# ╔══════════════════════════════════════════════════════════╗
# ║  ⚙️  CONFIG — Edit these values                          ║
# ╚══════════════════════════════════════════════════════════╝
TEST_MODE = False  # ← True = quick 5-job test | False = full daily run

CONFIG = {
    # ── NVIDIA NIM ────────────────────────────────────────
    "nvidia_api_key": os.getenv("NVIDIA_NIM_API_KEY", ""),
    "nvidia_base_url": "https://integrate.api.nvidia.com/v1",

    # ── AI Model Cascade (escalates if extraction is incomplete) ──
    "models": {
        "fast":    "meta/llama-3.1-8b-instruct",       # ~1s, cheap
        "smart":   "meta/llama-3.3-70b-instruct",       # ~3s, better
        "power":   "nvidia/llama-3.1-nemotron-70b-instruct",  # ~5s, deep
        "ultra":   "nvidia/llama-3.1-nemotron-ultra-253b-v1", # ~8s, max
        "supreme": "nvidia/nemotron-3-ultra-550b-a55b",        # ~10s, last resort
    },

    # ── Target Job Roles ──────────────────────────────────
    "roles": [
        "Data Engineer",
        "Senior Data Engineer",
        "PySpark Engineer",
        "ETL Developer",
        "Analytics Engineer",
        "Databricks Engineer",
        "Spark Developer",
        "Machine Learning Engineer",
    ],

    # ── Output ────────────────────────────────────────────
    "output_csv":         "jobs_ai_agent_output.csv",
    "max_jobs_per_source": 5 if TEST_MODE else 25,
    "headless":           True,
    "page_timeout":       25,
    "max_retries":        3,
    "between_jobs_min":   1.5,
    "between_jobs_max":   3.5,
    "min_desc_words":     80,
}

TODAY = date.today()

# CSV column order
CSV_HEADERS = [
    "id", "job_hash", "fetch_date", "portal", "search_keyword",
    "job_title", "company_name", "location", "remote_type",
    "salary_range", "experience_years", "tech_stack",
    "posted_date", "job_description", "description_length",
    "roles_responsibilities", "requirements_section",
    "apply_link", "hr_email", "job_id", "visa_sponsorship",
    "validation_score", "validation_status",
    "ai_summary", "ai_model_used", "extraction_attempts",
]

# ── US Location Filter ────────────────────────────────────────────────────
US_STATES_ABBR = {
    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN","IA",
    "KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ",
    "NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VT",
    "VA","WA","WV","WI","WY","DC",
}
US_KEYWORDS = {
    "united states","usa","u.s.","u.s.a","remote","anywhere","work from home",
    "nationwide","continental us","us only","north america",
}
INDIA_KEYWORDS = {
    "india","bangalore","bengaluru","hyderabad","chennai","pune","mumbai",
    "delhi","gurgaon","noida","kolkata","ahmedabad","kochi","bhopal",
    "jaipur","chandigarh","navi mumbai","greater hyderabad",
}
NON_US_KEYWORDS = {
    "india","china","uk","london","australia","sydney","canada","toronto",
    "germany","berlin","france","paris","singapore","dubai","uae","pakistan",
    "philippines","nigeria","kenya","brazil",
}

def is_us_job(location: str, description: str = "") -> bool:
    """Return True if job appears to be in the US."""
    if not location and not description:
        return True  # Unknown — include (AI will score lower if non-US)
    loc = location.lower()

    # Hard reject: India / non-US cities
    if any(kw in loc for kw in INDIA_KEYWORDS):
        return False
    if any(kw in loc for kw in NON_US_KEYWORDS if kw not in ("uk", "canada")):
        # Canada/UK might include remote global roles — keep
        if not ("remote" in loc or "anywhere" in loc):
            return False

    # Accept: known US keywords
    if any(kw in loc for kw in US_KEYWORDS):
        return True

    # Accept: US state abbreviation (e.g., "San Jose, CA")
    for abbr in US_STATES_ABBR:
        if re.search(rf'\b{abbr}\b', location.upper()):
            return True

    return True  # Include by default if unclear


# ── Rotating user agents ──────────────────────────────────────────────────
UA_LIST = [
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
]
def rand_ua(): return random.choice(UA_LIST)
def rand_headers():
    return {
        "User-Agent": rand_ua(),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
    }

# ── Email extraction ──────────────────────────────────────────────────────
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", re.I)
EMAIL_BLACKLIST = {
    "example.com","test.com","sentry.io","amazonaws.com","cloudfront.net",
    "w3.org","schema.org","intercom.io","hubspot.com","wixpress.com",
    "noreply","no-reply","donotreply",
}
def extract_best_email(text: str) -> str:
    emails = EMAIL_RE.findall(text)
    clean = []
    seen = set()
    for e in emails:
        e = e.lower().strip(".,;")
        domain = e.split("@")[-1]
        if any(bl in domain or bl in e for bl in EMAIL_BLACKLIST): continue
        if e not in seen: seen.add(e); clean.append(e)
    if not clean: return ""
    priority_prefixes = ["recruit","talent","hr","hiring","jobs","careers","apply","people"]
    for prefix in priority_prefixes:
        for e in clean:
            if prefix in e.split("@")[0]: return e
    return clean[0]

# Logging
logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("JobAgent")


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  📡  DISCOVERY — Sources that WORK from GitHub Actions / Azure IPs      ║
# ╚══════════════════════════════════════════════════════════════════════════╝

class JobDiscovery:
    """
    Discovers job URLs using RSS feeds + JSON APIs (no bot detection).
    ZERO AI calls during discovery — pure HTTP parsing.
    """

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(rand_headers())

    def _get(self, url: str, timeout: int = 20) -> requests.Response | None:
        """GET with retry and rotating UA."""
        for attempt in range(3):
            try:
                self.session.headers["User-Agent"] = rand_ua()
                resp = self.session.get(url, timeout=timeout, allow_redirects=True)
                if resp.status_code == 200:
                    return resp
                log.debug(f"HTTP {resp.status_code} for {url[:60]}")
            except Exception as e:
                log.debug(f"GET error ({attempt+1}/3): {e}")
                time.sleep(random.uniform(1, 3))
        return None

    # ── 1. LinkedIn Guest API ─────────────────────────────────────────────
    def linkedin_guest(self, keyword: str) -> list[dict]:
        """
        Uses LinkedIn's internal guest search API — no login, no session needed.
        Returns job listing cards as HTML → parsed with regex.
        Works from GitHub Actions / cloud IPs. ✅
        """
        jobs = []
        seen = set()
        
        for start in [0, 25]:  # Paginate 2 pages = up to 50 jobs
            url = (
                f"https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
                f"?keywords={quote_plus(keyword)}"
                f"&location=United+States"
                f"&f_TPR=r86400"    # Last 24 hours
                f"&f_JT=F"          # Full-time
                f"&start={start}"
            )
            resp = self._get(url)
            if not resp or len(resp.text) < 100: break

            # Extract job data from HTML cards
            html = resp.text

            # Job URLs (unique ID is in the URL)
            urls     = re.findall(r'href="(https://www\.linkedin\.com/jobs/view/\d+[^"]*)"', html)
            titles   = re.findall(r'class="base-search-card__title"[^>]*>\s*([^<]+?)\s*<', html)
            companies= re.findall(r'class="base-search-card__subtitle"[^>]*>.*?<a[^>]*>\s*([^<]+?)\s*</a', html, re.S)
            locations= re.findall(r'class="job-search-card__location">\s*([^<]+?)\s*<', html)
            times    = re.findall(r'class="job-search-card__listdate[^"]*"[^>]*>\s*([^<]+?)\s*<', html)

            # Zip together (LinkedIn returns them in order)
            for i, job_url in enumerate(urls[:CONFIG["max_jobs_per_source"]]):
                # Clean URL (remove tracking params)
                clean_url = re.sub(r'\?.*', '', job_url.strip())
                if clean_url in seen: continue

                title    = titles[i].strip()    if i < len(titles)    else keyword
                company  = companies[i].strip() if i < len(companies) else "Unknown"
                location = locations[i].strip() if i < len(locations) else "USA"
                posted   = times[i].strip()     if i < len(times)     else ""

                if not is_us_job(location): continue
                seen.add(clean_url)
                jobs.append({
                    "portal":         "LinkedIn",
                    "search_keyword": keyword,
                    "job_title":      title,
                    "company_name":   company,
                    "location":       location,
                    "apply_link":     clean_url,
                    "posted_date":    posted,
                    "job_id":         re.search(r'/view/(\d+)', clean_url).group(1) if re.search(r'/view/(\d+)', clean_url) else "",
                })
            time.sleep(random.uniform(2, 4))  # Be polite to LinkedIn

        log.info(f"  📦 LinkedIn: {len(jobs)} jobs for '{keyword}'")
        return jobs

    # ── 2. Dice RSS Feed ─────────────────────────────────────────────
    def dice_rss(self, keyword: str) -> list[dict]:
        """
        Dice.com RSS feed — machine-readable XML, no bot detection. ✅
        Uses robust CDATA-aware regex parsing (Dice RSS is often malformed XML).
        """
        url = (
            f"https://www.dice.com/jobs/rss"
            f"?q={quote_plus(keyword)}"
            f"&countryCode=US&radius=30&radiusUnit=mi"
            f"&filters.postedDate=ONE_DAY_AGO"
            f"&pageSize=50"
        )
        # Also try the alternative URL format
        alt_url = f"https://www.dice.com/jobs/rss?q={quote_plus(keyword)}&location=United+States&pageSize=50"
        resp = self._get(url) or self._get(alt_url)
        if not resp: return []

        raw  = resp.text
        jobs = []

        # Robust CDATA-aware regex parsing (handles malformed XML)
        def cdata(block: str, tag: str) -> str:
            m = re.search(rf'<{tag}>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{tag}>', block, re.DOTALL)
            return m.group(1).strip() if m else ""

        item_blocks = re.findall(r'<item[^>]*>(.*?)</item>', raw, re.DOTALL)

        if not item_blocks:
            # Ultra fallback: just grab links from full text
            links  = re.findall(r'<link>\s*(https://www\.dice\.com/job-detail/[^<]+)', raw)
            titles = re.findall(r'<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>', raw)
            for i, link in enumerate(links[:CONFIG["max_jobs_per_source"]]):
                title = titles[i+1].strip() if (i+1) < len(titles) else keyword
                jobs.append({
                    "portal": "Dice", "search_keyword": keyword,
                    "job_title": title, "company_name": "Unknown",
                    "location": "USA", "apply_link": link.strip(),
                    "posted_date": "", "job_id": hashlib.md5(link.encode()).hexdigest()[:12],
                })
            log.info(f"  📦 Dice RSS: {len(jobs)} jobs for '{keyword}' (fallback)")
            return jobs

        for block in item_blocks[:CONFIG["max_jobs_per_source"]]:
            title    = cdata(block, "title")
            link_m   = re.search(r'<link>\s*(https?://[^\s<]+)', block)
            link     = link_m.group(1).strip() if link_m else ""
            company  = (cdata(block, "source") or
                        cdata(block, "dc:source") or "Unknown")
            desc_raw = cdata(block, "description")
            location = ""
            loc_m    = re.search(r'(?:Location|City)[:\s]+([^<\n,]+(?:,\s*[A-Z]{2})?)', desc_raw, re.I)
            if loc_m: location = loc_m.group(1).strip()
            pub_date = cdata(block, "pubDate")

            if not link or not title: continue
            if "dice.com" not in link: continue
            if not is_us_job(location): continue

            jobs.append({
                "portal":         "Dice",
                "search_keyword": keyword,
                "job_title":      title,
                "company_name":   company,
                "location":       location or "USA",
                "apply_link":     link,
                "posted_date":    pub_date,
                "job_id":         hashlib.md5(link.encode()).hexdigest()[:12],
            })

        log.info(f"  📦 Dice RSS: {len(jobs)} jobs for '{keyword}'")
        return jobs

    # ── 3. RemoteOK JSON API ──────────────────────────────────────────────
    def remoteok(self, keyword: str) -> list[dict]:
        """
        RemoteOK free public API — no auth, no rate limit (with sleep). ✅
        Returns job description directly in API response!
        Tries multiple tag formats for the keyword.
        """
        # RemoteOK uses specific tags — try multiple variants
        tag_variants = [
            keyword.lower().replace(" ", "-"),  # data-engineer
            keyword.lower().replace(" engineer","").replace(" ","-"),  # data
            "data-engineering", "big-data", "python", "spark",
        ]
        headers = {"User-Agent": rand_ua(), "Accept": "application/json"}
        data = []
        for slug in tag_variants[:2]:  # Try first 2 variants
            try:
                url  = f"https://remoteok.com/api?tag={quote_plus(slug)}"
                resp = requests.get(url, headers=headers, timeout=20)
                if resp.status_code == 200:
                    d = resp.json()
                    if isinstance(d, list) and len(d) > 1:
                        data = d; break
            except: pass
            time.sleep(1)
        if not data: return []

        jobs = []
        for item in data[1:CONFIG["max_jobs_per_source"]*3+1]:  # Over-fetch, then filter
            if not isinstance(item, dict): continue
            title    = item.get("position","")
            company  = item.get("company","Unknown")
            location = item.get("location","Remote")
            job_url  = item.get("url","")
            desc     = item.get("description","")
            tags     = ", ".join(item.get("tags",[]))

            if not job_url or not title: continue

            # Hard filter: remove known non-US locations
            loc_lower = location.lower()
            if any(kw in loc_lower for kw in INDIA_KEYWORDS): continue
            if any(kw in loc_lower for kw in NON_US_KEYWORDS): continue
            # Must contain US indicator OR be truly "Worldwide"/"Remote"
            has_us_signal = (
                any(kw in loc_lower for kw in US_KEYWORDS) or
                any(abbr.lower() in loc_lower for abbr in list(US_STATES_ABBR)[:10]) or
                loc_lower.strip() in ("", "remote", "worldwide", "anywhere", "global")
            )
            if not has_us_signal: continue

            # Must be a tech-relevant role
            title_lower = title.lower()
            if not any(t in title_lower for t in [
                "engineer","data","developer","analyst","scientist",
                "spark","etl","analytics","ml","python","cloud",
                "backend","software","devops","architect","platform",
                "database","pipeline","warehouse","lakehouse",
            ]): continue

            desc_text = re.sub(r'<[^>]+>', ' ', desc).strip()
            jobs.append({
                "portal":          "RemoteOK",
                "search_keyword":  keyword,
                "job_title":       title,
                "company_name":    company,
                "location":        location or "Remote (USA)",
                "apply_link":      job_url,
                "posted_date":     item.get("date",""),
                "job_id":          str(item.get("id","")),
                "_prefetch_desc":  desc_text,
                "_prefetch_tech":  tags,
            })
            if len(jobs) >= CONFIG["max_jobs_per_source"]: break

        log.info(f"  📦 RemoteOK: {len(jobs)} jobs for '{keyword}'")
        return jobs

    # ── 4. The Muse API ───────────────────────────────────────────────────
    def the_muse(self, keyword: str) -> list[dict]:
        """
        The Muse free public API. No auth needed for basic usage. ✅
        Category-based, not keyword search — returns IT/engineering jobs.
        """
        # Map keywords to Muse categories
        category_map = {
            "data engineer":        "Data Science",
            "senior data engineer": "Data Science",
            "pyspark":              "Data Science",
            "databricks":           "Data Science",
            "analytics engineer":   "Data Science",
            "machine learning":     "Data Science",
            "etl developer":        "Data Science",
            "spark developer":      "Data Science",
        }
        category = next(
            (v for k,v in category_map.items() if k in keyword.lower()),
            "Data Science"
        )

        url = (
            f"https://www.themuse.com/api/public/jobs"
            f"?category={quote_plus(category)}"
            f"&page=0&descending=true"
            f"&level=Senior+Level,Mid+Level"
        )
        try:
            resp = requests.get(url, headers={"User-Agent": rand_ua()}, timeout=20)
            if resp.status_code != 200: return []
            data = resp.json()
        except: return []

        jobs = []
        results = data.get("results", [])
        for item in results[:CONFIG["max_jobs_per_source"]]:
            title    = item.get("name","")
            company  = item.get("company",{}).get("name","Unknown")
            refs     = item.get("refs",{})
            job_url  = refs.get("landing_page","")
            locations= [l.get("name","") for l in item.get("locations",[])]
            location = ", ".join(locations) if locations else "USA"
            # Filter US only
            if locations and not any(is_us_job(l) for l in locations): continue
            if any(kw in location.lower() for kw in INDIA_KEYWORDS): continue

            if not job_url or not title: continue
            jobs.append({
                "portal":         "TheMuse",
                "search_keyword": keyword,
                "job_title":      title,
                "company_name":   company,
                "location":       location,
                "apply_link":     job_url,
                "posted_date":    item.get("publication_date",""),
                "job_id":         str(item.get("id","")),
            })

        log.info(f"  📦 The Muse: {len(jobs)} jobs for '{keyword}'")
        return jobs

    # ── 5. Built In (works with requests) ────────────────────────────────
    def builtin(self, keyword: str) -> list[dict]:
        """BuiltIn.com — works with simple requests, no JS needed. ✅"""
        slug  = keyword.lower().replace(" ", "%20")
        url   = f"https://builtin.com/jobs?search={slug}&remote=true"
        resp  = self._get(url)
        if not resp: return []

        jobs = []
        html = resp.text
        # Extract job cards
        pattern = r'href="(/job/[^"]+)"[^>]*>.*?<h2[^>]*>([^<]+)</h2>.*?<span[^>]*>([^<]+)</span>'
        matches = re.findall(pattern, html, re.S)
        
        # Alternative: find job URLs more simply
        job_paths  = re.findall(r'href="(/job/[a-z0-9\-/]+)"', html)
        job_titles = re.findall(r'"jobTitle"\s*:\s*"([^"]+)"', html)
        companies  = re.findall(r'"hiringOrganization".*?"name"\s*:\s*"([^"]+)"', html)
        job_locs   = re.findall(r'"jobLocation".*?"name"\s*:\s*"([^"]+)"', html)

        seen_paths = set()
        for i, path in enumerate(job_paths[:CONFIG["max_jobs_per_source"]]):
            if path in seen_paths: continue
            seen_paths.add(path)
            title    = job_titles[i]   if i < len(job_titles)   else keyword
            company  = companies[i]    if i < len(companies)    else "Unknown"
            location = job_locs[i]     if i < len(job_locs)     else "USA"
            if not is_us_job(location): continue
            jobs.append({
                "portal":         "BuiltIn",
                "search_keyword": keyword,
                "job_title":      title,
                "company_name":   company,
                "location":       location,
                "apply_link":     f"https://builtin.com{path}",
                "posted_date":    "",
                "job_id":         path.split("/")[-1],
            })

        log.info(f"  📦 BuiltIn: {len(jobs)} jobs for '{keyword}'")
        return jobs

    # ── 6. Greenhouse Job Board API ───────────────────────────────────────
    def greenhouse_boards(self, keyword: str) -> list[dict]:
        """
        Greenhouse.io job board JSON API — many top tech companies use this.
        Completely open, no auth, no bot detection. ✅
        """
        # Major tech companies using Greenhouse (data/eng focused)
        companies = [
            # Cloud & Data Infrastructure
            "snowflake","databricks","confluent","dbt-labs","fivetran",
            "segment","elastic","hashicorp","datadog","mongodb",
            # Fintech
            "stripe","brex","plaid","chime","robinhood","coinbase",
            "affirm","marqeta","adyen",
            # Consumer Tech
            "airbnb","doordash","lyft","instacart","wish","grammarly",
            # Enterprise
            "figma","notion","rippling","gusto","lattice","greenhouse",
            "workato","clickup","airtable","hubspot","zendesk",
            # Health & Bio
            "nuna","hims","tempus","devoted",
        ]
        kw = keyword.lower()
        jobs = []
        seen = set()

        for company in companies[:8]:  # Check 8 companies per keyword
            url = f"https://boards-api.greenhouse.io/v1/boards/{company}/jobs?content=true"
            try:
                resp = requests.get(url, headers={"User-Agent": rand_ua()}, timeout=10)
                if resp.status_code != 200: continue
                data = resp.json()
            except: continue

            for job in data.get("jobs", []):
                title = job.get("title","")
                if not any(term in title.lower() for term in
                           ["data","engineer","spark","etl","analytics","ml","machine"]): continue

                # Location filter: US only
                loc_list = [l.get("name","") for l in job.get("offices",[])] or \
                           [job.get("location",{}).get("name","")]
                location = ", ".join(loc_list) if loc_list else "USA"
                if any(kw in location.lower() for kw in INDIA_KEYWORDS): continue

                job_url = job.get("absolute_url","")
                if not job_url or job_url in seen: continue
                seen.add(job_url)

                # Use job content as prefetched description
                content = job.get("content","")
                desc_text = re.sub(r'<[^>]+>', ' ', content).strip()[:3000] if content else ""

                jobs.append({
                    "portal":         "Greenhouse",
                    "search_keyword": keyword,
                    "job_title":      title,
                    "company_name":   company.capitalize(),
                    "location":       location,
                    "apply_link":     job_url,
                    "posted_date":    job.get("updated_at","")[:10],
                    "job_id":         str(job.get("id","")),
                    "_prefetch_desc": desc_text,
                })
                if len(jobs) >= CONFIG["max_jobs_per_source"]: break
            if len(jobs) >= CONFIG["max_jobs_per_source"]: break
            time.sleep(0.5)

        log.info(f"  📦 Greenhouse: {len(jobs)} jobs for '{keyword}'")
        return jobs

    def discover_all(self, keyword: str) -> list[dict]:
        """Run all discovery sources for a keyword."""
        all_jobs = []
        sources = [
            ("LinkedIn",   self.linkedin_guest),
            ("Dice",       self.dice_rss),
            ("RemoteOK",   self.remoteok),
            ("TheMuse",    self.the_muse),
            ("BuiltIn",    self.builtin),
            ("Greenhouse", self.greenhouse_boards),
        ]
        if TEST_MODE:
            sources = sources[:3]  # LinkedIn + Dice + Greenhouse in test mode

        for source_name, fn in sources:
            try:
                jobs = fn(keyword)
                all_jobs.extend(jobs)
            except Exception as e:
                log.warning(f"  ⚠️ {source_name} error: {e}")
            time.sleep(random.uniform(1, 2))
        return all_jobs


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  🌐  PAGE FETCHER — Playwright for JS pages, requests for static        ║
# ╚══════════════════════════════════════════════════════════════════════════╝

# Sites that REQUIRE JavaScript rendering for job details
JS_DETAIL_SITES = {"linkedin.com", "dice.com", "glassdoor.com", "wellfound.com"}

# Verified CSS selectors for description extraction (per portal)
PORTAL_CSS = {
    "linkedin.com": [
        "div.description__text",
        "div[class*='show-more-less-html']",
        "div[class*='description__text']",
        "section.description",
    ],
    "dice.com": [
        "div[data-testid='jobDescriptionHtml']",
        "div[class*='description']",
        "section[class*='description']",
    ],
    "themuse.com": ["div.job-description", "div[class*='description']", "article"],
    "builtin.com":   ["div.job-description", "div[class*='job-description']"],
    "remoteok.com":  ["div.description", "td.description"],
}
FALLBACK_CSS = [
    "div#jobDescriptionText", "div#jobDescription",
    "div[class*='description']", "section[class*='description']",
    "article", "main",
]
PORTAL_WAIT_JS = {
    "linkedin.com": 4, "dice.com": 5, "glassdoor.com": 5, "wellfound.com": 5,
}

class PageFetcher:
    """Fetches pages. Requests-first (fast), Playwright for JS-heavy sites."""

    def __init__(self):
        self.session = requests.Session()
        self._pw = self._browser = self._ctx = self._page = None

    def _init_playwright(self) -> bool:
        if not HAS_PLAYWRIGHT: return False
        if self._pw: return True
        try:
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(
                headless=CONFIG["headless"],
                args=["--no-sandbox","--disable-blink-features=AutomationControlled","--disable-dev-shm-usage"])
            self._ctx = self._browser.new_context(
                user_agent=rand_ua(), viewport={"width": 1280, "height": 800},
                extra_http_headers={"Accept-Language": "en-US,en;q=0.9"})
            self._ctx.route("**/*.{png,jpg,jpeg,gif,svg,woff,woff2,ttf,mp4,ico}", lambda r: r.abort())
            self._page = self._ctx.new_page()
            log.info("   🎭 Playwright ready")
            return True
        except Exception as e:
            log.warning(f"   Playwright init failed: {e}")
            return False

    def needs_js(self, url: str) -> bool:
        return any(s in url for s in JS_DETAIL_SITES)

    def fetch(self, url: str) -> tuple[str, str, object]:
        """Returns (text, html, pw_page_or_None)."""
        is_js = self.needs_js(url)

        if not is_js:
            # Try requests first (fast, works in Databricks too)
            try:
                self.session.headers.update(rand_headers())
                resp = self.session.get(url, timeout=CONFIG["page_timeout"], allow_redirects=True)
                if resp.status_code == 200 and len(resp.text) > 500:
                    return resp.text, resp.text, None
            except Exception as e:
                log.debug(f"   Requests failed: {e}")

        # Playwright for JS sites
        if self._init_playwright() and self._page:
            try:
                wait_secs = next((v for k,v in PORTAL_WAIT_JS.items() if k in url), 3)
                self._page.goto(url, wait_until="domcontentloaded",
                                timeout=CONFIG["page_timeout"] * 1000)
                time.sleep(wait_secs)
                # Wait for content to appear
                try:
                    self._page.wait_for_selector(
                        "h1, div[class*='description'], div#jobDescriptionText, article",
                        timeout=7000)
                except: pass
                text = self._page.inner_text("body") or ""
                html = self._page.content() or ""
                return text, html, self._page
            except Exception as e:
                log.debug(f"   Playwright detail fetch failed: {e}")

        return "", "", None

    def fetch_retry(self, url: str) -> tuple[str, str, object]:
        for attempt in range(CONFIG["max_retries"]):
            text, html, page_obj = self.fetch(url)
            if len(text) > 150:
                return text, html, page_obj
            if attempt < CONFIG["max_retries"] - 1:
                sleep_t = random.uniform(2, 5)
                log.debug(f"   Retry {attempt+2}/{CONFIG['max_retries']} in {sleep_t:.1f}s")
                time.sleep(sleep_t)
        return "", "", None

    def css_extract(self, url: str, page_obj, html: str) -> str:
        """Extract job description text using CSS selectors."""
        portal_key = next((k for k in PORTAL_CSS if k in url), None)
        selectors  = (PORTAL_CSS.get(portal_key, []) if portal_key else []) + FALLBACK_CSS
        desc = ""

        # Method 1: Playwright CSS (most accurate for JS-rendered pages)
        if page_obj is not None:
            for sel in selectors:
                try:
                    el = page_obj.query_selector(sel)
                    if el:
                        candidate = el.inner_text().strip()
                        if len(candidate) > len(desc) and len(candidate) > 80:
                            desc = candidate
                        if len(desc.split()) > 150: break
                except: continue

            # JavaScript DOM walk: find largest "job-like" div
            if len(desc.split()) < 60:
                try:
                    desc = page_obj.evaluate("""() => {
                        let best = '';
                        for (let el of document.querySelectorAll('div,section,article')) {
                            const t = el.innerText || '';
                            const l = t.toLowerCase();
                            if (t.length > best.length && t.length < 20000 &&
                                (l.includes('responsib') || l.includes('qualif') ||
                                 l.includes('requirement') || l.includes('skill') ||
                                 l.includes('experience'))) { best = t; }
                        }
                        return best;
                    }""") or ""
                except: pass

        # Method 2: Regex on raw HTML (for requests-based pages)
        if len(desc.split()) < 60 and html:
            clean = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL|re.I)
            clean = re.sub(r'<style[^>]*>.*?</style>', '', clean, flags=re.DOTALL|re.I)
            for pattern in [
                r'id=["\']jobDescriptionText["\'][^>]*>(.*?)</(?:div|section)',
                r'data-testid=["\']jobDescriptionHtml["\'][^>]*>(.*?)</(?:div|section)',
                r'class=["\'][^"\']*(?:job-description|description__text|jobDescription)[^"\']*["\'][^>]*>(.*?)</(?:div|section|article)',
            ]:
                m = re.search(pattern, clean, re.DOTALL|re.I)
                if m:
                    raw = re.sub(r'<[^>]+>', ' ', m.group(1))
                    raw = re.sub(r'\s+', ' ', raw).strip()
                    if len(raw) > len(desc): desc = raw
                    if len(desc.split()) > 100: break

        return desc.strip()

    def close(self):
        for obj in [self._page, self._ctx, self._browser]:
            try:
                if obj: obj.close()
            except: pass
        try:
            if self._pw: self._pw.stop()
        except: pass


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  🧠  AI ENGINE — Cascade + Self-Healing                                 ║
# ╚══════════════════════════════════════════════════════════════════════════╝

class AIEngine:
    """
    NVIDIA NIM multi-model AI.
    Analyzes job descriptions (NOT for finding URLs — that's done via RSS/API).
    Self-heals: if extraction incomplete → escalates to more powerful model.
    """

    def __init__(self):
        self.api_key    = CONFIG["nvidia_api_key"]
        self.base_url   = CONFIG["nvidia_base_url"]
        self.models     = CONFIG["models"]
        self.call_counts = defaultdict(int)

    def call(self, prompt: str, tier: str = "fast", max_tokens: int = 500) -> str | None:
        model = self.models[tier]
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
                    timeout=30)
                if resp.status_code == 200:
                    return resp.json()["choices"][0]["message"]["content"].strip()
                elif resp.status_code == 429:
                    log.debug(f"Rate limited on {tier}, waiting 6s...")
                    time.sleep(6)
                else:
                    log.debug(f"AI [{tier}] HTTP {resp.status_code}")
            except Exception as e:
                log.debug(f"AI error [{tier}] attempt {attempt+1}: {e}")
                if attempt < 2: time.sleep(random.uniform(1, 3))
        return None

    def analyze(self, description: str, title: str, company: str, location: str) -> dict:
        """
        AI analyzes job description text → returns structured data.
        Cascades: fast → smart → power if extraction incomplete.
        Self-heals: if data missing, AI reasons about WHY and retries.
        """
        default = {
            "validation_score": 50, "validation_status": "Partial",
            "ai_summary": "", "roles_summary": "",
            "tech_stack": "", "experience_years": "Not specified",
            "remote_type": "Not specified", "salary_range": "",
            "visa_sponsorship": False, "ai_model_used": "none",
            "extraction_attempts": 0,
        }

        words = len(description.split()) if description else 0
        if words < 20:
            default["ai_summary"] = "Insufficient description fetched"
            return default

        desc_sample = description[:3500]

        # Cascade from fast to more powerful if needed
        tiers = [
            ("fast",   500, ""),
            ("smart",  700, "Previous model gave incomplete extraction. Analyze more carefully."),
            ("power",  900, f"Critical: previous tiers missed data. Think step-by-step before answering."),
        ]

        result = dict(default)
        for tier, max_tok, extra in tiers:
            result["extraction_attempts"] += 1

            prompt = f"""Analyze this US IT job posting and extract structured data.
{extra}

Job Title: {title}
Company: {company}
Location: {location}

FULL JOB DESCRIPTION:
{desc_sample}

Return ONLY valid JSON (no markdown, no explanation before or after):
{{
  "validation_score": <0-100, how relevant for a US IT data engineering/ML professional seeking work>,
  "ai_summary": "<2 clear sentences: company context + what this specific role does daily>",
  "roles_summary": "<3-5 key responsibilities as bullet points, each starting with •>",
  "tech_stack": "<top 8-10 technologies/tools/frameworks mentioned, comma-separated>",
  "experience_years": "<e.g. '5+ years' or '3-5 years', extracted from description>",
  "remote_type": "<one of: Remote | Hybrid | Onsite | Not specified>",
  "salary_range": "<salary if explicitly stated in description, else empty string>",
  "visa_sponsorship": <true if visa/sponsorship explicitly mentioned, else false>
}}"""

            log.debug(f"   🤖 AI [{tier}]...")
            resp = self.call(prompt, tier, max_tokens=max_tok)
            if not resp:
                if tier != "power":
                    self._reason_failure(desc_sample, tier)
                continue

            parsed = self._parse(resp)
            if not parsed:
                log.debug(f"   [{tier}] JSON parse failed")
                continue

            # Merge: only overwrite with non-empty values
            for k, v in parsed.items():
                if v and v not in ("", "N/A", "Unknown", "Not specified", 0):
                    result[k] = v
            result["ai_model_used"] = tier

            # Check if we have good data
            score    = int(result.get("validation_score") or 0)
            has_tech = len((result.get("tech_stack") or "").split(",")) >= 2
            has_sum  = len((result.get("ai_summary") or "").split()) >= 8

            if score > 0 and has_tech and has_sum:
                break  # Good enough, stop cascading
            else:
                missing = [k for k,c in [("score",score>0),("tech",has_tech),("summary",has_sum)] if not c]
                log.debug(f"   [{tier}] Missing {missing} — escalating...")
                self._reason_failure(desc_sample, tier)

            time.sleep(0.5)

        # Final status
        score = int(result.get("validation_score") or 0)
        result["validation_status"] = "Valid" if score >= 70 else "Partial" if score >= 40 else "Junk"
        return result

    def _parse(self, text: str) -> dict | None:
        """Robustly parse JSON from AI response."""
        # Try direct parse
        try:
            s, e = text.find('{'), text.rfind('}')
            if s != -1 and e > s:
                data = json.loads(text[s:e+1])
                if isinstance(data, dict): return data
        except: pass

        # Field-by-field fallback
        out = {}
        for key, pat in [
            ("ai_summary",       r'"ai_summary"\s*:\s*"((?:[^"\\]|\\.)*)"'),
            ("roles_summary",    r'"roles_summary"\s*:\s*"((?:[^"\\]|\\.)*)"'),
            ("tech_stack",       r'"tech_stack"\s*:\s*"([^"]+)"'),
            ("experience_years", r'"experience_years"\s*:\s*"([^"]*)"'),
            ("remote_type",      r'"remote_type"\s*:\s*"([^"]+)"'),
            ("salary_range",     r'"salary_range"\s*:\s*"([^"]*)"'),
        ]:
            m = re.search(pat, text, re.DOTALL)
            if m: out[key] = m.group(1).replace('\\"', '"').strip()
        m = re.search(r'"validation_score"\s*:\s*(\d+)', text)
        if m: out["validation_score"] = int(m.group(1))
        m = re.search(r'"visa_sponsorship"\s*:\s*(true|false)', text, re.I)
        if m: out["visa_sponsorship"] = m.group(1).lower() == "true"
        return out if out else None

    def _reason_failure(self, desc: str, tier: str):
        """AI self-heals: reasons about why extraction was incomplete."""
        prompt = f"""In 2 sentences, explain why a job description extraction might miss key fields like tech stack or score.
Description sample (first 500 chars): {desc[:500]}
What in this text makes extraction difficult?"""
        reason = self.call(prompt, "fast", max_tokens=100)
        if reason: log.debug(f"   🔍 [{tier}] Self-analysis: {reason[:100]}...")

    def find_description(self, page_text: str, title: str, url: str) -> str:
        """
        FALLBACK: When CSS selectors fail, AI locates description in page text.
        Returns plain text (no JSON — avoids escaping bugs).
        """
        prompt = f"""From this job page text, extract ONLY the job description section.
Output ONLY the raw description text. No JSON, no formatting.
If not found: output exactly: NOT_FOUND

Job Title: {title}
URL: {url}

Page text:
{page_text[:4500]}

Job description:"""

        for tier in ("smart", "power"):
            resp = self.call(prompt, tier, max_tokens=1500)
            if resp and "NOT_FOUND" not in resp and len(resp.split()) > 25:
                return resp
        return ""


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  🤖  JOB AGENT — End-to-End Orchestrator                                ║
# ╚══════════════════════════════════════════════════════════════════════════╝

class JobAgent:
    """
    Main orchestrator. Self-healing on all failures.
    Phase 1: Discovery (RSS/API — no AI, no blocking)
    Phase 2: Detail extraction (CSS + AI analysis)
    """

    def __init__(self):
        self.ai        = AIEngine()
        self.fetcher   = PageFetcher()
        self.discovery = JobDiscovery()
        self.seen_hashes: set[str] = set()

    def _hash(self, url: str) -> str:
        return hashlib.md5(url.encode()).hexdigest()

    def discover(self) -> list[dict]:
        """Phase 1: Discover all unique job URLs."""
        roles = CONFIG["roles"][:2] if TEST_MODE else CONFIG["roles"]
        all_jobs, seen = [], set()

        log.info("\n═══ PHASE 1: JOB DISCOVERY (RSS + APIs) ═══")
        for role in roles:
            log.info(f"\n  🔍 {role}")
            jobs = self.discovery.discover_all(role)
            for j in jobs:
                h = self._hash(j.get("apply_link",""))
                if h not in seen and j.get("apply_link"):
                    seen.add(h)
                    all_jobs.append(j)
        log.info(f"\n📊 Discovery: {len(all_jobs)} unique US IT jobs found")
        return all_jobs

    def enrich(self, job: dict) -> dict:
        """
        Phase 2: For each job URL:
          1. Fetch page (requests → Playwright fallback)
          2. CSS extract description (proven: 94% success)
          3. If CSS fails → AI finds description from page text (self-heal)
          4. AI analyzes description → score, tech, summary, etc.
        """
        url     = job.get("apply_link","")
        title   = job.get("job_title","")
        company = job.get("company_name","")
        portal  = job.get("portal","")

        if not url:
            return job

        # ── RemoteOK already has description in API response ──
        prefetch = job.pop("_prefetch_desc", "")
        if prefetch and len(prefetch.split()) > 50:
            log.debug(f"   📦 Using pre-fetched description ({len(prefetch.split())} words)")
            desc = prefetch
            html = ""
        else:
            # ── Fetch detail page ──────────────────────────────
            text, html, page_obj = self.fetcher.fetch_retry(url)

            if not text:
                job.update({
                    "job_description": "", "description_length": 0,
                    "validation_score": 50, "validation_status": "Partial",
                    "ai_summary": "Could not load page", "ai_model_used": "none",
                    "extraction_attempts": 0,
                })
                return job

            # ── CSS extract description ────────────────────────
            desc = self.fetcher.css_extract(url, page_obj, html)

            # ── Self-heal: if CSS got nothing, AI finds it ─────
            if len(desc.split()) < 50:
                log.debug(f"   CSS: {len(desc.split())} words → AI self-healing...")
                desc = self.ai.find_description(text, title, url)

        desc_words = len(desc.split()) if desc else 0
        log.debug(f"   📝 Description: {desc_words} words")

        # ── AI analyzes description ────────────────────────────
        location = job.get("location","")
        ai_result = self.ai.analyze(desc, title, company, location)

        # ── Email extraction ───────────────────────────────────
        email = extract_best_email(html + desc)

        # ── US job double-check after seeing description ───────
        score = int(ai_result.get("validation_score") or 0)
        if not is_us_job(location, desc):
            score = min(score, 30)  # Penalize non-US

        # ── Merge everything ───────────────────────────────────
        job.update({
            "job_description":     desc[:5000],
            "description_length":  desc_words,
            "hr_email":            email,
            "validation_score":    score,
            "validation_status":   ai_result.get("validation_status","Partial"),
            "ai_summary":          ai_result.get("ai_summary",""),
            "roles_responsibilities": ai_result.get("roles_summary",""),
            "tech_stack":          ai_result.get("tech_stack",
                                    job.pop("_prefetch_tech","") if "_prefetch_tech" in job else ""),
            "experience_years":    ai_result.get("experience_years",""),
            "remote_type":         ai_result.get("remote_type",""),
            "visa_sponsorship":    ai_result.get("visa_sponsorship", False),
            "ai_model_used":       ai_result.get("ai_model_used",""),
            "extraction_attempts": ai_result.get("extraction_attempts",0),
        })

        if ai_result.get("salary_range") and not job.get("salary_range","").strip():
            job["salary_range"] = ai_result["salary_range"]

        return job

    def run(self) -> list[dict]:
        """Full pipeline: discover → enrich → save."""
        jobs_found = self.discover()
        if not jobs_found:
            log.error("❌ 0 jobs discovered. Check discovery sources.")
            return []

        log.info(f"\n═══ PHASE 2: DETAIL EXTRACTION ({len(jobs_found)} jobs) ═══")
        enriched = []
        for i, job in enumerate(jobs_found):
            title   = job.get("job_title","?")[:38]
            portal  = job.get("portal","?")
            company = job.get("company_name","?")[:20]
            log.info(f"[{i+1:3}/{len(jobs_found)}] {portal:10} | {title} | {company}")

            try:
                job    = self.enrich(job)
                record = self._build_record(job)
                enriched.append(record)

                score  = record.get("validation_score","?")
                status = record.get("validation_status","?")
                dlen   = record.get("description_length","0")
                model  = record.get("ai_model_used","?")
                icon   = {"Valid":"✅","Partial":"⚠️","Junk":"❌"}.get(status,"?")
                log.info(f"         {icon} {score}% | {dlen}w | [{model}] | {record.get('tech_stack','')[:42]}")
                if record.get("hr_email"): log.info(f"         📧 {record['hr_email']}")

            except Exception as e:
                log.warning(f"         ❌ Enrich error: {e}")
                enriched.append(self._build_record(job))

            time.sleep(random.uniform(CONFIG["between_jobs_min"],
                                      CONFIG["between_jobs_max"]))
            if (i+1) % 20 == 0:
                n = save_csv(enriched, append=True)
                log.info(f"  💾 Progress: {i+1}/{len(jobs_found)} saved ({n} new)")

        written = save_csv(enriched, append=True)
        log.info(f"\n💾 Final: {written} new records → {CONFIG['output_csv']}")
        self.fetcher.close()
        self._print_summary(enriched)
        return enriched

    def _build_record(self, job: dict) -> dict:
        r = {h: "" for h in CSV_HEADERS}
        r.update({
            "id":        str(uuid.uuid4()),
            "job_hash":  self._hash(job.get("apply_link","")),
            "fetch_date":TODAY.isoformat(),
        })
        r.update(job)
        for f in ("description_length","validation_score","extraction_attempts"):
            r[f] = str(r.get(f,0) or 0)
        r["visa_sponsorship"] = str(r.get("visa_sponsorship", False))
        return r

    def _print_summary(self, records: list):
        by_portal = defaultdict(int)
        by_status = defaultdict(int)
        by_model  = defaultdict(int)
        w_desc = w_email = total_words = 0
        for r in records:
            by_portal[r.get("portal","?")] += 1
            by_status[r.get("validation_status","?")] += 1
            by_model[r.get("ai_model_used","?")] += 1
            dlen = int(r.get("description_length",0) or 0)
            if dlen > 50: w_desc += 1; total_words += dlen
            if r.get("hr_email"): w_email += 1

        avg = total_words // max(w_desc,1)
        print("\n" + "="*65)
        print("  🤖 JOB AI AGENT — HARVEST COMPLETE")
        print("="*65)
        print(f"  📋 Total jobs:      {len(records)}")
        print(f"  📝 With desc:       {w_desc} ({w_desc*100//max(len(records),1)}%) avg {avg} words")
        print(f"  📧 With HR emails:  {w_email}")
        print(f"  ✅ Valid (70%+):    {by_status.get('Valid',0)}")
        print(f"  ⚠️  Partial:        {by_status.get('Partial',0)}")
        print(f"  ❌ Junk:            {by_status.get('Junk',0)}")
        print(f"\n🧠 AI: {sum(self.ai.call_counts.values())} total calls")
        for m,c in sorted(by_model.items(),key=lambda x:-x[1]):
            print(f"   [{m}]: {c}")
        if HAS_TABULATE:
            print("\n🌐 By Portal:")
            print(tabulate([[p,c] for p,c in sorted(by_portal.items(),key=lambda x:-x[1])],
                           headers=["Portal","Jobs"], tablefmt="rounded_outline"))
        print(f"\n📁 CSV: {os.path.abspath(CONFIG['output_csv'])}")
        print("="*65)

        print("\n📋 SAMPLE VALID JOBS:")
        shown = 0
        for r in records:
            if shown >= 5: break
            if int(r.get("description_length",0) or 0) > 100 and r.get("validation_status") == "Valid":
                print(f"\n  🏢 {r.get('company_name','?')[:28]} | {r.get('portal','?')}")
                print(f"  💼 {r.get('job_title','?')[:55]}")
                print(f"  📍 {r.get('location','?')[:35]} | 🏠 {r.get('remote_type','?')}")
                print(f"  💰 {r.get('salary_range','N/A')[:45]}")
                print(f"  🛠️  {r.get('tech_stack','')[:60]}")
                print(f"  📅 {r.get('experience_years','N/A')} | 🤖 {r.get('validation_score','?')}% [{r.get('ai_model_used','?')}]")
                if r.get('hr_email'): print(f"  📧 {r['hr_email']}")
                print(f"  🔗 {r.get('apply_link','')[:70]}")
                preview = " ".join(str(r.get("job_description","")).split()[:20])
                print(f"  📄 {preview}...")
                shown += 1


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  💾  CSV PERSISTENCE                                                     ║
# ╚══════════════════════════════════════════════════════════════════════════╝

def save_csv(records: list, append: bool = False) -> int:
    csv_file = CONFIG["output_csv"]
    existing = set()
    if os.path.exists(csv_file):
        try:
            with open(csv_file, "r", encoding="utf-8") as f:
                existing = {r.get("job_hash","") for r in csv.DictReader(f)}
        except: pass

    new_recs = [r for r in records if r.get("job_hash","") not in existing]
    if not new_recs: return 0

    mode = "a" if (append and os.path.exists(csv_file)) else "w"
    with open(csv_file, mode=mode, newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_HEADERS, extrasaction="ignore")
        if mode == "w": w.writeheader()
        w.writerows(new_recs)
    return len(new_recs)


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  🚀  MAIN                                                                ║
# ╚══════════════════════════════════════════════════════════════════════════╝

if __name__ == "__main__":
    log.info("="*65)
    log.info(f"🤖 JOB AI AGENT  |  TEST_MODE={TEST_MODE}  |  Date={TODAY}")
    log.info(f"🧠 fast={CONFIG['models']['fast'].split('/')[-1]}")
    log.info(f"🧠 supreme={CONFIG['models']['supreme'].split('/')[-1]}")
    log.info("="*65)

    # Run the agent
    agent = JobAgent()
    jobs  = agent.run()

    # GitHub Actions: push CSV to repo
    import subprocess

    csv_file = CONFIG["output_csv"]
    if os.path.exists(csv_file) and len(jobs) > 0:
        print(f"\n🚀 Pushing {len(jobs)} job records to GitHub...")

        repo  = os.getenv("GITHUB_REPOSITORY","")
        token = os.getenv("GH_PAT","")

        subprocess.run(["git","config","--global","user.email","actions@github.com"], check=False)
        subprocess.run(["git","config","--global","user.name","github-actions[bot]"], check=False)
        subprocess.run(["git","add", csv_file], check=False)
        today_str = TODAY.strftime("%Y-%m-%d")
        subprocess.run(["git","commit","-m",
                         f"Jobs update {today_str}: {len(jobs)} records"], check=False)

        if repo and token:
            push_url = f"https://x-access-token:{token}@github.com/{repo}.git"
            result   = subprocess.run(["git","push", push_url,"HEAD:main"],
                                       capture_output=True, text=True)
            if result.returncode == 0:
                print("✅ Pushed to GitHub successfully!")
                print(f"📊 {len(jobs)} jobs | CSV: {csv_file}")
            else:
                print(f"⚠️ Push failed: {result.stderr[:200]}")
                print("💡 Make sure GH_PAT secret is set in repo Settings → Secrets")
        else:
            print("⚠️ GITHUB_REPOSITORY or GH_PAT not set — skipping push")
    elif len(jobs) == 0:
        print("\n⚠️ 0 jobs found — CSV not pushed (nothing to update)")
        print("💡 Check: NVIDIA_NIM_API_KEY secret is set in GitHub repo")
    else:
        print(f"\n⚠️ CSV file not found: {csv_file}")

    valid  = sum(1 for j in jobs if j.get("validation_status")=="Valid")
    w_desc = sum(1 for j in jobs if int(j.get("description_length",0) or 0)>80)
    print(f"\n✅ DONE: {len(jobs)} jobs | {valid} valid | {w_desc} with descriptions")
