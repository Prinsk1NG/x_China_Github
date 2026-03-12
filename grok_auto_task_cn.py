# -*- coding: utf-8 -*-
"""
grok_auto_task_cn.py  v3.4
Architecture: Grok (pure search, per-account) + Claude / Kimi-k2.5 (analyse & summarise)
Target: 中文圈 AI/出海/独立开发者/创业者 accounts

Phase 1 - Tiered scan (deadline 40 min):
  All 80 accounts searched individually (from:account, limit=10, mode=Latest).
  Collect 3 newest posts + 1 metadata row per account.
  Auto-classify accounts into S / A / B / inactive.

Phase 2 - Differential collection + report (global deadline 85 min):
  S-tier: max_l>3000 & days<=7  -> 10 posts + thread fetch
  A-tier: max_l>800  & days<=14 -> 5 posts, qt field
  B-tier: reuse Phase 1 data (3 posts)
  Claude (primary) -> Kimi-k2.5 (fallback).
  Push to Feishu (interactive card) + WeChat.

v3.4 changelog (CN edition):
  - Based on grok_auto_task v3.4 architecture
  - Accounts replaced with 80 中文圈 AI/出海/独立开发者 accounts (6 batches)
  - Feishu card title: 昨晚，那些出海搞钱的人都在聊啥
  - LLM prompt tailored for 中文圈 AI创业/出海/SaaS/独立开发 themes

TEST MODE (added):
  - Enable via env: TEST_MODE=1/true/on/yes
  - Only runs ONE Phase1 batch, containing Batch1 (AI核心KOL 14 accounts)
  - All downstream work still runs: classification, phase2, llm, push, save data, etc.
  - Optional: set TEST_MODE_NO_PUSH=1 to skip Feishu/WeChat push during tests.
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

# -- Environment variables -----------------------------------------------------
JIJYUN_WEBHOOK_URL  = os.getenv("JIJYUN_WEBHOOK_URL", "")
SF_API_KEY          = os.getenv("SF_API_KEY", "")
KIMI_API_KEY        = os.getenv("KIMI_API_KEY", "")
OPENROUTER_API_KEY  = os.getenv("OPENROUTER_API_KEY", "")
GROK_COOKIES_JSON   = os.getenv("SUPER_GROK_COOKIES", "")
PAT_FOR_SECRETS     = os.getenv("PAT_FOR_SECRETS", "")
GITHUB_REPOSITORY   = os.getenv("GITHUB_REPOSITORY", "")

# -- Global timeout tracking ---------------------------------------------------
_START_TIME      = time.time()
PHASE1_DEADLINE  = 40 * 60   # v3.4: 40 min
GLOBAL_DEADLINE  = 85 * 60   # v3.4: 85 min total, gives Phase2 ~45 min

# -- 80 accounts (中文圈 AI/出海/独立开发者/创业者) ----------------------------
ALL_ACCOUNTS = [
    # 批次1 AI核心KOL (14人)
    "dotey", "op7418", "Gorden_Sun", "xiaohu", "shao__meng",
    "thinkingjimmy", "nishuang", "vista8", "lijigang", "kaifulee",
    "WaytoAGI", "oran_ge", "AlchainHust", "haibun",
    # 批次2 AI+创业者 (14人)
    "SamuelQZQ", "elliotchen100", "berryxia", "lidangzzz", "lxfater",
    "Fenng", "turingou", "tinyfool", "virushuo", "fankaishuoai",
    "XDash", "idoubicc", "Cydiar404", "JefferyTatsuya",
    # 批次3 创业者+SaaS (13人)
    "CoderJeffLee", "tuturetom", "iamtonyzhu", "Valley101_Qi",
    "AIMindCo", "AlanChenFun", "AuroraAIDev", "maboroshii", "nicekateyes",
    "paborobot", "porkybun", "0xDragonMaster", "LittleStar",
    # 批次4 SaaS+出海 (13人)
    "tualatrix", "luinlee", "seclink", "XiaohuiAI666", "gefei55",
    "AI_Jasonyu", "JourneymanChina", "dev_afei", "GoSailGlobal",
    "chuhaiqu", "daluoseo", "realNyarime", "DigitalNomadLC",
    # 批次5 独立开发者 (13人)
    "RocM301", "shuziyimin", "itangtalk", "guishou_56", "9yearfish",
    "OwenYoungZh", "waylybaye", "randyloop", "livid",
    "shengxj1", "FinanceYF5", "fkysly", "zhixianio",
    # 批次6 知识+副业+媒体 (13人)
    "hongming731", "penny777", "jiqizhixin", "evilcos", "wshuy",
    "Web3Yolanda", "maboroshi", "CryptoMasterAI", "AIProductDaily",
    "aigclink", "founder_park", "geekpark", "pingwest",
]

# -- Batch 1 only (AI核心KOL 14人) ---------------------------------------------
BATCH1_ACCOUNTS = [
    "dotey", "op7418", "Gorden_Sun", "xiaohu", "shao__meng",
    "thinkingjimmy", "nishuang", "vista8", "lijigang", "kaifulee",
    "WaytoAGI", "oran_ge", "AlchainHust", "haibun",
]


def _is_test_mode() -> bool:
    """
    Enable via env: TEST_MODE=1/true/on/yes
    """
    v = (os.getenv("TEST_MODE", "") or "").strip().lower()
    return v in {"1", "true", "yes", "y", "on"}


def _skip_push_in_test_mode() -> bool:
    """
    Optional: during tests, skip Feishu/WeChat push to avoid spamming.
    Enable via env: TEST_MODE_NO_PUSH=0
    """
    v = (os.getenv("TEST_MODE_NO_PUSH", "") or "").strip().lower()
    return v in {"1", "true", "yes", "y", "on"}


# ==============================================================================
# Feishu multi-webhook
# ==============================================================================
def get_feishu_webhooks() -> list:
    urls = []
    for suffix in ["", "_1", "_2", "_3"]:
        url = os.getenv(f"FEISHU_WEBHOOK_URL{suffix}", "")
        if url:
            urls.append(url)
    return urls


# ==============================================================================
# Date utilities
# ==============================================================================
def get_dates() -> tuple:
    tz = timezone(timedelta(hours=8))
    today = datetime.now(tz)
    yesterday = today - timedelta(days=1)
    return today.strftime("%Y-%m-%d"), yesterday.strftime("%Y-%m-%d")


# ==============================================================================
# Session management
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
            cookie = {
                "name":   c.get("name", ""),
                "value":  c.get("value", ""),
                "domain": c.get("domain", ".grok.com"),
                "path":   c.get("path", "/"),
            }
            if "httpOnly" in c:
                cookie["httpOnly"] = c["httpOnly"]
            if "secure" in c:
                cookie["secure"] = c["secure"]
            ss = c.get("sameSite", "")
            if ss in ("Strict", "Lax", "None"):
                cookie["sameSite"] = ss
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

    if not PAT_FOR_SECRETS or not GITHUB_REPOSITORY:
        print("[Session] Warning: PAT_FOR_SECRETS or GITHUB_REPOSITORY not configured, skip renewal",
              flush=True)
        return

    try:
        from nacl import encoding, public as nacl_public

        with open("session_state.json", "r", encoding="utf-8") as f:
            state_str = f.read()

        headers = {
            "Authorization": f"token {PAT_FOR_SECRETS}",
            "Accept": "application/vnd.github.v3+json",
        }

        key_resp = requests.get(
            f"https://api.github.com/repos/{GITHUB_REPOSITORY}/actions/secrets/public-key",
            headers=headers, timeout=30,
        )
        key_resp.raise_for_status()
        key_data = key_resp.json()

        pub_key = nacl_public.PublicKey(key_data["key"].encode(), encoding.Base64Encoder())
        sealed  = nacl_public.SealedBox(pub_key).encrypt(state_str.encode())
        enc_b64 = base64.b64encode(sealed).decode()

        put_resp = requests.put(
            f"https://api.github.com/repos/{GITHUB_REPOSITORY}/actions/secrets/SUPER_GROK_COOKIES",
            headers=headers,
            json={"encrypted_value": enc_b64, "key_id": key_data["key_id"]},
            timeout=30,
        )
        put_resp.raise_for_status()
        print("[Session] OK GitHub Secret SUPER_GROK_COOKIES auto-renewed", flush=True)

    except ImportError:
        print("[Session] Warning: PyNaCl not installed, skip renewal", flush=True)
    except Exception as e:
        print(f"[Session] ERROR Secret renewal failed: {e}", flush=True)


def check_cookie_expiry():
    if not GROK_COOKIES_JSON:
        return
    try:
        data = json.loads(GROK_COOKIES_JSON)
        if not isinstance(data, list):
            return
        watched_names = {"sso", "auth_token", "ct0"}
        for c in data:
            cname = c.get("name", "")
            if cname in watched_names and c.get("expirationDate"):
                exp = datetime.fromtimestamp(c["expirationDate"], tz=timezone.utc)
                days_left = (exp - datetime.now(timezone.utc)).days
                if days_left <= 5:
                    msg = (f"Warning: Grok Cookie '{cname}' expires in {days_left} days, "
                           f"please update SUPER_GROK_COOKIES!")
                    print(f"[Cookie] {msg}", flush=True)
                    for url in get_feishu_webhooks():
                        try:
                            requests.post(url,
                                          json={"msg_type": "text", "content": {"text": msg}},
                                          timeout=15)
                        except Exception:
                            pass
    except Exception:
        pass


# ==============================================================================
# Model selection — v3.3: broader selector, non-fatal
# ==============================================================================
def enable_grok4_beta(page):
    print("\n[Model] Trying to enable Beta Toggle...", flush=True)
    selectors = [
        "button:has-text('Fast')",
        "button:has-text('Auto')",
        "button:has-text('Grok')",
        "button[aria-label*='model' i]",
        "button[data-testid*='model' i]",
        "button[aria-label*='mode' i]",
    ]
    model_btn = None
    for sel in selectors:
        try:
            model_btn = page.wait_for_selector(sel, timeout=4000)
            if model_btn:
                break
        except Exception:
            continue

    if not model_btn:
        print("[Model] Warning: Model button not found, skip toggle (non-fatal)", flush=True)
        return

    try:
        model_btn.click()
        time.sleep(1)
        toggle = page.wait_for_selector(
            "button[role='switch'], input[type='checkbox']", timeout=6000,
        )
        is_on = page.evaluate("""() => {
            const sw = document.querySelector("button[role='switch']");
            if (sw) return sw.getAttribute('aria-checked') === 'true'
                        || sw.getAttribute('data-state') === 'checked';
            const cb = document.querySelector("input[type='checkbox']");
            return cb ? cb.checked : false;
        }""")
        if not is_on:
            toggle.click()
            print("[Model] OK Toggle enabled", flush=True)
            time.sleep(1)
        else:
            print("[Model] OK Already enabled", flush=True)
        page.keyboard.press("Escape")
        time.sleep(0.5)
    except Exception as e:
        print(f"[Model] Warning: Toggle action failed ({e}), skip (non-fatal)", flush=True)


# ==============================================================================
# Send prompt
# ==============================================================================
def send_prompt(page, prompt_text, label, screenshot_prefix):
    print(f"\n[{label}] Filling prompt ({len(prompt_text)} chars)...", flush=True)
    page.wait_for_selector("div[contenteditable='true'], textarea", timeout=30000)

    ok = page.evaluate("""(text) => {
        const el = document.querySelector("div[contenteditable='true']")
                || document.querySelector("textarea");
        if (!el) return false;
        el.focus();
        document.execCommand('selectAll', false, null);
        document.execCommand('delete', false, null);
        document.execCommand('insertText', false, text);
        return el.textContent.length > 0 || el.value?.length > 0;
    }""", prompt_text)

    if not ok:
        print(f"[{label}] execCommand failed, trying clipboard API...", flush=True)
        try:
            inp = page.query_selector("div[contenteditable='true'], textarea")
            if inp:
                inp.click()
                page.keyboard.press("Control+a")
                page.keyboard.press("Backspace")
                page.evaluate("""async (text) => {
                    const el = document.querySelector("div[contenteditable='true']")
                            || document.querySelector("textarea");
                    if (!el) return;
                    el.focus();
                    if (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT') {
                        el.value = text;
                        el.dispatchEvent(new Event('input', {bubbles: true}));
                    } else {
                        el.textContent = text;
                        el.dispatchEvent(new Event('input', {bubbles: true}));
                    }
                }""", prompt_text)
                ok = True
        except Exception as e2:
            print(f"[{label}] Clipboard API also failed: {e2}", flush=True)

    if not ok:
        print(f"[{label}] Falling back to keyboard typing...", flush=True)
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
        inp = page.query_selector("div[contenteditable='true'], textarea")
        if inp:
            inp.click()
            time.sleep(0.5)
    except Exception:
        pass

    clicked = False
    try:
        send_btn = page.wait_for_selector(
            "button[aria-label='Submit']:not([disabled]), "
            "button[aria-label='Send message']:not([disabled]), "
            "button[type='submit']:not([disabled])",
            timeout=30000, state="visible",
        )
        send_btn.click()
        clicked = True
    except Exception as e:
        print(f"[{label}] Warning: Normal click failed ({e}), trying JS...", flush=True)

    if not clicked:
        result = page.evaluate("""() => {
            const btn = document.query_selector("button[type='submit']")
                     || document.query_selector("button[aria-label='Submit']")
                     || document.query_selector("button[aria-label='Send message']");
            if (btn) { btn.click(); return true; }
            return false;
        }""")
        if result:
            print(f"[{label}] OK JS fallback click succeeded", flush=True)
        else:
            raise RuntimeError(f"[{label}] ERROR Submit button not found, aborting")

    print(f"[{label}] OK Sent", flush=True)
    time.sleep(5)


# ==============================================================================
# Wait for Grok to finish generating
# ==============================================================================
def _get_last_msg(page):
    return page.evaluate("""() => {
        const msgs = document.querySelectorAll(
            '[data-testid="message"], .message-bubble, .response-content'
        );
        return msgs.length ? msgs[msgs.length - 1].innerText : "";
    }""")


def wait_and_extract(page, label, screenshot_prefix,
                     interval=3, stable_rounds=4, max_wait=120,
                     extend_if_growing=False, min_len=80):
    print(f"[{label}] Waiting for reply (max {max_wait}s, min len {min_len})...", flush=True)
    last_len  = -1
    stable    = 0
    elapsed   = 0
    last_text = ""

    while elapsed < max_wait:
        time.sleep(interval)
        elapsed += interval
        try:
            text = _get_last_msg(page)
        except Exception as e:
            print(f"[{label}] Warning: Page error: {e}", flush=True)
            return last_text.strip()
        last_text = text
        cur_len = len(text.strip())
        print(f"  {elapsed}s | chars: {cur_len}", flush=True)

        if cur_len == last_len and cur_len >= min_len:
            stable += 1
            if stable >= stable_rounds:
                print(f"[{label}] OK Done ({cur_len} chars)", flush=True)
                return text.strip()
        else:
            stable   = 0
            last_len = cur_len

    if extend_if_growing:
        print(f"[{label}] Extending wait (up to 300s)...", flush=True)
        prev_len  = last_len
        prev_text = last_text
        ext = 0
        while ext < 300:
            time.sleep(5)
            ext += 5
            try:
                text = _get_last_msg(page)
            except Exception:
                return prev_text.strip()
            cur_len = len(text.strip())
            print(f"  +{ext}s | chars: {cur_len}", flush=True)
            if cur_len == prev_len:
                return text.strip()
            prev_len  = cur_len
            prev_text = text
        try:
            return _get_last_msg(page).strip()
        except Exception:
            return prev_text.strip()
    else:
        try:
            return _get_last_msg(page).strip()
        except Exception:
            return last_text.strip()


# ==============================================================================
# JSON Lines parser
# ==============================================================================
def parse_jsonlines(text: str) -> list:
    results = []
    for line in text.splitlines():
        line = line.strip()
        if not line or not line.startswith('{') or not line.endswith('}'):
            continue
        try:
            results.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return results


# ==============================================================================
# Phase 1 prompt
# ==============================================================================
def build_phase1_prompt(accounts: list) -> str:
    rounds = [accounts[i:i+3] for i in range(0, len(accounts), 3)]
    rounds_text = "\n".join(
        f"Round {i+1}: {' | '.join(r)}"
        for i, r in enumerate(rounds)
    )
    return (
        "You are an X/Twitter data collection tool. Search the following accounts "
        "and output pure JSON Lines format.\n\n"
        "[Search Rules]\n"
        "1. Search each account individually: x_keyword_search query=from:AccountName, mode=Latest, limit=10\n"
        "2. Execute in parallel rounds (3 accounts per round)\n"
        "3. No additional keywords, no since parameter\n"
        "4. Per account output: newest 3 posts + 1 metadata row\n\n"
        f"[Account List ({len(accounts)} accounts, by round)]\n"
        f"{rounds_text}\n\n"
        "[Output Format (JSON Lines ONLY, no other text)]\n"
        '  Post:     {"a":"AccountName","l":likes,"t":"MMDD","s":"English summary under 50 words","tag":"raw"}\n'
        '  Metadata: {"a":"AccountName","type":"meta","total":count,"max_l":max_likes,"latest":"MMDD"}\n'
        '  Inactive: {"a":"AccountName","type":"meta","total":0,"max_l":0,"latest":"NA"}\n\n'
        "[Strict Rules]\n"
        "- Account names without @, exactly matching the from: query\n"
        "- t field format MMDD (e.g. 0309 = March 9)\n"
        "- Per account: post rows first (max 3), then 1 metadata row\n"
        "- No translation, no explanation, no summary, no filtering\n"
        "- First line to last line must all be JSON"
    )


# ==============================================================================
# Phase 2 prompts
# ==============================================================================
def build_phase2_s_prompt(accounts: list) -> str:
    rounds = [accounts[i:i+3] for i in range(0, len(accounts), 3)]
    rounds_text = "\n".join(
        f"Round {i+1}: {' | '.join(r)}"
        for i, r in enumerate(rounds)
    )
    return (
        "You are an X/Twitter data collection tool. Deep-collect S-tier accounts, "
        "output pure JSON Lines.\n\n"
        "[S-tier Rules]\n"
        "1. x_keyword_search query=from:AccountName, mode=Latest, limit=10\n"
        "2. Output all 10 posts (no truncation)\n"
        "3. For posts with likes>5000, call x_thread_fetch for full thread (max 5 per account)\n"
        "4. Retweets/quotes: record original author and summary in qt field\n"
        "5. 3 accounts per parallel round\n\n"
        f"[S-tier Accounts ({len(accounts)})]\n"
        f"{rounds_text}\n\n"
        "[Output Format (JSON Lines ONLY)]\n"
        '  Normal:  {"a":"Name","l":likes,"t":"MMDD","s":"English summary","tag":"raw"}\n'
        '  Quote:   {"a":"Name","l":likes,"t":"MMDD","s":"comment summary","qt":"@orig: summary","tag":"raw"}\n'
        '  Thread:  {"a":"Name","l":likes,"t":"MMDD","s":"summary","tag":"raw",'
        '"replies":[{"from":"replier","text":"content","l":likes}]}\n\n'
        "[Strict Rules]\n"
        "- No @, exact account name match\n"
        "- No translation, JSON only\n"
        "- s field in English"
    )


def build_phase2_a_prompt(accounts: list) -> str:
    rounds = [accounts[i:i+3] for i in range(0, len(accounts), 3)]
    rounds_text = "\n".join(
        f"Round {i+1}: {' | '.join(r)}"
        for i, r in enumerate(rounds)
    )
    return (
        "You are an X/Twitter data collection tool. Collect A-tier accounts, "
        "output pure JSON Lines.\n\n"
        "[A-tier Rules]\n"
        "1. x_keyword_search query=from:AccountName, mode=Latest, limit=10\n"
        "2. Output newest 5 posts only\n"
        "3. Retweets/quotes: record original author and summary in qt field\n"
        "4. 3 accounts per parallel round\n\n"
        f"[A-tier Accounts ({len(accounts)})]\n"
        f"{rounds_text}\n\n"
        "[Output Format (JSON Lines ONLY)]\n"
        '  Normal:  {"a":"Name","l":likes,"t":"MMDD","s":"English summary","tag":"raw"}\n'
        '  Quote:   {"a":"Name","l":likes,"t":"MMDD","s":"comment summary","qt":"@orig: summary","tag":"raw"}\n\n'
        "[Strict Rules]\n"
        "- No @, exact account name match\n"
        "- No translation, JSON only\n"
        "- s field in English"
    )


# ==============================================================================
# Account classification — v3.3: lowered thresholds
# ==============================================================================
def classify_accounts(meta_results: dict) -> dict:
    tz = timezone(timedelta(hours=8))
    today = datetime.now(tz)
    classification = {}

    for account, meta in meta_results.items():
        total  = meta.get("total", 0)
        max_l  = meta.get("max_l", 0)
        latest = meta.get("latest", "NA")

        if total == 0 or latest == "NA":
            classification[account] = "inactive"
            continue

        try:
            mm = int(latest[:2])
            dd = int(latest[2:])
            latest_date = today.replace(month=mm, day=dd)
            if latest_date > today:
                latest_date = latest_date.replace(year=today.year - 1)
            days_since = (today - latest_date).days
        except (ValueError, TypeError):
            days_since = 999

        if days_since > 30:
            classification[account] = "inactive"
        elif max_l > 3000 and days_since <= 7:
            classification[account] = "S"
        elif max_l > 800 and days_since <= 14:
            classification[account] = "A"
        else:
            classification[account] = "B"

    return classification


# ==============================================================================
# Grok page helpers
# ==============================================================================
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
        print(f"ERROR: Failed to open Grok page: {e}", flush=True)
        try:
            page.close()
        except Exception:
            pass
        return None


def run_grok_batch(context, accounts: list, prompt_builder, label: str,
                   initial_wait: int = 60) -> list:
    if not accounts:
        return []

    elapsed = time.time() - _START_TIME
    print(f"\n[{label}] Starting batch ({len(accounts)} accounts, "
          f"elapsed: {elapsed:.0f}s)...", flush=True)

    page = open_grok_page(context)
    if page is None:
        return []

    try:
        prompt = prompt_builder(accounts)
        send_prompt(page, prompt, label, label.lower().replace(" ", "_"))

        print(f"[{label}] Waiting {initial_wait}s for Grok to start...", flush=True)
        time.sleep(initial_wait)

        raw_text = wait_and_extract(
            page, label, label.lower().replace(" ", "_"),
            interval=5, stable_rounds=5, max_wait=420,
            extend_if_growing=True, min_len=50,
        )
        results = parse_jsonlines(raw_text)
        print(f"[{label}] OK Parsed {len(results)} JSON objects", flush=True)
        return results

    except Exception as e:
        print(f"[{label}] ERROR: {e}", flush=True)
        return []
    finally:
        try:
            page.close()
        except Exception:
            pass


# ==============================================================================
# LLM Prompt Builder (中文圈版本)
# ==============================================================================
def _build_llm_prompt(combined_jsonl: str, today_str: str) -> str:
    return f"""
# Role
你是一位顶级的中文互联网科技/出海领域投资分析师，拥有10年经验。你每天为合伙人撰写一份"中文圈吃瓜日报"，同时产出一份适合微信公众号的版本——同样的事实，但更犀利、更接地气。

全文用中文回复。

# Task
分析过去24小时内，80+位中文圈AI创业者、出海开发者、独立开发者、SaaS创始人、知识博主在X上的推文（数据在末尾的JSONL中）。
过滤掉日常闲聊和无意义互动，提炼出有"创业参考价值"和"出海实操价值"的洞察。

# Output Structure (严格按照Markdown格式)

## ⚡️ 今日看板 (The Pulse)
> 用一句话总结今日最核心的 1-2 个行业动态或出海信号。

---

## 🧠 深度叙事追踪 (Thematic Narratives)
将零散的推文按「主题/赛道」进行聚合（如：AI工具创业、出海变现、独立开发、SaaS增长、流量打法等）。
每个主题输出格式严格如下（3-5个主题）：

**🔁 主题标题：副标题**

> 💡 叙事转向：[一句话核心判断，说清楚"什么在变化、为什么重要"]

- **@账号名** 具体行为 + 创业/出海视角解读（不超过 60 字）
- **@账号名** 具体行为 + 创业/出海视角解读（不超过 60 字）
- **@账号名** 具体行为 + 创业/出海视角解读（不超过 60 字）

（每个主题之间空一行，emoji 可根据主题选择：🔁🤖⚔️🏭🦾💡🔥📊🚀💰🌊）

---

## 💰 搞钱雷达 (Money Radar)
1. **变现快讯：** 扫描数据中提到的具体产品收入、MRR、用户增长数据。
2. **出海机会：** 提炼创业者分享的海外市场洞察、流量渠道、变现模式。
3. **工具推荐：** 汇总被多人提及或推荐的AI工具、SaaS产品、开发者工具。

---

## 📊 风险与趋势 (Risk & Trends)
1. **踩坑预警：** 汇总创业者分享的失败教训、被封号、合规问题等。
2. **趋势判断：** 结合多条推文，给出1-2个中短期趋势判断。

---

## 📣 今日精选推文 (Top 5 Picks)

从今日数据中精选 5 条最具代表性的原始推文，格式严格如下（不得偏离）：

- **@账号** · 姓名 · 身份标签
  > 「中文原文或译文，限 60 字内，保留原文语气」

示例：
- **@dotey** · 宝玉 · AI翻译/开发者
  > 「用Claude写了个小工具，自动把英文Newsletter翻译成中文，已经跑了一个月零成本。」

选取标准：优先选点赞数最高、实操价值最大的推文，覆盖不同账号。

# Constraints
- **格式纪律（严格遵守）：**
  - 只允许使用 ## 二级标题，禁止出现 ### 三级标题
  - 每个要点用 `- ` 开头的短 bullet，单条不超过 80 个汉字（约两行）
  - 禁止出现超过 3 行的连续正文段落，超长内容必须拆成多条 bullet
  - 每个 ## 段落之间不加多余空行
- **禁止技术堆砌：** 不要解释算法原理，只需说该技术如何帮助变现或降低成本。
- **实操视角：** 重点关注「怎么赚钱」和「怎么做」。
- **语言风格：** 专业、干脆、利落，适合在飞书移动端快速扫读。

# Input Data (JSONL)
{combined_jsonl}

# Date
{today_str}

---
**输出完正文后，必须在最后附上以下三行（不可省略，紧跟正文末尾）：**
TITLE: （5-10字中文标题，适合微信公众号，如"AI出海日报｜谁又月入百万了"）
PROMPT: （英文封面图生成提示词，100字以内，描述科技感画面，如"futuristic digital nomad working on laptop global map holographic interface neon blue"）
INSIGHT: （一句话核心洞察，中文，30字以内）
"""


# ==============================================================================
# LLM helper functions
# ==============================================================================
def _get_proxies_from_env():
    proxy_url = (os.getenv("HTTPS_PROXY") or os.getenv("https_proxy")
                 or os.getenv("HTTP_PROXY") or os.getenv("http_proxy"))
    if proxy_url:
        return {"https": proxy_url, "http": proxy_url}
    return None


def _get_openrouter_endpoints() -> list:
    env_eps = os.getenv("OPENROUTER_ENDPOINTS")
    if env_eps:
        return [e.strip() for e in env_eps.split(",") if e.strip()]
    return ["https://openrouter.ai/api/v1/chat/completions"]


def _openrouter_post(endpoint: str, payload: dict, timeout: int = 300,
                     proxies: dict = None):
    json_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return requests.post(
        endpoint,
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json; charset=utf-8",
            "HTTP-Referer": "https://github.com/Prinsk1NG/X_AI_Github",
            "X-Title": "AI-Daily-Report-CN",
        },
        data=json_bytes,
        timeout=timeout,
        proxies=proxies,
    )


# ==============================================================================
# Claude (OpenRouter) — PRIMARY
# ==============================================================================
def llm_call_claude(combined_jsonl: str, today_str: str):
    if not OPENROUTER_API_KEY:
        print("[LLM/Claude] Warning: OPENROUTER_API_KEY not configured", flush=True)
        return "", "", "", ""

    max_data_chars = 200000
    data = combined_jsonl[:max_data_chars] if len(combined_jsonl) > max_data_chars else combined_jsonl
    prompt = _build_llm_prompt(data, today_str)

    proxies = _get_proxies_from_env()
    endpoints = _get_openrouter_endpoints()

    for ep in endpoints:
        print(f"[LLM/Claude] Trying endpoint: {ep}", flush=True)
        for attempt in range(1, 4):
            try:
                print(f"[LLM/Claude] POST (attempt {attempt}/3)", flush=True)
                payload = {
                    "model": os.getenv("OPENROUTER_MODEL", "anthropic/claude-sonnet-4-6"),
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.7,
                    "max_tokens": 16000,
                }
                resp = _openrouter_post(ep, payload, timeout=300, proxies=proxies)
                resp.raise_for_status()
                result = resp.json()["choices"][0]["message"]["content"].strip()
                print(f"[LLM/Claude] OK Response received ({len(result)} chars)", flush=True)
                return _parse_llm_result(result)
            except Exception as e:
                print(f"[LLM/Claude] attempt {attempt} at {ep} failed: {e}", flush=True)
                if attempt < 3:
                    time.sleep((2 ** attempt) + 0.5)
                else:
                    if isinstance(e, (ConnectionError, Timeout)) or "NameResolutionError" in str(e):
                        print(f"[LLM/Claude] Network error on {ep}, trying next endpoint", flush=True)
                        break
    return "", "", "", ""


# ==============================================================================
# Kimi (Moonshot API) — FALLBACK
# ==============================================================================
def llm_call_kimi(combined_jsonl: str, today_str: str):
    if not KIMI_API_KEY:
        print("[LLM/Kimi] Warning: KIMI_API_KEY not configured", flush=True)
        return "", "", "", ""

    max_data_chars = 200000
    data = combined_jsonl[:max_data_chars] if len(combined_jsonl) > max_data_chars else combined_jsonl
    prompt = _build_llm_prompt(data, today_str)

    try:
        env_temp = os.getenv("KIMI_TEMPERATURE")
        temperature = float(env_temp) if env_temp is not None else 1.0
    except Exception:
        temperature = 1.0

    for attempt in range(1, 4):
        try:
            print(f"[LLM/Kimi] Calling kimi-k2.5 (attempt {attempt}/3, temp={temperature})", flush=True)
            resp = requests.post(
                "https://api.moonshot.cn/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {KIMI_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "kimi-k2.5",
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": temperature,
                    "max_tokens": 16000,
                },
                timeout=300,
            )
            resp.raise_for_status()
            result = resp.json()["choices"][0]["message"]["content"].strip()
            print(f"[LLM/Kimi] OK Response received ({len(result)} chars)", flush=True)
            return _parse_llm_result(result)
        except Exception as e:
            print(f"[LLM/Kimi] attempt {attempt} failed: {e}", flush=True)
            if attempt < 3:
                time.sleep(2 ** attempt)
    return "", "", "", ""


# ==============================================================================
# LLM result parser
# ==============================================================================
def _parse_llm_result(result: str):
    report_text = _extract_markdown_block(result) or result

    try:
        data = json.loads(report_text)
        if isinstance(data, dict):
            return (
                report_text,
                data.get("cover_title", ""),
                data.get("cover_prompt", ""),
                data.get("cover_insight", ""),
            )
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    # v3.4: search entire result for TITLE/PROMPT/INSIGHT (not just after @@@END@@@)
    if "@@@END@@@" in result:
        search_text = result[result.find("@@@END@@@") + 9:]
    else:
        search_text = result

    title_m   = re.search(r"TITLE[:：]\s*(.+)",                      search_text)
    prompt_m  = re.search(r"PROMPT[:：]\s*([\s\S]+?)(?=INSIGHT[:：]|$)", search_text)
    insight_m = re.search(r"INSIGHT[:：]\s*([\s\S]+)",              search_text)

    cover_title   = title_m.group(1).strip()   if title_m   else ""
    cover_prompt  = prompt_m.group(1).strip()  if prompt_m  else ""
    cover_insight = insight_m.group(1).strip() if insight_m else ""

    # Strip TITLE/PROMPT/INSIGHT block from report_text so it doesn't appear in Feishu card
    clean_report = re.sub(r"\n?TITLE[:：][\s\S]*$", "", report_text).strip()

    return clean_report, cover_title, cover_prompt, cover_insight


def _extract_markdown_block(text):
    start = text.find("@@@START@@@")
    end   = text.find("@@@END@@@")
    if start == -1:
        return ""
    cs = start + len("@@@START@@@")
    return text[cs:end].strip() if (end != -1 and end > start) else text[cs:].strip()


# ==============================================================================
# LLM fallback (cover metadata only)
# ==============================================================================
def llm_fallback(raw_b_text):
    if not raw_b_text or len(raw_b_text) < 100:
        return "", "", ""

    fallback_prompt = (
        "Based on the following content, generate three lines:\n" + raw_b_text[:6000] +
        "\nTITLE: <title>\nPROMPT: <English image prompt>\nINSIGHT: <100 chars max insight>"
    )

    def _extract(text):
        title_m   = re.search(r"TITLE[:：]\s*(.+)", text)
        prompt_m  = re.search(r"PROMPT[:：]\s*([\s\S]+?)(?=INSIGHT[:：]|$)", text)
        insight_m = re.search(r"INSIGHT[:：]\s*([\s\S]+)", text)
        return (
            title_m.group(1).strip()   if title_m   else "",
            prompt_m.group(1).strip()  if prompt_m  else "",
            insight_m.group(1).strip() if insight_m else "",
        )

    if OPENROUTER_API_KEY:
        proxies = _get_proxies_from_env()
        endpoints = _get_openrouter_endpoints()
        for ep in endpoints:
            for attempt in range(1, 4):
                try:
                    payload = {
                        "model": os.getenv("OPENROUTER_MODEL", "anthropic/claude-sonnet-4-6"),
                        "messages": [{"role": "user", "content": fallback_prompt}],
                        "temperature": 0.7,
                        "max_tokens": 2000,
                    }
                    resp = _openrouter_post(ep, payload, timeout=60, proxies=proxies)
                    resp.raise_for_status()
                    return _extract(resp.json()["choices"][0]["message"]["content"].strip())
                except Exception as e:
                    print(f"[LLM] ERROR OpenRouter fallback attempt {attempt}/3 at {ep}: {e}", flush=True)
                    if attempt < 3:
                        time.sleep(2 ** attempt)
                    else:
                        if isinstance(e, (ConnectionError, Timeout)) or "NameResolutionError" in str(e):
                            break

    if KIMI_API_KEY:
        try:
            env_temp = os.getenv("KIMI_TEMPERATURE")
            temperature = float(env_temp) if env_temp is not None else 1.0
        except Exception:
            temperature = 1.0
        try:
            resp = requests.post(
                "https://api.moonshot.cn/v1/chat/completions",
                headers={"Authorization": f"Bearer {KIMI_API_KEY}",
                         "Content-Type": "application/json"},
                json={
                    "model": "kimi-k2.5",
                    "messages": [{"role": "user", "content": fallback_prompt}],
                    "temperature": temperature, "max_tokens": 1000,
                },
                timeout=60,
            )
            resp.raise_for_status()
            return _extract(resp.json()["choices"][0]["message"]["content"].strip())
        except Exception:
            pass

    return "", "", ""


# ==============================================================================
# Format cleanup
# ==============================================================================
def clean_format(text: str) -> str:
    text = re.sub(r'(@\S[^\n]*)\n\n+(> )', r'\1\n\2', text)
    text = re.sub(r'(> "[^\n]*"[^\n]*)\n\n+(\*\*)', r'\1\n\2', text)
    text = re.sub(r'(- [^\n]+)\n\n+(- )', r'\1\n\2', text)
    return text


def generate_cover_image(prompt):
    if not SF_API_KEY or not prompt:
        return ""
    try:
        resp = requests.post(
            "https://api.siliconflow.cn/v1/images/generations",
            headers={"Authorization": f"Bearer {SF_API_KEY}",
                     "Content-Type": "application/json"},
            json={"model": "black-forest-labs/FLUX.1-schnell",
                  "prompt": prompt, "n": 1, "image_size": "1280x720"},
            timeout=120,
        )
        resp.raise_for_status()
        return resp.json()["data"][0]["url"]
    except Exception:
        return ""


def upload_to_imgbb(image_path):
    imgbb_key = os.getenv("IMGBB_API_KEY", "")
    if not imgbb_key or not os.path.exists(image_path):
        return ""
    try:
        with open(image_path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode("utf-8")
        resp = requests.post(
            "https://api.imgbb.com/1/upload",
            params={"key": imgbb_key}, data={"image": img_b64}, timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("success"):
            return data["data"]["url"]
        return ""
    except Exception:
        return ""


# ==============================================================================
# Feishu card — v3.3: fix ## and --- artifacts
# ==============================================================================
def _preprocess_md(content_md: str) -> str:
    content_md = re.sub(r'^###\s+(.+)$', r'**\1**', content_md, flags=re.MULTILINE)
    content_md = re.sub(
        r'^##\s+(.+)$',
        r'\n**▌ \1**',
        content_md,
        flags=re.MULTILINE,
    )
    content_md = re.sub(r'^\s*---\s*$', '', content_md, flags=re.MULTILINE)
    content_md = re.sub(
        r'\n(\*\*[🔁🤖⚔️🏭🦾💡🔥📊🧠💰🌐🚀🌊])',
        r'\n\n\n\1',
        content_md,
    )
    content_md = re.sub(r'\n{3,}', '\n\n', content_md)
    return content_md.strip()


def _split_to_elements(content_md: str) -> list:
    sections = re.split(r'\n(?=\n\*\*▌)', content_md)
    elements = []
    for section in sections:
        section = section.strip()
        if not section:
            continue
        if len(section) <= 4000:
            elements.append({"tag": "markdown", "content": section})
        else:
            paragraphs = section.split('\n\n')
            chunk = ""
            for para in paragraphs:
                if len(chunk) + len(para) + 2 > 3800 and chunk:
                    elements.append({"tag": "markdown", "content": chunk.strip()})
                    chunk = para
                else:
                    chunk = chunk + "\n\n" + para if chunk else para
            if chunk.strip():
                elements.append({"tag": "markdown", "content": chunk.strip()})
    return elements


def send_to_feishu_card(content_md: str, today_str: str, model_label: str = "Claude"):
    webhooks = get_feishu_webhooks()
    if not webhooks:
        print("[Push] Warning: No Feishu webhooks found.", flush=True)
        return

    formatted_content = _preprocess_md(content_md)
    content_elements  = _split_to_elements(formatted_content)

    card_payload = {
        "msg_type": "interactive",
        "card": {
            "config": {
                "wide_screen_mode": True,
                "enable_forward": True,
            },
            "header": {
                "title": {
                    "content": f"昨晚，那些出海搞钱的人都在聊啥 | {today_str}",
                    "tag": "plain_text",
                },
                "template": "blue",
            },
            "elements": content_elements + [
                {"tag": "hr"},
                {
                    "tag": "note",
                    "elements": [
                        {
                            "tag": "plain_text",
                            "content": f"Powered by Grok + {model_label}",
                        }
                    ],
                },
            ],
        },
    }

    for url in webhooks:
        try:
            resp = requests.post(url, json=card_payload, timeout=20)
            resp.raise_for_status()
            print(f"[Push/{model_label}] OK Card sent to Feishu: {url.split('/')[-1][:8]}...",
                  flush=True)
        except Exception as e:
            print(f"[Push/{model_label}] ERROR Failed to send card: {e}", flush=True)


# ==============================================================================
# WeChat HTML push — v3.3: handle ## and ---
# ==============================================================================
def _md_to_html(text):
    lines = text.split("\n")
    html_lines = []
    for line in lines:
        m = re.match(r'^##\s+(.+)$', line)
        if m:
            html_lines.append(
                f'<h3 style="margin:18px 0 6px 0;font-size:16px;'
                f'border-left:4px solid #4A90E2;padding-left:8px;">'
                f'{m.group(1)}</h3>'
            )
            continue
        m3 = re.match(r'^###\s+(.+)$', line)
        if m3:
            html_lines.append(f'<p><strong>{m3.group(1)}</strong></p>')
            continue
        if re.match(r'^\s*---\s*$', line):
            html_lines.append('<hr style="border:none;border-top:1px solid #eee;margin:12px 0;"/>')
            continue
        converted = re.sub(r'\*\*([^*]+?)\*\*', r'<b>\1</b>', line)
        html_lines.append(converted)

    return "<br/>".join(html_lines)


def build_wechat_html(text, cover_url="", insight=""):
    cover_block = (
        f'<p style="text-align:center;margin:0 0 16px 0;">'
        f'<img src="{cover_url}" style="max-width:100%;border-radius:8px;" /></p>'
        if cover_url else ""
    )
    insight_block = (
        f'<div style="border-radius:8px;background:#FFF7E6;padding:12px 14px;'
        f'margin:0 0 16px 0;"><div style="font-weight:bold;margin-bottom:6px;">'
        f'Insight</div><div>{insight.replace(chr(10), "<br/>")}</div></div>'
        if insight else ""
    )
    text = clean_format(text)
    return cover_block + insight_block + _md_to_html(text)


def push_to_jijyun(html_content, title, cover_url=""):
    if not JIJYUN_WEBHOOK_URL:
        return
    try:
        resp = requests.post(
            JIJYUN_WEBHOOK_URL,
            json={"title": title, "author": "Prinski",
                  "html_content": html_content, "cover_jpg": cover_url},
            timeout=30,
        )
        print(f"WeChat push: {resp.status_code} | {resp.text[:120]}", flush=True)
    except Exception as e:
        print(f"WeChat push error: {e}", flush=True)


# ==============================================================================
# Save daily data
# ==============================================================================
def save_daily_data(today_str: str, post_objects: list, meta_results: dict,
                    report_text: str, classification: dict):
    data_dir = Path(f"data_cn/{today_str}")
    data_dir.mkdir(parents=True, exist_ok=True)

    combined_txt = "\n".join(
        json.dumps(obj, ensure_ascii=False)
        for obj in post_objects
        if obj.get("type") != "meta"
    )
    (data_dir / "combined.txt").write_text(combined_txt, encoding="utf-8")
    (data_dir / "meta.json").write_text(
        json.dumps(meta_results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if report_text:
        (data_dir / "daily_report.txt").write_text(report_text, encoding="utf-8")

    cls_path = Path("data_cn/classification.json")
    cls_path.write_text(
        json.dumps({"date": today_str, "classification": classification},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    post_count = sum(1 for o in post_objects if o.get("type") != "meta")
    print(
        f"[Data] OK Saved to {data_dir} "
        f"({post_count} posts, {len(meta_results)} accounts)",
        flush=True,
    )


# ==============================================================================
# Main
# ==============================================================================
def main():
    print("=" * 60, flush=True)
    print("昨晚，那些出海搞钱的人都在聊啥 v3.4 (Grok search + Claude/Kimi summary)", flush=True)
    print("=" * 60, flush=True)

    test_mode = _is_test_mode()
    selected_accounts = BATCH1_ACCOUNTS if test_mode else ALL_ACCOUNTS
    if test_mode:
        print("[TEST_MODE] Enabled: only running ONE Phase1 batch with Batch1 (14 accounts).", flush=True)
        if _skip_push_in_test_mode():
            print("[TEST_MODE] TEST_MODE_NO_PUSH enabled: will skip Feishu/WeChat pushes.", flush=True)

    check_cookie_expiry()
    is_storage_state = prepare_session_file()
    today_str, _ = get_dates()

    Path("data_cn").mkdir(exist_ok=True)

    meta_results  = {}
    phase1_posts  = {}
    phase2_posts  = {}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox", "--disable-setuid-sandbox",
                "--disable-dev-shm-usage", "--disable-gpu",
                "--disable-blink-features=AutomationControlled",
                "--window-size=1280,800",
            ],
        )

        ctx_opts = {
            "viewport":   {"width": 1280, "height": 800},
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            "locale": "zh-CN",
        }
        if is_storage_state:
            ctx_opts["storage_state"] = "session_state.json"

        context = browser.new_context(**ctx_opts)
        if not is_storage_state:
            load_raw_cookies(context)

        # -- Login verification --
        verify_page = context.new_page()
        verify_page.goto("https://grok.com", wait_until="domcontentloaded", timeout=60000)
        time.sleep(3)
        if _is_login_page(verify_page.url):
            print("ERROR: Not logged in - Cookie/Session expired. "
                  "Please update SUPER_GROK_COOKIES.", flush=True)
            browser.close()
            raise SystemExit(1)
        print("OK Logged in to Grok", flush=True)
        verify_page.close()

        # ======================================================================
        # Phase 1: Tiered scan (batches × 25, deadline 40 min)
        # ======================================================================
        print("\n" + "=" * 50, flush=True)
        print(f"Phase 1: Tiered scan (PHASE1_DEADLINE={PHASE1_DEADLINE//60} min)", flush=True)
        print("=" * 50, flush=True)

        # In test mode, keep accounts in a single batch.
        BATCH_SIZE = 25 if not test_mode else 50

        for batch_num, batch_start in enumerate(
                range(0, len(selected_accounts), BATCH_SIZE), start=1):

            if test_mode and batch_num > 1:
                print("[TEST_MODE] Phase1 limited to 1 batch, stopping further batches.", flush=True)
                break

            elapsed = time.time() - _START_TIME
            if elapsed > PHASE1_DEADLINE:
                remaining_accounts = selected_accounts[batch_start:]
                print(
                    f"\n[Phase 1] Warning: Timeout ({elapsed:.0f}s > {PHASE1_DEADLINE}s), "
                    f"skipping {len(remaining_accounts)} remaining accounts (B-tier degradation).",
                    flush=True,
                )
                for acc in remaining_accounts:
                    if acc not in meta_results:
                        meta_results.setdefault(acc, {"total": 1, "max_l": 0, "latest": "NA"})
                break

            batch   = selected_accounts[batch_start:batch_start + BATCH_SIZE]
            label   = f"Phase1-Batch{batch_num}" + ("-TEST" if test_mode else "")
            results = run_grok_batch(context, batch, build_phase1_prompt, label)

            # Parse Phase 1 results (single clean loop)
            for obj in results:
                account = obj.get("a", "").lstrip("@")
                if not account:
                    continue

                if obj.get("type") == "meta":
                    meta_results[account] = {
                        "total":  obj.get("total", 0),
                        "max_l":  obj.get("max_l", 0),
                        "latest": obj.get("latest", "NA"),
                    }
                else:
                    phase1_posts.setdefault(account, []).append(obj)

        print(
            f"\n[Phase 1] Done: {len(meta_results)} metadata rows, "
            f"{len(phase1_posts)} accounts with posts.",
            flush=True,
        )

        # -- Classification --
        classification = classify_accounts(meta_results)

        # Fallback: any un-classified account -> "B"
        for acc in selected_accounts:
            if acc not in classification:
                classification[acc] = "B"

        s_accounts = [a for a, t in classification.items() if t == "S"]
        a_accounts = [a for a, t in classification.items() if t == "A"]
        b_accounts = [a for a, t in classification.items() if t == "B"]
        inactive   = [a for a, t in classification.items() if t == "inactive"]

        print(f"\n[Classification] S={len(s_accounts)} A={len(a_accounts)} "
              f"B={len(b_accounts)} inactive={len(inactive)}", flush=True)
        if s_accounts:
            print(f"  S-tier: {', '.join(s_accounts)}", flush=True)
        if a_accounts:
            print(f"  A-tier (first 10): {', '.join(a_accounts[:10])}", flush=True)

        # ======================================================================
        # Phase 2-S: Deep-collect S-tier
        # ======================================================================
        if s_accounts and time.time() - _START_TIME < GLOBAL_DEADLINE:
            print("\n" + "=" * 50, flush=True)
            print(f"Phase 2-S: Deep collection ({len(s_accounts)} S accounts)", flush=True)
            print("=" * 50, flush=True)

            s_results = run_grok_batch(
                context, s_accounts, build_phase2_s_prompt,
                label="Phase2-S" + ("-TEST" if test_mode else ""),
                initial_wait=60,
            )
            for obj in s_results:
                account = obj.get("a", "").lstrip("@")
                if account and obj.get("type") != "meta":
                    phase2_posts.setdefault(account, []).append(obj)

            print(f"[Phase 2-S] OK {sum(len(v) for v in phase2_posts.values())} posts collected",
                  flush=True)
        else:
            print("[Phase 2-S] Skipped (no S-tier accounts or global timeout)", flush=True)

        # ======================================================================
        # Phase 2-A: Collect A-tier
        # ======================================================================
        if a_accounts and time.time() - _START_TIME < GLOBAL_DEADLINE:
            print("\n" + "=" * 50, flush=True)
            print(f"Phase 2-A: Collection ({len(a_accounts)} A accounts)", flush=True)
            print("=" * 50, flush=True)

            a_results = run_grok_batch(
                context, a_accounts, build_phase2_a_prompt,
                label="Phase2-A" + ("-TEST" if test_mode else ""),
                initial_wait=60,
            )
            for obj in a_results:
                account = obj.get("a", "").lstrip("@")
                if account and obj.get("type") != "meta":
                    phase2_posts.setdefault(account, []).append(obj)

            a_post_count = sum(len(v) for v in phase2_posts.values())
            print(f"[Phase 2-A] OK Total phase2 posts: {a_post_count}", flush=True)
        else:
            print("[Phase 2-A] Skipped (no A-tier accounts or global timeout)", flush=True)

        # -- Session renewal --
        save_and_renew_session(context)
        browser.close()

    # ==========================================================================
    # Build combined JSONL for LLM
    # ==========================================================================
    print("\n[Data] Building combined JSONL...", flush=True)

    all_posts_flat = []

    for acc in s_accounts + a_accounts:
        if phase2_posts.get(acc):
            all_posts_flat.extend(phase2_posts[acc])
        elif phase1_posts.get(acc):
            all_posts_flat.extend(phase1_posts[acc])

    for acc in b_accounts:
        if phase1_posts.get(acc):
            all_posts_flat.extend(phase1_posts[acc])

    combined_jsonl = "\n".join(
        json.dumps(obj, ensure_ascii=False)
        for obj in all_posts_flat
        if obj.get("type") != "meta"
    )
    print(f"[Data] Combined JSONL: {len(all_posts_flat)} posts, "
          f"{len(combined_jsonl)} chars", flush=True)

    # ==========================================================================
    # LLM: Claude first, Kimi fallback
    # ==========================================================================
    report_text   = ""
    cover_title   = ""
    cover_prompt  = ""
    cover_insight = ""
    model_label   = ""

    if combined_jsonl.strip():
        print("\n[LLM] Calling Claude (primary)...", flush=True)
        report_text, cover_title, cover_prompt, cover_insight = llm_call_claude(
            combined_jsonl, today_str
        )
        if report_text:
            model_label = "Claude"
        else:
            print("[LLM] Claude failed, falling back to Kimi-k2.5...", flush=True)
            report_text, cover_title, cover_prompt, cover_insight = llm_call_kimi(
                combined_jsonl, today_str
            )
            if report_text:
                model_label = "Kimi-k2.5"

        if not report_text:
            print("[LLM] WARNING: Both Claude and Kimi failed to generate report.", flush=True)
    else:
        print("[LLM] WARNING: No posts collected, skipping LLM call.", flush=True)

    # -- Cover image --
    cover_url = ""
    if cover_prompt:
        print("\n[Image] Generating cover image...", flush=True)
        cover_url = generate_cover_image(cover_prompt)
        print(f"[Image] {'OK ' + cover_url[:60] if cover_url else 'Skipped'}", flush=True)

    # ==========================================================================
    # Push to Feishu
    # ==========================================================================
    if report_text:
        if test_mode and _skip_push_in_test_mode():
            print("[TEST_MODE] Skip Feishu push.", flush=True)
        else:
            print("\n[Push] Sending to Feishu...", flush=True)
            send_to_feishu_card(report_text, today_str, model_label=model_label or "AI")

    # ==========================================================================
    # Push to WeChat (Jijyun)
    # ==========================================================================
    if report_text and JIJYUN_WEBHOOK_URL:
        if test_mode and _skip_push_in_test_mode():
            print("[TEST_MODE] Skip WeChat push.", flush=True)
        else:
            print("\n[Push] Sending to WeChat (Jijyun)...", flush=True)
            html_content = build_wechat_html(report_text, cover_url=cover_url, insight=cover_insight)
            wechat_title = cover_title or f"中文圈出海日报 | {today_str}"
            push_to_jijyun(html_content, title=wechat_title, cover_url=cover_url)

    # ==========================================================================
    # Save daily data
    # ==========================================================================
    print("\n[Data] Saving daily data...", flush=True)
    save_daily_data(
        today_str=today_str,
        post_objects=all_posts_flat,
        meta_results=meta_results,
        report_text=report_text,
        classification=classification,
    )

    print("\n" + "=" * 60, flush=True)
    print(f"DONE | today={today_str} | posts={len(all_posts_flat)} | "
          f"model={model_label or 'none'} | feishu_hooks={len(get_feishu_webhooks())}",
          flush=True)
    print("=" * 60, flush=True)


if __name__ == "__main__":
    main()
