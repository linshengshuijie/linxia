import os
import sys
import asyncio
from playwright.async_api import async_playwright
from config import SESSIONS_DIR, DEFAULT_USER_AGENT, PLATFORMS_CONFIG

def get_platform_info(platform_key: str):
    return PLATFORMS_CONFIG.get(platform_key.lower())

async def capture_session(platform_key: str):
    platform_info = get_platform_info(platform_key)
    if not platform_info:
        print(f"不支持的平台: {platform_key}，目前可选: {list(PLATFORMS_CONFIG.keys())}")
        return

    login_url = platform_info["login_url"]
    save_file = os.path.join(SESSIONS_DIR, f"{platform_key}_state.json")

    print(f"正在启动 {platform_key} 登录授权助手...")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"]
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=DEFAULT_USER_AGENT
        )
        page = await context.new_page()
        await page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")

        print(f"打开登录界面: {login_url}")
        await page.goto(login_url)

        print("\n请在弹出的浏览器中完成登录（扫码/密码/验证码）。")
        print("登录成功并看到个人主页后，回到本终端按 [Enter] 保存凭据...")
        
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, input)

        await context.storage_state(path=save_file)
        print(f"凭据已持久化保存至: {save_file}")

        await context.close()
        await browser.close()

def show_available_sessions():
    files = [f.replace("_state.json", "") for f in os.listdir(SESSIONS_DIR) if f.endswith("_state.json")]
    print("\n当前本地已持久化的凭据列表:")
    if not files:
        print("  (暂无已存凭据)")
    for name in files:
        print(f"  {name}")
    print()

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "list":
        show_available_sessions()
    elif len(sys.argv) > 1:
        asyncio.run(capture_session(sys.argv[1]))
    else:
        print("使用方式:")
        print("  python login_helper.py bilibili   # 登录并保存 bilibili 凭据")
        print("  python login_helper.py list       # 列出当前所有已存凭据")