"""
Snowflake Release Monitor — Free Edition
Stack: GitHub Actions (schedule) + Gemini Flash (AI) + Telegram (alerts)
No server, no paid APIs, no installation needed.
"""
import hashlib, json, logging, os, re, sys
from datetime import datetime
from typing import Optional

import requests
from bs4 import BeautifulSoup
import feedparser
import google.generativeai as genai

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# ── Config (all from GitHub Secrets) ─────────────────────────────────────────

GEMINI_API_KEY     = os.environ["GEMINI_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]
MAX_PER_RUN        = int(os.getenv("MAX_PER_RUN", "5"))
STATE_FILE         = "seen_items.json"

# ── Sources ───────────────────────────────────────────────────────────────────

RELEASE_NOTES_URL = "https://docs.snowflake.com/en/release-notes/new-features"
BLOG_RSS_URL      = "https://www.snowflake.com/feed/"
HEADERS           = {"User-Agent": "SnowflakeMonitorBot/1.0"}

BLOG_KEYWORDS = {
    "new feature", "generally available", "public preview", "private preview",
    "introducing", "now available", "ga:", "preview:", "announcing",
    "cortex", "snowpark", "arctic", "dynamic table", "streamlit in snowflake"
}

# ── State management (JSON file committed back to GitHub repo) ────────────────

def load_seen() -> set:
    try:
        with open(STATE_FILE) as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()

def save_seen(seen: set) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(sorted(seen), f, indent=2)

def item_key(title: str, url: str) -> str:
    return hashlib.md5(f"{title.lower().strip()}|{url.lower().strip()}".encode()).hexdigest()

# ── Scraper ───────────────────────────────────────────────────────────────────

def scrape_release_notes() -> list[dict]:
    """Scrape docs.snowflake.com/en/release-notes/new-features"""
    items = []
    try:
        resp = requests.get(RELEASE_NOTES_URL, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        current_release  = ""
        current_category = ""

        for section in soup.find_all("section"):
            h = section.find(["h2", "h3", "h4", "h5"], recursive=False)
            if not h:
                continue
            level = int(h.name[1])
            text  = re.sub(r"[¶#\u00b6]", "", h.get_text()).strip()
            text  = " ".join(text.split())

            if level == 2:
                current_release  = text
                current_category = ""
            elif level == 3:
                current_category = text
            elif level in (4, 5) and 5 <= len(text) <= 200:
                anchor = section.get("id") or h.get("id") or ""
                url    = f"{RELEASE_NOTES_URL}#{anchor}" if anchor else RELEASE_NOTES_URL

                # Extract description text from the section
                desc = []
                for elem in section.children:
                    name = getattr(elem, "name", None)
                    if name in ("h2","h3","h4","h5","h6"):
                        continue
                    if name == "p":
                        t = elem.get_text().strip()
                        if t: desc.append(t)
                    elif name in ("ul","ol"):
                        for li in elem.find_all("li", limit=4):
                            desc.append(f"• {li.get_text().strip()}")
                    if sum(len(d) for d in desc) > 600:
                        break

                items.append({
                    "title":    text,
                    "url":      url,
                    "content":  "\n".join(desc)[:700],
                    "date":     current_release,
                    "category": current_category,
                    "source":   "release_notes"
                })

        # Keep only the 2 most recently seen release sections
        seen_releases: list[str] = []
        for item in items:
            if item["date"] not in seen_releases:
                seen_releases.append(item["date"])
            if len(seen_releases) > 2:
                break
        recent = set(seen_releases[:2])
        items  = [i for i in items if i["date"] in recent]

        logger.info(f"Release notes: {len(items)} item(s)")
    except Exception as e:
        logger.warning(f"Release notes scrape failed: {e}")
    return items


def scrape_blog_rss() -> list[dict]:
    """Scrape Snowflake blog RSS, filtered to feature-related posts only."""
    items = []
    try:
        resp = requests.get(BLOG_RSS_URL, headers=HEADERS, timeout=30)
        feed = feedparser.parse(resp.content)
        for entry in feed.entries[:30]:
            title = (entry.get("title") or "").strip()
            link  = (entry.get("link") or "").strip()
            if not title or not link:
                continue
            combined = f"{title} {entry.get('summary','')}".lower()
            if not any(kw in combined for kw in BLOG_KEYWORDS):
                continue
            raw     = entry.get("summary") or entry.get("description") or ""
            summary = BeautifulSoup(raw, "html.parser").get_text()[:400]
            pub     = entry.get("published", "")
            items.append({
                "title":    title,
                "url":      link,
                "content":  summary,
                "date":     pub[:10] if len(pub) >= 10 else datetime.now().strftime("%Y-%m-%d"),
                "category": "Blog Announcement",
                "source":   "blog"
            })
        logger.info(f"Blog RSS: {len(items)} feature post(s)")
    except Exception as e:
        logger.warning(f"Blog RSS failed: {e}")
    return items[:8]

# ── Summarizer (Gemini 1.5 Flash — free tier) ─────────────────────────────────

PROMPT = """You are a senior Snowflake data engineer writing feature briefings for a team.
Be direct and specific. No marketing language.

Feature: {title}
Category: {category}
Date: {date}
Content:
{content}
URL: {url}

Return ONLY a valid JSON object — no markdown fences, no extra text:
{{
  "what_it_is": "1-3 sentence plain English explanation of what this feature does",
  "why_it_matters": "Why data engineers should care — concrete impact on workflows, cost, or performance",
  "key_capabilities": ["specific capability 1", "specific capability 2", "specific capability 3"],
  "benefits": "Concrete benefits — faster/cheaper/simpler/more secure",
  "use_cases": ["concrete use case 1", "concrete use case 2"],
  "prerequisites": "Role, edition, or account requirements — or 'No special requirements'",
  "feature_type": "New Feature | Enhancement | GA | Public Preview | Behavior Change | Deprecation"
}}"""


def summarize(item: dict) -> Optional[dict]:
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel("gemini-1.5-flash")
    prompt = PROMPT.format(
        title    = item["title"],
        content  = (item.get("content") or "No additional content.")[:2000],
        url      = item["url"],
        category = item.get("category", "Snowflake"),
        date     = item.get("date", "Recent")
    )
    try:
        resp = model.generate_content(prompt)
        text = resp.text.strip()
        text = re.sub(r"```(?:json)?", "", text).strip("`").strip()
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except Exception:
                pass
        logger.error(f"JSON parse failed for: {item['title']}")
        return None
    except Exception as e:
        logger.error(f"Gemini error for '{item['title']}': {e}")
        return None

# ── Telegram notifier ─────────────────────────────────────────────────────────

TYPE_EMOJI = {
    "New Feature":    "🆕",
    "Enhancement":    "⚡",
    "GA":             "✅",
    "Public Preview": "🔬",
    "Private Preview":"🧪",
    "Behavior Change":"⚠️",
    "Deprecation":    "🚫",
}


def format_telegram_msg(item: dict, summary: dict) -> str:
    emoji  = TYPE_EMOJI.get(summary.get("feature_type", ""), "📦")
    ftype  = summary.get("feature_type", "Update")
    caps   = "\n".join(f"  • {c}" for c in summary.get("key_capabilities", [])[:4])
    uses   = "\n".join(f"  • {u}" for u in summary.get("use_cases", [])[:3])
    date   = item.get("date","")
    cat    = item.get("category","")
    meta   = f"_{date}  |  {cat}_" if date or cat else ""

    return (
        f"{emoji} *{ftype}: {item['title']}*\n"
        f"{meta}\n\n"
        f"📝 *What it is:*\n{summary.get('what_it_is','')}\n\n"
        f"💡 *Why it matters:*\n{summary.get('why_it_matters','')}\n\n"
        f"⚙️ *Key Capabilities:*\n{caps}\n\n"
        f"✅ *Benefits:*\n{summary.get('benefits','')}\n\n"
        f"🎯 *Use Cases:*\n{uses}\n\n"
        f"🔧 *Prerequisites:*\n{summary.get('prerequisites','No special requirements')}\n\n"
        f"📎 {item['url']}"
    ).strip()


def send_telegram(messages: list[str]) -> bool:
    if not messages:
        return True

    count    = len(messages)
    date_str = datetime.now().strftime("%B %d, %Y")
    header   = (
        f"🔔 *Snowflake Monitor* — {date_str}\n"
        f"_{count} new update{'s' if count > 1 else ''} detected_\n\n"
        f"{'─' * 24}\n\n"
    )
    separator = f"\n\n{'─' * 24}\n\n"
    full_text = header + separator.join(messages)

    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    # Telegram limit is 4096 chars; split if needed
    chunks = [full_text[i:i+4000] for i in range(0, len(full_text), 4000)]
    for chunk in chunks:
        resp = requests.post(api_url, json={
            "chat_id":                 TELEGRAM_CHAT_ID,
            "text":                    chunk,
            "parse_mode":              "Markdown",
            "disable_web_page_preview": True
        }, timeout=15)
        if not resp.ok:
            logger.error(f"Telegram send failed: {resp.status_code} {resp.text}")
            return False

    return True

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    logger.info("=" * 50)
    logger.info(f"Run start: {datetime.now().isoformat()}")

    # 1. Scrape
    all_items = scrape_release_notes() + scrape_blog_rss()
    logger.info(f"Total items fetched: {len(all_items)}")

    # 2. Load state
    seen        = load_seen()
    is_first    = len(seen) == 0

    # 3. First run → seed without sending
    if is_first:
        logger.info("First run detected — seeding state, no notifications sent")
        for item in all_items:
            seen.add(item_key(item["title"], item["url"]))
        save_seen(seen)
        logger.info(f"Seeded {len(seen)} items. Next scheduled run will send real alerts.")
        return

    # 4. Find new items
    new_items = [i for i in all_items if item_key(i["title"], i["url"]) not in seen]
    logger.info(f"New (unseen) items: {len(new_items)}")

    if not new_items:
        logger.info("Nothing new. Done.")
        return

    to_process = new_items[:MAX_PER_RUN]
    if len(new_items) > MAX_PER_RUN:
        logger.info(f"Capped at {MAX_PER_RUN}; {len(new_items)-MAX_PER_RUN} held for next run")

    # 5. Summarise
    formatted = []
    for item in to_process:
        logger.info(f"Summarising: {item['title'][:80]}")
        summary = summarize(item)
        if summary:
            formatted.append(format_telegram_msg(item, summary))
        # Always mark seen to avoid infinite retries on failures
        seen.add(item_key(item["title"], item["url"]))

    # 6. Send
    if formatted:
        ok = send_telegram(formatted)
        logger.info(f"Sent {len(formatted)} summary/summaries to Telegram. ok={ok}")
    else:
        logger.warning("No summaries generated — nothing sent.")

    # 7. Save updated state
    save_seen(seen)
    logger.info("Run complete.")


if __name__ == "__main__":
    main()
