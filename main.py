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
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")


# ── Supabase helpers ──────────────────────────────────────────────────────────
def supa_headers():
    return {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


async def supa_get(table: str, filters: dict) -> list:
    params = "&".join(f"{k}=eq.{v}" for k, v in filters.items())
    async with httpx.AsyncClient(timeout=8.0) as client:
        resp = await client.get(
            f"{SUPABASE_URL}/rest/v1/{table}?{params}",
            headers=supa_headers(),
        )
        if resp.status_code == 200:
            return resp.json()
    return []


async def supa_insert(table: str, data: dict) -> dict | None:
    async with httpx.AsyncClient(timeout=8.0) as client:
        resp = await client.post(
            f"{SUPABASE_URL}/rest/v1/{table}",
            headers=supa_headers(),
            json=data,
        )
        if resp.status_code in (200, 201):
            result = resp.json()
            return result[0] if result else None
    return None


async def supa_update(table: str, filters: dict, data: dict) -> dict | None:
    params = "&".join(f"{k}=eq.{v}" for k, v in filters.items())
    data["updated_at"] = datetime.now().isoformat()
    async with httpx.AsyncClient(timeout=8.0) as client:
        resp = await client.patch(
            f"{SUPABASE_URL}/rest/v1/{table}?{params}",
            headers=supa_headers(),
            json=data,
        )
        if resp.status_code == 200:
            result = resp.json()
            return result[0] if result else None
    return None


async def get_or_create_guest(phone: str) -> dict:
    rows = await supa_get("guests", {"phone": phone})
    if rows:
        return rows[0]
    new_guest = await supa_insert("guests", {"phone": phone})
    return new_guest or {"phone": phone, "stage": "intro"}


async def get_or_create_conversation(phone: str) -> dict:
    rows = await supa_get("conversations", {"phone": phone})
    if rows:
        return rows[0]
    new_conv = await supa_insert("conversations", {
        "phone": phone,
        "messages": [],
        "stage": "intro",
    })
    return new_conv or {"phone": phone, "messages": [], "stage": "intro"}


async def save_conversation(phone: str, messages: list, stage: str):
    rows = await supa_get("conversations", {"phone": phone})
    if rows:
        await supa_update("conversations", {"phone": phone}, {
            "messages": messages,
            "stage": stage,
        })
    else:
        await supa_insert("conversations", {
            "phone": phone,
            "messages": messages,
            "stage": stage,
        })


async def get_active_event() -> dict | None:
    """Get the most recently created event."""
    async with httpx.AsyncClient(timeout=8.0) as client:
        resp = await client.get(
            f"{SUPABASE_URL}/rest/v1/events?order=created_at.desc&limit=1",
            headers=supa_headers(),
        )
        if resp.status_code == 200:
            rows = resp.json()
            return rows[0] if rows else None
    return None


async def get_confirmed_guests(event_id: str) -> list:
    async with httpx.AsyncClient(timeout=8.0) as client:
        resp = await client.get(
            f"{SUPABASE_URL}/rest/v1/guests?event_id=eq.{event_id}&rsvp_status=eq.confirmed",
            headers=supa_headers(),
        )
        if resp.status_code == 200:
            return resp.json()
    return []


# ── Sona onboarding system prompt ─────────────────────────────────────────────
def build_onboarding_prompt(event: dict | None, stage: str, guest: dict) -> str:
    event_info = ""
    if event:
        event_info = f"""
ACTIVE EVENT:
- Name: {event.get('name', 'An exclusive event')}
- Date: {event.get('date', 'TBD')}
- Venue: {event.get('venue', 'TBD')} in {event.get('city', 'Los Angeles')}
- Expected guests: {event.get('guest_count', '~30')}
- Vibe: {event.get('vibe', 'Intimate, curated networking')}
- Audience: {event.get('audience', 'Asian tech founders and operators')}
- Hosts: {event.get('host_name', 'Joe and Eric')}
"""

    guest_info = f"""
GUEST SO FAR:
- Name: {guest.get('name', 'unknown')}
- LinkedIn: {guest.get('linkedin_url', 'not provided')}
- What they do: {guest.get('what_they_do', 'unknown')}
- Who they want to meet: {guest.get('who_they_want_to_meet', 'unknown')}
- RSVP status: {guest.get('rsvp_status', 'pending')}
"""

    return f"""You are Sona, an AI concierge for an exclusive invite-only event community in Los Angeles run by Joe Yeh and Eric Tsai.

Your job is to have a SHORT, warm, conversational intake with potential guests — figure out who they are and what they're looking for, then generate a personalized invite that makes them feel like this event was made for them.

{event_info}
{guest_info}

CURRENT STAGE: {stage}

ONBOARDING STAGES IN ORDER:
1. intro — Warm greeting, explain you're reaching out on behalf of Joe. Ask for their LinkedIn URL first.
2. what_they_do — Ask what they're working on / building right now (1 question only)
3. who_to_meet — Ask who they're most trying to connect with (founders, investors, operators, talent, etc.)
4. interests — Ask one lightweight personal question (what do you do outside of work, what are you into lately)
5. generate_invite — Generate their personalized invite based on everything collected
6. rsvp — They've seen the invite, handle yes/no/maybe response
7. confirmed — They said yes, send event details and what to expect
8. declined — They said no, be gracious

RULES:
- Ask ONE question at a time — never multiple questions in one message
- Keep messages SHORT — this is WhatsApp, 2-4 sentences max
- Be warm and human, not salesy or corporate
- When generating the invite (stage 5): write a compelling 3-4 sentence personalized message that explains exactly why THIS person should come based on what they told you. Reference specific things they said. End with "You in?"
- When they RSVP yes: confirm warmly, tell them the date/venue, say you'll send more details closer to the event
- Do NOT mention you are an AI unless directly asked

LINKEDIN EXTRACTION:
When you receive a LinkedIn URL, acknowledge it and move to the next question. Do not try to look it up — the system will handle that separately.

Respond naturally as Sona. No bullet points, no markdown. Just conversational text."""


# ── Onboarding stage detection ─────────────────────────────────────────────────
def detect_next_stage(current_stage: str, user_message: str, guest: dict) -> str:
    """Advance the stage based on what we've collected."""
    msg_lower = user_message.lower().strip()

    if current_stage == "intro":
        # Check if they gave us a LinkedIn URL
        if "linkedin.com" in msg_lower or "linked.in" in msg_lower:
            return "what_they_do"
        return "intro"

    if current_stage == "what_they_do":
        if len(user_message) > 10:  # They answered
            return "who_to_meet"
        return "what_they_do"

    if current_stage == "who_to_meet":
        if len(user_message) > 5:
            return "interests"
        return "who_to_meet"

    if current_stage == "interests":
        if len(user_message) > 5:
            return "generate_invite"
        return "interests"

    if current_stage == "generate_invite":
        # After generating invite, move to rsvp
        return "rsvp"

    if current_stage == "rsvp":
        yes_signals = ["yes", "yeah", "yep", "sure", "in", "absolutely", "definitely", "count me", "i'm in", "im in", "sounds good", "let's go", "lets go"]
        no_signals = ["no", "nope", "can't", "cant", "won't", "wont", "pass", "not this time", "maybe next"]
        if any(s in msg_lower for s in yes_signals):
            return "confirmed"
        if any(s in msg_lower for s in no_signals):
            return "declined"
        return "rsvp"

    return current_stage


def extract_linkedin_url(text: str) -> str | None:
    patterns = [
        r'https?://(?:www\.)?linkedin\.com/in/[\w\-]+/?',
        r'linkedin\.com/in/[\w\-]+/?',
        r'linked\.in/[\w\-]+/?',
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            url = match.group()
            if not url.startswith("http"):
                url = "https://" + url
            return url
    return None


# ── Main WhatsApp onboarding handler ─────────────────────────────────────────
async def handle_guest_onboarding(phone: str, user_message: str) -> str:
    """
    Stateful onboarding conversation stored in Supabase.
    Guides guest through 5 questions then generates personalized invite.
    """
    # Load state from Supabase
    conv = await get_or_create_conversation(phone)
    guest = await get_or_create_guest(phone)
    event = await get_active_event()

    messages = conv.get("messages") or []
    current_stage = conv.get("stage") or "intro"

    # Extract LinkedIn URL if present
    linkedin_url = extract_linkedin_url(user_message)
    if linkedin_url and not guest.get("linkedin_url"):
        await supa_update("guests", {"phone": phone}, {"linkedin_url": linkedin_url})
        guest["linkedin_url"] = linkedin_url

    # Detect stage transitions and update guest data
    next_stage = detect_next_stage(current_stage, user_message, guest)

    # Save answers to guest profile based on stage
    if current_stage == "what_they_do" and len(user_message) > 10:
        await supa_update("guests", {"phone": phone}, {"what_they_do": user_message})
        guest["what_they_do"] = user_message

    elif current_stage == "who_to_meet" and len(user_message) > 5:
        await supa_update("guests", {"phone": phone}, {"who_they_want_to_meet": user_message})
        guest["who_they_want_to_meet"] = user_message

    elif current_stage == "interests" and len(user_message) > 5:
        await supa_update("guests", {"phone": phone}, {"interests": user_message})
        guest["interests"] = user_message

    # Build messages for Claude
    messages.append({"role": "user", "content": user_message})

    system_prompt = build_onboarding_prompt(event, next_stage, guest)

    # Get confirmed guests for personalization context
    confirmed_guests = []
    if next_stage == "generate_invite" and event:
        confirmed_guests = await get_confirmed_guests(event.get("id", ""))

    # Add confirmed guest context if generating invite
    extra_context = ""
    if next_stage == "generate_invite" and confirmed_guests:
        guest_summaries = [f"- {g.get('name', 'Guest')}: {g.get('what_they_do', '')}" for g in confirmed_guests[:5]]
        extra_context = f"\n\nCONFIRMED GUESTS SO FAR (use to personalize the invite):\n" + "\n".join(guest_summaries)
        system_prompt += extra_context

    # Call Claude
    response = anthropic_client.messages.create(
        model="claude-opus-4-5",
        max_tokens=500,
        system=system_prompt,
        messages=messages[-20:],  # Keep last 20 messages for context
    )
    reply = response.content[0].text

    # Save assistant reply
    messages.append({"role": "assistant", "content": reply})

    # Update RSVP status
    if next_stage == "confirmed":
        await supa_update("guests", {"phone": phone}, {
            "rsvp_status": "confirmed",
            "onboarding_complete": True,
            "event_id": event.get("id") if event else None,
        })
    elif next_stage == "declined":
        await supa_update("guests", {"phone": phone}, {"rsvp_status": "declined"})
    elif next_stage == "generate_invite":
        await supa_update("guests", {"phone": phone}, {"personalized_invite": reply})

    # Save conversation state
    await save_conversation(phone, messages, next_stage)

    return reply


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


# ── Vendor ranking ─────────────────────────────────────────────────────────────
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


# ── Email extraction ───────────────────────────────────────────────────────────
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
    "giftly.com", "giggster.com", "heathercoros.com", "pandaexpressed.com",
    "escholarship.org", "partyslate.com",
    "png", "jpg", "jpeg", "gif", "webp", "svg", "pdf", "css", "js",
}

DIRECTORY_DOMAINS = {
    "partyslate.com", "theknot.com", "weddingwire.com", "junebugweddings.com",
    "stylemepretty.com", "thumbtack.com", "bark.com", "houzz.com", "expertise.com",
    "gigsalad.com", "gigmasters.com", "styleseat.com", "vagaro.com", "booksy.com",
    "eventective.com", "peerspace.com", "tagvenue.com",
    "yelp.com", "tripadvisor.com", "google.com", "bing.com",
    "yellowpages.com", "bbb.org", "manta.com", "zoominfo.com",
    "chamberofcommerce.com", "mapquest.com",
    "doordash.com", "grubhub.com", "ubereats.com", "postmates.com", "toasttab.com",
    "facebook.com", "instagram.com", "twitter.com", "linkedin.com", "pinterest.com", "tiktok.com",
    "escholarship.org", "academia.edu",
    "giftly.com", "giggster.com", "heathercoros.com",
}

PRIORITY_LOCAL_PARTS = ["contact", "info", "hello", "events", "booking", "catering", "hire", "enquir", "inquir", "photo", "studio"]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


def is_directory_site(url: str) -> bool:
    if not url:
        return False
    domain = urlparse(url).netloc.replace("www.", "")
    return any(d in domain for d in DIRECTORY_DOMAINS)


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
    if not email or not website:
        return False
    email_domain = email.split("@")[-1].replace("www.", "")
    site_domain = urlparse(website).netloc.replace("www.", "")
    return email_domain in site_domain or site_domain in email_domain


def is_valid_email(email: str, website: str | None) -> bool:
    if not email or not website:
        return False
    if is_directory_site(website):
        return False
    return email_matches_website(email, website)


def get_website_from_results(results: list) -> str | None:
    for result in results:
        link = result.get("link", "")
        if link and not is_directory_site(link):
            return link.split("?")[0].rstrip("/")
    return None


# ── Search APIs ────────────────────────────────────────────────────────────────
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
                website = website.rstrip("/")
                if is_directory_site(website):
                    return None
                print(f"[gmaps] {vendor_name}: {website}")
                return website
    except Exception as e:
        print(f"[gmaps error] {vendor_name}: {e}")
    return None


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
                json={"q_organization_name": vendor_name, "organization_locations": [f"{city}, {state}"] if city else [], "page": 1, "per_page": 3},
            )
            if resp.status_code in (403, 402):
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
            if website and is_directory_site(website):
                website = None
            email = None
            if best_org.get("contact_emails"):
                email = best_org["contact_emails"][0]
            if not email and best_org.get("id"):
                people_resp = await client.post(
                    "https://api.apollo.io/v1/mixed_people/search",
                    headers={"Content-Type": "application/json", "Cache-Control": "no-cache", "X-Api-Key": APOLLO_API_KEY},
                    json={"organization_ids": [best_org["id"]], "page": 1, "per_page": 5, "person_titles": ["owner", "manager", "catering manager", "event coordinator", "director"]},
                )
                if people_resp.status_code == 200:
                    for person in people_resp.json().get("people", []):
                        person_email = person.get("email")
                        if person_email and "@" in person_email and "apollo" not in person_email:
                            email = person_email
                            break
            return email, website
    except Exception as e:
        print(f"[apollo error] {vendor_name}: {e}")
        return None, None


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
                    for prefix in ["info", "contact", "hello", "events", "book"]:
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
            if best and best.split("@")[-1] in IGNORE_DOMAINS:
                return None
            print(f"[hunter] {best} for {domain}")
            return best
    except Exception as e:
        print(f"[hunter error] {domain}: {e}")
        return None


async def scrape_email_from_website(website_url: str) -> str | None:
    if not website_url or is_directory_site(website_url):
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
    city = address.split(",")[-2].strip() if "," in address else ""
    website = None

    # 1. Apollo
    apollo_email, apollo_website = await apollo_find_email(vendor_name, address)
    if apollo_website and not is_directory_site(apollo_website):
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
                candidate = result.get("website", "")
                if candidate and not is_directory_site(candidate):
                    website = candidate.rstrip("/")
                    break

    # 4. SerpAPI Google
    if not website:
        serp_site = await serp_search(f'"{vendor_name}" {city} official website')
        website = get_website_from_results(serp_site.get("organic_results", []))

    if not website:
        print(f"[email] No real website found for {vendor_name}")
        return None, None

    # 5. Scrape website
    email = await scrape_email_from_website(website)
    if email and is_valid_email(email, website):
        print(f"[scrape] {vendor_name}: {email}")
        return email, website

    # 6. Hunter.io
    domain = urlparse(website).netloc.replace("www.", "")
    if domain:
        email = await hunter_find_email(domain)
        if email and is_valid_email(email, website):
            return email, website

    # 7. SerpAPI Google snippets
    serp_data = await serp_search(f'"{vendor_name}" {city} email contact')
    for result in serp_data.get("organic_results", []):
        for email in extract_emails_from_text(result.get("snippet", "") + " " + result.get("link", "")):
            if is_valid_email(email, website):
                print(f"[serp-snippet] {vendor_name}: {email}")
                return email, website

    # 8. DuckDuckGo
    try:
        ddg_url = f"https://html.duckduckgo.com/html/?q={quote_plus(f'{vendor_name} {city} email contact')}"
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
            resp = await client.get(ddg_url, headers={**HEADERS, "Referer": "https://duckduckgo.com/"})
            if resp.status_code == 200:
                snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', resp.text, re.DOTALL)
                for snippet in snippets:
                    for email in extract_emails_from_text(re.sub(r'<[^>]+>', '', snippet)):
                        if is_valid_email(email, website):
                            print(f"[ddg] {vendor_name}: {email}")
                            return email, website
    except Exception as e:
        print(f"[ddg error] {e}")

    print(f"[email] No email found for {vendor_name} (website: {website})")
    return None, website


def send_whatsapp_message(to: str, body: str):
    twilio_client.messages.create(from_=TWILIO_WHATSAPP_NUMBER, body=body, to=to)


# ── Webhook ────────────────────────────────────────────────────────────────────
@app.post("/webhook", response_class=PlainTextResponse)
async def webhook(From: str = Form(...), Body: str = Form(...)):
    phone = From
    user_message = Body.strip()
    print(f"[{phone}] → {user_message}")

    try:
        reply = await handle_guest_onboarding(phone, user_message)
    except Exception as e:
        print(f"[webhook error] {e}")
        reply = "Hey! Something went wrong on my end. Give me a moment and try again."

    print(f"[Sona → {phone}] {reply}")
    resp = MessagingResponse()
    resp.message(reply)
    return str(resp)


# ── Vendor search ──────────────────────────────────────────────────────────────
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
Here are the top vendors: {json.dumps([{"id": v["id"], "name": v["name"], "rating": v["rating"], "review_count": v["review_count"], "price": v["price"], "categories": v["categories"], "email": v.get("email")} for v in vendors[:5]], indent=2)}

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


# ── Send vendor email ──────────────────────────────────────────────────────────
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


# ── Guest API endpoints ────────────────────────────────────────────────────────
@app.get("/guests")
async def list_guests():
    """Get all guests with their profiles — for the host dashboard."""
    async with httpx.AsyncClient(timeout=8.0) as client:
        resp = await client.get(
            f"{SUPABASE_URL}/rest/v1/guests?order=created_at.desc",
            headers=supa_headers(),
        )
        if resp.status_code == 200:
            return resp.json()
    return []


@app.get("/guests/{phone}")
async def get_guest(phone: str):
    rows = await supa_get("guests", {"phone": phone})
    return rows[0] if rows else {"error": "not found"}


@app.patch("/guests/{phone}")
async def update_guest(phone: str, payload: dict):
    result = await supa_update("guests", {"phone": phone}, payload)
    return result or {"error": "update failed"}


# ── Event API endpoints ────────────────────────────────────────────────────────
@app.get("/events")
async def list_events():
    async with httpx.AsyncClient(timeout=8.0) as client:
        resp = await client.get(
            f"{SUPABASE_URL}/rest/v1/events?order=created_at.desc",
            headers=supa_headers(),
        )
        if resp.status_code == 200:
            return resp.json()
    return []


@app.post("/events")
async def create_event(payload: dict):
    result = await supa_insert("events", payload)
    return result or {"error": "create failed"}


@app.patch("/events/{event_id}")
async def update_event(event_id: str, payload: dict):
    result = await supa_update("events", {"id": event_id}, payload)
    return result or {"error": "update failed"}


# ── Admin: send intro message ──────────────────────────────────────────────────
@app.post("/admin/send-intro")
async def send_intro(payload: dict):
    """
    Send the Sona intro message to a phone number to kick off onboarding.
    POST: { "phone": "+13105551234", "from_name": "Joe" }
    """
    phone = payload.get("phone")
    from_name = payload.get("from_name", "Joe")
    if not phone:
        return {"error": "phone required"}

    # Format for WhatsApp
    if not phone.startswith("whatsapp:"):
        phone = f"whatsapp:{phone}"

    intro_msg = f"Hey! I'm Sona, {from_name}'s AI concierge. He asked me to reach out about something exclusive he's putting together in LA. Before I tell you about it — what's your LinkedIn? Want to make sure this is the right fit for you."

    send_whatsapp_message(phone, intro_msg)

    # Initialize conversation in Supabase
    await save_conversation(phone, [{"role": "assistant", "content": intro_msg}], "intro")

    return {"status": "sent", "to": phone}


@app.post("/admin/event-reminder")
async def send_event_reminder(payload: dict):
    async with httpx.AsyncClient(timeout=8.0) as client:
        resp = await client.get(
            f"{SUPABASE_URL}/rest/v1/guests?rsvp_status=eq.confirmed",
            headers=supa_headers(),
        )
        confirmed = resp.json() if resp.status_code == 200 else []

    phones = [g["phone"] for g in confirmed if g.get("phone")]
    msg = f"🗓 Reminder: {payload.get('event_name', 'the event')} is happening {payload.get('date', 'soon')} at {payload.get('time', '')}! Come to {payload.get('location', '')}. See you there!"

    for phone in phones:
        send_whatsapp_message(phone, msg)

    return {"status": "sent", "count": len(phones)}


# ── Debug email endpoint ────────────────────────────────────────────────────────
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
        "supabase_set": bool(SUPABASE_URL),
    }

    apollo_email, apollo_website = await apollo_find_email(name, address)
    results["apollo_email"] = apollo_email
    results["apollo_website"] = apollo_website
    results["google_maps_website"] = await google_maps_find_website(name, address)

    email, website = await find_vendor_email(name, address)
    results["final_email"] = email
    results["final_website"] = website

    return results


# ── Health ─────────────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    guest_count = 0
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            resp = await client.get(
                f"{SUPABASE_URL}/rest/v1/guests?select=count",
                headers={**supa_headers(), "Prefer": "count=exact"},
            )
            if resp.status_code == 200:
                guest_count = int(resp.headers.get("content-range", "0/0").split("/")[-1])
    except Exception:
        pass
    return {"status": "ok", "guests_in_db": guest_count}
