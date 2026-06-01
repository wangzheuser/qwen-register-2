#!/usr/bin/env python3
"""
Qwen Auto Register Script v2 - With Proxy Support
Uses generator.email for temporary emails and automates Qwen registration + verification.
Supports rotating proxies from proxy.txt file.
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
PROXY_FILE = "proxy.txt"
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
# Proxy Management
# ──────────────────────────────────────────────────────────

class ProxyRotator:
    """Manages rotating proxies from proxy.txt"""
    
    def __init__(self, proxy_file=PROXY_FILE):
        self.proxies = []
        self.current_index = 0
        self.load_proxies(proxy_file)
    
    def load_proxies(self, proxy_file):
        """Load proxies from file. Format: hostname:port:username:password"""
        try:
            with open(proxy_file, 'r') as f:
                lines = [line.strip() for line in f if line.strip() and not line.startswith('#')]
            
            if not lines:
                print(f"⚠️  No proxies found in {proxy_file}")
                return
            
            self.proxies = lines
            print(f"✅ Loaded {len(self.proxies)} proxy(ies)")
            for i, p in enumerate(self.proxies[:3]):
                print(f"   [{i+1}] {p.split(':')[0]}:{p.split(':')[1]}")
            if len(self.proxies) > 3:
                print(f"   ... and {len(self.proxies) - 3} more")
        
        except FileNotFoundError:
            print(f"⚠️  {proxy_file} not found. Running without proxy.")
            self.proxies = []
    
    def get_next(self):
        """Get next proxy in rotation"""
        if not self.proxies:
            return None
        
        proxy = self.proxies[self.current_index]
        self.current_index = (self.current_index + 1) % len(self.proxies)
        return proxy
    
    def parse_proxy(self, proxy_str):
        """Parse proxy string to dict for Playwright"""
        if not proxy_str:
            return None
        
        parts = proxy_str.split(':')
        if len(parts) == 4:
            hostname, port, username, password = parts
            return {
                'server': f'http://{hostname}:{port}',
                'username': username,
                'password': password,
            }
        elif len(parts) == 2:
            hostname, port = parts
            return {
                'server': f'http://{hostname}:{port}',
            }
        else:
            print(f"⚠️  Invalid proxy format: {proxy_str}")
            return None

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

def wait_for_email(inbox_page, timeout=300, keywords=('qwen', 'alibaba')):
    """Poll inbox for an email matching any keyword. Returns the verification URL."""
    print(f"  ⏳ Waiting for verification email (up to {timeout}s)...")
    deadline = time.time() + timeout
    last_count = 0

    while time.time() < deadline:
        try:
            # Refresh inbox
            try:
                inbox_page.evaluate("""
                    [...document.querySelectorAll('a, button, span, div')]
                      .find(el => el.innerText && el.innerText.trim() === 'Refresh')
                      ?.click()
                """)
            except Exception:
                pass

            time.sleep(3)

            # Look for email items with class e7m list-group-item-info (generator.email structure)
            items = inbox_page.evaluate("""
                [...document.querySelectorAll('div.e7m.list-group-item')]
                  .filter(el => !el.className.includes('active'))
                  .map(el => ({
                    text: el.innerText.trim(),
                    html: el.outerHTML,
                  }))
                  .filter(e => e.text.length > 5)
            """) or []

            count = len(items)
            if count != last_count:
                print(f"  📨 Inbox has {count} email(s)")
                last_count = count

            for item in items:
                lower = item['text'].lower()
                # Only match qwen/alibaba (strict keywords)
                if any(kw in lower for kw in keywords):
                    print(f"  🎯 Found Qwen email: {item['text'][:80]}...")

                    # Click the email using the element itself
                    try:
                        inbox_page.evaluate(f"""
                            const html = {repr(item['html'])};
                            const items = [...document.querySelectorAll('div.e7m.list-group-item')];
                            const target = items.find(el => el.outerHTML === html);
                            if (target) target.click();
                        """)
                        time.sleep(6)
                    except Exception as click_err:
                        print(f"  ⚠️ Click error (non-fatal): {click_err}")
                        time.sleep(4)

                    # Extract verification link
                    try:
                        links = inbox_page.evaluate("""
                            [...document.querySelectorAll('a[href]')]
                              .map(a => a.href)
                              .filter(h => h.startsWith('http'))
                              .filter(h => !h.includes('generator.email') && !h.includes('googleads') && !h.includes('doubleclick'))
                        """) or []
                    except Exception as e:
                        print(f"  ⚠️ Link extract retry: {e}")
                        time.sleep(3)
                        try:
                            links = inbox_page.evaluate("""
                                [...document.querySelectorAll('a[href]')]
                                  .map(a => a.href)
                                  .filter(h => h.startsWith('http'))
                                  .filter(h => !h.includes('generator.email') && !h.includes('googleads') && !h.includes('doubleclick'))
                            """) or []
                        except Exception:
                            links = []

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

    try:
        page.goto(QWEN_REGISTER_URL, wait_until='domcontentloaded', timeout=60000)
        # Wait for form to be fully interactive
        page.wait_for_selector('input[name="username"]', timeout=20000)
        time.sleep(3)

        # Fill form with explicit waits
        page.fill('input[name="username"]', name)
        time.sleep(0.8)
        page.fill('input[name="email"]', email)
        time.sleep(0.8)
        page.fill('input[name="password"]', password)
        time.sleep(0.8)
        page.fill('input[name="checkPassword"]', password)
        time.sleep(0.8)

        # Check terms
        try:
            page.check('input[type="checkbox"]')
            time.sleep(0.5)
        except Exception:
            pass

        # Click submit and wait for navigation
        submit_btn = page.locator('button:has-text("Create Account")')
        submit_btn.scroll_into_view_if_needed()
        time.sleep(1)
        
        try:
            with page.expect_navigation(wait_until='domcontentloaded', timeout=30000):
                submit_btn.click()
        except PlaywrightTimeout:
            # Navigation timeout is OK — form may have submitted without redirect
            pass

        time.sleep(5)

        body = page.evaluate('document.body.innerText') or ''
        if 'pending activation' in body.lower() or 'verification email' in body.lower():
            print("  ✅ Registration submitted, awaiting verification")
            return True
        elif 'error' in body.lower() or 'failed' in body.lower():
            print(f"  ❌ Registration error: {body[:200]}")
            return False
        else:
            # Assume success if form is gone
            if 'create account' not in body.lower():
                print("  ✅ Registration likely submitted")
                return True
            print(f"  ⚠️ Unexpected state: {body[:200]}")
            return True

    except PlaywrightTimeout as e:
        print(f"  ⚠️ Timeout: {str(e)[:100]}")
        try:
            body = page.evaluate('document.body.innerText') or ''
            if 'create account' not in body.lower():
                print("  ✅ Form likely submitted despite timeout")
                return True
        except Exception:
            pass
        return False
    except Exception as e:
        print(f"  ❌ Form fill error: {e}")
        try:
            page.screenshot(path='qwen_form_error.png')
        except Exception:
            pass
        return False

# ──────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────

def main():
    if len(sys.argv) > 1:
        try:
            num_accounts = int(sys.argv[1])
        except ValueError:
            print("Usage: python3 qwenv2.py <num_accounts>")
            sys.exit(1)
    else:
        try:
            num_accounts = int(input("📊 How many accounts to create? "))
        except (ValueError, EOFError):
            print("❌ Invalid number")
            sys.exit(1)

    # Load proxies
    proxy_rotator = ProxyRotator(PROXY_FILE)

    print(f"\n🎯 Creating {num_accounts} Qwen account(s)...\n")
    success_count = 0

    with sync_playwright() as p:
        for i in range(num_accounts):
            print(f"\n{'═'*60}")
            print(f"🔢 Account {i+1}/{num_accounts}")
            print(f"{'═'*60}")

            # Get next proxy
            proxy_str = proxy_rotator.get_next()
            proxy_dict = proxy_rotator.parse_proxy(proxy_str) if proxy_str else None
            
            if proxy_dict:
                print(f"  🌐 Using proxy: {proxy_str.split(':')[0]}:{proxy_str.split(':')[1]}")
            else:
                print(f"  🌐 No proxy (direct connection)")

            # Launch browser with proxy
            try:
                browser = p.chromium.launch(
                    headless=HEADLESS,
                    args=BROWSER_ARGS,
                    proxy=proxy_dict
                )
            except Exception as e:
                print(f"  ❌ Browser launch error: {e}")
                continue

            context = browser.new_context(
                user_agent=USER_AGENT,
                viewport={'width': 1280, 'height': 800},
            )

            # Generate credentials
            prefix = gen_prefix()
            email = f"{prefix}@{EMAIL_DOMAIN}"
            password = gen_password()
            name = gen_name()

            print(f"  📧 Email:    {email}")
            print(f"  👤 Name:     {name}")
            print(f"  🔑 Password: {password}")

            try:
                # Open email inbox first
                inbox = open_email_inbox(context, email)

                # Open Qwen registration
                qwen = context.new_page()
                registered = register_qwen(qwen, name, email, password)

                if not registered:
                    print(f"  ❌ Account {i+1} registration failed, skipping...")
                    qwen.close()
                    inbox.close()
                    browser.close()
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
                        print(f"  ❌ Verification error: {e}")
                else:
                    print(f"  ⚠️ Verification email not received within timeout")

                qwen.close()
                inbox.close()

            except Exception as e:
                print(f"  ❌ Account {i+1} error: {e}")

            finally:
                browser.close()

            # Brief pause between accounts
            if i < num_accounts - 1:
                time.sleep(5)

    print(f"\n{'═'*60}")
    print(f"🎉 Done: {success_count}/{num_accounts} accounts created")
    print(f"💾 Results saved to: {OUTPUT_FILE}")
    print(f"{'═'*60}")

if __name__ == "__main__":
    main()
