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

# ── clients ───────────────────────────────────────────────────────────────────
twilio_client = Client(
    os.environ["TWILIO_ACCOUNT_SID"],
    os.environ["TWILIO_AUTH_TOKEN"],
)
anthropic_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

TWILIO_WHATSAPP_NUMBER = os.environ.get("TWILIO_WHATSAPP_NUMBER", "whatsapp:+14155238886")
YELP_API_KEY = os.environ.get("YELP_API_KEY", "")
SERP_API_KEY = os.environ.get("SERP_API_KEY", "")
HUNTER_API_KEY = os.environ.get("HUNTER_API_KEY", "")
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

Your goals in early conversations:
1. Learn their name and what brings them here
2. Understand what they're looking for (professional connections, friendships, co-founders, mentors, romantic — whatever they're open to)
3. Learn their professional background (you can ask for LinkedIn)
4. Understand their personal interests and personality
5. Set expectations: you'll introduce them to relevant people via WhatsApp, and there are in-person events they can attend

When you have enough info, tell them:
- You'll be in touch when you find a relevant match
- Mention upcoming events if relevant
- Be specific: "I think you'd really click with someone who's also building in the health space — I'll keep an eye out"

When introducing matches (you'll receive a MATCH_INTRO message with details):
- Explain specifically WHY these two people should meet
- Give them a fun ice-breaker or talking point
- If there's an upcoming event, tell them to find each other there

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


# ── Gmail sending ─────────────────────────────────────────────────────────────
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
    else:
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


# ── email finding ─────────────────────────────────────────────────────────────
EMAIL_PATTERN = re.compile(r'\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b')

IGNORE_DOMAINS = {
    "sentry.io", "wixpress.com", "squarespace.com", "wordpress.com",
    "shopify.com", "example.com", "yourdomain.com", "email.com",
    "google.com", "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "yelp.com", "facebook.com", "instagram.com", "twitter.com",
    "png", "jpg", "jpeg", "gif", "webp", "svg",
}

PRIORITY_LOCAL_PARTS = ["contact", "info", "hello", "events", "booking", "catering", "hire", "enquir", "inquir"]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


def extract_emails_from_text(text: str) -> list:
    mailto = re.findall(r'mailto:([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})', text)
    plain = EMAIL_PATTERN.findall(text)
    all_emails = mailto + plain
    clean = []
    seen = set()
    for email in all_emails:
        email = email.lower().strip().rstrip(".,;")
        domain = email.split("@")[-1]
        if domain not in IGNORE_DOMAINS and email not in seen and len(email) < 80:
            seen.add(email)
            clean.append(email)

    def priority(e):
        local = e.split("@")[0]
        return 0 if any(kw in local for kw in PRIORITY_LOCAL_PARTS) else 1

    return sorted(clean, key=priority)


async def serp_search(query: str) -> dict:
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            "https://serpapi.com/search",
            params={"q": query, "api_key": SERP_API_KEY, "engine": "google", "num": 5},
        )
        if resp.status_code == 200:
            return resp.json()
    return {}


async def hunter_find_email(domain: str) -> str | None:
    """
    Use Hunter.io domain search to find the best contact email for a domain.
    Returns the highest confidence email found, or None.
    """
    if not HUNTER_API_KEY or not domain:
        return None
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(
                "https://api.hunter.io/v2/domain-search",
                params={
                    "domain": domain,
                    "api_key": HUNTER_API_KEY,
                    "limit": 5,
                    "type": "generic",  # prefer generic contact emails over personal
                },
            )
            if resp.status_code != 200:
                return None

            data = resp.json()
            emails = data.get("data", {}).get("emails", [])

            if not emails:
                # Fall back to pattern-based guess from Hunter
                pattern = data.get("data", {}).get("pattern")
                org = data.get("data", {}).get("organization", "")
                if pattern and org:
                    # Hunter found a pattern but no emails — try common prefixes
                    for prefix in ["info", "contact", "hello", "events"]:
                        guessed = f"{prefix}@{domain}"
                        print(f"[hunter] Pattern found, guessing: {guessed}")
                        return guessed
                return None

            # Sort by confidence, prefer generic/role emails
            def email_priority(e):
                local = e.get("value", "").split("@")[0]
                score = e.get("confidence", 0)
                if any(kw in local for kw in PRIORITY_LOCAL_PARTS):
                    score += 50  # boost contact-type emails
                return score

            emails_sorted = sorted(emails, key=email_priority, reverse=True)
            best = emails_sorted[0].get("value")
            confidence = emails_sorted[0].get("confidence", 0)
            print(f"[hunter] Found {best} (confidence: {confidence}) for {domain}")
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


async def find_vendor_email(vendor_name: str, address: str) -> tuple:
    """
    Multi-strategy email finder:
    1. SerpAPI — look for email directly in search snippets
    2. SerpAPI — find their website, then scrape it
    3. Hunter.io — domain search on their website
    4. Hunter.io — domain search on guessed domain from vendor name
    """
    city = address.split(",")[-2].strip() if "," in address else ""

    # Strategy 1: Email directly in SerpAPI snippets
    data = await serp_search(f'"{vendor_name}" {city} email contact')
    organic = data.get("organic_results", [])
    website = None

    for result in organic:
        snippet = result.get("snippet", "")
        emails = extract_emails_from_text(snippet)
        if emails:
            print(f"[serp] Found email in snippet for {vendor_name}: {emails[0]}")
            return emails[0], result.get("link")
        link = result.get("link", "")
        emails = extract_emails_from_text(link)
        if emails:
            print(f"[serp] Found email in link for {vendor_name}: {emails[0]}")
            return emails[0], link

    # Strategy 2: Find website via SerpAPI
    skip_domains = {
        "yelp.com", "facebook.com", "instagram.com", "twitter.com",
        "doordash.com", "grubhub.com", "ubereats.com", "tripadvisor.com",
        "yellowpages.com", "bbb.org", "google.com", "yelp.to",
    }

    data2 = await serp_search(f'"{vendor_name}" {city} official website catering')
    for result in data2.get("organic_results", []):
        link = result.get("link", "")
        domain = urlparse(link).netloc.replace("www.", "")
        if link and not any(skip in domain for skip in skip_domains):
            website = link.split("?")[0].rstrip("/")
            break

    # Strategy 3: Scrape the website directly
    if website:
        email = await scrape_email_from_website(website)
        if email:
            print(f"[scrape] Found email on website for {vendor_name}: {email}")
            return email, website

        # Strategy 4: Hunter.io on the website domain
        domain = urlparse(website).netloc.replace("www.", "")
        if domain:
            email = await hunter_find_email(domain)
            if email:
                return email, website

    # Strategy 5: Hunter.io on a guessed domain
    # Try common patterns: vendorname.com, vendornamela.com etc
    if HUNTER_API_KEY:
        clean_name = re.sub(r'[^a-z0-9]', '', vendor_name.lower())
        guessed_domains = [
            f"{clean_name}.com",
            f"{clean_name}catering.com",
            f"{clean_name}la.com",
        ]
        for guessed_domain in guessed_domains:
            email = await hunter_find_email(guessed_domain)
            if email:
                print(f"[hunter-guess] Found email for {vendor_name} via {guessed_domain}: {email}")
                return email, f"https://{guessed_domain}"

    print(f"[email] Nothing found for {vendor_name}")
    return None, website


# ── webhook endpoint ──────────────────────────────────────────────────────────
@app.post("/webhook", response_class=PlainTextResponse)
async def webhook(
    From: str = Form(...),
    Body: str = Form(...),
):
    phone = From
    user_message = Body.strip()
    print(f"[{phone}] → {user_message}")
    reply = chat_with_sona(phone, user_message)
    print(f"[Sona → {phone}] {reply}")
    resp = MessagingResponse()
    resp.message(reply)
    return str(resp)


# ── vendor search endpoint ────────────────────────────────────────────────────
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

For each vendor write a 1-sentence reason why they're a good fit for this event.
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


# ── send vendor email endpoint ────────────────────────────────────────────────
@app.post("/send-vendor-email")
async def send_vendor_email(payload: dict):
    to = payload.get("to")
    subject = payload.get("subject")
    body = payload.get("body")
    vendor_name = payload.get("vendor_name", "vendor")

    if not all([to, subject, body]):
        return {"error": "Missing required fields: to, subject, body"}

    if not all([GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, GMAIL_REFRESH_TOKEN]):
        return {"error": "Gmail not configured — add GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, GMAIL_REFRESH_TOKEN to Railway"}

    try:
        result = await send_gmail(to, subject, body)
        print(f"[gmail] Sent to {vendor_name} <{to}>: {subject}")
        return {"status": "sent", "to": to, "vendor_name": vendor_name, "subject": subject, "message_id": result.get("message_id")}
    except Exception as e:
        print(f"[gmail error] {e}")
        return {"error": str(e)}


# ── debug email endpoint ──────────────────────────────────────────────────────
@app.post("/debug-email")
async def debug_email(payload: dict):
    name = payload.get("name", "Rutt's Catering")
    address = payload.get("address", "Los Angeles, CA")
    city = address.split(",")[-2].strip() if "," in address else ""

    results = {}
    results["serp_api_key_set"] = bool(SERP_API_KEY)
    results["hunter_api_key_set"] = bool(HUNTER_API_KEY)

    query1 = f'"{name}" {city} email contact'
    results["query_1"] = query1
    data1 = await serp_search(query1)
    results["organic_results_count"] = len(data1.get("organic_results", []))
    results["snippets"] = [
        {"title": r.get("title"), "snippet": r.get("snippet"), "link": r.get("link")}
        for r in data1.get("organic_results", [])[:3]
    ]

    query2 = f'"{name}" {city} official website catering'
    results["query_2"] = query2
    data2 = await serp_search(query2)
    results["website_results"] = [
        {"title": r.get("title"), "link": r.get("link")}
        for r in data2.get("organic_results", [])[:3]
    ]

    email, website = await find_vendor_email(name, address)
    results["final_email"] = email
    results["final_website"] = website

    return results


# ── admin endpoints ───────────────────────────────────────────────────────────
@app.get("/admin/users")
async def list_users():
    return {
        phone: {
            "joined_at": data["joined_at"],
            "message_count": len(data["messages"]),
            "profile": data["profile"],
        }
        for phone, data in conversations.items()
    }


@app.get("/admin/conversation/{phone_number}")
async def get_conversation(phone_number: str):
    key = f"whatsapp:+{phone_number}"
    return conversations.get(key, {"error": "not found"})


@app.post("/admin/introduce")
async def introduce_two_people(payload: dict):
    phone_a = f"whatsapp:{payload['phone_a']}"
    phone_b = f"whatsapp:{payload['phone_b']}"
    msg_to_a = f"Hey! I've been thinking about you and I have someone you should meet. {payload['name_b']} — {payload['reason']}. Tip: {payload['icebreaker']} 🎯"
    msg_to_b = f"Hey! I want to introduce you to someone. {payload['name_a']} — {payload['reason']}. Tip: {payload['icebreaker']} 🎯"
    send_whatsapp_message(phone_a, msg_to_a)
    send_whatsapp_message(phone_b, msg_to_b)
    return {"status": "sent", "to": [phone_a, phone_b]}


@app.post("/admin/event-reminder")
async def send_event_reminder(payload: dict):
    phones = payload.get("phones")
    if not phones:
        phones = [p.replace("whatsapp:", "") for p in conversations.keys()]
    msg = f"🗓 Reminder: {payload['event_name']} is happening {payload['date']} at {payload['time']}! Come to {payload['location']}. I'll send you a heads-up on who to look out for before the night. See you there!"
    for phone in phones:
        send_whatsapp_message(f"whatsapp:{phone}", msg)
    return {"status": "sent", "count": len(phones)}


@app.get("/health")
async def health():
    return {"status": "ok", "users": len(conversations)}
