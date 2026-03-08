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
    "intern", "internship", "co-op", "coop",
    "junior", "jr.",
    "entry", "entry-level", "entry level",
    "graduate", "grad",
    "emerging",
    "architectural designer",
    "architectural technologist",
    "junior designer",
    "junior architect",
    "idp",
    "designer i", "architect i", "architect 1",
]

# Keywords in descriptions that suggest > 3 yrs experience — used to REJECT
EXPERIENCE_REJECT_KEYWORDS = [
    "5+ years", "5 years experience", "6+ years", "7+ years", "8+ years",
    "10 years", "15 years", "senior", "principal", "associate principal",
    "project architect", "project manager",
]

# ── CALGARY FIRM CAREER PAGES ─────────────────────────────────────────────────
# Format: (firm_name, careers_url)
# ✏️  TO ADD A NEW FIRM: just paste one new line here. Nothing else needs to change.
FIRM_CAREER_PAGES = [
    # ── Established Calgary / AB firms ──────────────────────────────────────
    ("GEC Architecture",        "https://gecarchitecture.com/careers/"),
    ("DIALOG",                  "https://dialogdesign.ca/careers/"),
    ("Stantec",                 "https://www.stantec.com/en/careers/job-opportunities?location=Calgary"),
    ("Morrison Hershfield",     "https://www.morrisonhershfield.com/careers/"),
    ("Kasian Architecture",     "https://www.kasian.com/careers/"),
    ("Gibbs Gage Architects",   "https://www.gibbsgage.com/careers"),
    ("NORR",                    "https://norr.com/careers/all/"),
    ("Riddell Kurczaba",        "https://rka.ca/about/careers/"),
    ("S2 Architecture",         "https://s2architecture.com/architecture-careers/"),
    ("IBI Group (Arcadis)",     "https://www.ibigroup.com/careers/"),
    ("Entuitive",               "https://www.entuitive.com/careers/"),
    ("Calgary Municipal Land",  "https://www.calgarymlc.ca/about/careers"),
    ("HOK",                     "https://www.hok.com/about/careers/"),
    ("Gensler Calgary",         "https://www.gensler.com/careers"),

    # ── Your added firms ────────────────────────────────────────────────────
    # NOTE: McKinley's /contact page was provided — using it as fallback.
    # If they add a /careers page later, update this URL.
    ("McKinley Studios",        "https://www.mckinleystudios.com/contact"),
    ("Metafor Studio",          "https://metafor.studio/join-our-team/"),
    ("HCMA Architecture",       "https://hcma.ca/careers/"),
    ("Perkins & Will",          "https://perkinswill.com/careers/"),
    ("LOLA Architecture",       "https://lolaarchitecture.la/careers/"),
    ("GGA Architects",          "https://gga-arch.com/careers/"),
    ("FAAS Architecture",       "https://faasarch.com/career/"),
    ("Zeidler Architecture",    "https://zeidler.com/culture-and-careers/"),
    # NOTE: Gravity Architecture homepage provided — no dedicated careers page found.
    # Bot will scan their homepage for any hiring mentions.
    ("Gravity Architecture",    "https://www.gravityarchitecture.com/"),
    # NOTE: Studio North contact page provided — no dedicated careers page found.
    # Bot will scan for hiring mentions.
    ("Studio North",            "https://www.studionorth.ca/contact"),

    # ── AAA (Alberta Association of Architects) official job board ───────────
    # This is the best single source for Alberta-licensed architect postings.
    ("AAA Job Board",           "https://aaa.ab.ca/web/Web/Professional_Resources/Careers/Career_Opportunities.aspx?hkey=ac6171fe-e494-4a4f-b647-a1057e3a0fb2"),
]

SEEN_JOBS_FILE = Path("seen_jobs.json")
OUTPUT_HTML    = Path("docs/index.html")

TWILIO_ENABLED     = False
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN  = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM        = os.environ.get("TWILIO_FROM", "")
TWILIO_TO          = os.environ.get("TWILIO_TO", "")

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

    # Hard reject senior titles
    if any(kw in title for kw in ["senior", "sr.", "principal", "director", "manager", "lead architect"]):
        return False

    # Accept if title contains entry-level keyword
    if any(kw in title for kw in ENTRY_LEVEL_TITLE_KEYWORDS):
        return True

    # Accept "architect" / "designer" titles unless description flags seniority
    if "architect" in title or "designer" in title:
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
            site_name=["indeed", "linkedin", "zip_recruiter", "glassdoor"],
            search_term=query,
            location=LOCATION,
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


# ── FIRM CAREER PAGE SCRAPER ──────────────────────────────────────────────────

def scrape_firm_careers() -> list[dict]:
    """
    Crawl each Calgary firm's careers page and pull any links
    that look like job postings (title contains architecture keywords).
    """
    jobs = []
    job_keywords = ENTRY_LEVEL_TITLE_KEYWORDS + [
        "architect", "designer", "technologist", "technician", "intern"
    ]
    skip_domains = {"facebook", "twitter", "instagram", "linkedin.com/company", "mailto:", "tel:"}

    for firm_name, page_url in FIRM_CAREER_PAGES:
        try:
            resp = requests.get(page_url, headers=HEADERS, timeout=20)
            soup = BeautifulSoup(resp.text, "html.parser")
            found = 0

            for link in soup.find_all("a"):
                text = link.get_text(strip=True)
                href = link.get("href", "")

                if not text or len(text) < 5 or not href:
                    continue
                if any(s in href for s in skip_domains):
                    continue

                text_lower = text.lower()
                if any(kw in text_lower for kw in job_keywords):
                    full_url = href if href.startswith("http") else (
                        "/".join(page_url.rstrip("/").split("/")[:3]) + "/" + href.lstrip("/")
                    )
                    jobs.append({
                        "id":          f"firm_{abs(hash(full_url + firm_name))}",
                        "title":       text[:80].title(),
                        "company":     firm_name,
                        "location":    LOCATION_SHORT,
                        "source":      "Firm Website",
                        "url":         full_url,
                        "date":        "See posting",
                        "description": f"Direct listing from {firm_name} careers page",
                        "scraped_at":  datetime.now().isoformat(),
                    })
                    found += 1
                    if found >= 4:
                        break

            # If no specific links found but firm has a careers page, list it anyway
            if found == 0:
                page_text = soup.get_text().lower()
                if any(w in page_text for w in ["opening", "position", "hiring", "join our team", "opportunit"]):
                    jobs.append({
                        "id":          f"firm_page_{abs(hash(page_url + firm_name))}",
                        "title":       "View Open Positions",
                        "company":     firm_name,
                        "location":    LOCATION_SHORT,
                        "source":      "Firm Website",
                        "url":         page_url,
                        "date":        "Check site",
                        "description": "Visit their careers page to see current openings",
                        "scraped_at":  datetime.now().isoformat(),
                    })

            print(f"  {firm_name}: {found} job links found")
            time.sleep(2)

        except Exception as e:
            print(f"  {firm_name}: Error — {e}")

    return jobs


# ── DEDUP ─────────────────────────────────────────────────────────────────────

def load_seen() -> set:
    if SEEN_JOBS_FILE.exists():
        return set(json.loads(SEEN_JOBS_FILE.read_text()))
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


# ── HTML GENERATOR ────────────────────────────────────────────────────────────

def generate_html(new_jobs: list[dict], all_jobs: list[dict]):
    today     = datetime.now().strftime("%B %d, %Y")
    new_count = len(new_jobs)
    new_ids   = {j["id"] for j in new_jobs}

    SOURCE_COLORS = {
        "Indeed CA":    "#003A9B",
        "Indeed":       "#003A9B",
        "Zip Recruiter":"#4A90D9",
        "ZipRecruiter": "#4A90D9",
        "Glassdoor":    "#0CAA41",
        "LinkedIn":     "#0A66C2",
        "Firm Website": "#8B5E3C",
        "Work Alberta": "#CC3333",
        "Workopolis":   "#E05C00",
    }

    def job_card(j: dict) -> str:
        color     = SOURCE_COLORS.get(j["source"], "#555")
        is_new    = j["id"] in new_ids
        new_badge = '<span class="badge-new">NEW</span>' if is_new else ""
        desc_html = ""
        if j.get("description") and j["description"] not in ("", "N/A"):
            desc_html = f'<p class="job-desc">{j["description"][:130]}…</p>'
        return (
            f'<a href="{j["url"]}" target="_blank" rel="noopener" '
            f'class="job-card{"  job-card--new" if is_new else ""}" '
            f'data-source="{j["source"]}">'
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
<title>Calgary Architecture Jobs</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@400;700;900&family=DM+Mono:wght@300;400;500&display=swap" rel="stylesheet">
<style>
:root{{--bg:#0D0D0D;--surface:#161616;--surface2:#1F1F1F;--border:#2A2A2A;--accent:#C8A96E;--accent2:#E8D5B0;--text:#F0EDE6;--muted:#888;--glow:rgba(200,169,110,0.12)}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--bg);color:var(--text);font-family:'DM Mono',monospace;min-height:100vh;background-image:repeating-linear-gradient(0deg,transparent,transparent 39px,var(--border) 39px,var(--border) 40px),repeating-linear-gradient(90deg,transparent,transparent 39px,var(--border) 39px,var(--border) 40px);background-size:40px 40px}}
.masthead{{background:var(--bg);border-bottom:2px solid var(--accent);padding:2.5rem 2rem 2rem;position:relative;overflow:hidden}}
.masthead::before{{content:'YYC ARCH JOBS';position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);font-family:'Playfair Display',serif;font-size:clamp(3rem,10vw,8rem);font-weight:900;color:transparent;-webkit-text-stroke:1px rgba(200,169,110,0.06);white-space:nowrap;pointer-events:none;letter-spacing:-2px}}
.masthead__inner{{max-width:1200px;margin:0 auto;position:relative;z-index:1;display:flex;align-items:flex-end;justify-content:space-between;flex-wrap:wrap;gap:1rem}}
.masthead__title{{font-family:'Playfair Display',serif;font-size:clamp(2rem,5vw,3.5rem);font-weight:900;color:var(--accent);line-height:1;letter-spacing:-1px}}
.masthead__sub{{font-size:0.7rem;color:var(--muted);letter-spacing:0.2em;text-transform:uppercase;margin-top:0.4rem}}
.masthead__city{{display:inline-block;background:var(--accent);color:#000;font-size:0.6rem;letter-spacing:0.25em;padding:0.2rem 0.7rem;margin-top:0.5rem;font-weight:600}}
.masthead__date{{font-size:0.75rem;color:var(--muted);letter-spacing:0.1em}}
.counter{{display:inline-block;background:var(--accent);color:#000;font-family:'Playfair Display',serif;font-size:2rem;font-weight:900;padding:0.2rem 0.8rem;margin-top:0.3rem}}
.counter__label{{font-size:0.65rem;color:var(--muted);display:block;letter-spacing:0.15em;text-transform:uppercase;margin-top:0.2rem}}
.container{{max-width:1200px;margin:0 auto;padding:2rem}}
.section-label{{font-size:0.65rem;letter-spacing:0.3em;text-transform:uppercase;color:var(--accent);border-bottom:1px solid var(--border);padding-bottom:0.75rem;margin-bottom:1.5rem;display:flex;align-items:center;gap:0.75rem}}
.section-label::before{{content:'';display:inline-block;width:8px;height:8px;background:var(--accent);transform:rotate(45deg);flex-shrink:0}}
.info-box{{background:var(--surface);border:1px solid var(--border);border-left:3px solid var(--accent);padding:1rem 1.2rem;margin-bottom:2rem;font-size:0.68rem;color:var(--muted);line-height:2}}
.info-box strong{{color:var(--accent2)}}
.jobs-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(290px,1fr));gap:1px;background:var(--border);margin-bottom:3rem}}
.job-card{{background:var(--surface);padding:1.4rem;text-decoration:none;color:inherit;display:block;transition:background 0.15s,transform 0.15s;position:relative}}
.job-card:hover{{background:var(--surface2);transform:translate(-2px,-2px);z-index:1;box-shadow:4px 4px 0 var(--accent)}}
.job-card--new{{background:#1A1610}}
.job-card__header{{display:flex;align-items:center;gap:0.5rem;margin-bottom:0.75rem}}
.job-source{{font-size:0.58rem;letter-spacing:0.12em;text-transform:uppercase;color:#fff;padding:0.2rem 0.5rem;font-weight:500}}
.badge-new{{font-size:0.55rem;letter-spacing:0.2em;background:var(--accent);color:#000;padding:0.15rem 0.4rem;font-weight:500;animation:pulse 2s infinite}}
@keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:0.5}}}}
.job-title{{font-family:'Playfair Display',serif;font-size:1.05rem;font-weight:700;color:var(--accent2);line-height:1.3;margin-bottom:0.4rem}}
.job-company{{font-size:0.78rem;color:var(--text);margin-bottom:0.3rem}}
.job-location,.job-date{{font-size:0.68rem;color:var(--muted);margin-top:0.2rem}}
.job-desc{{font-size:0.64rem;color:var(--muted);margin-top:0.5rem;line-height:1.55;opacity:0.8}}
.no-jobs{{grid-column:1/-1;text-align:center;padding:3rem;color:var(--muted);font-size:0.8rem;letter-spacing:0.1em;background:var(--surface)}}
.filters{{display:flex;gap:0.5rem;flex-wrap:wrap;margin-bottom:1.5rem}}
.filter-btn{{background:transparent;border:1px solid var(--border);color:var(--muted);padding:0.35rem 0.8rem;font-family:'DM Mono',monospace;font-size:0.63rem;letter-spacing:0.08em;text-transform:uppercase;cursor:pointer;transition:all 0.15s}}
.filter-btn:hover,.filter-btn.active{{border-color:var(--accent);color:var(--accent);background:var(--glow)}}
.divider{{height:2px;background:linear-gradient(90deg,transparent,var(--accent),transparent);margin:2rem 0;opacity:0.3}}
.footer{{border-top:1px solid var(--border);padding:1.5rem 2rem;text-align:center;font-size:0.63rem;color:var(--muted);letter-spacing:0.08em;max-width:1200px;margin:0 auto;line-height:2}}
</style>
</head>
<body>

<header class="masthead">
  <div class="masthead__inner">
    <div>
      <div class="masthead__title">Arch<br>Jobs</div>
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
    <div class="jobs-grid" id="all-grid">{all_cards}</div>
  </section>

</div>

<footer class="footer">
  Auto-updated daily via GitHub Actions · Calgary, Alberta, Canada<br>
  Entry level · Intern · Junior · Graduate · IDP · &lt; 3 years experience
</footer>

<script>
function filterJobs(source, btn) {{
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  document.querySelectorAll('#all-grid .job-card').forEach(card => {{
    card.style.display = (source === 'all' || card.dataset.source === source) ? 'block' : 'none';
  }});
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


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    print(f"\n{'='*55}")
    print(f"Calgary Architecture Job Scraper — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*55}\n")

    seen = load_seen()
    all_jobs_file = Path("all_jobs.json")
    all_jobs: list[dict] = json.loads(all_jobs_file.read_text()) if all_jobs_file.exists() else []
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

    # ── Firm career pages ──
    print("\n[Firms] Scraping Calgary firm career pages ...")
    raw.extend(scrape_firm_careers())

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

    save_seen(seen)
    all_jobs_file.write_text(json.dumps(all_jobs[-500:], indent=2))
    generate_html(new_jobs, all_jobs)
    send_sms(new_jobs)
    print("\n✓ Done!")


if __name__ == "__main__":
    main()
