"""
ServiceNow Job Alert System for Bharath Reddy
Uses Claude AI + Web Search to find and score new ServiceNow jobs every hour.
Sends Email (SendGrid) + SMS (Twilio) for all matches.

No scraping. No API keys for job sites. Claude searches the web directly.
"""

import os
import json
import time
import hashlib
import logging
import requests
from datetime import datetime

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("job_alert.log")],
)
log = logging.getLogger(__name__)

# ─── Configuration (Railway environment variables) ────────────────────────────
ANTHROPIC_API_KEY  = os.environ["ANTHROPIC_API_KEY"]
SENDGRID_API_KEY   = os.environ["SENDGRID_API_KEY"]
TWILIO_ACCOUNT_SID = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_AUTH_TOKEN  = os.environ["TWILIO_AUTH_TOKEN"]
TWILIO_FROM_NUMBER = os.environ["TWILIO_FROM_NUMBER"]
ALERT_TO_EMAIL     = os.environ["ALERT_TO_EMAIL"]
ALERT_FROM_EMAIL   = os.environ["ALERT_FROM_EMAIL"]
ALERT_TO_PHONE     = os.environ["ALERT_TO_PHONE"]

CHECK_INTERVAL_SECS = 3600  # every hour
SEEN_JOBS_FILE      = "seen_jobs.json"

# ─── Bharath's Resume (for AI matching) ───────────────────────────────────────
RESUME_SUMMARY = """
Name: Bharath Reddy | Senior ServiceNow Developer | 9+ years experience
Location: Atlanta, GA (Alpharetta)

EMPLOYERS:
- Fiserv, Alpharetta GA (Dec 2024-Present) — Sr. ServiceNow Developer/Admin
- JP Morgan Chase, Atlanta GA (Mar 2023–Nov 2024) — Sr. ServiceNow Developer
- Optum, Chicago (Jul 2021–Feb 2023) — ServiceNow Admin/Developer
- Lowe's, Mooresville NC (Jul 2019–Jun 2021) — ServiceNow Admin/Developer
- Carbynetech, India (Mar 2016–Oct 2018) — ServiceNow Administrator

CORE SKILLS:
- ITSM: Incident, Problem, Change, Request, SLA, Escalations
- ITOM: Event Management, Discovery, Service Mapping, MID Server
- CMDB & CSDM alignment, CI modeling, relationship mapping
- ITAM: SAM Pro, HAM Pro, SCCM/BigFix integrations
- IRM/GRC: Risk, Policy & Compliance, TPRM, audit workflows
- SecOps: Vulnerability Response, SIR, Rapid7/Tenable/Splunk/Dynatrace
- ITBM/SPM: Demand, PPM, Portfolio Management
- Scripting: JavaScript, GlideRecord/Ajax, Business Rules, Client Scripts,
  UI Policies, Script Includes, Flow Designer, IntegrationHub
- Integrations: REST/SOAP, OAuth 2.0, SSO/SAML, Azure Logic Apps
- App Engine Studio, UI Builder, Service Portal, Agent Workspace, ATF, PA

CERTIFICATIONS (10): CSA, CIS, CAD, CMDB Health, Playbooks Advanced,
ATF, Flow Designer, IntegrationHub, App Engine Studio, UI Builder

INDUSTRY: Banking/FinTech (Fiserv payments, JPMC), Healthcare (Optum/HIPAA), Retail (Lowe's)
"""

# ─── Seen Jobs Tracking ───────────────────────────────────────────────────────
def load_seen_jobs() -> set:
    if os.path.exists(SEEN_JOBS_FILE):
        with open(SEEN_JOBS_FILE) as f:
            return set(json.load(f))
    return set()


def save_seen_jobs(seen: set):
    with open(SEEN_JOBS_FILE, "w") as f:
        json.dump(list(seen), f)


def make_job_id(job: dict) -> str:
    key = f"{job.get('title','').lower()}|{job.get('company','').lower()}|{job.get('url','')}"
    return hashlib.md5(key.encode()).hexdigest()


# ─── Claude Web Search — Find & Score Jobs ────────────────────────────────────
def find_and_score_jobs() -> list[dict]:
    """
    Ask Claude to search the web for new ServiceNow jobs posted today,
    then score each one against Bharath's resume. Returns structured list.
    """
    today = datetime.now().strftime('%B %d, %Y')
    prompt = f"""Today is {today}. You MUST use the web_search tool multiple times to find real job postings.

Do NOT skip the searches. Do NOT return [] without searching first.

Run ALL of these searches right now using your web_search tool:
1. Search for: ServiceNow Developer jobs {today}
2. Search for: ServiceNow Developer contract remote jobs 2026
3. Search for: ServiceNow ITOM Discovery CMDB jobs hiring now
4. Search for: ServiceNow IRM GRC developer jobs
5. Search for: ServiceNow Vulnerability Response SecOps jobs

After searching, collect every real job posting URL you find from dice.com, indeed.com, linkedin.com, ziprecruiter.com, glassdoor.com, or any job board.

Then evaluate each job against this candidate profile:
{RESUME_SUMMARY}

Return ONLY a raw JSON array (no markdown, no code fences, no explanation):
[
  {{
    "title": "Exact job title",
    "company": "Company name",
    "location": "City, State or Remote",
    "salary": "$XX/hr or $XXXk/yr or Not listed",
    "job_type": "Contract or Full-time",
    "url": "Full job posting URL",
    "score": 85,
    "tier": "Tier 1",
    "match_reason": "Why this matches Bharath's ITOM/CMDB/IRM/SecOps skills specifically",
    "requires_clearance": false
  }}
]

Scoring:
- Tier 1 (80-100): Near-perfect match
- Tier 2 (60-79): Strong match
- Tier 3 (40-59): Good match
- Skip jobs below score 40 or requiring clearance/citizenship

You MUST search first before responding. Include every job you find that scores 40+."""

    def _call_claude(use_web_search: bool) -> requests.Response:
        body = {
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 4000,
            "messages": [{"role": "user", "content": prompt}],
        }
        if use_web_search:
            body["tools"] = [{"type": "web_search_20250305", "name": "web_search"}]
            body["tool_choice"] = {"type": "auto"}  # Claude decides when to search
        return requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json=body,
            timeout=120,
        )

    try:
        # Try with web search first; fall back to plain Claude if tool not available
        resp = _call_claude(use_web_search=True)
        if resp.status_code == 400:
            err = resp.json().get("error", {})
            log.warning(f"Web search tool error ({err.get('type')}): {err.get('message')} — retrying without web search")
            resp = _call_claude(use_web_search=False)

        if resp.status_code != 200:
            err_body = resp.text
            try:
                err_body = resp.json().get("error", {}).get("message", resp.text)
            except Exception:
                pass
            log.error(f"Anthropic API error {resp.status_code}: {err_body}")
            if resp.status_code == 401:
                log.error("👉 Fix: ANTHROPIC_API_KEY is invalid or missing in Railway Variables")
            elif resp.status_code == 403:
                log.error("👉 Fix: Check your Anthropic account has API access and billing set up at console.anthropic.com")
            elif resp.status_code == 429:
                log.error("👉 Fix: Anthropic rate limit hit — will retry next cycle")
            return []
        data = resp.json()

        # Extract final text block (comes after tool_use blocks)
        full_text = ""
        tool_uses = 0
        for block in data.get("content", []):
            if block.get("type") == "text":
                full_text += block.get("text", "")
            elif block.get("type") == "tool_use":
                tool_uses += 1

        log.info(f"Claude used web_search {tool_uses} time(s)")

        if not full_text.strip():
            log.warning("Claude returned no text — possibly only tool_use blocks")
            log.warning(f"Full response content types: {[b.get('type') for b in data.get('content', [])]}")
            return []

        log.info(f"Claude raw response (first 300 chars): {full_text[:300]}")

        # Find JSON array in response
        text  = full_text.strip()
        start = text.find("[")
        end   = text.rfind("]") + 1
        if start == -1 or end == 0:
            log.warning(f"No JSON array in response: {text[:300]}")
            return []

        jobs = json.loads(text[start:end])
        log.info(f"Claude web search found {len(jobs)} matching jobs")
        return [j for j in jobs if not j.get("requires_clearance", False)]

    except json.JSONDecodeError as e:
        log.error(f"JSON parse error: {e}")
        return []
    except Exception as e:
        log.error(f"Claude web search error: {e}")
        return []


# ─── Email via SendGrid ────────────────────────────────────────────────────────
def send_email(jobs: list[dict]):
    tier_emoji = {"Tier 1": "🏆", "Tier 2": "🥈", "Tier 3": "🥉"}
    rows = ""
    for job in jobs:
        emoji    = tier_emoji.get(job.get("tier", ""), "📌")
        url      = job.get("url", "#")
        salary   = job.get("salary") or "Not listed"
        location = job.get("location") or "Not listed"
        job_type = job.get("job_type") or ""
        rows += f"""
        <tr>
          <td style="padding:14px;border-bottom:1px solid #eee;">
            <div style="margin-bottom:4px;">
              <strong>{emoji} {job.get('tier','')} &nbsp;·&nbsp; Score: {job.get('score','')}/100</strong>
            </div>
            <a href="{url}" style="font-size:16px;font-weight:bold;color:#1a73e8;text-decoration:none;">
              {job.get('title','N/A')}
            </a><br>
            <span style="color:#444;">🏢 {job.get('company','N/A')}</span>
            &nbsp;·&nbsp;
            <span style="color:#444;">📍 {location}</span>
            &nbsp;·&nbsp;
            <span style="color:#444;">💰 {salary}</span>
            &nbsp;·&nbsp;
            <span style="color:#666;">{job_type}</span><br>
            <em style="color:#777;font-size:13px;margin-top:4px;display:block;">
              {job.get('match_reason','')}
            </em>
          </td>
        </tr>"""

    html = f"""
    <html><body style="font-family:Arial,sans-serif;max-width:700px;margin:auto;padding:16px;">
      <div style="background:linear-gradient(135deg,#1a73e8,#0d47a1);padding:24px;border-radius:10px 10px 0 0;">
        <h2 style="color:white;margin:0;font-size:22px;">🔔 New ServiceNow Job Matches</h2>
        <p style="color:#cce;margin:6px 0 0;font-size:14px;">
          {datetime.now().strftime('%B %d, %Y at %I:%M %p')} &nbsp;·&nbsp;
          {len(jobs)} new match{'es' if len(jobs)>1 else ''}
        </p>
      </div>
      <div style="background:#f9f9f9;padding:20px;border-radius:0 0 10px 10px;border:1px solid #eee;">
        <p style="margin-top:0;">Hi Bharath 👋 — here are today's top ServiceNow roles:</p>
        <table style="width:100%;border-collapse:collapse;background:white;
                      border-radius:8px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,0.08);">
          {rows}
        </table>
        <p style="color:#aaa;font-size:11px;margin-top:16px;text-align:center;">
          ServiceNow Job Alert · Powered by Claude AI Web Search · Every hour
        </p>
      </div>
    </body></html>"""

    payload = {
        "personalizations": [{"to": [{"email": ALERT_TO_EMAIL}]}],
        "from": {"email": ALERT_FROM_EMAIL, "name": "ServiceNow Job Alert"},
        "subject": (
            f"🔔 {len(jobs)} New ServiceNow Job Match{'es' if len(jobs)>1 else ''}"
            f" — {datetime.now().strftime('%b %d, %I:%M %p')}"
        ),
        "content": [{"type": "text/html", "value": html}],
    }
    try:
        r = requests.post(
            "https://api.sendgrid.com/v3/mail/send",
            headers={"Authorization": f"Bearer {SENDGRID_API_KEY}",
                     "Content-Type": "application/json"},
            json=payload, timeout=15,
        )
        if r.status_code in (200, 202):
            log.info(f"✅ Email sent to {ALERT_TO_EMAIL}")
        else:
            log.error(f"Email failed {r.status_code}: {r.text}")
    except Exception as e:
        log.error(f"Email error: {e}")


# ─── SMS via Twilio ───────────────────────────────────────────────────────────
def send_sms(jobs: list[dict]):
    tier_emoji = {"Tier 1": "🏆", "Tier 2": "🥈", "Tier 3": "🥉"}
    lines = [f"🔔 {len(jobs)} new ServiceNow match{'es' if len(jobs)>1 else ''} found!\n"]
    for job in jobs[:4]:
        emoji = tier_emoji.get(job.get("tier", ""), "📌")
        lines.append(
            f"{emoji} {job.get('title','N/A')} @ {job.get('company','N/A')}\n"
            f"   {job.get('location','')} | {job.get('salary','')}\n"
            f"   {job.get('url','')}\n"
        )
    if len(jobs) > 4:
        lines.append(f"...+{len(jobs)-4} more. Check your email!")

    try:
        r = requests.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Messages.json",
            auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
            data={"From": TWILIO_FROM_NUMBER, "To": ALERT_TO_PHONE, "Body": "\n".join(lines)},
            timeout=15,
        )
        d = r.json()
        if d.get("sid"):
            log.info(f"✅ SMS sent to {ALERT_TO_PHONE}")
        else:
            log.error(f"SMS failed: {d.get('message', d)}")
    except Exception as e:
        log.error(f"SMS error: {e}")


# ─── Main Loop ────────────────────────────────────────────────────────────────
def run():
    log.info("🚀 ServiceNow Job Alert started — Claude AI web search every hour")
    seen_jobs = load_seen_jobs()

    while True:
        log.info("🔍 Asking Claude to search for new ServiceNow jobs...")
        try:
            jobs     = find_and_score_jobs()
            new_jobs = [j for j in jobs if make_job_id(j) not in seen_jobs]
            log.info(f"{len(new_jobs)} new jobs (of {len(jobs)} found total)")

            if new_jobs:
                log.info(f"📬 Sending alerts for {len(new_jobs)} jobs")
                send_email(new_jobs)
                send_sms(new_jobs)
                for j in new_jobs:
                    seen_jobs.add(make_job_id(j))
                save_seen_jobs(seen_jobs)
            else:
                log.info("No new matches this cycle — no alert sent")

        except Exception as e:
            log.error(f"Unexpected error: {e}")

        log.info(f"💤 Sleeping {CHECK_INTERVAL_SECS // 60} min...\n")
        time.sleep(CHECK_INTERVAL_SECS)


if __name__ == "__main__":
    run()
