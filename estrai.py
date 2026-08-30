import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=['--no-sandbox'])
        page = await browser.new_page()
        await page.set_extra_http_headers({'Referer': 'https://ondemand.st'})

        # Intercetta le richieste .m3u8
        async def on_request(request):
            url = request.url
            if '.m3u8' in url:
                print(f'\n🎯 LINK M3U8 TROVATO:\n{url}\n')
        page.on('request', on_request)

        print('📡 Vado su ondemand.st...')
        await page.goto('https://ondemand.st', wait_until='networkidle', timeout=60000)
        await page.wait_for_timeout(3000)

        # Prova a cliccare su Live TV
        selectors = ['a[href*="live"]', 'button:has-text("Live TV")', 'text=Live TV']
        for sel in selectors:
            try:
                await page.click(sel, timeout=3000)
                print('🖱️ Cliccato su Live TV')
                break
            except:
                continue

        # Attendi caricamento player
        print('⏳ Attendo 15 secondi...')
        await page.wait_for_timeout(15000)

        await browser.close()

asyncio.run(main())