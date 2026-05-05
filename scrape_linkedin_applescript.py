#!/usr/bin/env python3
"""
Sona LinkedIn Scraper — AppleScript method
Types directly into OpenClaw console (which has your logged-in Chrome session)
then reads the response and saves to Supabase.

Usage: python3 scrape_linkedin_applescript.py <phone> <linkedin_url>
Example: python3 scrape_linkedin_applescript.py "+16263756580" "https://linkedin.com/in/joeyeh"
"""

import sys
import json
import re
import time
import subprocess
import urllib.request
import urllib.parse

# ── Config ────────────────────────────────────────────────────────────────────
OPENCLAW_URL = "http://127.0.0.1:18789"
OPENCLAW_TOKEN = "f07845638a6e53206515f4eb63703cf466cc0b4b27a4727c"
RAILWAY_URL = "https://sona-production-529e.up.railway.app"
OPENCLAW_CHAT_URL = "http://127.0.0.1:18789/chat?session=agent%3Amain%3Amain"

SCRAPE_PROMPT = "Go to {linkedin_url} in my browser, scroll to the bottom of the page, click all Show More and Show All buttons you see, then extract and return the full profile as JSON with fields: full_name, headline, current_role, current_company, location, summary, experiences (array with title/company/dates/duration/description), education (array with school/degree/field), skills (array), publications (array). Return ONLY the JSON."


def open_openclaw_and_scrape(linkedin_url):
    """Open OpenClaw in Chrome and type the scrape prompt via AppleScript."""
    prompt = SCRAPE_PROMPT.format(linkedin_url=linkedin_url)

    # Escape the prompt for AppleScript
    prompt_escaped = prompt.replace('"', '\\"').replace("'", "\\'")

    # AppleScript to:
    # 1. Open OpenClaw chat in Chrome
    # 2. Wait for page to load
    # 3. Find the input field and type the prompt
    # 4. Press Enter
    # Copy prompt to clipboard first
    clip_proc = subprocess.run(["pbcopy"], input=prompt.encode(), capture_output=True)
    print("   ✓ Prompt copied to clipboard")

    applescript = f'''
tell application "Google Chrome"
    activate
    delay 0.5
    open location "{OPENCLAW_CHAT_URL}"
    delay 4
end tell

-- Use JavaScript to focus and fill the input directly
tell application "Google Chrome"
    execute front window's active tab javascript "
        var inputs = document.querySelectorAll('textarea, [contenteditable]');
        var last = inputs[inputs.length - 1];
        if (last) {{
            last.focus();
            last.click();
        }}
    "
    delay 0.5
    execute front window's active tab javascript "
        var inputs = document.querySelectorAll('textarea, [contenteditable]');
        var last = inputs[inputs.length - 1];
        if (last) {{
            if (last.tagName === 'TEXTAREA') {{
                last.value = '';
                var nativeInputValueSetter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
                nativeInputValueSetter.call(last, String.fromCharCode(1));
                last.dispatchEvent(new Event('input', {{bubbles: true}}));
                nativeInputValueSetter.call(last, '');
                last.dispatchEvent(new Event('input', {{bubbles: true}}));
            }}
            last.focus();
        }}
    "
    delay 0.3
end tell

tell application "System Events"
    tell process "Google Chrome"
        -- Paste the prompt
        keystroke "v" using command down
        delay 1
        -- Send with Enter
        key code 36
    end tell
end tell
'''

    print("🦞 Opening OpenClaw console and sending scrape prompt...")
    result = subprocess.run(
        ["osascript", "-e", applescript],
        capture_output=True, text=True
    )

    if result.returncode != 0:
        print(f"   ⚠ AppleScript warning: {result.stderr.strip()}")
    else:
        print("   ✓ Prompt sent to OpenClaw")

    return result.returncode == 0


def wait_and_get_response(linkedin_url, wait_seconds=60):
    """
    Wait for OpenClaw to finish, then fetch the latest session history via API.
    """
    print(f"   ⏳ Waiting {wait_seconds}s for OpenClaw to scrape LinkedIn...")

    for i in range(wait_seconds):
        time.sleep(1)
        if i % 10 == 9:
            print(f"   ... {i+1}s elapsed")

    # Get session history from OpenClaw API
    print("   📥 Fetching response from OpenClaw session history...")

    req = urllib.request.Request(
        f"{OPENCLAW_URL}/v1/responses",
        data=json.dumps({
            "model": "openclaw",
            "input": "What was the JSON result from the LinkedIn profile you just scraped? Return ONLY the JSON object, nothing else.",
        }).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {OPENCLAW_TOKEN}",
            "x-openclaw-agent-id": "main",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
            text = data.get("output", [{}])[0].get("content", [{}])[0].get("text", "")
            return text
    except Exception as e:
        print(f"   ✗ Error fetching response: {e}")
        return None


def extract_json(text):
    if not text:
        return None
    clean = text.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(clean)
    except Exception:
        pass
    match = re.search(r'\{[\s\S]+\}', text)
    if match:
        try:
            return json.loads(match.group())
        except Exception:
            pass
    return None


def save_to_supabase(phone, profile):
    phone = phone.strip()
    if not phone.startswith("whatsapp:"):
        phone = f"whatsapp:{phone}"

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
            return json.loads(resp.read().decode())
    except Exception as e:
        print(f"   ✗ Save error: {e}")
        return None


def main():
    if len(sys.argv) < 3:
        print("Usage: python3 scrape_linkedin_applescript.py <phone> <linkedin_url>")
        print('Example: python3 scrape_linkedin_applescript.py "+16263756580" "https://linkedin.com/in/joeyeh"')
        sys.exit(1)

    phone = sys.argv[1]
    linkedin_url = sys.argv[2]

    print(f"\n{'='*60}")
    print(f"  SONA LINKEDIN SCRAPER (AppleScript method)")
    print(f"{'='*60}")
    print(f"  Phone:    {phone}")
    print(f"  LinkedIn: {linkedin_url}")
    print(f"{'='*60}")
    print(f"\n  ⚠ Make sure:")
    print(f"  1. OpenClaw is running")
    print(f"  2. Browser Relay extension is ON in Chrome")
    print(f"  3. This terminal has Accessibility permissions")
    print(f"{'='*60}\n")

    input("  Press ENTER when ready...")

    # Step 1: Send prompt to OpenClaw via AppleScript
    success = open_openclaw_and_scrape(linkedin_url)

    if not success:
        print("\n✗ Failed to send prompt to OpenClaw.")
        print("  Make sure System Events has Accessibility access:")
        print("  System Preferences → Privacy & Security → Accessibility → add Terminal")
        sys.exit(1)

    # Step 2: Wait for OpenClaw to finish scraping
    text = wait_and_get_response(linkedin_url, wait_seconds=90)

    if not text:
        print("\n✗ No response from OpenClaw.")
        sys.exit(1)

    # Step 3: Extract JSON
    print("\n📋 Extracting profile data...")
    profile = extract_json(text)

    if not profile or not profile.get("full_name"):
        print("✗ Could not extract valid profile JSON.")
        print("\nOpenClaw said:")
        print(text[:800])
        sys.exit(1)

    print(f"   ✓ Name: {profile.get('full_name')}")
    print(f"   ✓ Role: {profile.get('current_role')} @ {profile.get('current_company')}")
    print(f"   ✓ Experiences: {len(profile.get('experiences', []))}")
    print(f"   ✓ Education: {len(profile.get('education', []))}")
    print(f"   ✓ Skills: {len(profile.get('skills', []))}")

    experiences = profile.get('experiences', [])
    if experiences:
        print(f"\n   Experiences:")
        for exp in experiences:
            print(f"     • {exp.get('title')} @ {exp.get('company')} ({exp.get('dates', '')})")

    # Step 4: Save
    print(f"\n💾 Saving to Supabase...")
    result = save_to_supabase(phone, profile)

    if result:
        print(f"   ✓ Saved!")
        print(f"\n{'='*60}")
        print(f"  ✓ DONE — {profile.get('full_name')} scraped and saved")
        print(f"{'='*60}\n")
    else:
        print(f"   ✗ Failed to save.")
        print(json.dumps(profile, indent=2))


if __name__ == "__main__":
    main()
