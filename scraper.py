"""
Architecture Job Scraper — Calgary, AB Edition
Scrapes job boards + Calgary architecture firm websites daily.
Generates a beautiful HTML job board hosted via GitHub Pages.
Optional SMS alerts via Twilio.
"""

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ── CONFIG ────────────────────────────────────────────────────────────────────

# All search terms targeting entry-level / intern / <3 yrs experience roles
SEARCH_TERMS = [
    # ── Core titles (your requested ones) ───────────────────────────────────
    "architect Calgary",
    "junior architect Calgary",
    "intern architect Calgary",
    "junior designer Calgary",
    # ── Extended entry-level variants ────────────────────────────────────────
    "entry level architect Calgary",
    "graduate architect Calgary",
    "junior architectural designer Calgary",
    "junior designer architecture Calgary",
    "architectural designer Calgary",
    "architectural intern Calgary",
    "architecture graduate Calgary",
    "IDP architect Calgary",
    "emerging architect Calgary",
    "co-op architect Calgary",
    "architecture co-op Calgary",
]

# Location strings used for jobspy / Indeed
LOCATION       = "Calgary, Alberta, Canada"
LOCATION_SHORT = "Calgary, AB"

# Keywords that MUST appear in title or location to pass the location filter
CALGARY_KEYWORDS = {"calgary", "ab", "alberta"}

# Title keywords that confirm entry-level / early-career roles
ENTRY_LEVEL_TITLE_KEYWORDS = [
    "intern architect", "architectural intern", "internship",
    "co-op", "coop",
    "junior architect", "junior architectural", "junior designer",
    "jr. architect",
    "entry level architect", "entry-level architect",
    "graduate architect", "architecture graduate",
    "emerging architect",
    "architectural designer",   # common new-grad title
    "idp",                      # intern development program
    "architect i",  "architect 1",  # level-based titles
]

# Title keywords that HARD REJECT a job regardless of anything else
TITLE_REJECT_KEYWORDS = [
    # Wrong seniority
    "senior", "sr.", "principal", "director", "manager",
    "lead architect", "associate principal", "project architect",
    "intermediate architect", "intermediate designer",  # mid-level, not entry
    # IT / tech "architect" roles (not building architecture)
    "solution architect", "solutions architect",
    "cloud architect", "software architect",
    "network architect", "data architect", "enterprise architect",
    "security architect", "systems architect", "it architect",
    "technical architect", "infrastructure architect",
    "application architect", "integration architect",
    "salesforce architect", "azure architect", "aws architect",
    # Engineering — not architecture
    "structural engineer", "civil engineer", "mechanical engineer",
    "electrical engineer", "hvac", "process engineer",
    "geotechnical", "environmental engineer",
    # Other wrong fields
    "interior design", "interior designer",
    "landscape architect",
    "architectural technologist",
    "urban planner", "urban designer",
]

# Keywords in descriptions that suggest > 3 yrs experience — used to REJECT
EXPERIENCE_REJECT_KEYWORDS = [
    "5+ years", "5 years experience", "6+ years", "7+ years", "8+ years",
    "10 years", "15 years", "senior", "principal", "associate principal",
    "project architect", "project manager",
]


SEEN_JOBS_FILE = Path("seen_jobs.json")

# How many days to keep a listing before it expires off the board
JOB_EXPIRY_DAYS = 30
OUTPUT_HTML    = Path("docs/index.html")

TWILIO_ENABLED     = False
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN  = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM        = os.environ.get("TWILIO_FROM", "")
TWILIO_TO          = os.environ.get("TWILIO_TO", "")

# ── Email config (uses Outlook SMTP — free, no paid service needed) ────────────
# To activate: set EMAIL_ENABLED = True, then add these as GitHub Secrets:
#   SMTP_USER      → your Outlook/Hotmail address (e.g. yourname@outlook.com)
#   SMTP_PASSWORD  → your Outlook password, OR an App Password if you have
#                    2FA enabled: account.microsoft.com → Security → App passwords
#   EMAIL_TO       → address to receive the digest (can be any email address)
#   BOARD_URL      → your GitHub Pages URL e.g. https://username.github.io/calgary-arch-jobs
EMAIL_ENABLED  = True
SMTP_HOST      = "smtp-mail.outlook.com"
SMTP_PORT      = 587
SMTP_USER      = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD  = os.environ.get("SMTP_PASSWORD", "")
EMAIL_TO       = os.environ.get("EMAIL_TO", "")
BOARD_URL      = os.environ.get("BOARD_URL", "")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-CA,en;q=0.9",
}

# ── FILTERS ───────────────────────────────────────────────────────────────────

def is_calgary(job: dict) -> bool:
    haystack = (job.get("location", "") + " " + job.get("title", "")).lower()
    if "remote" in haystack:
        return True
    return any(kw in haystack for kw in CALGARY_KEYWORDS)


def is_entry_level(job: dict) -> bool:
    title = job.get("title", "").lower()
    desc  = job.get("description", "").lower()
    combined = title + " " + desc

    # Hard reject anything in the title reject list (wrong field or seniority)
    if any(kw in title for kw in TITLE_REJECT_KEYWORDS):
        return False

    # Accept if title contains an entry-level keyword
    if any(kw in title for kw in ENTRY_LEVEL_TITLE_KEYWORDS):
        return True

    # Accept titles with "architect" in them (catches plain "architect" postings)
    if "architect" in title:
        if any(kw in desc for kw in EXPERIENCE_REJECT_KEYWORDS):
            return False
        return True

    # Accept "junior designer" or "designer" titles from architecture firms
    # (firms like DIALOG, GEC sometimes post just "Junior Designer")
    if "designer" in title or "junior" in title:
        if any(kw in desc for kw in EXPERIENCE_REJECT_KEYWORDS):
            return False
        return True

    # Accept if description mentions entry-level / student / graduate signals
    desc_entry_signals = [
        "entry level", "entry-level", "new grad", "new graduate",
        "recent graduate", "students and graduates", "students & graduates",
        "0-2 years", "0 to 2 years", "1-3 years", "1 to 3 years",
        "intern", "internship", "co-op", "coop", "idp",
        "emerging professional", "graduate architect",
    ]
    if any(sig in combined for sig in desc_entry_signals):
        if any(kw in desc for kw in EXPERIENCE_REJECT_KEYWORDS):
            return False
        return True

    return False


# ── JOB BOARD SCRAPERS ────────────────────────────────────────────────────────

def scrape_jobspy(query: str) -> list[dict]:
    """Best coverage — hits Indeed, LinkedIn, ZipRecruiter, Glassdoor simultaneously."""
    jobs = []
    try:
        from jobspy import scrape_jobs
        df = scrape_jobs(
            site_name=["indeed", "linkedin", "glassdoor"],  # ziprecruiter blocks GitHub IPs
            search_term=query,
            location="Calgary, Alberta",  # format Glassdoor accepts for Canadian cities
            country_indeed="Canada",
            linkedin_fetch_description=True,
            results_wanted=25,
            hours_old=48,
        )
        for _, row in df.iterrows():
            title   = str(row.get("title", ""))
            url     = str(row.get("job_url", ""))
            jobs.append({
                "id":          f"jobspy_{abs(hash(url + title))}",
                "title":       title,
                "company":     str(row.get("company", "N/A")),
                "location":    str(row.get("location", LOCATION_SHORT)),
                "source":      str(row.get("site", "JobSpy")).replace("_", " ").title(),
                "url":         url,
                "date":        str(row.get("date_posted", "Recent")),
                "description": str(row.get("description", ""))[:500],
                "scraped_at":  datetime.now().isoformat(),
            })
    except ImportError:
        print("[JobSpy] Not installed — falling back to direct scrapers")
    except Exception as e:
        print(f"[JobSpy] Error: {e}")
    return jobs


def scrape_indeed_ca(query: str) -> list[dict]:
    jobs = []
    url = (
        f"https://ca.indeed.com/jobs"
        f"?q={requests.utils.quote(query)}"
        f"&l={requests.utils.quote(LOCATION_SHORT)}"
        f"&fromage=3&sort=date"
    )
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(resp.text, "html.parser")
        for card in soup.select("div.job_seen_beacon")[:15]:
            title_el   = card.select_one("h2.jobTitle span")
            company_el = card.select_one("[data-testid='company-name']")
            loc_el     = card.select_one("[data-testid='text-location']")
            link_el    = card.select_one("h2.jobTitle a")
            date_el    = card.select_one("[data-testid='myJobsStateDate']")
            if not title_el:
                continue
            jk   = link_el["id"].replace("job_", "") if link_el and link_el.get("id") else ""
            href = f"https://ca.indeed.com/viewjob?jk={jk}" if jk else ""
            jobs.append({
                "id":          f"indeed_ca_{jk}",
                "title":       title_el.get_text(strip=True),
                "company":     company_el.get_text(strip=True) if company_el else "N/A",
                "location":    loc_el.get_text(strip=True) if loc_el else LOCATION_SHORT,
                "source":      "Indeed CA",
                "url":         href,
                "date":        date_el.get_text(strip=True) if date_el else "Recent",
                "description": "",
                "scraped_at":  datetime.now().isoformat(),
            })
    except Exception as e:
        print(f"[Indeed CA] Error: {e}")
    return jobs


def scrape_linkedin_jobs(query: str) -> list[dict]:
    jobs = []
    url = (
        "https://www.linkedin.com/jobs/search/"
        f"?keywords={requests.utils.quote(query)}"
        f"&location={requests.utils.quote(LOCATION)}"
        "&f_TPR=r172800&sortBy=DD"   # last 48 hours
    )
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(resp.text, "html.parser")
        for card in soup.select("div.base-card")[:15]:
            title_el   = card.select_one("h3.base-search-card__title")
            company_el = card.select_one("h4.base-search-card__subtitle")
            loc_el     = card.select_one("span.job-search-card__location")
            link_el    = card.select_one("a.base-card__full-link")
            date_el    = card.select_one("time")
            if not title_el:
                continue
            href = link_el["href"].split("?")[0] if link_el else ""
            jobs.append({
                "id":          f"linkedin_{abs(hash(href))}",
                "title":       title_el.get_text(strip=True),
                "company":     company_el.get_text(strip=True) if company_el else "N/A",
                "location":    loc_el.get_text(strip=True) if loc_el else LOCATION_SHORT,
                "source":      "LinkedIn",
                "url":         href,
                "date":        date_el.get("datetime", "Recent") if date_el else "Recent",
                "description": "",
                "scraped_at":  datetime.now().isoformat(),
            })
    except Exception as e:
        print(f"[LinkedIn] Error: {e}")
    return jobs


def scrape_workopolis(query: str) -> list[dict]:
    """Workopolis — popular Canadian job board."""
    jobs = []
    url = (
        f"https://www.workopolis.com/jobsearch/find-jobs"
        f"?ak={requests.utils.quote(query)}&l=Calgary%2C+AB"
    )
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(resp.text, "html.parser")
        for card in soup.select("article.JobCard, div.job-card")[:10]:
            title_el   = card.select_one("h2, h3, .job-title")
            company_el = card.select_one(".company-name, .employer")
            link_el    = card.select_one("a")
            if not title_el:
                continue
            href = link_el["href"] if link_el else ""
            if href and not href.startswith("http"):
                href = "https://www.workopolis.com" + href
            jobs.append({
                "id":          f"workopolis_{abs(hash(href))}",
                "title":       title_el.get_text(strip=True),
                "company":     company_el.get_text(strip=True) if company_el else "N/A",
                "location":    LOCATION_SHORT,
                "source":      "Workopolis",
                "url":         href,
                "date":        "Recent",
                "description": "",
                "scraped_at":  datetime.now().isoformat(),
            })
    except Exception as e:
        print(f"[Workopolis] Error: {e}")
    return jobs


# ── AAA JOB BOARD SCRAPER ────────────────────────────────────────────────────

def scrape_aaa_board() -> list[dict]:
    """
    Scrape the Alberta Association of Architects official job board.
    This is the most authoritative source for Alberta architecture job postings.
    """
    jobs = []
    url = (
        "https://aaa.ab.ca/web/Web/Professional_Resources/Careers/"
        "Career_Opportunities.aspx?hkey=ac6171fe-e494-4a4f-b647-a1057e3a0fb2"
    )
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        soup = BeautifulSoup(resp.text, "html.parser")

        # AAA board uses a table or list of postings
        # Try multiple selectors to be resilient to page structure changes
        entries = (
            soup.select("table tr")
            or soup.select(".career-opportunity, .job-posting, li.opportunity")
            or soup.select("div.sfContentBlock a")
        )

        for entry in entries:
            # Try to find a link with a job title
            link = entry.select_one("a") if entry.name != "a" else entry
            if not link:
                continue
            title = link.get_text(strip=True)
            href  = link.get("href", "")
            if not title or len(title) < 5:
                continue
            if not href:
                continue

            # Make absolute URL
            if href.startswith("/"):
                href = "https://aaa.ab.ca" + href
            elif not href.startswith("http"):
                href = "https://aaa.ab.ca/" + href

            # Grab any surrounding text for company/date info
            parent_text = entry.get_text(separator=" ", strip=True) if hasattr(entry, "get_text") else title

            jobs.append({
                "id":          f"aaa_{abs(hash(href + title))}",
                "title":       title[:100],
                "company":     "See posting",
                "location":    "Alberta",
                "source":      "AAA Job Board",
                "url":         href,
                "date":        "See posting",
                "description": parent_text[:200],
                "scraped_at":  datetime.now().isoformat(),
            })

        # Fallback: if the page loaded but no structured entries found,
        # at least surface the board link itself
        if not jobs:
            jobs.append({
                "id":          "aaa_board_main",
                "title":       "Check AAA Career Opportunities",
                "company":     "Alberta Association of Architects",
                "location":    "Alberta",
                "source":      "AAA Job Board",
                "url":         url,
                "date":        "Updated regularly",
                "description": "Official AAA job board — check for new Alberta architecture postings",
                "scraped_at":  datetime.now().isoformat(),
            })

        print(f"  [AAA Board] {len(jobs)} postings found")
    except Exception as e:
        print(f"  [AAA Board] Error — {e}")
    return jobs


# ── FIRM-TARGETED SEARCH ─────────────────────────────────────────────────────
# Instead of scraping firm websites (unreliable — picks up news/events/nav),
# Two strategies depending on how reliable each firm's careers page is:
#   FIRM_CAREER_PAGES → scraped directly (clean dedicated careers pages)
#   FIRM_NAMES_SEARCH → searched via Indeed CA (sites without clean pages)

# ✏️ FIRM_CAREER_PAGES: firms with clean, dedicated careers pages that list
# individual job postings. The scraper visits these URLs directly and pulls
# any links whose URL path looks like a job posting.
# Add more here as you discover firms with good careers pages.
FIRM_CAREER_PAGES = [
    ("McKinley Studios",     "https://www.mckinleystudios.com/careers/"),
    ("Zeidler Architecture", "https://zeidler.com/culture-and-careers/"),
    ("Metafor Studio",       "https://metafor.studio/join-our-team/"),
    ("FAAS Architecture",    "https://faasarch.com/career/"),
    ("GGA Architects",       "https://gga-arch.com/careers/"),
    ("HCMA Architecture",    "https://hcma.ca/careers/"),
    ("S2 Architecture",      "https://s2architecture.com/architecture-careers/"),
    ("Gibbs Gage Architects","https://www.gibbsgage.com/careers"),
    ("Riddell Kurczaba",     "https://rka.ca/about/careers/"),
    ("GEC Architecture",     "https://gecarchitecture.com/careers/"),
    ("Kasian Architecture",  "https://kasian.com/kasian-careers/"),
    # ✏️ To add a firm with a clean careers page, paste a new line here.
]

# ✏️ FIRM_NAMES_SEARCH: firms whose websites pull in news/events/project pages
# alongside job listings, making direct scraping unreliable. We search for
# them on Indeed CA instead, which only returns actual job postings.
FIRM_NAMES_SEARCH = [
    "DIALOG", "Stantec", "Morrison Hershfield",
    "NORR", "IBI Group", "Entuitive", "HOK", "Gensler",
    "Perkins and Will", "Studio North",
    # ✏️ To add a firm to search-based lookup, paste a name here.
]

# Job posting URL patterns used by real ATS systems and career pages
JOB_URL_PATTERNS = [
    "/job/", "/jobs/", "/job-", "-job/",
    "/opening/", "/openings/",
    "/position/", "/positions/",
    "/posting/", "/postings/",
    "/vacancy/", "/vacancies/",
    "/apply/", "/application/",
    "/requisition/", "/req/",
    "/opportunity/", "/opportunities/",
    "jobid=", "job_id=", "jobId=",
    "myworkdayjobs.com", "greenhouse.io", "lever.co",
    "taleo.net", "icims.com", "jobvite.com", "bamboohr.com",
]

# URL patterns that are definitely NOT job postings
NON_JOB_URL_PATTERNS = [
    "/news/", "/blog/", "/insight/", "/article/", "/press/",
    "/project/", "/portfolio/", "/work/", "/case-stud",
    "/event/", "/publication/", "/resource/", "/media/",
    "/about/", "/contact/", "/team/", "/people/",
    "/service/", "/expertise/", "/sector/",
]

# Link text that is nav/content, not a job title
NON_JOB_TEXT = [
    "home", "about", "contact", "services", "projects", "portfolio",
    "news", "blog", "events", "awards", "press", "media",
    "team", "people", "leadership", "culture", "values",
    "learn more", "read more", "view more", "apply here",
    "see all", "view all", "all openings", "explore careers",
    "privacy", "terms", "sitemap", "cookie",
    "instagram", "facebook", "linkedin", "twitter", "youtube",
]

SKIP_IN_HREF = {"mailto:", "tel:", "javascript:", "facebook.com",
                "twitter.com", "instagram.com", "youtube.com"}


def scrape_firm_careers() -> list[dict]:
    """Scrape firms with clean dedicated careers pages directly."""
    jobs = []

    for firm_name, page_url in FIRM_CAREER_PAGES:
        try:
            resp = requests.get(page_url, headers=HEADERS, timeout=20)
            soup = BeautifulSoup(resp.text, "html.parser")
            base_domain = "/".join(page_url.split("/")[:3])
            found = 0

            for link in soup.find_all("a"):
                text = link.get_text(strip=True)
                href = link.get("href", "")

                if not text or not href:
                    continue
                if len(text) < 5 or len(text) > 150:
                    continue
                if any(s in href for s in SKIP_IN_HREF):
                    continue
                if href.startswith("#") or href.startswith("?"):
                    continue

                text_lower = text.lower()
                if any(nav in text_lower for nav in NON_JOB_TEXT):
                    continue

                # Build absolute URL
                if href.startswith("http"):
                    full_url = href
                elif href.startswith("/"):
                    full_url = base_domain + href
                else:
                    full_url = base_domain + "/" + href.lstrip("/")

                full_url_lower = full_url.lower()

                # Skip non-job URL patterns
                if any(pat in full_url_lower for pat in NON_JOB_URL_PATTERNS):
                    continue
                # Skip same page
                if full_url.rstrip("/") == page_url.rstrip("/"):
                    continue

                # URL must look like a job posting OR go 2+ levels deeper than careers page
                url_looks_like_job = any(pat in full_url_lower for pat in JOB_URL_PATTERNS)
                careers_path = "/" + "/".join(page_url.split("/")[3:]).rstrip("/")
                full_path    = "/" + "/".join(full_url.split("/")[3:]).rstrip("/")
                path_is_deeper = (
                    full_path.startswith(careers_path) and
                    full_path != careers_path and
                    full_path.count("/") >= careers_path.count("/") + 2
                )
                if not url_looks_like_job and not path_is_deeper:
                    continue

                # Title must pass entry-level filter
                fake_job = {"title": text, "description": "", "location": LOCATION_SHORT}
                if not is_entry_level(fake_job):
                    continue

                jobs.append({
                    "id":          f"firm_{abs(hash(full_url + firm_name))}",
                    "title":       text[:100].strip(),
                    "company":     firm_name,
                    "location":    LOCATION_SHORT,
                    "source":      "Firm Website",
                    "url":         full_url,
                    "date":        "See posting",
                    "description": f"Listed on {firm_name} careers page",
                    "scraped_at":  datetime.now().isoformat(),
                })
                found += 1
                if found >= 5:
                    break

            print(f"  {firm_name}: {found} job link(s) found")
            time.sleep(2)

        except Exception as e:
            print(f"  {firm_name}: Error — {e}")

    return jobs


def scrape_firm_targeted() -> list[dict]:
    """
    For firms whose websites mix job postings with news/events/projects,
    search Indeed CA directly — only returns actual job listings.
    """
    jobs = []
    entry_queries = [
        "junior architect", "intern architect",
        "architectural designer", "graduate architect", "junior designer"
    ]
    for firm in FIRM_NAMES_SEARCH:
        for query in entry_queries:
            try:
                results = scrape_indeed_ca(f"{query} {firm} Calgary")
                for j in results:
                    co = j.get("company", "").lower()
                    firm_word = firm.lower().split()[0]
                    if firm_word in co:
                        jobs.append(j)
            except Exception as e:
                print(f"  [Firm search] {firm}: {e}")
            time.sleep(1)

    print(f"  [Firm search] {len(jobs)} results across {len(FIRM_NAMES_SEARCH)} firms")
    return jobs

# ── DEDUP ─────────────────────────────────────────────────────────────────────

def load_seen() -> set:
    if SEEN_JOBS_FILE.exists():
        try:
            content = SEEN_JOBS_FILE.read_text().strip()
            if content:
                return set(json.loads(content))
        except (json.JSONDecodeError, ValueError):
            pass
    return set()

def save_seen(seen: set):
    SEEN_JOBS_FILE.write_text(json.dumps(list(seen)))

def filter_new(jobs: list[dict], seen: set) -> list[dict]:
    new = []
    for job in jobs:
        if job["id"] not in seen and job["title"] and job["url"]:
            new.append(job)
            seen.add(job["id"])
    return new



def expire_old_jobs(all_jobs: list[dict]) -> list[dict]:
    """Remove jobs scraped more than JOB_EXPIRY_DAYS days ago."""
    cutoff = datetime.now().timestamp() - (JOB_EXPIRY_DAYS * 86400)
    kept, removed = [], 0
    for job in all_jobs:
        try:
            scraped = datetime.fromisoformat(job.get("scraped_at", "")).timestamp()
            if scraped >= cutoff:
                kept.append(job)
            else:
                removed += 1
        except (ValueError, TypeError):
            kept.append(job)  # keep if date unparseable
    if removed:
        print(f"[Expire] Removed {removed} listings older than {JOB_EXPIRY_DAYS} days")
    return kept

# ── HTML GENERATOR ────────────────────────────────────────────────────────────

def generate_html(new_jobs: list[dict], all_jobs: list[dict]):
    today     = datetime.now().strftime("%B %d, %Y")
    new_count = len(new_jobs)
    new_ids   = {j["id"] for j in new_jobs}

    SOURCE_COLORS = {
        "Indeed CA":    "#7A9DB0",
        "Indeed":       "#7A9DB0",
        "Zip Recruiter":"#7A9DB0",
        "ZipRecruiter": "#7A9DB0",
        "Glassdoor":    "#879979",
        "LinkedIn":     "#7A9DB0",
        "Firm Website": "#503228",
        "Work Alberta": "#8B6F5E",
        "Workopolis":   "#8B6F5E",
        "AAA Job Board":"#879979",
    }

    def job_card(j: dict) -> str:
        color     = SOURCE_COLORS.get(j["source"], "#555")
        is_new    = j["id"] in new_ids
        new_badge = '<span class="badge-new">NEW</span>' if is_new else ""
        desc_html = ""
        if j.get("description") and j["description"] not in ("", "N/A"):
            desc_html = f'<p class="job-desc">{j["description"][:130]}…</p>'
        scraped_ts = ""
        try:
            scraped_ts = str(int(datetime.fromisoformat(j.get("scraped_at","")).timestamp()))
        except Exception:
            pass
        return (
            f'<a href="{j["url"]}" target="_blank" rel="noopener" '
            f'class="job-card{"  job-card--new" if is_new else ""}" '
            f'data-source="{j["source"]}" data-scraped="{scraped_ts}">'
            f'<div class="job-card__header">'
            f'<span class="job-source" style="background:{color}">{j["source"]}</span>'
            f'{new_badge}</div>'
            f'<h3 class="job-title">{j["title"]}</h3>'
            f'<p class="job-company">{j["company"]}</p>'
            f'<p class="job-location">📍 {j["location"]}</p>'
            f'<p class="job-date">🕐 {j["date"]}</p>'
            f'{desc_html}</a>'
        )

    new_cards = "\n".join(job_card(j) for j in new_jobs) if new_jobs else (
        '<p class="no-jobs">No new jobs today — check back tomorrow! 🏛</p>'
    )
    all_cards = "\n".join(job_card(j) for j in reversed(all_jobs[-150:]))

    sources    = sorted({j["source"] for j in all_jobs[-150:]})
    filter_btns = "\n".join(
        f'<button class="filter-btn" onclick="filterJobs(\'{s}\',this)">{s}</button>'
        for s in sources
    )
    search_display = " · ".join(t.replace(" Calgary", "") for t in SEARCH_TERMS)
    firm_display   = " · ".join(f[0] for f in FIRM_CAREER_PAGES)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>Job Search — Intern Architect | Calgary</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Georgia&family=DM+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
:root{{
  --bg:#F2F1EB;
  --surface:#FFFFFF;
  --surface2:#EDE9DF;
  --border:#D6D0C4;
  --brown:#503228;
  --green:#879979;
  --yellow:#E8DCC1;
  --text:#2C1F1A;
  --muted:#7A6E68;
  --glow:rgba(80,50,40,0.07);
}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{
  background:var(--bg);color:var(--text);
  font-family:'DM Sans',sans-serif;min-height:100vh;
}}
.masthead{{
  background:var(--brown);
  padding:2.5rem 2rem 2rem;position:relative;overflow:hidden;
}}
.masthead::before{{display:none}}
.masthead__inner{{
  max-width:1200px;margin:0 auto;position:relative;z-index:1;
  display:flex;align-items:flex-end;justify-content:space-between;
  flex-wrap:wrap;gap:1rem;
}}
.masthead__title{{
  font-family:'Georgia',serif;
  font-size:clamp(2rem,5vw,3.5rem);font-weight:700;
  color:var(--yellow);line-height:1;letter-spacing:-1px;
}}
.masthead__sub{{font-size:0.72rem;color:rgba(251,230,183,0.7);letter-spacing:0.2em;text-transform:uppercase;margin-top:0.5rem}}
.masthead__city{{
  display:inline-block;background:var(--yellow);color:var(--brown);
  font-size:0.62rem;letter-spacing:0.2em;padding:0.25rem 0.75rem;
  margin-top:0.5rem;font-weight:700;
}}
.masthead__date{{font-size:0.75rem;color:rgba(251,230,183,0.6);letter-spacing:0.08em}}
.counter{{
  display:inline-block;background:var(--yellow);color:var(--brown);
  font-family:'Georgia',serif;font-size:2.2rem;font-weight:700;
  padding:0.15rem 0.8rem;margin-top:0.3rem;
}}
.counter__label{{font-size:0.65rem;color:rgba(251,230,183,0.6);display:block;letter-spacing:0.15em;text-transform:uppercase;margin-top:0.25rem}}
.container{{max-width:1200px;margin:0 auto;padding:2rem}}
.section-label{{
  font-size:0.65rem;letter-spacing:0.25em;text-transform:uppercase;
  color:var(--brown);border-bottom:2px solid var(--brown);
  padding-bottom:0.6rem;margin-bottom:1.5rem;
  display:flex;align-items:center;gap:0.75rem;font-weight:700;
}}
.section-label::before{{
  content:'';display:inline-block;width:8px;height:8px;
  background:var(--green);transform:rotate(45deg);flex-shrink:0;
}}
.info-box{{
  background:var(--yellow);border:1px solid var(--border);
  border-left:4px solid var(--brown);padding:1rem 1.2rem;
  margin-bottom:2rem;font-size:0.7rem;color:var(--brown);line-height:2;
}}
.info-box strong{{color:var(--brown);font-weight:700}}
.jobs-grid{{
  display:grid;grid-template-columns:repeat(auto-fill,minmax(290px,1fr));
  gap:12px;margin-bottom:3rem;
}}
.job-card{{
  background:var(--surface);padding:1.4rem;text-decoration:none;
  color:inherit;display:block;
  transition:box-shadow 0.15s,transform 0.15s;
  border:1px solid var(--border);
  border-radius:2px;
  position:relative;
}}
.job-card:hover{{
  transform:translateY(-3px);
  box-shadow:0 6px 20px rgba(80,50,40,0.12);
  border-color:var(--green);
}}
.job-card--new{{
  background:#FFFDF5;
  border-color:var(--brown);
  border-left:4px solid var(--brown);
}}
.job-card__header{{display:flex;align-items:center;gap:0.5rem;margin-bottom:0.75rem;flex-wrap:wrap}}
.job-source{{
  font-size:0.58rem;letter-spacing:0.12em;text-transform:uppercase;
  color:#fff;padding:0.2rem 0.55rem;font-weight:600;border-radius:2px;
}}
.badge-new{{
  font-size:0.55rem;letter-spacing:0.15em;background:var(--brown);
  color:var(--yellow);padding:0.15rem 0.5rem;font-weight:700;
  border-radius:2px;animation:pulse 2.5s infinite;
}}
@keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:0.6}}}}
.job-title{{
  font-family:'Georgia',serif;font-size:1.05rem;font-weight:700;
  color:var(--brown);line-height:1.35;margin-bottom:0.4rem;
}}
.job-company{{font-size:0.8rem;color:var(--green);font-weight:600;margin-bottom:0.3rem}}
.job-location,.job-date{{font-size:0.68rem;color:var(--muted);margin-top:0.2rem}}
.job-desc{{font-size:0.67rem;color:var(--muted);margin-top:0.6rem;line-height:1.6;border-top:1px solid var(--border);padding-top:0.5rem}}
.no-jobs{{
  grid-column:1/-1;text-align:center;padding:3rem;
  color:var(--muted);font-size:0.85rem;background:var(--surface);
  border:1px solid var(--border);
}}
.filters{{display:flex;gap:0.5rem;flex-wrap:wrap;margin-bottom:1.5rem}}
.filter-btn{{
  background:var(--surface);border:1px solid var(--border);color:var(--muted);
  padding:0.4rem 0.9rem;font-family:'DM Sans',sans-serif;font-size:0.65rem;
  letter-spacing:0.08em;text-transform:uppercase;cursor:pointer;
  transition:all 0.15s;border-radius:2px;font-weight:500;
}}
.filter-btn:hover,.filter-btn.active{{
  border-color:var(--brown);color:var(--brown);
  background:var(--yellow);font-weight:700;
}}
.divider{{
  height:1px;background:var(--border);
  margin:2.5rem 0;
}}
.footer{{
  border-top:2px solid var(--brown);padding:1.5rem 2rem;text-align:center;
  font-size:0.65rem;color:var(--muted);letter-spacing:0.06em;
  max-width:1200px;margin:0 auto;line-height:2;
}}
</style>
</head>
<body>

<header class="masthead">
  <div class="masthead__inner">
    <div>
      <div class="masthead__title">Job Search —<br><span style="font-size:0.72em;opacity:0.9;font-style:italic">Intern Architect</span></div>
      <div class="masthead__sub">Entry Level · Intern · Junior · Graduate</div>
      <div class="masthead__city">📍 Calgary, Alberta</div>
    </div>
    <div style="text-align:right">
      <div class="masthead__date">Updated {today}</div>
      <div class="counter">{new_count}</div>
      <span class="counter__label">New today</span>
    </div>
  </div>
</header>

<div class="container">

  <div class="info-box">
    <strong>Job boards:</strong> Indeed CA · LinkedIn · Glassdoor · ZipRecruiter · Workopolis<br>
    <strong>Firm pages:</strong> {firm_display}<br>
    <strong>Roles searched:</strong> {search_display}<br>
    <strong>Filter logic:</strong> Calgary/AB only · No senior/principal/5+ yr roles
  </div>

  <section>
    <div class="section-label">New Listings — {today}</div>
    <div class="jobs-grid" id="new-grid">{new_cards}</div>
  </section>

  <div class="divider"></div>

  <section>
    <div class="section-label">All Recent Listings</div>
    <div class="filters">
      <button class="filter-btn active" onclick="filterJobs('all',this)">All</button>
      {filter_btns}
    </div>
    <div class="filters" style="margin-top:-0.75rem">
      <span style="font-size:0.62rem;color:var(--muted);letter-spacing:0.1em;text-transform:uppercase;align-self:center">Added:</span>
      <button class="filter-btn date-btn active" onclick="filterDate(0,this)">All Time</button>
      <button class="filter-btn date-btn" onclick="filterDate(1,this)">Today</button>
      <button class="filter-btn date-btn" onclick="filterDate(3,this)">Last 3 Days</button>
      <button class="filter-btn date-btn" onclick="filterDate(7,this)">Last Week</button>
      <button class="filter-btn date-btn" onclick="filterDate(14,this)">Last 2 Weeks</button>
    </div>
    <div class="jobs-grid" id="all-grid">{all_cards}</div>
  </section>

</div>

<footer class="footer">
  Auto-updated daily via GitHub Actions · Calgary, Alberta, Canada<br>
  Entry level · Intern · Junior · Graduate · IDP · &lt; 3 years experience
</footer>

<script>
let activeSource = 'all';
let activeDays = 0;

function applyFilters() {{
  const now = Math.floor(Date.now() / 1000);
  document.querySelectorAll('#all-grid .job-card').forEach(card => {{
    const srcMatch = activeSource === 'all' || card.dataset.source === activeSource;
    let dateMatch = true;
    if (activeDays > 0) {{
      const scraped = parseInt(card.dataset.scraped || '0');
      dateMatch = scraped > 0 && (now - scraped) <= activeDays * 86400;
    }}
    card.style.display = (srcMatch && dateMatch) ? 'block' : 'none';
  }});
}}

function filterJobs(source, btn) {{
  document.querySelectorAll('.filter-btn:not(.date-btn)').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  activeSource = source;
  applyFilters();
}}

function filterDate(days, btn) {{
  document.querySelectorAll('.date-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  activeDays = days;
  applyFilters();
}}
</script>
</body>
</html>"""

    OUTPUT_HTML.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_HTML.write_text(html, encoding="utf-8")
    print(f"[HTML] Saved to {OUTPUT_HTML} — {new_count} new / {len(all_jobs)} total")


# ── SMS ───────────────────────────────────────────────────────────────────────

def send_sms(jobs: list[dict]):
    if not TWILIO_ENABLED or not jobs:
        return
    try:
        from twilio.rest import Client
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        lines = [f"🏛 {len(jobs)} new Calgary arch jobs!\n"]
        for j in jobs[:5]:
            lines.append(f"• {j['title']} @ {j['company']}\n  {j['url'][:60]}\n")
        if len(jobs) > 5:
            lines.append(f"…+{len(jobs)-5} more on your job board.")
        client.messages.create(body="\n".join(lines), from_=TWILIO_FROM, to=TWILIO_TO)
        print(f"[SMS] Sent — {len(jobs)} jobs")
    except Exception as e:
        print(f"[SMS] Error: {e}")


def send_email(jobs: list[dict]):
    """Send a daily digest email via Outlook SMTP. No paid service needed."""
    if not EMAIL_ENABLED or not jobs or not SMTP_USER or not SMTP_PASSWORD or not EMAIL_TO:
        if EMAIL_ENABLED and not SMTP_USER:
            print("[Email] Skipped — SMTP_USER secret not set")
        return
    try:
        import smtplib
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText

        today     = datetime.now().strftime("%B %d, %Y")
        count     = len(jobs)
        subject   = f"\U0001f3db {count} New Calgary Arch Job{'s' if count != 1 else ''} — {today}"

        SOURCE_COLORS = {
            "Indeed CA":"#7A9DB0","Indeed":"#7A9DB0","LinkedIn":"#7A9DB0",
            "Glassdoor":"#879979","Firm Website":"#503228",
            "Workopolis":"#8B6F5E","AAA Job Board":"#879979",
        }

        def make_card(j):
            color = SOURCE_COLORS.get(j["source"], "#7A9DB0")
            desc_row = ""
            if j.get("description"):
                d = j["description"][:150]
                desc_row = (
                    '<p style="font-size:12px;color:#7A6E68;margin:6px 0 0;line-height:1.5">'
                    + d + "\u2026</p>"
                )
            return (
                f'<a href="{j["url"]}" style="display:block;text-decoration:none;color:inherit;' +
                f'background:#fff;border:1px solid #D6D0C4;border-left:4px solid {color};' +
                f'border-radius:3px;padding:14px 16px;margin-bottom:12px">' +
                f'<div style="margin-bottom:6px">' +
                f'<span style="background:{color};color:#fff;font-size:10px;letter-spacing:1px;' +
                f'text-transform:uppercase;padding:2px 7px;border-radius:2px;font-weight:600">' +
                f'{j["source"]}</span></div>' +
                f'<div style="font-family:Georgia,serif;font-size:16px;font-weight:700;' +
                f'color:#503228;margin-bottom:3px">{j["title"]}</div>' +
                f'<div style="font-size:13px;color:#879979;font-weight:600;margin-bottom:2px">' +
                f'{j["company"]}</div>' +
                f'<div style="font-size:12px;color:#7A6E68">\U0001f4cd {j["location"]} ' +
                f'&nbsp;\u00b7&nbsp; \U0001f550 {j["date"]}</div>' +
                desc_row +
                '</a>'
            )

        cards_html = "\n".join(make_card(j) for j in jobs)
        board_link = (
            f'<a href="{BOARD_URL}" style="color:#E8DCC1;font-weight:600">{BOARD_URL}</a>'
            if BOARD_URL else "your GitHub Pages job board"
        )

        html_body = (
            '<!DOCTYPE html><html><head><meta charset="UTF-8"/></head>' +
            '<body style="margin:0;padding:0;background:#F2F1EB;font-family:Arial,sans-serif">' +
            '<table width="100%" cellpadding="0" cellspacing="0" style="background:#F2F1EB;padding:30px 0">' +
            '<tr><td align="center"><table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%">' +
            '<tr><td style="background:#503228;padding:28px 30px;border-radius:4px 4px 0 0">' +
            '<div style="font-family:Georgia,serif;font-size:26px;font-weight:700;color:#E8DCC1;line-height:1.2">' +
            'Job Search — Intern Architect</div>' +
            '<div style="font-size:12px;color:rgba(232,220,193,0.7);letter-spacing:2px;text-transform:uppercase;margin-top:6px">' +
            '\U0001f4cd Calgary, Alberta</div></td></tr>' +
            f'<tr><td style="background:#E8DCC1;padding:10px 30px">' +
            f'<span style="font-size:12px;color:#503228;font-weight:700;letter-spacing:1px;text-transform:uppercase">' +
            f'{count} New Listing{"s" if count != 1 else ""} — {today}</span></td></tr>' +
            f'<tr><td style="padding:20px 30px;background:#F2F1EB">{cards_html}</td></tr>' +
            f'<tr><td style="background:#503228;padding:18px 30px;border-radius:0 0 4px 4px;text-align:center">' +
            f'<p style="color:rgba(232,220,193,0.7);font-size:11px;margin:0;line-height:1.8">' +
            f'View all listings: {board_link}<br>' +
            'Auto-generated daily · Calgary Architecture Jobs</p></td></tr>' +
            '</table></td></tr></table></body></html>'
        )

        plain_lines = [f"\U0001f3db {count} new Calgary architecture jobs — {today}\n"]
        for j in jobs:
            plain_lines.append(f"- {j['title']} @ {j['company']}")
            plain_lines.append(f"  {j['location']} | {j['source']}")
            plain_lines.append(f"  {j['url']}\n")
        if BOARD_URL:
            plain_lines.append(f"View all: {BOARD_URL}")
        plain_body = "\n".join(plain_lines)

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = SMTP_USER  # Outlook requires From to match login address
        msg["To"]      = EMAIL_TO
        msg.attach(MIMEText(plain_body, "plain"))
        msg.attach(MIMEText(html_body,  "html"))

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_USER, EMAIL_TO, msg.as_string())

        print(f"[Email] Sent to {EMAIL_TO} — {count} jobs")

    except Exception as e:
        print(f"[Email] Error: {e}")


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    print(f"\n{'='*55}")
    print(f"Calgary Architecture Job Scraper — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*55}\n")

    seen = load_seen()
    all_jobs_file = Path("all_jobs.json")
    try:
        all_jobs: list[dict] = json.loads(all_jobs_file.read_text().strip()) if all_jobs_file.exists() and all_jobs_file.read_text().strip() else []
    except (json.JSONDecodeError, ValueError):
        all_jobs: list[dict] = []
    raw: list[dict] = []

    # ── Job board scraping ──
    for term in SEARCH_TERMS:
        print(f"[Boards] '{term}' ...")
        jobspy_results = scrape_jobspy(term)
        if jobspy_results:
            raw.extend(jobspy_results)
            print(f"  → JobSpy: {len(jobspy_results)}")
        else:
            # JobSpy not available — hit all boards individually
            for fn, label in [
                (scrape_indeed_ca,      "Indeed CA"),
                (scrape_linkedin_jobs,  "LinkedIn"),   # always included
                (scrape_workopolis,     "Workopolis"),
            ]:
                results = fn(term)
                raw.extend(results)
                print(f"  → {label}: {len(results)}")
                time.sleep(2)
        # Always try LinkedIn separately even when JobSpy runs,
        # since JobSpy's LinkedIn support is limited
        li_extra = scrape_linkedin_jobs(term)
        raw.extend(li_extra)
        print(f"  → LinkedIn (direct): {len(li_extra)}")
        time.sleep(3)

    # ── Firm career pages (clean dedicated pages — scraped directly) ──
    print("\n[Firms] Scraping firm career pages ...")
    raw.extend(scrape_firm_careers())

    # ── Firm-targeted searches (sites where scraping picks up junk) ──
    print("\n[Firms] Searching Indeed for listings at known Calgary firms ...")
    raw.extend(scrape_firm_targeted())

    # ── AAA official job board ──
    print("\n[AAA] Scraping Alberta Association of Architects job board ...")
    raw.extend(scrape_aaa_board())

    # ── Apply filters ──
    filtered = [j for j in raw if is_calgary(j) and is_entry_level(j)]
    print(f"\n[Filter] {len(raw)} raw → {len(filtered)} after Calgary + entry-level filters")

    # ── Dedup within this run ──
    seen_ids: set = set()
    unique: list[dict] = []
    for j in filtered:
        if j["id"] not in seen_ids:
            seen_ids.add(j["id"])
            unique.append(j)

    new_jobs = filter_new(unique, seen)
    all_jobs.extend(new_jobs)
    print(f"[Result] {len(new_jobs)} brand-new jobs (of {len(unique)} unique filtered)")

    # Remove listings older than JOB_EXPIRY_DAYS
    all_jobs = expire_old_jobs(all_jobs)

    save_seen(seen)
    all_jobs_file.write_text(json.dumps(all_jobs, indent=2))
    generate_html(new_jobs, all_jobs)
    send_sms(new_jobs)
    send_email(new_jobs)
    print("\n✓ Done!")


if __name__ == "__main__":
    main()
