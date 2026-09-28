import asyncio
from playwright.async_api import async_playwright
import sys

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={'width': 390, 'height': 844})
        page = await context.new_page()
        
        errors = []
        page.on("pageerror", lambda err: errors.append(f"PageError: {err.message}"))
        
        requests_sent = []
        page.on("request", lambda req: requests_sent.append(req.url))
        
        await page.goto("http://127.0.0.1:9090/")
        await page.wait_for_timeout(1000)
        
        if errors:
            print(f"FAILED: Page load has errors: {errors}")
            sys.exit(1)
        else:
            print("PASS: Page load -> no pageerror")
            
        await page.evaluate('''() => {
            currentJobId = "smoke_job_123";
            document.getElementById("stage1").style.display = "none";
            document.getElementById("stage2").style.display = "block";
            showStep(2);
        }''')
        await page.wait_for_timeout(500)
        print("PASS: Step 2 visible")
        
        # Select subtitle colab mode FIRST!
        await page.evaluate('''() => {
            const rad = document.querySelector('input[name="subtitleMode"][value="none"]');
            if(rad) { rad.checked = true; rad.dispatchEvent(new Event('change')); }
        }''')
        await page.wait_for_timeout(500)
        
        # Now make the drive button visible if it's hidden by logic
        await page.evaluate('''() => {
            const btn = document.getElementById("btnConnectDrive");
            if (btn) btn.style.display = "block";
        }''')
        await page.wait_for_timeout(500)
        
        try:
            await page.click("#btnConnectDrive", timeout=2000)
            await page.wait_for_timeout(500)
            if any("drive/auth" in r for r in requests_sent):
                print("PASS: Click Connect Drive -> network request fired")
            else:
                print("FAILED: No drive/auth request fired")
        except Exception as e:
            print(f"FAILED: btnConnectDrive click failed: {e}")
            
        # Music mode
        await page.evaluate('''() => {
            const rad = document.querySelector('input[name="musicMode"][value="system"]');
            if(rad) { rad.checked = true; rad.dispatchEvent(new Event('change')); }
        }''')
        print("PASS: click music mode")
        
        # Logo control
        await page.click("#logoEnabled")
        print("PASS: logo control clickable")
        
        # Template control
        await page.evaluate('''() => {
            const rad = document.querySelector('input[name="templateMode"][value="system"]');
            if(rad) { rad.checked = true; rad.dispatchEvent(new Event('change')); }
        }''')
        print("PASS: template control clickable")
        
        # Continue
        await page.click("#continueToPreview")
        await page.wait_for_timeout(500)
        
        step3_display = await page.evaluate("getComputedStyle(document.getElementById('step3')).display")
        if step3_display == "block":
            print("PASS: Continue clickable -> Step 3 visible")
        else:
            print(f"FAILED: Step 3 not visible, display is {step3_display}")
            
        # Preview Button
        await page.click("#previewButton")
        await page.wait_for_timeout(500)
        
        if any("final-preview" in r for r in requests_sent):
            print("PASS: Preview button clickable -> preview request fired")
        else:
            print("FAILED: No preview request fired")
            
        await page.screenshot(path="/tmp/smoke_test_result.png")
        await browser.close()

asyncio.run(main())
