"""
🐠 Reef Tank Dual AI LINE Bot
==============================
Gemini  → brainstorm / engagement
Claude  → technical accuracy
Synthesis → Claude รวมข้อดีทั้งสอง → ส่งกลับ LINE

Memory: SQLite persistent (จำได้ข้ามเซสชัน ไม่ลืมเมื่อ restart)

Env vars required:
  LINE_CHANNEL_SECRET
  LINE_CHANNEL_ACCESS_TOKEN
  GEMINI_API_KEY
  CLAUDE_API_KEY
  DB_PATH (optional, default: reef_memory.db)
"""

import os
import sys
import sqlite3
import threading

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage,
    ShowLoadingAnimationRequest,
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from linebot.v3.exceptions import InvalidSignatureError
from google import genai
from anthropic import Anthropic

# ──────────────────────────────────────────
# Config
# ──────────────────────────────────────────
LINE_CHANNEL_SECRET       = os.environ.get("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
GEMINI_API_KEY            = os.environ.get("GEMINI_API_KEY")
CLAUDE_API_KEY            = os.environ.get("CLAUDE_API_KEY")
DB_PATH                   = os.environ.get("DB_PATH", "reef_memory.db")
MAX_HISTORY               = 10  # จำนวน turns ที่จำ (1 turn = 1 คำถาม + 1 คำตอบ)

for name, val in [
    ("LINE_CHANNEL_SECRET",       LINE_CHANNEL_SECRET),
    ("LINE_CHANNEL_ACCESS_TOKEN", LINE_CHANNEL_ACCESS_TOKEN),
    ("GEMINI_API_KEY",            GEMINI_API_KEY),
    ("CLAUDE_API_KEY",            CLAUDE_API_KEY),
]:
    if not val:
        raise EnvironmentError(f"กรุณาตั้งค่า {name} ใน environment variables")

# ──────────────────────────────────────────
# SQLite Persistent Memory
# ──────────────────────────────────────────
def init_db():
    """สร้าง database และ table ถ้ายังไม่มี"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS history (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    TEXT    NOT NULL,
            role       TEXT    NOT NULL,
            content    TEXT    NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()
    print(f"✅ Database พร้อมใช้: {DB_PATH}")


def get_history(user_id: str) -> list:
    """ดึง history ล่าสุด N turns สำหรับ user"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        """SELECT role, content FROM history
           WHERE user_id = ?
           ORDER BY created_at DESC
           LIMIT ?""",
        (user_id, MAX_HISTORY * 2),
    )
    rows = c.fetchall()
    conn.close()
    # reverse เพื่อให้เรียงจากเก่าไปใหม่
    return [{"role": row[0], "content": row[1]} for row in reversed(rows)]


def add_to_history(user_id: str, role: str, content: str):
    """บันทึก message ลง database"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT INTO history (user_id, role, content) VALUES (?, ?, ?)",
        (user_id, role, content),
    )
    conn.commit()
    conn.close()


def build_history_text(history: list) -> str:
    """แปลง history เป็น text สำหรับใส่ใน prompt"""
    if not history:
        return ""
    lines = ["--- บทสนทนาก่อนหน้า ---"]
    for msg in history:
        prefix = "ผู้ใช้" if msg["role"] == "user" else "AI"
        lines.append(f"{prefix}: {msg['content']}")
    lines.append("--- จบบทสนทนาก่อนหน้า ---\n")
    return "\n".join(lines)


# ──────────────────────────────────────────
# AI Clients
# ──────────────────────────────────────────
gemini_client = genai.Client(api_key=GEMINI_API_KEY)
claude_client = Anthropic(api_key=CLAUDE_API_KEY)

# ──────────────────────────────────────────
# Prompts
# ──────────────────────────────────────────
GEMINI_SYSTEM = """คุณเป็นผู้เชี่ยวชาญตู้ทะเลที่ตอบตรง ชัดเจน จริงจัง
ไม่ใช้ roleplay ไม่เล่นบทบาท ไม่มีอารมณ์ขัน
ให้ข้อมูลที่ถูกต้องและครบถ้วน ตรงประเด็น
ตอบกระชับ ไม่เกิน 250 คำ ใช้ emoji แบ่งหัวข้อได้เล็กน้อย"""

CLAUDE_SYSTEM = """คุณเป็นผู้เชี่ยวชาญตู้ทะเลที่เน้นความถูกต้องทางเทคนิค
บริบทตู้: LPS-dominant ขนาด 24 นิ้ว (~140L) ระบบ KZ (ZeoStart3 + CV)
ปลา: Clownfish 2 ตัว (Black + Orange)
ปะการัง: Hammer, Octopus, Brain, Candy Cane, Acan, GSP, ลูกโป่ง, Zoa
Skimmer: Aqua Excel Nano 70D | PO4 ~0–0.03 | NO2 = 0
ตอบตรง ชัดเจน เน้นข้อเท็จจริงเชิงเทคนิค
ตอบกระชับ ไม่เกิน 200 คำ"""

SYNTHESIS_TEMPLATE = """คุณเป็นผู้สังเคราะห์คำตอบ ได้รับคำตอบ 2 ฉบับสำหรับคำถามเรื่องตู้ทะเล:

[คำถามผู้ใช้]:
{question}

[Gemini]:
{gemini}

[Claude — เชิงเทคนิค]:
{claude}

จงสังเคราะห์คำตอบที่ดีที่สุดโดย:
1. ถ้า Claude แก้ไขข้อมูลของ Gemini ให้ใช้ข้อมูล Claude เป็นหลัก
2. ถ้าทั้งสองเห็นตรงกัน ให้รวมเป็นคำตอบเดียวกันอย่างลื่นไหล
3. ตอบเป็นภาษาไทย กระชับ ไม่เกิน 400 คำ
4. ใช้ emoji แบ่งหัวข้อให้อ่านง่ายบน LINE
5. ลงท้ายด้วยบรรทัดเล็กๆ: "🤖 Gemini + 🔬 Claude"
"""

# ──────────────────────────────────────────
# AI Functions
# ──────────────────────────────────────────
def ask_gemini(question: str, history: list) -> str:
    history_text = build_history_text(history)
    prompt = f"{GEMINI_SYSTEM}\n\n{history_text}คำถาม: {question}"
    try:
        resp = gemini_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
        )
        return resp.text
    except Exception as e:
        return f"[Gemini error: {e}]"


def ask_claude(question: str, history: list) -> str:
    """ถาม Claude พร้อม history — fallback ไป Gemini ถ้า credits หมด"""
    history_text = build_history_text(history)
    messages = [{"role": "user", "content": f"{CLAUDE_SYSTEM}\n\n{history_text}คำถาม: {question}"}]
    try:
        resp = claude_client.messages.create(
            model="claude-opus-4-8",
            max_tokens=800,
            messages=messages,
        )
        return resp.content[0].text
    except Exception:
        try:
            resp = gemini_client.models.generate_content(
                model="gemini-2.5-flash",
                contents=f"{CLAUDE_SYSTEM}\n\n{history_text}คำถาม: {question}",
            )
            return resp.text
        except Exception as e:
            return f"[error: {e}]"


def synthesize(question: str, gemini_ans: str, claude_ans: str) -> str:
    prompt = SYNTHESIS_TEMPLATE.format(
        question=question,
        gemini=gemini_ans,
        claude=claude_ans,
    )
    try:
        resp = claude_client.messages.create(
            model="claude-opus-4-8",
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text
    except Exception:
        try:
            resp = gemini_client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
            )
            return resp.text
        except Exception:
            return gemini_ans


def get_best_answer(question: str, user_id: str) -> str:
    """ถาม Gemini + Claude พร้อมกัน พร้อม history แล้ว synthesize และบันทึกลง DB"""
    history = get_history(user_id)
    results = {"gemini": None, "claude": None}

    def run_gemini():
        results["gemini"] = ask_gemini(question, history)

    def run_claude():
        results["claude"] = ask_claude(question, history)

    t1 = threading.Thread(target=run_gemini)
    t2 = threading.Thread(target=run_claude)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    answer = synthesize(question, results["gemini"], results["claude"])

    # บันทึกลง SQLite (จำได้ข้ามเซสชัน)
    add_to_history(user_id, "user", question)
    add_to_history(user_id, "assistant", answer)

    return answer


# ──────────────────────────────────────────
# Flask & LINE Webhook
# ──────────────────────────────────────────
init_db()  # เรียกตอน import — ทำงานทั้ง gunicorn และ python โดยตรง
app = Flask(__name__)
configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)


@app.route("/webhook/line", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"


@app.route("/", methods=["GET"])
def health():
    return "🐠 Reef Bot is running!"


@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_message = event.message.text
    user_id = event.source.user_id

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)

        # แสดง loading animation ขณะรอ AI
        try:
            line_bot_api.show_loading_animation(
                ShowLoadingAnimationRequest(chatId=user_id, loadingSeconds=60)
            )
        except Exception:
            pass

        # ดึงคำตอบ (พร้อม persistent memory ต่อ user)
        answer = get_best_answer(user_message, user_id)

        # ตัดถ้าเกิน 4900 ตัวอักษร (LINE limit = 5000)
        if len(answer) > 4900:
            answer = answer[:4897] + "..."

        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=answer)],
            )
        )


# ──────────────────────────────────────────
# Main
# ──────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"🐠 Reef Bot กำลังเริ่มต้น... port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
