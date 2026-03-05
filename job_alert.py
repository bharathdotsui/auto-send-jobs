"""
ServiceNow Job Alert System for Bharath Reddy
Checks Dice every hour for new ServiceNow jobs and scores them
against Bharath's resume using Claude AI, then sends email + SMS alerts.
"""

import os
import json
import time
import hashlib
import logging
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from anthropic import Anthropic

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("job_alert.log")],
)
log = logging.getLogger(__name__)

# ─── Configuration (set via environment variables) ────────────────────────────
ANTHROPIC_API_KEY   = os.environ["ANTHROPIC_API_KEY"]
SENDGRID_API_KEY    = os.environ["SENDGRID_API_KEY"]
TWILIO_ACCOUNT_SID  = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_AUTH_TOKEN   = os.environ["TWILIO_AUTH_TOKEN"]
TWILIO_FROM_NUMBER  = os.environ["TWILIO_FROM_NUMBER"]   # e.g. +15005550006
ALERT_TO_EMAIL      = os.environ["ALERT_TO_EMAIL"]        # bharath921r@gmail.com
ALERT_FROM_EMAIL    = os.environ["ALERT_FROM_EMAIL"]      # your verified SendGrid sender
ALERT_TO_PHONE      = os.environ["ALERT_TO_PHONE"]        # e.g. +16782504141

CHECK_INTERVAL_SECS = 3600   # every hour
SEEN_JOBS_FILE      = "seen_jobs.json"

# ─── Bharath's Resume Summary (used for AI scoring) ───────────────────────────
RESUME_SUMMARY = """
Name: Bharath Reddy
Title: Senior ServiceNow Developer
Experience: 9+ years

Current Role: Senior ServiceNow Developer/Admin at Fiserv (Dec 2024–Present), Alpharetta GA
Previous: Senior ServiceNow Developer at JP Morgan Chase (Mar 2023–Nov 2024)
Previous: ServiceNow Admin/Developer at Optum (Jul 2021–Feb 2023)
Previous: ServiceNow Admin/Developer at Lowe's (Jul 2019–Jun 2021)
Previous: ServiceNow Administrator at Carbynetech, India (Mar 2016–Oct 2018)

Core Modules: ITSM (Incident, Problem, Change, Request), ITOM (Event Management, Discovery,
Service Mapping), CMDB & CSDM, ITAM (SAM Pro, HAM Pro), IRM/GRC (Risk, Policy & Compliance,
TPRM), SecOps (Vulnerability Response / SIR), ITBM (SPM/PPM), APM, ESM, CSM, Service Catalog,
Service Portal, Performance Analytics, ATF, App Engine Studio, UI Builder, Agent Workspace.

Scripting: JavaScript, Glide Record/Ajax/Aggregate, Script Includes, Business Rules,
Client Scripts, UI Policies/UI Actions, Flow Designer, IntegrationHub, Scheduled Jobs, ACLs.

Integrations: REST/SOAP, OAuth 2.0, SSO (SAML/OIDC), MID Server, IntegrationHub spokes,
Import Sets/Transform Maps, Azure Logic Apps, SCCM/BigFix, Splunk, Dynatrace, Rapid7/Tenable.

Certifications (10):
- ServiceNow Certified System Administrator (CSA)
- ServiceNow Certified Implementation Specialist (CIS)
- ServiceNow Certified Application Developer (CAD)
- ServiceNow Certified CMDB Health
- ServiceNow Certified Playbooks Advanced
- ServiceNow Certified ATF
- ServiceNow Certified Flow Designer
- ServiceNow Certified IntegrationHub
- ServiceNow Certified App Engine Studio
- ServiceNow Certified UI Builder

Industry Experience: Banking/FinTech (Fiserv payments, JPMC), Healthcare (Optum/HIPAA),
Retail (Lowe's), Staffing/IT services

Location: Atlanta, GA (Alpharetta area)
Education: MS Information Technology – St. Francis; B.Tech Electronics – JNTU Hyderabad
"""

# ─── Job Sources (RSS feeds — no API key needed, no IP blocking) ──────────────
# We pull from multiple sources so you never miss a role.
RSS_FEEDS = [
    # Indeed RSS — most reliable, no auth needed
    {
        "name": "Indeed",
        "url": (
            "https://www.indeed.com/rss?q=ServiceNow+Developer&l=United+States"
            "&sort=date&fromage=1&limit=50"
        ),
    },
    # Indeed — ITOM/CMDB specialist search
    {
        "name": "Indeed-ITOM",
        "url": (
            "https://www.indeed.com/rss?q=ServiceNow+ITOM+CMDB&l=United+States"
            "&sort=date&fromage=1&limit=25"
        ),
    },
    # Indeed — IRM/GRC/SecOps search
    {
        "name": "Indeed-SecOps",
        "url": (
            "https://www.indeed.com/rss?q=ServiceNow+IRM+GRC+SecOps&l=United+States"
            "&sort=date&fromage=1&limit=25"
        ),
    },
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}


def fetch_rss_jobs() -> list[dict]:
    """Fetch jobs from Indeed RSS feeds — reliable, no IP blocking."""
    all_jobs = []
    seen_titles = set()

    for feed in RSS_FEEDS:
        try:
            resp = requests.get(feed["url"], headers=HEADERS, timeout=20)
            resp.raise_for_status()

            root = ET.fromstring(resp.content)
            ns = ""
            items = root.findall(".//item")

            for item in items:
                title   = (item.findtext("title") or "").strip()
                link    = (item.findtext("link") or "").strip()
                company = (item.findtext("source") or "").strip()
                summary = (item.findtext("description") or "").strip()
                pub_date = (item.findtext("pubDate") or "").strip()
                location = ""

                # Indeed puts location in title like "ServiceNow Developer - Atlanta, GA"
                if " - " in title:
                    parts    = title.rsplit(" - ", 1)
                    title    = parts[0].strip()
                    location = parts[1].strip()

                # Deduplicate by title+company
                key = f"{title.lower()}|{company.lower()}"
                if key in seen_titles:
                    continue
                seen_titles.add(key)

                all_jobs.append({
                    "guid":        link,
                    "title":       title,
                    "companyName": company,
                    "jobLocation": {"displayName": location} if location else None,
                    "summary":     summary[:500],
                    "salary":      "See posting",
                    "detailsPageUrl": link,
                    "workplaceTypes": [],
                    "employmentType": "See posting",
                    "postedDate":  pub_date,
                    "source":      feed["name"],
                })

            log.info(f"  [{feed['name']}] fetched {len(items)} jobs")

        except Exception as e:
            log.error(f"  [{feed['name']}] RSS fetch failed: {e}")

    log.info(f"Total jobs fetched across all feeds: {len(all_jobs)}")
    return all_jobs


# ─── Seen Jobs Tracking ────────────────────────────────────────────────────────
def load_seen_jobs() -> set:
    if os.path.exists(SEEN_JOBS_FILE):
        with open(SEEN_JOBS_FILE) as f:
            return set(json.load(f))
    return set()


def save_seen_jobs(seen: set):
    with open(SEEN_JOBS_FILE, "w") as f:
        json.dump(list(seen), f)


def job_id(job: dict) -> str:
    return job.get("guid") or job.get("id") or hashlib.md5(
        job.get("title", "") + job.get("companyName", "")).hexdigest()


# ─── AI Scoring via Claude ────────────────────────────────────────────────────
def score_jobs_with_claude(jobs: list[dict]) -> list[dict]:
    """Use Claude to score each job against Bharath's resume and assign a tier."""
    if not jobs:
        return []

    client = Anthropic(api_key=ANTHROPIC_API_KEY)

    # Build a compact job list for the prompt
    jobs_text = ""
    for i, job in enumerate(jobs):
        jobs_text += f"""
Job #{i+1}
Title: {job.get('title', 'N/A')}
Company: {job.get('companyName', 'N/A')}
Location: {job.get('jobLocation', {}).get('displayName', 'Remote/Unknown') if job.get('jobLocation') else 'Remote/Unknown'}
Type: {job.get('employmentType', 'N/A')}
Salary: {job.get('salary', 'Not specified')}
Workplace: {', '.join(job.get('workplaceTypes') or ['Not specified'])}
Summary: {(job.get('summary') or '')[:400]}
---"""

    prompt = f"""You are an expert technical recruiter evaluating ServiceNow job postings against a candidate's resume.

CANDIDATE RESUME:
{RESUME_SUMMARY}

JOBS TO EVALUATE:
{jobs_text}

For each job, respond ONLY with valid JSON (no extra text, no markdown) in this exact format:
[
  {{
    "job_index": 1,
    "tier": "Tier 1",
    "score": 95,
    "match_reason": "Brief 1-2 sentence explanation of why this matches",
    "flag_clearance": false
  }},
  ...
]

Tier definitions:
- Tier 1 (score 80-100): Near-perfect match, candidate will almost certainly get interview
- Tier 2 (score 60-79): Strong match, very likely to get interview
- Tier 3 (score 40-59): Good match, likely to get interview
- No Match (score 0-39): Poor fit, skip

Set flag_clearance=true if the job requires US citizenship, security clearance, or government clearance.
Evaluate ALL {len(jobs)} jobs."""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=4000,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = response.content[0].text.strip()
        # Strip markdown fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        scores = json.loads(raw.strip())

        # Merge scores back into job dicts
        scored_jobs = []
        for s in scores:
            idx = s["job_index"] - 1
            if 0 <= idx < len(jobs):
                job = jobs[idx].copy()
                job["ai_tier"]          = s.get("tier", "No Match")
                job["ai_score"]         = s.get("score", 0)
                job["ai_match_reason"]  = s.get("match_reason", "")
                job["flag_clearance"]   = s.get("flag_clearance", False)
                scored_jobs.append(job)

        # Filter out poor matches and clearance jobs
        filtered = [j for j in scored_jobs
                    if j["ai_score"] >= 40 and not j["flag_clearance"]]
        log.info(f"AI scored {len(jobs)} jobs → {len(filtered)} matches (Tier 1/2/3)")
        return sorted(filtered, key=lambda x: x["ai_score"], reverse=True)

    except Exception as e:
        log.error(f"Claude scoring failed: {e}")
        return []


# ─── Email Notification (SendGrid) ────────────────────────────────────────────
def send_email(jobs: list[dict]):
    """Send an HTML email summary of matched jobs via SendGrid."""
    tier_emoji = {"Tier 1": "🏆", "Tier 2": "🥈", "Tier 3": "🥉"}

    rows = ""
    for job in jobs:
        emoji   = tier_emoji.get(job["ai_tier"], "📌")
        loc     = job.get("jobLocation", {})
        location = loc.get("displayName", "Remote") if loc else "Remote"
        workplace = ", ".join(job.get("workplaceTypes") or ["N/A"])
        salary   = job.get("salary") or "Not specified"
        url      = job.get("detailsPageUrl", "#")
        rows += f"""
        <tr>
          <td style="padding:12px;border-bottom:1px solid #eee;">
            <strong>{emoji} {job['ai_tier']} (Score: {job['ai_score']}/100)</strong><br>
            <a href="{url}" style="font-size:16px;color:#1a73e8;text-decoration:none;">
              {job.get('title','N/A')}
            </a><br>
            <span style="color:#555;">🏢 {job.get('companyName','N/A')}</span> &nbsp;|&nbsp;
            <span style="color:#555;">📍 {location} ({workplace})</span> &nbsp;|&nbsp;
            <span style="color:#555;">💰 {salary}</span><br>
            <em style="color:#777;font-size:13px;">{job['ai_match_reason']}</em>
          </td>
        </tr>"""

    html = f"""
    <html><body style="font-family:Arial,sans-serif;max-width:700px;margin:auto;">
      <div style="background:#1a73e8;padding:20px;border-radius:8px 8px 0 0;">
        <h2 style="color:white;margin:0;">🔔 New ServiceNow Job Matches Found</h2>
        <p style="color:#dde;margin:4px 0 0;">{datetime.now().strftime('%B %d, %Y at %I:%M %p')}</p>
      </div>
      <div style="background:#f9f9f9;padding:16px;border-radius:0 0 8px 8px;">
        <p>Hi Bharath, <strong>{len(jobs)} new job(s)</strong> matched your ServiceNow profile:</p>
        <table style="width:100%;border-collapse:collapse;background:white;border-radius:6px;overflow:hidden;">
          {rows}
        </table>
        <p style="color:#999;font-size:12px;margin-top:16px;">
          Auto-generated by your ServiceNow Job Alert · Powered by Claude AI + Indeed RSS
        </p>
      </div>
    </body></html>"""

    payload = {
        "personalizations": [{"to": [{"email": ALERT_TO_EMAIL}]}],
        "from": {"email": ALERT_FROM_EMAIL, "name": "ServiceNow Job Alert"},
        "subject": f"🔔 {len(jobs)} New ServiceNow Job Match{'es' if len(jobs)>1 else ''} Found",
        "content": [{"type": "text/html", "value": html}],
    }

    try:
        resp = requests.post(
            "https://api.sendgrid.com/v3/mail/send",
            headers={"Authorization": f"Bearer {SENDGRID_API_KEY}",
                     "Content-Type": "application/json"},
            json=payload, timeout=15
        )
        if resp.status_code in (200, 202):
            log.info(f"✅ Email sent to {ALERT_TO_EMAIL}")
        else:
            log.error(f"Email failed: {resp.status_code} {resp.text}")
    except Exception as e:
        log.error(f"Email error: {e}")


# ─── SMS Notification (Twilio) ────────────────────────────────────────────────
def send_sms(jobs: list[dict]):
    """Send a concise SMS summary via Twilio."""
    tier_emoji = {"Tier 1": "🏆", "Tier 2": "🥈", "Tier 3": "🥉"}
    lines = [f"🔔 {len(jobs)} new ServiceNow job match{'es' if len(jobs)>1 else ''} found!\n"]

    for job in jobs[:5]:   # SMS cap at 5 to keep it readable
        emoji = tier_emoji.get(job["ai_tier"], "📌")
        loc   = job.get("jobLocation", {})
        location = loc.get("displayName", "Remote") if loc else "Remote"
        salary = job.get("salary") or "N/A"
        lines.append(
            f"{emoji} {job.get('title','N/A')} @ {job.get('companyName','N/A')}\n"
            f"   {location} | {salary}\n"
            f"   {job.get('detailsPageUrl','')}\n"
        )

    if len(jobs) > 5:
        lines.append(f"...and {len(jobs)-5} more. Check your email for full list.")

    body = "\n".join(lines)

    try:
        resp = requests.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Messages.json",
            auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
            data={"From": TWILIO_FROM_NUMBER, "To": ALERT_TO_PHONE, "Body": body},
            timeout=15
        )
        data = resp.json()
        if data.get("sid"):
            log.info(f"✅ SMS sent to {ALERT_TO_PHONE}")
        else:
            log.error(f"SMS failed: {data}")
    except Exception as e:
        log.error(f"SMS error: {e}")


# ─── Main Loop ────────────────────────────────────────────────────────────────
def run():
    log.info("🚀 ServiceNow Job Alert started — checking Indeed RSS every hour")
    seen_jobs = load_seen_jobs()

    while True:
        log.info(f"🔍 Checking Indeed RSS feeds for new ServiceNow jobs...")

        jobs       = fetch_rss_jobs()
        new_jobs   = [j for j in jobs if job_id(j) not in seen_jobs]
        log.info(f"Found {len(new_jobs)} new (unseen) jobs out of {len(jobs)} total")

        if new_jobs:
            matched = score_jobs_with_claude(new_jobs)

            if matched:
                log.info(f"📬 Sending alerts for {len(matched)} matched jobs")
                send_email(matched)
                send_sms(matched)
            else:
                log.info("No qualifying matches this cycle — no alert sent")

            # Mark ALL new jobs as seen (even non-matches) to avoid re-processing
            for j in new_jobs:
                seen_jobs.add(job_id(j))
            save_seen_jobs(seen_jobs)
        else:
            log.info("No new jobs since last check")

        log.info(f"💤 Sleeping for {CHECK_INTERVAL_SECS // 60} minutes...\n")
        time.sleep(CHECK_INTERVAL_SECS)


if __name__ == "__main__":
    run()
