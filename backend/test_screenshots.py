import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv(Path(__file__).parent.parent / ".env")

USERNAME = os.getenv("JEEN_USERNAME", "")
PASSWORD = os.getenv("JEEN_PASSWORD", "")
OUTPUT = Path(__file__).parent / "debug_screenshots"


async def main():
    OUTPUT.mkdir(exist_ok=True)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1920, "height": 1080})

        # Step 1
        await page.goto("https://jeenai.app/login", wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(3000)
        await page.screenshot(path=str(OUTPUT / "step1_login_page.png"))
        print(f"[1] Login page — URL: {page.url}")

        # Step 2
        email = page.locator('input[type="email"], input[name="email"]').first
        await email.click()
        await email.press_sequentially(USERNAME, delay=50)
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(3000)
        await page.screenshot(path=str(OUTPUT / "step2_after_email.png"))
        print(f"[2] After email — URL: {page.url}")

        # Step 3
        pwd = page.locator('input[type="password"]').first
        await pwd.click()
        await pwd.press_sequentially(PASSWORD, delay=50)
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(5000)
        await page.screenshot(path=str(OUTPUT / "step3_after_login.png"))
        print(f"[3] After login — URL: {page.url}")

        # Step 4
        await page.goto("https://jeenai.app/chat", wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(5000)
        await page.screenshot(path=str(OUTPUT / "step4_chat_page.png"))
        print(f"[4] Chat page — URL: {page.url}")

        await browser.close()
    print(f"\nAll screenshots saved to: {OUTPUT}")


asyncio.run(main())
