"""
chatbot.py — Snowflake Expert Chatbot
Runs on Render.com (free web service), receives Telegram messages via webhook,
answers any Snowflake question using Gemini Flash.
"""
import logging
import os

import google.generativeai as genai
import requests
from flask import Flask, jsonify, request

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GEMINI_API_KEY     = os.environ["GEMINI_API_KEY"]
TELEGRAM_CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]   # only answer YOUR messages

# ── Gemini system prompt ──────────────────────────────────────────────────────

SYSTEM = """You are SnowflakeBot — a senior Snowflake data engineer assistant inside Telegram.

Answer any question about Snowflake clearly and completely. Always include:
- A plain English explanation of the concept
- The exact SQL/Python syntax
- A working code example
- Common use cases or tips

Topics you cover:
• SQL — SELECT, JOIN, CTE, WINDOW functions, MERGE, COPY, PUT, GET
• Snowflake features — Dynamic Tables, Streams, Tasks, Pipes, Stages
• Cortex AI — COMPLETE, EMBED_TEXT, CLASSIFY_TEXT, EXTRACT_ANSWER, AI_FILTER, Cortex Analyst, Cortex Agents
• Snowpark — Python DataFrames, stored procedures, UDFs
• Streamlit in Snowflake — building apps
• RBAC — roles, grants, row access policies, masking policies
• Performance — clustering, query profiling, warehouse sizing, materialization
• Cost optimization — credit usage, auto-suspend, result cache
• Schema — star schema, views, dynamic data masking
• Fivetran, dbt patterns with Snowflake
• Any recent Snowflake release or announcement

Formatting rules for Telegram Markdown:
- Use *bold* for section headers
- Use `backticks` for SQL keywords and function names
- Use ``` code blocks ``` for multi-line SQL or Python
- Use • for bullet points
- Keep answers complete but to the point
- If the question is vague, answer the most common interpretation and offer to go deeper"""

# ── Gemini call ───────────────────────────────────────────────────────────────

def ask_gemini(question: str) -> str:
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel("gemini-1.5-flash")
    try:
        resp = model.generate_content(f"{SYSTEM}\n\nUser: {question}")
        return resp.text.strip()
    except Exception as e:
        logger.error(f"Gemini error: {e}")
        return "❌ Sorry, something went wrong. Please try again in a moment."

# ── Telegram sender ───────────────────────────────────────────────────────────

def send(chat_id: int, text: str, parse_mode: str = "Markdown"):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for i in range(0, len(text), 4000):
        chunk = text[i : i + 4000]
        resp = requests.post(url, json={
            "chat_id": chat_id,
            "text": chunk,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True
        }, timeout=15)
        if not resp.ok:
            # Retry without markdown if formatting causes error
            requests.post(url, json={
                "chat_id": chat_id,
                "text": chunk,
                "disable_web_page_preview": True
            }, timeout=15)

# ── Webhook endpoint ──────────────────────────────────────────────────────────

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    if not data or "message" not in data:
        return jsonify({"ok": True})

    message = data["message"]
    chat_id = message["chat"]["id"]
    text    = message.get("text", "").strip()

    # Security — only respond to your own Telegram account
    if str(chat_id) != str(TELEGRAM_CHAT_ID):
        logger.warning(f"Blocked unauthorized chat_id: {chat_id}")
        return jsonify({"ok": True})

    if not text:
        return jsonify({"ok": True})

    logger.info(f"Received: {text[:80]}")

    # ── Commands ──────────────────────────────────────────────────────────────

    if text == "/start":
        send(chat_id,
            "👋 *Welcome to Snowflake Expert Bot!*\n\n"
            "Ask me *anything* about Snowflake and I'll give you a detailed answer with syntax and examples.\n\n"
            "*Try asking:*\n"
            "• `How do I write a MERGE statement?`\n"
            "• `What is a Dynamic Table and how to create one?`\n"
            "• `Show me how to use Cortex AI COMPLETE function`\n"
            "• `What is the difference between a Stream and a Task?`\n"
            "• `How to create a masking policy in Snowflake?`\n"
            "• `What are the latest Snowflake features?`\n\n"
            "_Type /help to see more examples_"
        )
        return jsonify({"ok": True})

    if text == "/help":
        send(chat_id,
            "*Example questions you can ask:*\n\n"
            "*SQL & Functions*\n"
            "• How do I use window functions in Snowflake?\n"
            "• Write a CTE with RECURSIVE example\n"
            "• How does QUALIFY work?\n\n"
            "*Features*\n"
            "• Explain Streams and Tasks with an example\n"
            "• What is a Snowflake Stage and how to use it?\n"
            "• How to create a Snowpipe?\n\n"
            "*Cortex AI*\n"
            "• How to use CORTEX.COMPLETE?\n"
            "• What is a Cortex Agent?\n"
            "• How to build a semantic view?\n\n"
            "*Performance*\n"
            "• How to check query performance?\n"
            "• When should I use clustering keys?\n"
            "• How to reduce Snowflake costs?\n\n"
            "Just type your question normally — no commands needed!"
        )
        return jsonify({"ok": True})

    # ── Main Q&A ──────────────────────────────────────────────────────────────

    send(chat_id, "🤔 _Looking that up..._")
    answer = ask_gemini(text)
    send(chat_id, answer)

    return jsonify({"ok": True})


# ── Health check (Render pings this to keep service alive) ────────────────────

@app.route("/", methods=["GET"])
def health():
    return "✅ Snowflake Bot is running", 200


# ── Start ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"Starting chatbot on port {port}")
    app.run(host="0.0.0.0", port=port)
