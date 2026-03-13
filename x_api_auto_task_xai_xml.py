# -*- coding: utf-8 -*-
"""
grok_xai_xml_pipeline.py  v8.0 (Grok Web UI 抓取 + xAI SDK XML 深度提纯)
Architecture: Playwright(Grok Web) -> JSONL -> xAI SDK (XML Prompt) -> Feishu/WeChat UI
"""

import os
import re
import json
import time
import base64
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from requests.exceptions import ConnectionError, Timeout
from playwright.sync_api import sync_playwright

# 🚨 引入官方 xAI SDK
from xai_sdk import Client
from xai_sdk.chat import user, system

# -- 环境变量 -----------------------------------------------------
JIJYUN_WEBHOOK_URL  = os.getenv("JIJYUN_WEBHOOK_URL", "")
SF_API_KEY          = os.getenv("SF_API_KEY", "")
XAI_API_KEY         = os.getenv("XAI_API_KEY", "")    
IMGBB_API_KEY       = os.getenv("IMGBB_API_KEY", "") 

GROK_COOKIES_JSON   = os.getenv("SUPER_GROK_COOKIES", "")
PAT_FOR_SECRETS     = os.getenv("PAT_FOR_SECRETS", "")
GITHUB_REPOSITORY   = os.getenv("GITHUB_REPOSITORY", "")

# -- 全局超时设置 ---------------------------------------------------
_START_TIME      = time.time()
PHASE1_DEADLINE  = 40 * 60   # 第一阶段最多 40 分钟
GLOBAL_DEADLINE  = 85 * 60   # 全局最多 85 分钟

TEST_MODE = os.getenv("TEST_MODE_ENV", "false").lower() == "true"

# -- 80 accounts (中文圈 AI/出海/独立开发者/创业者) ----------------------------
ALL_ACCOUNTS = [
    "dotey", "op7418", "Gorden_Sun", "xiaohu", "shao__meng", "thinkingjimmy", "nishuang", "vista8", "lijigang", "kaifulee", "WaytoAGI", "oran_ge", "AlchainHust", "haibun",
    "SamuelQZQ", "elliotchen100", "berryxia", "lidangzzz", "lxfater", "Fenng", "turingou", "tinyfool", "virushuo", "fankaishuoai", "XDash", "idoubicc", "Cydiar404", "JefferyTatsuya",
    "CoderJeffLee", "tuturetom", "iamtonyzhu", "Valley101_Qi", "AIMindCo", "AlanChenFun", "AuroraAIDev", "maboroshii", "nicekateyes", "paborobot", "porkybun", "0xDragonMaster", "LittleStar",
    "tualatrix", "luinlee", "seclink", "XiaohuiAI666", "gefei55", "AI_Jasonyu", "JourneymanChina", "dev_afei", "GoSailGlobal", "chuhaiqu", "daluoseo", "realNyarime", "DigitalNomadLC",
    "RocM301", "shuziyimin", "itangtalk", "guishou_56", "9yearfish", "OwenYoungZh", "waylybaye", "randyloop", "livid", "shengxj1", "FinanceYF5", "fkysly", "zhixianio",
    "hongming731", "penny777", "jiqizhixin", "evilcos", "wshuy", "Web3Yolanda", "maboroshi", "CryptoMasterAI", "AIProductDaily", "aigclink", "founder_park", "geekpark", "pingwest",
]

# -- 测试模式专用名单 (只扫第一批 14 人) ---------------------------------------------
BATCH1_ACCOUNTS = [
    "dotey", "op7418", "Gorden_Sun", "xiaohu", "shao__meng", "thinkingjimmy", "nishuang", 
    "vista8", "lijigang", "kaifulee", "WaytoAGI", "oran_ge", "AlchainHust", "haibun",
]

def get_feishu_webhooks() -> list:
    urls = []
    for suffix in ["", "_1", "_2", "_3"]:
        url = os.getenv(f"FEISHU_WEBHOOK_URL{suffix}", "")
        if url: urls.append(url)
    return urls

def get_dates() -> tuple:
    tz = timezone(timedelta(hours=8))
    today = datetime.now(tz)
    yesterday = today - timedelta(days=1)
    return today.strftime("%Y-%m-%d"), yesterday.strftime("%Y-%m-%d")


# ==============================================================================
# 🕸️ 网页版 Grok 自动化会话管理 (Playwright)
# ==============================================================================
def prepare_session_file() -> bool:
    if not GROK_COOKIES_JSON:
        print("[Session] Warning: SUPER_GROK_COOKIES not configured", flush=True)
        return False
    try:
        data = json.loads(GROK_COOKIES_JSON)
        if isinstance(data, dict) and "cookies" in data:
            with open("session_state.json", "w", encoding="utf-8") as f:
                json.dump(data, f)
            print("[Session] OK Playwright storage-state format (renewed)", flush=True)
            return True
        else:
            print(f"[Session] OK Cookie-Editor array format ({len(data)} entries)", flush=True)
            return False
    except Exception as e:
        print(f"[Session] ERROR Parse failed: {e}", flush=True)
        return False

def load_raw_cookies(context):
    try:
        cookies = json.loads(GROK_COOKIES_JSON)
        formatted = []
        for c in cookies:
            cookie = {"name": c.get("name", ""), "value": c.get("value", ""), "domain": c.get("domain", ".grok.com"), "path": c.get("path", "/")}
            if "httpOnly" in c: cookie["httpOnly"] = c["httpOnly"]
            if "secure" in c: cookie["secure"] = c["secure"]
            ss = c.get("sameSite", "")
            if ss in ("Strict", "Lax", "None"): cookie["sameSite"] = ss
            formatted.append(cookie)
        context.add_cookies(formatted)
        print(f"[Session] OK Injected {len(formatted)} cookies", flush=True)
    except Exception as e:
        print(f"[Session] ERROR Cookie injection failed: {e}", flush=True)

def save_and_renew_session(context):
    try:
        context.storage_state(path="session_state.json")
        print("[Session] OK Storage state saved locally", flush=True)
    except Exception as e:
        print(f"[Session] ERROR Save storage state failed: {e}", flush=True)
        return

def enable_grok4_beta(page):
    print("\n[Model] Trying to enable Beta Toggle...", flush=True)
    selectors = ["button:has-text('Fast')", "button:has-text('Auto')", "button:has-text('Grok')", "button[aria-label*='model' i]", "button[data-testid*='model' i]"]
    model_btn = None
    for sel in selectors:
        try:
            model_btn = page.wait_for_selector(sel, timeout=4000)
            if model_btn: break
        except: continue
    if not model_btn: return
    try:
        model_btn.click()
        time.sleep(1)
        toggle = page.wait_for_selector("button[role='switch'], input[type='checkbox']", timeout=6000)
        is_on = page.evaluate("""() => { const sw = document.querySelector("button[role='switch']"); if (sw) return sw.getAttribute('aria-checked') === 'true' || sw.getAttribute('data-state') === 'checked'; const cb = document.querySelector("input[type='checkbox']"); return cb ? cb.checked : false; }""")
        if not is_on: toggle.click()
        page.keyboard.press("Escape")
        time.sleep(0.5)
    except Exception as e: pass

def _is_login_page(url: str) -> bool:
    lower = url.lower()
    return any(kw in lower for kw in ("sign", "login", "oauth", "x.com/i/flow"))

def open_grok_page(context):
    page = context.new_page()
    try:
        page.goto("https://grok.com", wait_until="domcontentloaded", timeout=60000)
        time.sleep(3)
        if _is_login_page(page.url):
            print("ERROR: Not logged in - session expired", flush=True)
            page.close()
            return None
        enable_grok4_beta(page)
        return page
    except Exception as e:
        try: page.close()
        except: pass
        return None

def send_prompt(page, prompt_text, label):
    page.wait_for_selector("div[contenteditable='true'], textarea", timeout=30000)
    ok = page.evaluate("""(text) => { const el = document.querySelector("div[contenteditable='true']") || document.querySelector("textarea"); if (!el) return false; el.focus(); document.execCommand('selectAll', false, null); document.execCommand('delete', false, null); document.execCommand('insertText', false, text); return el.textContent.length > 0 || el.value?.length > 0; }""", prompt_text)
    if not ok:
        inp = page.query_selector("div[contenteditable='true'], textarea")
        if inp:
            inp.click()
            page.keyboard.press("Control+a")
            page.keyboard.press("Backspace")
            for i in range(0, len(prompt_text), 500):
                page.keyboard.type(prompt_text[i:i+500])
                time.sleep(0.2)
    time.sleep(1.5)
    try:
        send_btn = page.wait_for_selector("button[aria-label='Submit']:not([disabled]), button[aria-label='Send message']:not([disabled]), button[type='submit']:not([disabled])", timeout=30000, state="visible")
        send_btn.click()
    except Exception as e:
        page.evaluate("""() => { const btn = document.query_selector("button[type='submit']") || document.query_selector("button[aria-label='Submit']") || document.query_selector("button[aria-label='Send message']"); if (btn) btn.click(); }""")
    print(f"[{label}] OK Prompt Sent", flush=True)
    time.sleep(5)

def wait_and_extract(page, label, interval=3, stable_rounds=4, max_wait=120, extend_if_growing=False, min_len=80):
    last_len, stable, elapsed, last_text = -1, 0, 0, ""
    while elapsed < max_wait:
        time.sleep(interval)
        elapsed += interval
        try: text = page.evaluate("""() => { const msgs = document.querySelectorAll('[data-testid="message"], .message-bubble, .response-content'); return msgs.length ? msgs[msgs.length - 1].innerText : ""; }""")
        except: return last_text.strip()
        last_text = text
        cur_len = len(text.strip())
        if cur_len == last_len and cur_len >= min_len:
            stable += 1
            if stable >= stable_rounds: return text.strip()
        else:
            stable = 0
            last_len = cur_len
    return last_text.strip()

def parse_jsonlines(text: str) -> list:
    results = []
    for line in text.splitlines():
        line = line.strip()
        if not line or not line.startswith('{') or not line.endswith('}'): continue
        try: results.append(json.loads(line))
        except: continue
    return results

# ==============================================================================
# 🤖 抓取策略 Prompts (让网页版Grok去当爬虫)
# ==============================================================================
def build_phase1_prompt(accounts: list) -> str:
    rounds = [accounts[i:i+3] for i in range(0, len(accounts), 3)]
    rounds_text = "\n".join(f"Round {i+1}: {' | '.join(r)}" for i, r in enumerate(rounds))
    return (
        "You are an X/Twitter data collection tool. Search the following accounts and output pure JSON Lines format.\n\n"
        "[Search Rules]\n"
        "1. Search each account individually: x_keyword_search query=from:AccountName, mode=Latest, limit=10\n"
        "2. Execute in parallel rounds (3 accounts per round)\n"
        "3. Output newest 3 posts + 1 metadata row per account\n\n"
        f"[Account List]\n{rounds_text}\n\n"
        "[Output Format (JSON Lines ONLY)]\n"
        '  Post: {"a":"AccountName","l":likes,"t":"MMDD","s":"English summary","tag":"raw"}\n'
        '  Meta: {"a":"AccountName","type":"meta","total":count,"max_l":max_likes,"latest":"MMDD"}\n'
    )

def build_phase2_s_prompt(accounts: list) -> str:
    rounds = [accounts[i:i+3] for i in range(0, len(accounts), 3)]
    rounds_text = "\n".join(f"Round {i+1}: {' | '.join(r)}" for i, r in enumerate(rounds))
    return (
        "You are an X/Twitter data collection tool. Deep-collect S-tier accounts, output pure JSON Lines.\n\n"
        "1. x_keyword_search query=from:AccountName, mode=Latest, limit=10\n"
        "2. Output all 10 posts\n"
        f"[S-tier Accounts]\n{rounds_text}\n\n"
        "[Output Format (JSON Lines ONLY)]\n"
        '  Normal: {"a":"Name","l":likes,"t":"MMDD","s":"English summary","tag":"raw"}\n'
        '  Quote:  {"a":"Name","l":likes,"t":"MMDD","s":"summary","qt":"@orig: summary","tag":"raw"}\n'
    )

def run_grok_batch(context, accounts: list, prompt_builder, label: str) -> list:
    if not accounts: return []
    page = open_grok_page(context)
    if not page: return []
    try:
        prompt = prompt_builder(accounts)
        send_prompt(page, prompt, label)
        print(f"[{label}] Waiting 60s for Grok to start searching...", flush=True)
        time.sleep(60)
        raw_text = wait_and_extract(page, label, interval=5, stable_rounds=5, max_wait=420, extend_if_growing=True, min_len=50)
        results = parse_jsonlines(raw_text)
        print(f"[{label}] OK Parsed {len(results)} JSON objects", flush=True)
        return results
    except Exception as e: print(f"[{label}] ERROR: {e}", flush=True); return []
    finally:
        try: page.close()
        except: pass

def classify_accounts(meta_results: dict) -> dict:
    tz = timezone(timedelta(hours=8))
    today = datetime.now(tz)
    classification = {}
    for account, meta in meta_results.items():
        total, max_l, latest = meta.get("total", 0), meta.get("max_l", 0), meta.get("latest", "NA")
        if total == 0 or latest == "NA":
            classification[account] = "inactive"
            continue
        try:
            mm, dd = int(latest[:2]), int(latest[2:])
            latest_date = today.replace(month=mm, day=dd)
            if latest_date > today: latest_date = latest_date.replace(year=today.year - 1)
            days_since = (today - latest_date).days
        except: days_since = 999
        if days_since > 30: classification[account] = "inactive"
        elif max_l > 3000 and days_since <= 7: classification[account] = "S"
        elif max_l > 800 and days_since <= 14: classification[account] = "A"
        else: classification[account] = "B"
    return classification


# ==============================================================================
# 🚀 第二阶段：纯 XML 提示词与 xAI 提纯 (完美接入 V7.11 核心)
# ==============================================================================
def _build_xml_prompt(combined_jsonl: str, today_str: str) -> str:
    return f"""
你是一位顶级的中文互联网科技/出海领域投资分析师，拥有10年经验。
分析过去24小时内，中文圈AI创业者、出海开发者、独立开发者、SaaS创始人在X上的推文。
过滤掉日常闲聊，提炼出有"创业参考价值"和"出海实操价值"的犀利洞察。

【重要纪律】
1. 只允许输出纯文本内容，严格按照以下 XML 标签结构填入信息。不要缺漏闭合标签。禁止输出 Markdown 符号（如 #, *）。
2. 🚨【动态封面指令】COVER标签的prompt属性中，请务必根据今日最火爆、最核心的出海/开发话题，**自动决定最契合的英文美术风格（例如：Digital Nomad, Vaporwave, Cyberpunk, 3D render, Minimalist Tech等）**，并生成极具视觉冲击力的图生图提示词。
3. 🚨【翻译铁律】TWEET 标签内容必须以中文为主体翻译！严禁直接复制纯英文！保留圈内黑话（如 MRR, PMF, SaaS等）。

【输出结构规范】
<REPORT>
  <COVER title="5-10字中文爆款标题" prompt="100字英文图生图提示词（根据今日内容动态选择最佳画风）" insight="30字内核心洞察，中文"/>
  <PULSE>用一句话总结今日最核心的 1-2 个出海/搞钱动态信号。</PULSE>
  
  <THEMES>
    <THEME type="new" emoji="💰">
      <TITLE>主题标题：副标题</TITLE>
      <NARRATIVE>一句话核心判断，说清楚“什么在变化、为什么重要”（直接输出观点，不带前缀）</NARRATIVE>
      <TWEET account="X账号名" role="中文身份标签">具体行为 + 创业/出海视角解读（中文为主，限60字内）</TWEET>
      <TWEET account="..." role="...">...</TWEET>
      <OUTLOOK>对该现象的深度解读与未来变现展望</OUTLOOK>
      <OPPORTUNITY>具体的出海实操机会、变现路径或搞钱思路</OPPORTUNITY>
      <RISK>踩坑预警：可能面临的失败教训、封号、合规等风险</RISK>
    </THEME>
  </THEMES>

  <MONEY_RADAR>
    <ITEM category="变现快讯">具体的MRR增长、收入数据、被验证的商业模式等。</ITEM>
    <ITEM category="出海渠道">海外市场洞察、流量获取打法、增长黑客手段。</ITEM>
    <ITEM category="工具推荐">被多位开发者提及或强烈推荐的 AI 工具、SaaS、效率神器。</ITEM>
  </MONEY_RADAR>

  <RISK_AND_TRENDS>
    <ITEM category="踩坑预警">平台政策变化、被封禁的风险、开发过程中遇到的技术/运营大坑。</ITEM>
    <ITEM category="趋势判断">未来 1-3 个月的独立开发或出海赛道趋势。</ITEM>
  </RISK_AND_TRENDS>

  <TOP_PICKS>
    <TWEET account="..." role="...">实操价值最大或点赞极高的原味金句（中文精译）</TWEET>
  </TOP_PICKS>
</REPORT>

# 原始数据输入 (JSONL):
{combined_jsonl}
# 日期: {today_str}
"""

def llm_call_xai(combined_jsonl: str, today_str: str) -> str:
    api_key = XAI_API_KEY.strip()
    if not api_key: 
        print("[LLM/xAI] Error: XAI_API_KEY is missing!")
        return ""
    prompt = _build_xml_prompt(combined_jsonl[:100000], today_str)
    model_name = "grok-4.20-beta-latest-non-reasoning" 
    client = Client(api_key=api_key)
    print(f"\n[LLM/xAI] Requesting {model_name} via Official xai-sdk...", flush=True)
    for attempt in range(1, 4):
        try:
            chat = client.chat.create(model=model_name)
            chat.append(system("You are a professional analytical bot. You strictly output in XML format as instructed. Do not ignore the translation rules."))
            chat.append(user(prompt))
            result = chat.sample().content.strip()
            print(f"[LLM/xAI] OK Response received ({len(result)} chars)", flush=True)
            return result
        except Exception as e: 
            print(f"[LLM/xAI] attempt {attempt} failed: {e}")
            time.sleep(2 ** attempt)
    return ""

def parse_llm_xml(xml_text: str) -> dict:
    data = {"cover": {"title": "", "prompt": "", "insight": ""}, "pulse": "", "themes": [], "money_radar": [], "risk_and_trends": [], "top_picks": []}
    if not xml_text: return data

    cover_match = re.search(r'<COVER\s+title=[\'"“”](.*?)[\'"“”]\s+prompt=[\'"“”](.*?)[\'"“”]\s+insight=[\'"“”](.*?)[\'"“”]\s*/?>', xml_text, re.IGNORECASE | re.DOTALL)
    if not cover_match: cover_match = re.search(r'<COVER\s+title="(.*?)"\s+prompt="(.*?)"\s+insight="(.*?)"\s*/?>', xml_text, re.IGNORECASE | re.DOTALL)
    if cover_match: data["cover"] = {"title": cover_match.group(1).strip(), "prompt": cover_match.group(2).strip(), "insight": cover_match.group(3).strip()}
        
    pulse_match = re.search(r'<PULSE>(.*?)</PULSE>', xml_text, re.IGNORECASE | re.DOTALL)
    if pulse_match: data["pulse"] = pulse_match.group(1).strip()
        
    for theme_match in re.finditer(r'<THEME([^>]*)>(.*?)</THEME>', xml_text, re.IGNORECASE | re.DOTALL):
        attrs = theme_match.group(1)
        theme_body = theme_match.group(2)
        emoji_m = re.search(r'emoji\s*=\s*[\'"“”](.*?)[\'"“”]', attrs, re.IGNORECASE)
        emoji = emoji_m.group(1).strip() if emoji_m else "💡"
        t_tag = re.search(r'<TITLE>(.*?)</TITLE>', theme_body, re.IGNORECASE | re.DOTALL)
        theme_title = t_tag.group(1).strip() if t_tag else ""
        narrative_match = re.search(r'<NARRATIVE>(.*?)</NARRATIVE>', theme_body, re.IGNORECASE | re.DOTALL)
        narrative = narrative_match.group(1).strip() if narrative_match else ""
        
        tweets = []
        for t_match in re.finditer(r'<TWEET\s+account=[\'"“”](.*?)[\'"“”]\s+role=[\'"“”](.*?)[\'"“”]>(.*?)</TWEET>', theme_body, re.IGNORECASE | re.DOTALL):
            tweets.append({"account": t_match.group(1).strip(), "role": t_match.group(2).strip(), "content": t_match.group(3).strip()})
        if not tweets:
            for t_match in re.finditer(r'<TWEET\s+account="(.*?)"\s+role="(.*?)">(.*?)</TWEET>', theme_body, re.IGNORECASE | re.DOTALL):
                tweets.append({"account": t_match.group(1).strip(), "role": t_match.group(2).strip(), "content": t_match.group(3).strip()})
        
        out_match = re.search(r'<OUTLOOK>(.*?)</OUTLOOK>', theme_body, re.IGNORECASE | re.DOTALL)
        outlook = out_match.group(1).strip() if out_match else ""
        opp_match = re.search(r'<OPPORTUNITY>(.*?)</OPPORTUNITY>', theme_body, re.IGNORECASE | re.DOTALL)
        opportunity = opp_match.group(1).strip() if opp_match else ""
        risk_match = re.search(r'<RISK>(.*?)</RISK>', theme_body, re.IGNORECASE | re.DOTALL)
        risk = risk_match.group(1).strip() if risk_match else ""
        
        data["themes"].append({"emoji": emoji, "title": theme_title, "narrative": narrative, "tweets": tweets, "outlook": outlook, "opportunity": opportunity, "risk": risk})
        
    def extract_items(tag_name, target_list):
        block_match = re.search(rf'<{tag_name}>(.*?)</{tag_name}>', xml_text, re.IGNORECASE | re.DOTALL)
        if block_match:
            for item in re.finditer(r'<ITEM\s+category=[\'"“”](.*?)[\'"“”]>(.*?)</ITEM>', block_match.group(1), re.IGNORECASE | re.DOTALL):
                target_list.append({"category": item.group(1).strip(), "content": item.group(2).strip()})

    extract_items("MONEY_RADAR", data["money_radar"])
    extract_items("RISK_AND_TRENDS", data["risk_and_trends"])

    picks_match = re.search(r'<TOP_PICKS>(.*?)</TOP_PICKS>', xml_text, re.IGNORECASE | re.DOTALL)
    if picks_match:
        for t_match in re.finditer(r'<TWEET\s+account=[\'"“”](.*?)[\'"“”]\s+role=[\'"“”](.*?)[\'"“”]>(.*?)</TWEET>', picks_match.group(1), re.IGNORECASE | re.DOTALL):
            data["top_picks"].append({"account": t_match.group(1).strip(), "role": t_match.group(2).strip(), "content": t_match.group(3).strip()})
            
    return data

# ==============================================================================
# 🚀 第三阶段：结构化渲染与工具
# ==============================================================================
def render_feishu_card(parsed_data: dict, today_str: str):
    webhooks = get_feishu_webhooks()
    if not webhooks or not parsed_data.get("pulse"): return

    elements = []
    elements.append({"tag": "markdown", "content": f"**▌ ⚡️ 今日看板 (The Pulse)**\n<font color='grey'>{parsed_data['pulse']}</font>"})
    elements.append({"tag": "hr"})

    if parsed_data["themes"]:
        elements.append({"tag": "markdown", "content": "**▌ 🧠 深度叙事追踪**"})
        for idx, theme in enumerate(parsed_data["themes"]):
            theme_md = f"**{theme['emoji']} {theme['title']}**\n<font color='grey'>💡 核心判断：{theme['narrative']}</font>\n"
            for t in theme["tweets"]: theme_md += f"🗣️ **@{t['account']} | {t['role']}**\n<font color='grey'>“{t['content']}”</font>\n"
            if theme.get("outlook"): theme_md += f"<font color='blue'>**🔭 深度展望：**</font> {theme['outlook']}\n"
            if theme.get("opportunity"): theme_md += f"<font color='green'>**🎯 潜在机会：**</font> {theme['opportunity']}\n"
            if theme.get("risk"): theme_md += f"<font color='red'>**⚠️ 踩坑预警：**</font> {theme['risk']}\n"
            elements.append({"tag": "markdown", "content": theme_md.strip()})
            if idx < len(parsed_data["themes"]) - 1: elements.append({"tag": "hr"})
        elements.append({"tag": "hr"})

    def add_list_section(title, icon, items):
        if not items: return
        content = f"**▌ {icon} {title}**\n\n"
        for item in items: content += f"👉 **{item['category']}**：<font color='grey'>{item['content']}</font>\n"
        elements.append({"tag": "markdown", "content": content.strip()})
        elements.append({"tag": "hr"})

    add_list_section("搞钱雷达 (Money Radar)", "💰", parsed_data["money_radar"])
    add_list_section("风险与趋势 (Risk & Trends)", "📊", parsed_data["risk_and_trends"])

    if parsed_data["top_picks"]:
        picks_md = "**▌ 📣 今日精选推文 (Top 5 Picks)**\n"
        for t in parsed_data["top_picks"]: picks_md += f"\n🗣️ **@{t['account']} | {t['role']}**\n<font color='grey'>\"{t['content']}\"</font>\n"
        elements.append({"tag": "markdown", "content": picks_md.strip()})

    card_payload = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True, "enable_forward": True},
            "header": {"title": {"content": f"出海搞钱的中国人都在聊啥 | {today_str}", "tag": "plain_text"}, "template": "orange"},
            "elements": elements + [{"tag": "note", "elements": [{"tag": "plain_text", "content": "Powered by Grok Web Scraper + xAI SDK"}]}]
        }
    }

    for url in webhooks:
        try: requests.post(url, json=card_payload, timeout=20)
        except: pass

def render_wechat_html(parsed_data: dict, cover_url: str = "") -> str:
    html_lines = []
    if cover_url: html_lines.append(f'<p style="text-align:center;margin:0 0 16px 0;"><img src="{cover_url}" style="max-width:100%;border-radius:8px;" /></p>')
    if parsed_data["cover"].get("insight"): html_lines.append(f'<div style="border-radius:8px;background:#FFF7E6;padding:12px 14px;margin:0 0 20px 0;color:#d97706;"><div style="font-weight:bold;margin-bottom:6px;">💡 Insight | 核心洞察</div><div>{parsed_data["cover"]["insight"]}</div></div>')
    def make_h3(title): return f'<h3 style="margin:24px 0 12px 0;font-size:18px;border-left:4px solid #f97316;padding-left:10px;color:#2c3e50;font-weight:bold;">{title}</h3>'
    def make_quote(content): return f'<div style="background:#f8f9fa;border-left:4px solid #8c98a4;padding:10px 14px;color:#555;font-size:15px;border-radius:0 4px 4px 0;margin:6px 0 10px 0;line-height:1.6;">{content}</div>'

    html_lines.append(make_h3("⚡️ 今日看板 (The Pulse)"))
    html_lines.append(make_quote(parsed_data.get('pulse', '')))

    if parsed_data["themes"]:
        html_lines.append(make_h3("🧠 深度叙事追踪"))
        for idx, theme in enumerate(parsed_data["themes"]):
            html_lines.append(f'<p style="font-weight:bold;font-size:16px;color:#1e293b;margin:16px 0 8px 0;">{theme["emoji"]} {theme["title"]}</p>')
            html_lines.append(f'<div style="background:#fff7ed; padding:10px 12px; border-radius:6px; margin:0 0 8px 0; font-size:14px; color:#c2410c;"><strong>💡 核心判断：</strong>{theme["narrative"]}</div>')
            for t in theme["tweets"]:
                html_lines.append(f'<p style="margin:8px 0 2px 0;font-size:14px;font-weight:bold;color:#2c3e50;">🗣️ @{t["account"]} <span style="color:#94a3b8;font-weight:normal;">| {t["role"]}</span></p>')
                html_lines.append(make_quote(f'"{t["content"]}"'))
            if theme.get("outlook"): html_lines.append(f'<p style="margin:6px 0; font-size:15px; line-height:1.6; background:#eef2ff; padding: 8px 12px; border-radius: 4px;"><strong style="color:#4f46e5;">🔭 深度展望：</strong>{theme["outlook"]}</p>')
            if theme.get("opportunity"): html_lines.append(f'<p style="margin:6px 0; font-size:15px; line-height:1.6; background:#f0fdf4; padding: 8px 12px; border-radius: 4px;"><strong style="color:#16a34a;">🎯 潜在机会：</strong>{theme["opportunity"]}</p>')
            if theme.get("risk"): html_lines.append(f'<p style="margin:6px 0; font-size:15px; line-height:1.6; background:#fef2f2; padding: 8px 12px; border-radius: 4px;"><strong style="color:#dc2626;">⚠️ 踩坑预警：</strong>{theme["risk"]}</p>')
            if idx < len(parsed_data["themes"]) - 1: html_lines.append('<hr style="border:none;border-top:1px dashed #cbd5e1;margin:24px 0;"/>')

    def make_list_section(title, items):
        if not items: return
        html_lines.append(make_h3(title))
        for item in items: html_lines.append(f'<p style="margin:10px 0;font-size:15px;line-height:1.6;">👉 <strong style="color:#2c3e50;">{item["category"]}：</strong><span style="color:#333;">{item["content"]}</span></p>')

    make_list_section("💰 搞钱雷达 (Money Radar)", parsed_data["money_radar"])
    make_list_section("📊 风险与趋势 (Risk & Trends)", parsed_data["risk_and_trends"])

    if parsed_data["top_picks"]:
        html_lines.append(make_h3("📣 今日精选推文 (Top 5 Picks)"))
        for t in parsed_data["top_picks"]:
             html_lines.append(f'<p style="margin:12px 0 4px 0;font-size:14px;font-weight:bold;color:#2c3e50;">🗣️ @{t["account"]} <span style="color:#94a3b8;font-weight:normal;">| {t["role"]}</span></p>')
             html_lines.append(make_quote(f'"{t["content"]}"'))

    return "<br/>".join(html_lines)

def generate_cover_image(prompt):
    if not SF_API_KEY or not prompt: return ""
    try:
        resp = requests.post(URL_SF_IMAGE, headers={"Authorization": f"Bearer {SF_API_KEY}", "Content-Type": "application/json"}, json={"model": "black-forest-labs/FLUX.1-schnell", "prompt": prompt, "n": 1, "image_size": "1024x576"}, timeout=60)
        if resp.status_code == 200: return resp.json().get("images", [{}])[0].get("url") or resp.json().get("data", [{}])[0].get("url")
    except: return ""

def upload_to_imgbb_via_url(sf_url):
    if not IMGBB_API_KEY or not sf_url: return sf_url 
    try:
        img_resp = requests.get(sf_url, timeout=30)
        img_b64 = base64.b64encode(img_resp.content).decode("utf-8")
        upload_resp = requests.post(URL_IMGBB, data={"key": IMGBB_API_KEY, "image": img_b64}, timeout=45)
        if upload_resp.status_code == 200: return upload_resp.json()["data"]["url"]
    except: return sf_url

def push_to_jijyun(html_content, title, cover_url=""):
    if not JIJYUN_WEBHOOK_URL: return
    try: requests.post(JIJYUN_WEBHOOK_URL, json={"title": title, "author": "Prinski", "html_content": html_content, "cover_jpg": cover_url}, timeout=30)
    except: pass


# ==============================================================================
# 🚀 主程序入口
# ==============================================================================
def main():
    print("=" * 60, flush=True)
    mode_str = "测试模式(1个Batch)" if TEST_MODE else "全量模式"
    print(f"出海搞钱的中国人 v8.0 (Grok网页抓取 + xAI提纯 - {mode_str})", flush=True)
    print("=" * 60, flush=True)

    today_str, _ = get_dates()
    Path("data_cn").mkdir(exist_ok=True)
    
    selected_accounts = BATCH1_ACCOUNTS if TEST_MODE else ALL_ACCOUNTS
    meta_results, phase1_posts, phase2_posts = {}, {}, {}
    
    is_storage_state = prepare_session_file()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--disable-blink-features=AutomationControlled", "--window-size=1280,800"]
        )
        ctx_opts = {"viewport": {"width": 1280, "height": 800}, "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36", "locale": "zh-CN"}
        if is_storage_state: ctx_opts["storage_state"] = "session_state.json"
        
        context = browser.new_context(**ctx_opts)
        if not is_storage_state: load_raw_cookies(context)

        # 验证登录
        verify_page = context.new_page()
        verify_page.goto("https://grok.com", wait_until="domcontentloaded", timeout=60000)
        time.sleep(3)
        if _is_login_page(verify_page.url):
            print("ERROR: Grok Cookie expired. Update SUPER_GROK_COOKIES.", flush=True)
            browser.close()
            return
        verify_page.close()

        # --- Phase 1: 扫描 ---
        BATCH_SIZE = 25 if not TEST_MODE else 50
        for batch_num, batch_start in enumerate(range(0, len(selected_accounts), BATCH_SIZE), start=1):
            if TEST_MODE and batch_num > 1: break
            if time.time() - _START_TIME > PHASE1_DEADLINE: break
            
            batch = selected_accounts[batch_start:batch_start + BATCH_SIZE]
            label = f"Phase1-Batch{batch_num}"
            results = run_grok_batch(context, batch, build_phase1_prompt, label)
            
            for obj in results:
                account = obj.get("a", "").lstrip("@")
                if not account: continue
                if obj.get("type") == "meta": meta_results[account] = obj
                else: phase1_posts.setdefault(account, []).append(obj)

        # 分层
        classification = classify_accounts(meta_results)
        s_accounts = [a for a, t in classification.items() if t == "S"]
        a_accounts = [a for a, t in classification.items() if t == "A"]
        
        # --- Phase 2: S / A 级深挖 (如果在测试模式且不深挖，这步可跳过，但保留结构) ---
        if s_accounts and time.time() - _START_TIME < GLOBAL_DEADLINE:
            s_results = run_grok_batch(context, s_accounts, build_phase2_s_prompt, label="Phase2-S")
            for obj in s_results:
                if obj.get("type") != "meta": phase2_posts.setdefault(obj.get("a", "").lstrip("@"), []).append(obj)

        save_and_renew_session(context)
        browser.close()

    # 组装 JSONL 喂给 xAI SDK
    all_posts_flat = []
    for acc in s_accounts + a_accounts:
        all_posts_flat.extend(phase2_posts.get(acc) or phase1_posts.get(acc) or [])
    for acc in [a for a, t in classification.items() if t == "B"]:
        all_posts_flat.extend(phase1_posts.get(acc) or [])

    combined_jsonl = "\n".join(json.dumps(obj, ensure_ascii=False) for obj in all_posts_flat if obj.get("type") != "meta")
    print(f"\n[Data] Ready for xAI SDK: {len(all_posts_flat)} posts.")

    if combined_jsonl.strip():
        xml_result = llm_call_xai(combined_jsonl, today_str)
        if xml_result:
            print("\n[Parser] Parsing XML to structured data...", flush=True)
            parsed_data = parse_llm_xml(xml_result)
            
            cover_url = ""
            if parsed_data["cover"]["prompt"]:
                sf_url = generate_cover_image(parsed_data["cover"]["prompt"])
                cover_url = upload_to_imgbb_via_url(sf_url) if sf_url else ""
            
            render_feishu_card(parsed_data, today_str)
            
            if JIJYUN_WEBHOOK_URL:
                html_content = render_wechat_html(parsed_data, cover_url)
                wechat_title = parsed_data["cover"]["title"] or f"出海搞钱的中国人 | {today_str}"
                push_to_jijyun(html_content, title=wechat_title, cover_url=cover_url)
            
            print("\n🎉 V8.0 运行完毕！", flush=True)
        else:
            print("❌ LLM 处理失败，任务终止。")

if __name__ == "__main__":
    main()
