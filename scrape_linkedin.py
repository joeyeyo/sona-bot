#!/usr/bin/env python3
"""
Sona LinkedIn Scraper
Usage: python3 scrape_linkedin.py <phone> <linkedin_url>
Example: python3 scrape_linkedin.py "+16263756580" "https://linkedin.com/in/joeyeh"

Calls OpenClaw to scrape LinkedIn using your logged-in Chrome session,
then saves the result to Supabase via Railway.
"""

import sys
import json
import re
import time
import urllib.request
import urllib.error

# ── Config ────────────────────────────────────────────────────────────────────
OPENCLAW_URL = "http://127.0.0.1:18789"
OPENCLAW_TOKEN = "f07845638a6e53206515f4eb63703cf466cc0b4b27a4727c"
RAILWAY_URL = "https://sona-production-529e.up.railway.app"

PROMPT = """The LinkedIn profile {linkedin_url} is already open in the currently attached browser tab. Do NOT navigate anywhere.

You MUST follow ALL these steps before extracting data. Do not skip any step:

STEP 1: Take a snapshot. Check for login form or "Sign in" button. If found, return ONLY: {{"error": "not_logged_in"}}

STEP 2: Scroll down 1000px. Take a snapshot. Tell me what new content appeared.

STEP 3: Scroll down another 1000px. Take a snapshot. Tell me what new content appeared.

STEP 4: Scroll down another 1000px. Take a snapshot.

STEP 5: Scroll down another 1000px. Take a snapshot.

STEP 6: Scroll down another 1000px. Take a snapshot.

STEP 7: Scroll down another 1000px. Take a snapshot.

STEP 8: Look for buttons labeled "Show all X experiences" or "Show all X skills" — click each one. Take a snapshot after each click.

STEP 9: Scroll down 1000px more to load any newly expanded content. Take a snapshot.

STEP 10: List every job title and company you saw across ALL the snapshots above. Write them out as plain text first.

STEP 11: List every school and degree you saw across ALL snapshots.

STEP 12: List every skill you saw across ALL snapshots.

STEP 13: Now format everything from steps 10-12 into ONLY a JSON object with these exact fields:
{{
  "full_name": "string",
  "headline": "string",
  "current_role": "string",
  "current_company": "string",
  "location": "string",
  "summary": "string or null",
  "experiences": [{{"title": "string", "company": "string", "dates": "string", "duration": "string", "description": "string or null"}}],
  "education": [{{"school": "string", "degree": "string or null", "field": "string or null", "description": "string or null"}}],
  "skills": ["string"],
  "publications": ["string"],
  "connection_count": null
}}

Return ONLY the JSON, no explanation, no markdown backticks."""


def normalize_phone(phone):
    phone = phone.strip()
    if not phone.startswith("whatsapp:"):
        phone = f"whatsapp:{phone}"
    return phone


def call_openclaw(linkedin_url):
    print(f"\n  BEFORE CONTINUING:")
    print(f"  1. Make sure {linkedin_url} is open in Chrome")
    print(f"  2. Make sure OpenClaw Browser Relay icon is ON (green) on that tab")
    input("  Press ENTER when relay is attached to the LinkedIn tab...")

    print(f"\n🦞 Calling OpenClaw for {linkedin_url}...")
    print("   (This may take 30-60 seconds while OpenClaw scrolls and extracts)")

    prompt = PROMPT.format(linkedin_url=linkedin_url)

    payload = json.dumps({
        "model": "openclaw",
        "input": prompt,
    }).encode()

    req = urllib.request.Request(
        f"{OPENCLAW_URL}/v1/responses",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {OPENCLAW_TOKEN}",
            "x-openclaw-agent-id": "main",
            "x-openclaw-session-id": "agent:main:main",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode())
            text = data.get("output", [{}])[0].get("content", [{}])[0].get("text", "")
            tokens = data.get("usage", {}).get("total_tokens", 0)
            print(f"   ✓ OpenClaw responded ({tokens:,} tokens)")
            with open("/tmp/openclaw_last_response.txt", "w") as dbg:
                dbg.write(text)
            return text
    except urllib.error.URLError as e:
        print(f"   ✗ OpenClaw error: {e}")
        return None
    except Exception as e:
        print(f"   ✗ Unexpected error: {e}")
        return None


def extract_json(text):
    if not text:
        return None
    clean = text.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(clean)
    except Exception:
        pass
    # Try all JSON objects from last to first — last is most complete
    for match in reversed(list(re.finditer(r'\{[\s\S]+\}', clean))):
        try:
            parsed = json.loads(match.group())
            if isinstance(parsed, dict) and parsed.get("full_name"):
                return parsed
        except Exception:
            continue
    return None


def save_to_supabase(phone, profile):
    phone = normalize_phone(phone)
    encoded_phone = urllib.parse.quote(phone, safe='')

    payload = json.dumps({
        "linkedin_data": profile,
        "name": profile.get("full_name"),
    }).encode()

    req = urllib.request.Request(
        f"{RAILWAY_URL}/guests/{encoded_phone}",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="PATCH",
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode())
            return result
    except Exception as e:
        print(f"   ✗ Save error: {e}")
        return None


def main():
    if len(sys.argv) < 3:
        print("Usage: python3 scrape_linkedin.py <phone> <linkedin_url>")
        print('Example: python3 scrape_linkedin.py "+16263756580" "https://linkedin.com/in/joeyeh"')
        sys.exit(1)

    phone = sys.argv[1]
    linkedin_url = sys.argv[2]

    print(f"\n{'='*60}")
    print(f"  SONA LINKEDIN SCRAPER")
    print(f"{'='*60}")
    print(f"  Phone:    {phone}")
    print(f"  LinkedIn: {linkedin_url}")
    print(f"{'='*60}\n")

    # Step 1: Call OpenClaw
    text = call_openclaw(linkedin_url)
    if not text:
        print("\n✗ OpenClaw failed to respond. Make sure OpenClaw is running.")
        sys.exit(1)

    # Step 2: Extract JSON
    print("\n📋 Extracting profile data...")
    profile = extract_json(text)

    if not profile:
        print("✗ Could not extract JSON from OpenClaw response.")
        print("\nOpenClaw said:")
        print(text[:1000])
        sys.exit(1)

    # Check for login error
    if profile.get("error") == "not_logged_in":
        print("\n✗ ERROR: LinkedIn is not logged in!")
        print("  Log into LinkedIn in Chrome, attach the relay, and try again.")
        sys.exit(1)

    # Step 3: Show what we got
    print(f"   ✓ Name: {profile.get('full_name')}")
    print(f"   ✓ Role: {profile.get('current_role')} @ {profile.get('current_company')}")
    print(f"   ✓ Experiences: {len(profile.get('experiences', []))}")
    print(f"   ✓ Education: {len(profile.get('education', []))}")
    print(f"   ✓ Skills: {len(profile.get('skills', []))}")

    if not profile.get('full_name'):
        print("\n⚠ Warning: full_name is empty — OpenClaw may not have scraped correctly.")
        print("   OpenClaw response:")
        print(text[:500])
        cont = input("\n   Save anyway? (y/n): ")
        if cont.lower() != 'y':
            sys.exit(1)

    # Show experiences
    experiences = profile.get('experiences', [])
    if experiences:
        print(f"\n   Experiences found:")
        for exp in experiences:
            print(f"     • {exp.get('title')} @ {exp.get('company')} ({exp.get('dates', '')})")
    else:
        print("\n   ⚠ No experiences found — OpenClaw may not have scrolled fully.")
        print("   Try running the script again, or use the paste method.")

    # Step 4: Save to Supabase
    print(f"\n💾 Saving to Supabase...")

    import urllib.parse  # need this for encoding
    result = save_to_supabase(phone, profile)

    if result:
        print(f"   ✓ Saved! Guest record updated.")
        print(f"\n{'='*60}")
        print(f"  ✓ DONE — {profile.get('full_name')} scraped and saved")
        print(f"{'='*60}\n")
    else:
        print(f"   ✗ Failed to save to Supabase.")
        print(f"   Profile JSON:")
        print(json.dumps(profile, indent=2))


if __name__ == "__main__":
    import urllib.parse
    main()
