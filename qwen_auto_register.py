#!/usr/bin/env python3
"""
Qwen Auto Register Script
Uses generator.email for temporary emails and automates Qwen registration + verification.
"""

import time
import random
import string
import sys
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# Configuration
QWEN_REGISTER_URL = "https://chat.qwen.ai/auth?mode=register"
EMAIL_BASE_URL = "https://generator.email"
EMAIL_DOMAIN = "Hotmeil.net"
OUTPUT_FILE = "qwen_accounts.txt"
HEADLESS = True

BROWSER_ARGS = [
    '--disable-blink-features=AutomationControlled',
    '--no-sandbox',
    '--disable-dev-shm-usage',
]

USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/124.0.0.0 Safari/537.36'
)

# ──────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────

def gen_prefix(length=10):
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=length))

def gen_password(length=16):
    chars = string.ascii_letters + string.digits + "!@#$%^&*"
    return ''.join(random.choice(chars) for _ in range(length))

def gen_name():
    first = ['Alex', 'Jordan', 'Taylor', 'Morgan', 'Casey', 'Riley',
             'Quinn', 'Avery', 'Blake', 'Cameron', 'Skyler', 'Drew']
    last = ['Smith', 'Johnson', 'Williams', 'Brown', 'Jones', 'Garcia',
            'Miller', 'Davis', 'Martinez', 'Wilson', 'Anderson', 'Taylor']
    return f"{random.choice(first)} {random.choice(last)}"

def save_account(email, password, name):
    with open(OUTPUT_FILE, 'a') as f:
        f.write(f"{email}:{password}\t# {name}\n")
    print(f"  💾 Saved to {OUTPUT_FILE}")

# ──────────────────────────────────────────────────────────
# Email service (generator.email)
# ──────────────────────────────────────────────────────────

def open_email_inbox(context, email):
    """Open generator.email page for the given address"""
    page = context.new_page()
    url = f"{EMAIL_BASE_URL}/{email}"
    print(f"  📥 Opening inbox: {url}")
    page.goto(url, wait_until='domcontentloaded', timeout=60000)
    time.sleep(6)
    return page

def wait_for_email(inbox_page, timeout=300, keywords=('qwen', 'verify', 'confirm', 'activate', 'alibaba')):
    """Poll inbox for an email matching any keyword. Returns the verification URL."""
    print(f"  ⏳ Waiting for verification email (up to {timeout}s)...")
    deadline = time.time() + timeout
    last_count = 0

    while time.time() < deadline:
        try:
            # generator.email auto-refreshes, but click refresh to be safe
            try:
                inbox_page.evaluate("""
                    [...document.querySelectorAll('a, button, span, div')]
                      .find(el => el.innerText && el.innerText.trim() === 'Refresh')
                      ?.click()
                """)
            except Exception:
                pass

            time.sleep(3)

            # Look at email list items in inbox
            items = inbox_page.evaluate("""
                [...document.querySelectorAll('.e7m, .row1, .e7m_lit_box, [id*="mail"], .inbox-message, table.list_emails tr')]
                  .map(el => ({
                    text: el.innerText.trim(),
                    id: el.id || '',
                  }))
                  .filter(e => e.text.length > 5)
            """) or []

            count = len(items)
            if count != last_count:
                print(f"  📨 Inbox has {count} item(s)")
                last_count = count

            for item in items:
                lower = item['text'].lower()
                if any(kw in lower for kw in keywords):
                    print(f"  🎯 Found relevant email: {item['text'][:80]}...")

                    # Click the email
                    if item['id']:
                        inbox_page.evaluate(f"document.getElementById('{item['id']}')?.click()")
                    else:
                        inbox_page.evaluate(f"""
                            [...document.querySelectorAll('.e7m, .row1, table.list_emails tr')]
                              .find(el => el.innerText.includes({repr(item['text'][:50])}))
                              ?.click()
                        """)

                    time.sleep(4)

                    # Extract verification link
                    links = inbox_page.evaluate("""
                        [...document.querySelectorAll('a[href]')]
                          .map(a => a.href)
                          .filter(h => h.startsWith('http'))
                          .filter(h => !h.includes('generator.email') && !h.includes('googleads') && !h.includes('doubleclick'))
                    """) or []

                    # Prefer qwen/verify/activate links
                    for link in links:
                        ll = link.lower()
                        if any(kw in ll for kw in ['qwen', 'verify', 'activate', 'confirm', 'alibaba']):
                            print(f"  🔗 Verify link: {link}")
                            return link

                    # Fallback: first non-tracking link
                    if links:
                        print(f"  🔗 First link found: {links[0]}")
                        return links[0]

        except Exception as e:
            print(f"  ⚠️ Inbox check error: {e}")

        elapsed = int(time.time() - (deadline - timeout))
        if elapsed % 30 == 0 and elapsed > 0:
            print(f"  ⏳ Still waiting... ({elapsed}s)")
        time.sleep(8)

    return None

# ──────────────────────────────────────────────────────────
# Qwen registration
# ──────────────────────────────────────────────────────────

def register_qwen(page, name, email, password):
    """Fill and submit the Qwen registration form."""
    print(f"  📝 Registering Qwen account...")
    page.goto(QWEN_REGISTER_URL, wait_until='domcontentloaded', timeout=30000)
    time.sleep(4)

    try:
        page.fill('input[name="username"]', name)
        time.sleep(0.5)
        page.fill('input[name="email"]', email)
        time.sleep(0.5)
        page.fill('input[name="password"]', password)
        time.sleep(0.5)
        page.fill('input[name="checkPassword"]', password)
        time.sleep(0.5)

        # Check terms
        try:
            page.check('input[type="checkbox"]')
        except Exception:
            pass

        page.click('button:has-text("Create Account")')
        time.sleep(6)

        body = page.evaluate('document.body.innerText') or ''
        if 'pending activation' in body.lower() or 'verification email' in body.lower():
            print("  ✅ Registration submitted, awaiting verification")
            return True
        elif 'error' in body.lower() or 'failed' in body.lower():
            print(f"  ❌ Registration error: {body[:200]}")
            return False
        else:
            print(f"  ⚠️ Unexpected state: {body[:200]}")
            return True  # still try waiting for email

    except Exception as e:
        print(f"  ❌ Form fill error: {e}")
        page.screenshot(path='qwen_form_error.png')
        return False

# ──────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────

def main():
    if len(sys.argv) > 1:
        try:
            num_accounts = int(sys.argv[1])
        except ValueError:
            print("Usage: python3 qwen_auto_register.py <num_accounts>")
            sys.exit(1)
    else:
        try:
            num_accounts = int(input("📊 How many accounts to create? "))
        except (ValueError, EOFError):
            print("❌ Invalid number")
            sys.exit(1)

    print(f"\n🎯 Creating {num_accounts} Qwen account(s)...\n")
    success_count = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS, args=BROWSER_ARGS)
        context = browser.new_context(
            user_agent=USER_AGENT,
            viewport={'width': 1280, 'height': 800},
        )

        for i in range(num_accounts):
            print(f"\n{'═'*60}")
            print(f"🔢 Account {i+1}/{num_accounts}")
            print(f"{'═'*60}")

            # Generate credentials
            prefix = gen_prefix()
            email = f"{prefix}@{EMAIL_DOMAIN}"
            password = gen_password()
            name = gen_name()

            print(f"  📧 Email:    {email}")
            print(f"  👤 Name:     {name}")
            print(f"  🔑 Password: {password}")

            # Open email inbox first (so it's ready to receive)
            inbox = open_email_inbox(context, email)

            # Open Qwen registration
            qwen = context.new_page()
            registered = register_qwen(qwen, name, email, password)

            if not registered:
                print(f"  ❌ Account {i+1} registration failed, skipping...")
                qwen.close()
                inbox.close()
                continue

            # Wait for verification email
            verify_url = wait_for_email(inbox, timeout=300)

            if verify_url:
                print(f"  🔄 Opening verification link...")
                try:
                    qwen.goto(verify_url, wait_until='domcontentloaded', timeout=30000)
                    time.sleep(5)
                    qwen.screenshot(path=f'qwen_verified_{i+1}.png')

                    body = qwen.evaluate('document.body.innerText') or ''
                    print(f"  📋 Verify result: {body[:200]}")

                    save_account(email, password, name)
                    success_count += 1
                    print(f"  ✅ Account {i+1} verified and saved!")
                except Exception as e:
                    print(f"  ❌ Verification click error: {e}")
            else:
                print(f"  ⚠️ Verification email not received within timeout")

            qwen.close()
            inbox.close()

            # Brief pause between accounts
            if i < num_accounts - 1:
                time.sleep(5)

        browser.close()

    print(f"\n{'═'*60}")
    print(f"🎉 Done: {success_count}/{num_accounts} accounts created")
    print(f"💾 Results saved to: {OUTPUT_FILE}")
    print(f"{'═'*60}")

if __name__ == "__main__":
    main()
