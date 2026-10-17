import os
import asyncio
from playwright.async_api import async_playwright

SESSIONS_DIR = "sessions"

async def run_with_saved_session(platform_name: str, check_url: str):
    session_file = os.path.join(SESSIONS_DIR, f"{platform_name}_state.json")
    
    if not os.path.exists(session_file):
        print(f"找不到凭证文件 {session_file}，请先运行 login_helper.py 完成登录。")
        return

    async with async_playwright() as p:
        # 正式爬取时可设为 headless=True 静默运行，这里为了直观验证设为 False
        browser = await p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"]
        )
        
        # 核心：直接加载保存的 storage_state
        print(f"正在注入 {platform_name} 的持久化登录态...")
        context = await browser.new_context(
            storage_state=session_file,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        # 访问个人中心或主页验证登录状态
        print(f"正在访问验证页面: {check_url}")
        await page.goto(check_url)

        # 停留几秒观察页面右上角是否已显示头像/昵称
        print("已打开页面，请检查是否处于已登录状态（等待 8 秒）...")
        await asyncio.sleep(8)

        await context.close()
        await browser.close()
        print("验证结束。")

if __name__ == "__main__":
    platform = "bilibili"
    # 访问 B站 首页或个人中心验证是否已登录
    target_page = "https://www.bilibili.com"
    
    asyncio.run(run_with_saved_session(platform, target_page))