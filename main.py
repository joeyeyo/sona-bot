from fastapi import FastAPI, Form, Request
from fastapi.responses import PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse
import anthropic
import os
import json
import httpx
import re
import base64
from datetime import datetime
from statistics import mean
from urllib.parse import quote_plus, unquote, urlparse
from email.mime.text import MIMEText

app = FastAPI()

# ── CORS ──────────────────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://sona-frontend-lyart.vercel.app", "http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── env vars ──────────────────────────────────────────────────────────────────
twilio_client = Client(
    os.environ["TWILIO_ACCOUNT_SID"],
    os.environ["TWILIO_AUTH_TOKEN"],
)
anthropic_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

TWILIO_WHATSAPP_NUMBER = os.environ.get("TWILIO_WHATSAPP_NUMBER", "whatsapp:+14155238886")
YELP_API_KEY = os.environ.get("YELP_API_KEY", "")
SERP_API_KEY = os.environ.get("SERP_API_KEY", "")
HUNTER_API_KEY = os.environ.get("HUNTER_API_KEY", "")
APOLLO_API_KEY = os.environ.get("APOLLO_API_KEY", "")
GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "")
GMAIL_CLIENT_ID = os.environ.get("GMAIL_CLIENT_ID", "")
GMAIL_CLIENT_SECRET = os.environ.get("GMAIL_CLIENT_SECRET", "")
GMAIL_REFRESH_TOKEN = os.environ.get("GMAIL_REFRESH_TOKEN", "")
GMAIL_SENDER = os.environ.get("GMAIL_SENDER", "yeh.joseph@gmail.com")

# ── in-memory store ───────────────────────────────────────────────────────────
conversations: dict = {}

# ── Sona system prompt ────────────────────────────────────────────────────────
SONA_SYSTEM_PROMPT = """You are Sona, a warm and perceptive networking concierge. You help people meet other people who are genuinely relevant to them — professionally and personally. You work for a company that hosts in-person events where members can meet face-to-face.

Your personality:
- Warm, curious, and a little playful — like a smart friend who happens to know everyone
- You ask ONE question at a time, never overwhelm people with forms
- You remember everything someone tells you and reference it naturally
- You're honest if you don't have a match yet — you say "I'm keeping an eye out for you"

Current date: """ + datetime.now().strftime("%B %d, %Y") + """

Keep responses SHORT — this is WhatsApp, not email. 2-4 sentences max per message. No bullet points or markdown formatting. Just natural, conversational text."""


# ── helpers ───────────────────────────────────────────────────────────────────
def get_or_create_session(phone: str) -> dict:
    if phone not in conversations:
        conversations[phone] = {
            "messages": [],
            "profile": {},
            "joined_at": datetime.now().isoformat(),
        }
    return conversations[phone]


def chat_with_sona(phone: str, user_message: str) -> str:
    session = get_or_create_session(phone)
    session["messages"].append({"role": "user", "content": user_message})
    response = anthropic_client.messages.create(
        model="claude-opus-4-5",
        max_tokens=500,
        system=SONA_SYSTEM_PROMPT,
        messages=session["messages"],
    )
    reply = response.content[0].text
    session["messages"].append({"role": "assistant", "content": reply})
    return reply


def send_whatsapp_message(to: str, body: str):
    twilio_client.messages.create(
        from_=TWILIO_WHATSAPP_NUMBER,
        body=body,
        to=to,
    )


# ── Gmail ─────────────────────────────────────────────────────────────────────
async def get_gmail_access_token() -> str:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": GMAIL_CLIENT_ID,
                "client_secret": GMAIL_CLIENT_SECRET,
                "refresh_token": GMAIL_REFRESH_TOKEN,
                "grant_type": "refresh_token",
            },
        )
        data = resp.json()
        if "access_token" not in data:
            raise Exception(f"Failed to get access token: {data}")
        return data["access_token"]


async def send_gmail(to: str, subject: str, body: str) -> dict:
    access_token = await get_gmail_access_token()
    msg = MIMEText(body, "plain")
    msg["To"] = to
    msg["From"] = GMAIL_SENDER
    msg["Subject"] = subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            json={"raw": raw},
        )
    if resp.status_code == 200:
        return {"status": "sent", "message_id": resp.json().get("id")}
    raise Exception(f"Gmail send failed: {resp.status_code} {resp.text}")


# ── vendor ranking ────────────────────────────────────────────────────────────
def bayesian_score(rating: float, review_count: int, mean_rating: float, C: int = 50) -> float:
    return (C * mean_rating + rating * review_count) / (C + review_count)


def rank_vendors(vendors: list, search_params: dict) -> list:
    if not vendors:
        return vendors
    ratings = [v.get("rating", 0) for v in vendors if v.get("rating")]
    mean_rating = mean(ratings) if ratings else 4.5
    keyword = search_params.get("keywords", "").lower()
    price_boosts = {"$": 1.0, "$$": 0.9, "$$$": 0.7, "$$$$": 0.5}
    for v in vendors:
        b_score = bayesian_score(v.get("rating", 0), v.get("review_count", 0), mean_rating)
        price_boost = price_boosts.get(v.get("price", "N/A"), 0.85)
        keyword_boost = 1.1 if keyword and keyword in v.get("name", "").lower() else 1.0
        n = v.get("review_count", 0)
        review_boost = 1.05 if n >= 200 else (1.02 if n >= 100 else 1.0)
        email_boost = 1.05 if v.get("email") else 1.0
        v["score"] = round(b_score * price_boost * keyword_boost * review_boost * email_boost, 4)
    return sorted(vendors, key=lambda v: v["score"], reverse=True)


# ── email extraction ──────────────────────────────────────────────────────────
EMAIL_PATTERN = re.compile(r'\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b')

IGNORE_DOMAINS = {
    "sentry.io", "mapquest.com", "doubleclick.net", "googletagmanager.com",
    "googleapis.com", "gstatic.com", "cloudflare.com", "akamai.com",
    "newrelic.com", "segment.com", "mixpanel.com", "amplitude.com",
    "intercom.io", "zendesk.com", "hubspot.com", "mailchimp.com",
    "wixpress.com", "squarespace.com", "wordpress.com", "shopify.com",
    "weebly.com", "webflow.io", "godaddy.com",
    "example.com", "yourdomain.com", "email.com", "domain.com",
    "yoursite.com", "company.com",
    "google.com", "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "icloud.com", "me.com", "mac.com", "live.com",
    "yelp.com", "facebook.com", "instagram.com", "twitter.com",
    "tiktok.com", "linkedin.com", "pinterest.com",
    "doordash.com", "grubhub.com", "ubereats.com", "tripadvisor.com",
    "opentable.com", "resy.com", "thumbtack.com",
    "duckduckgo.com", "bing.com",
    "giftly.com", "giggster.com", "heathercoros.com",
    "pandaexpressed.com",
    "png", "jpg", "jpeg", "gif", "webp", "svg", "pdf", "css", "js",
}

PRIORITY_LOCAL_PARTS = ["contact", "info", "hello", "events", "booking", "catering", "hire", "enquir", "inquir"]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

SKIP_DOMAINS = {
    "yelp.com", "facebook.com", "instagram.com", "twitter.com",
    "doordash.com", "grubhub.com", "ubereats.com", "tripadvisor.com",
    "yellowpages.com", "bbb.org", "google.com", "yelp.to",
    "duckduckgo.com", "bing.com", "zoominfo.com", "manta.com",
    "chamberofcommerce.com", "mapquest.com", "giftly.com", "giggster.com",
}


def extract_emails_from_text(text: str) -> list:
    text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'\[at\]|\(at\)| at ', '@', text, flags=re.IGNORECASE)
    text = re.sub(r'\[dot\]|\(dot\)| dot ', '.', text, flags=re.IGNORECASE)

    mailto = re.findall(r'mailto:([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})', text)
    plain = EMAIL_PATTERN.findall(text)
    all_emails = mailto + plain

    clean = []
    seen = set()
    for email in all_emails:
        email = email.lower().strip().rstrip(".,;")
        # Strip www. from email domain (e.g. catering@www.site.com → catering@site.com)
        parts = email.split("@")
        if len(parts) == 2 and parts[1].startswith("www."):
            email = parts[0] + "@" + parts[1][4:]
        domain = email.split("@")[-1]
        if domain in IGNORE_DOMAINS:
            continue
        if any(ignored in domain for ignored in IGNORE_DOMAINS if "." in ignored):
            continue
        if email in seen or len(email) > 80:
            continue
        seen.add(email)
        clean.append(email)

    def priority(e):
        local = e.split("@")[0]
        return 0 if any(kw in local for kw in PRIORITY_LOCAL_PARTS) else 1

    return sorted(clean, key=priority)


def email_matches_website(email: str, website: str) -> bool:
    """Email domain must match the vendor's confirmed website domain."""
    if not email or not website:
        return False
    email_domain = email.split("@")[-1].replace("www.", "")
    site_domain = urlparse(website).netloc.replace("www.", "")
    return email_domain in site_domain or site_domain in email_domain


def is_valid_email(email: str, website: str | None) -> bool:
    """
    Strict validation: only accept an email if its domain matches
    the vendor's confirmed website. No website = no email from snippets.
    This eliminates false positives from unrelated businesses in search results.
    """
    if not email or not website:
        return False
    return email_matches_website(email, website)


def get_website_from_results(results: list) -> str | None:
    for result in results:
        link = result.get("link", "")
        domain = urlparse(link).netloc.replace("www.", "")
        if link and not any(skip in domain for skip in SKIP_DOMAINS):
            return link.split("?")[0].rstrip("/")
    return None


# ── Apollo.io ─────────────────────────────────────────────────────────────────
async def apollo_find_email(vendor_name: str, address: str) -> tuple:
    if not APOLLO_API_KEY:
        return None, None
    city = address.split(",")[-2].strip() if "," in address else ""
    state = address.split(",")[-1].strip().split()[0] if "," in address else ""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                "https://api.apollo.io/v1/mixed_companies/search",
                headers={"Content-Type": "application/json", "Cache-Control": "no-cache", "X-Api-Key": APOLLO_API_KEY},
                json={
                    "q_organization_name": vendor_name,
                    "organization_locations": [f"{city}, {state}"] if city else [],
                    "page": 1, "per_page": 3,
                },
            )
            if resp.status_code == 403:
                return None, None
            if resp.status_code != 200:
                return None, None

            organizations = resp.json().get("organizations", [])
            if not organizations:
                return None, None

            best_org = None
            for org in organizations:
                org_name = org.get("name", "").lower()
                vendor_words = [w for w in vendor_name.lower().split() if len(w) > 3]
                if any(w in org_name for w in vendor_words):
                    best_org = org
                    break
            if not best_org:
                best_org = organizations[0]

            website = best_org.get("website_url") or best_org.get("primary_domain")
            if website and not website.startswith("http"):
                website = f"https://{website}"

            email = None
            if best_org.get("contact_emails"):
                email = best_org["contact_emails"][0]

            if not email and best_org.get("id"):
                people_resp = await client.post(
                    "https://api.apollo.io/v1/mixed_people/search",
                    headers={"Content-Type": "application/json", "Cache-Control": "no-cache", "X-Api-Key": APOLLO_API_KEY},
                    json={
                        "organization_ids": [best_org["id"]],
                        "page": 1, "per_page": 5,
                        "person_titles": ["owner", "manager", "catering manager", "event coordinator", "director"],
                    },
                )
                if people_resp.status_code == 200:
                    for person in people_resp.json().get("people", []):
                        person_email = person.get("email")
                        if person_email and "@" in person_email and "apollo" not in person_email:
                            email = person_email
                            break

            if email or website:
                print(f"[apollo] {vendor_name} → email: {email}, website: {website}")
            return email, website
    except Exception as e:
        print(f"[apollo error] {vendor_name}: {e}")
        return None, None


# ── Google Maps Places ────────────────────────────────────────────────────────
async def google_maps_find_website(vendor_name: str, address: str) -> str | None:
    if not GOOGLE_MAPS_API_KEY:
        return None
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            search_resp = await client.get(
                "https://maps.googleapis.com/maps/api/place/textsearch/json",
                params={"query": f"{vendor_name} {address}", "key": GOOGLE_MAPS_API_KEY},
            )
            if search_resp.status_code != 200:
                return None
            results = search_resp.json().get("results", [])
            if not results:
                return None
            place_id = results[0].get("place_id")
            if not place_id:
                return None
            details_resp = await client.get(
                "https://maps.googleapis.com/maps/api/place/details/json",
                params={"place_id": place_id, "fields": "website,name", "key": GOOGLE_MAPS_API_KEY},
            )
            if details_resp.status_code != 200:
                return None
            website = details_resp.json().get("result", {}).get("website")
            if website:
                print(f"[gmaps] {vendor_name}: {website}")
                return website.rstrip("/")
    except Exception as e:
        print(f"[gmaps error] {vendor_name}: {e}")
    return None


# ── SerpAPI ───────────────────────────────────────────────────────────────────
async def serp_search(query: str) -> dict:
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            "https://serpapi.com/search",
            params={"q": query, "api_key": SERP_API_KEY, "engine": "google", "num": 5},
        )
        if resp.status_code == 200:
            return resp.json()
    return {}


async def serp_yelp_search(vendor_name: str, city: str) -> dict:
    if not SERP_API_KEY:
        return {}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://serpapi.com/search",
                params={"engine": "yelp", "find_desc": vendor_name, "find_loc": city, "api_key": SERP_API_KEY},
            )
            if resp.status_code == 200:
                return resp.json()
    except Exception as e:
        print(f"[serp-yelp error] {e}")
    return {}


# ── Hunter.io ─────────────────────────────────────────────────────────────────
async def hunter_find_email(domain: str) -> str | None:
    if not HUNTER_API_KEY or not domain:
        return None
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(
                "https://api.hunter.io/v2/domain-search",
                params={"domain": domain, "api_key": HUNTER_API_KEY, "limit": 5, "type": "generic"},
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
            emails = data.get("data", {}).get("emails", [])
            if not emails:
                pattern = data.get("data", {}).get("pattern")
                if pattern:
                    for prefix in ["info", "contact", "hello", "events"]:
                        candidate = f"{prefix}@{domain}"
                        if candidate.split("@")[-1] not in IGNORE_DOMAINS:
                            return candidate
                return None

            def email_priority(e):
                local = e.get("value", "").split("@")[0]
                score = e.get("confidence", 0)
                if any(kw in local for kw in PRIORITY_LOCAL_PARTS):
                    score += 50
                return score

            emails_sorted = sorted(emails, key=email_priority, reverse=True)
            best = emails_sorted[0].get("value")
            # Make sure Hunter result isn't from ignore list
            if best and best.split("@")[-1] in IGNORE_DOMAINS:
                return None
            print(f"[hunter] {best} for {domain}")
            return best
    except Exception as e:
        print(f"[hunter error] {domain}: {e}")
        return None


async def scrape_email_from_website(website_url: str) -> str | None:
    if not website_url:
        return None
    base = website_url.rstrip("/")
    pages_to_check = [base, f"{base}/contact", f"{base}/contact-us", f"{base}/about"]
    async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
        for page_url in pages_to_check:
            try:
                resp = await client.get(page_url, headers=HEADERS)
                if resp.status_code == 200:
                    emails = extract_emails_from_text(resp.text)
                    if emails:
                        return emails[0]
            except Exception:
                continue
    return None


# ── main email finder ─────────────────────────────────────────────────────────
async def find_vendor_email(vendor_name: str, address: str) -> tuple:
    """
    Pipeline focused on finding the vendor's WEBSITE first,
    then only accepting emails that match that website's domain.
    This eliminates false positives from unrelated businesses in search results.

    Phase 1 — Find website:
      1. Apollo.io
      2. Google Maps Places API
      3. SerpAPI Yelp engine
      4. SerpAPI Google

    Phase 2 — Find email from confirmed website:
      5. Scrape website pages
      6. Hunter.io domain search
      7. SerpAPI Google snippets (only if email matches website)
      8. DuckDuckGo (only if email matches website)
    """
    city = address.split(",")[-2].strip() if "," in address else ""
    website = None

    # ── Phase 1: Find website ─────────────────────────────────────────────────

    # 1. Apollo
    apollo_email, apollo_website = await apollo_find_email(vendor_name, address)
    if apollo_website:
        website = apollo_website
    if apollo_email and is_valid_email(apollo_email, website):
        return apollo_email, website

    # 2. Google Maps
    if not website:
        website = await google_maps_find_website(vendor_name, address)

    # 3. SerpAPI Yelp
    if not website:
        yelp_data = await serp_yelp_search(vendor_name, city)
        for result in yelp_data.get("organic_results", []):
            result_name = result.get("name", "").lower()
            vendor_words = [w for w in vendor_name.lower().split() if len(w) > 3]
            if any(w in result_name for w in vendor_words):
                if result.get("website"):
                    website = result["website"].rstrip("/")
                    break

    # 4. SerpAPI Google for website
    if not website:
        serp_site = await serp_search(f'"{vendor_name}" {city} official website catering')
        website = get_website_from_results(serp_site.get("organic_results", []))

    # ── Phase 2: Find email from confirmed website ────────────────────────────

    if not website:
        print(f"[email] No website found for {vendor_name} — skipping email search")
        return None, None

    # 5. Scrape website
    email = await scrape_email_from_website(website)
    if email and is_valid_email(email, website):
        print(f"[scrape] {vendor_name}: {email}")
        return email, website
    elif email:
        print(f"[scrape-fail] {email} doesn't match {website}")

    # 6. Hunter.io
    domain = urlparse(website).netloc.replace("www.", "")
    if domain:
        email = await hunter_find_email(domain)
        if email and is_valid_email(email, website):
            return email, website
        elif email:
            print(f"[hunter-fail] {email} doesn't match {website}")

    # 7. SerpAPI Google snippets — only accept if matches website
    serp_data = await serp_search(f'"{vendor_name}" {city} email contact')
    for result in serp_data.get("organic_results", []):
        for email in extract_emails_from_text(result.get("snippet", "") + " " + result.get("link", "")):
            if is_valid_email(email, website):
                print(f"[serp-snippet] {vendor_name}: {email}")
                return email, website
            else:
                print(f"[serp-snippet-fail] {email} rejected (no website match)")

    # 8. DuckDuckGo — only accept if matches website
    try:
        ddg_url = f"https://html.duckduckgo.com/html/?q={quote_plus(f'{vendor_name} {city} email contact catering')}"
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
            resp = await client.get(ddg_url, headers={**HEADERS, "Referer": "https://duckduckgo.com/"})
            if resp.status_code == 200:
                snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', resp.text, re.DOTALL)
                for snippet in snippets:
                    for email in extract_emails_from_text(re.sub(r'<[^>]+>', '', snippet)):
                        if is_valid_email(email, website):
                            print(f"[ddg] {vendor_name}: {email}")
                            return email, website
                        else:
                            print(f"[ddg-fail] {email} rejected (no website match)")
    except Exception as e:
        print(f"[ddg error] {e}")

    print(f"[email] No email found for {vendor_name} (website: {website})")
    return None, website


# ── webhook ───────────────────────────────────────────────────────────────────
@app.post("/webhook", response_class=PlainTextResponse)
async def webhook(From: str = Form(...), Body: str = Form(...)):
    phone = From
    user_message = Body.strip()
    print(f"[{phone}] → {user_message}")
    reply = chat_with_sona(phone, user_message)
    print(f"[Sona → {phone}] {reply}")
    resp = MessagingResponse()
    resp.message(reply)
    return str(resp)


# ── vendor search ─────────────────────────────────────────────────────────────
@app.post("/search-vendors")
async def search_vendors(payload: dict):
    category = payload.get("category", "caterers")
    location = payload.get("location", "Los Angeles, CA")
    budget = payload.get("budget")
    guest_count = payload.get("guest_count")
    min_rating = payload.get("min_rating", 4.0)
    keywords = payload.get("keywords", "")
    scrape_emails = payload.get("scrape_emails", True)

    search_term = f"{keywords} {category}".strip()

    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(
            "https://api.yelp.com/v3/businesses/search",
            headers={"Authorization": f"Bearer {YELP_API_KEY}"},
            params={"term": search_term, "location": location, "limit": 20, "sort_by": "rating"},
        )

    if response.status_code != 200:
        return {"error": "Yelp API error", "detail": response.text}

    businesses = response.json().get("businesses", [])
    filtered = [b for b in businesses if b.get("rating", 0) >= min_rating]

    vendors = []
    for b in filtered:
        vendors.append({
            "id": b.get("id"),
            "name": b.get("name"),
            "rating": b.get("rating"),
            "review_count": b.get("review_count"),
            "price": b.get("price", "N/A"),
            "phone": b.get("display_phone", "N/A"),
            "address": ", ".join(b.get("location", {}).get("display_address", [])),
            "url": b.get("url"),
            "image_url": b.get("image_url"),
            "categories": [c["title"] for c in b.get("categories", [])],
            "coordinates": b.get("coordinates", {}),
            "email": None,
            "website": None,
        })

    if scrape_emails and vendors:
        import asyncio

        async def enrich_vendor(vendor):
            try:
                email, website = await find_vendor_email(vendor["name"], vendor["address"])
                vendor["email"] = email
                vendor["website"] = website
            except Exception as e:
                print(f"[email error] {vendor['name']}: {e}")
            return vendor

        top = vendors[:10]
        rest = vendors[10:]
        enriched = await asyncio.gather(*[enrich_vendor(v) for v in top])
        vendors = list(enriched) + rest

    vendors = rank_vendors(vendors, {"keywords": keywords})

    if vendors:
        try:
            summary_prompt = f"""I'm looking for {category} for an event with {guest_count} guests, budget under ${budget}.
Here are the top vendors: {json.dumps([{
    "id": v["id"], "name": v["name"], "rating": v["rating"],
    "review_count": v["review_count"], "price": v["price"],
    "categories": v["categories"], "email": v.get("email")
} for v in vendors[:5]], indent=2)}

For each vendor write a 1-sentence reason why they're a good fit.
Respond ONLY with a JSON array: [{{"id": "...", "reason": "..."}}]
No markdown, no explanation."""
            claude_response = anthropic_client.messages.create(
                model="claude-opus-4-5",
                max_tokens=500,
                messages=[{"role": "user", "content": summary_prompt}],
            )
            rankings = json.loads(claude_response.content[0].text)
            ranked_ids = {r["id"]: r["reason"] for r in rankings}
            for v in vendors:
                v["ai_reason"] = ranked_ids.get(v["id"], "")
        except Exception:
            pass

    return {
        "vendors": vendors,
        "total": len(vendors),
        "emails_found": sum(1 for v in vendors if v.get("email")),
        "query": {"category": category, "location": location, "budget": budget, "guest_count": guest_count, "min_rating": min_rating}
    }


# ── send vendor email ─────────────────────────────────────────────────────────
@app.post("/send-vendor-email")
async def send_vendor_email(payload: dict):
    to = payload.get("to")
    subject = payload.get("subject")
    body = payload.get("body")
    vendor_name = payload.get("vendor_name", "vendor")
    if not all([to, subject, body]):
        return {"error": "Missing required fields: to, subject, body"}
    if not all([GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, GMAIL_REFRESH_TOKEN]):
        return {"error": "Gmail not configured in Railway env vars"}
    try:
        result = await send_gmail(to, subject, body)
        print(f"[gmail] Sent to {vendor_name} <{to}>: {subject}")
        return {"status": "sent", "to": to, "vendor_name": vendor_name, "subject": subject, "message_id": result.get("message_id")}
    except Exception as e:
        print(f"[gmail error] {e}")
        return {"error": str(e)}


# ── debug email ───────────────────────────────────────────────────────────────
@app.post("/debug-email")
async def debug_email(payload: dict):
    name = payload.get("name", "Rutt's Catering")
    address = payload.get("address", "Los Angeles, CA")
    city = address.split(",")[-2].strip() if "," in address else ""

    results = {
        "apollo_key_set": bool(APOLLO_API_KEY),
        "serp_key_set": bool(SERP_API_KEY),
        "hunter_key_set": bool(HUNTER_API_KEY),
        "gmaps_key_set": bool(GOOGLE_MAPS_API_KEY),
    }

    apollo_email, apollo_website = await apollo_find_email(name, address)
    results["apollo_email"] = apollo_email
    results["apollo_website"] = apollo_website
    results["google_maps_website"] = await google_maps_find_website(name, address)

    yelp_data = await serp_yelp_search(name, city)
    results["serp_yelp_results"] = len(yelp_data.get("organic_results", []))

    email, website = await find_vendor_email(name, address)
    results["final_email"] = email
    results["final_website"] = website

    return results


# ── admin ─────────────────────────────────────────────────────────────────────
@app.get("/admin/users")
async def list_users():
    return {phone: {"joined_at": d["joined_at"], "message_count": len(d["messages"]), "profile": d["profile"]} for phone, d in conversations.items()}


@app.get("/admin/conversation/{phone_number}")
async def get_conversation(phone_number: str):
    return conversations.get(f"whatsapp:+{phone_number}", {"error": "not found"})


@app.post("/admin/introduce")
async def introduce_two_people(payload: dict):
    phone_a = f"whatsapp:{payload['phone_a']}"
    phone_b = f"whatsapp:{payload['phone_b']}"
    send_whatsapp_message(phone_a, f"Hey! I have someone you should meet. {payload['name_b']} — {payload['reason']}. Tip: {payload['icebreaker']} 🎯")
    send_whatsapp_message(phone_b, f"Hey! I want to introduce you to someone. {payload['name_a']} — {payload['reason']}. Tip: {payload['icebreaker']} 🎯")
    return {"status": "sent", "to": [phone_a, phone_b]}


@app.post("/admin/event-reminder")
async def send_event_reminder(payload: dict):
    phones = payload.get("phones") or [p.replace("whatsapp:", "") for p in conversations.keys()]
    msg = f"🗓 Reminder: {payload['event_name']} is happening {payload['date']} at {payload['time']}! Come to {payload['location']}. See you there!"
    for phone in phones:
        send_whatsapp_message(f"whatsapp:{phone}", msg)
    return {"status": "sent", "count": len(phones)}


@app.get("/health")
async def health():
    return {"status": "ok", "users": len(conversations)}
