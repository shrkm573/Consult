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
    PushMessageRequest,
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
GEMINI_SYSTEM = """คุณเป็นผู้เชี่ยวชาญตู้ทะเลฝั่ง Gemini
ตอบคำถามอย่างตรงไปตรงมา ให้ข้อมูลที่ครบถ้วน
ตอบกระชับ ไม่เกิน 180 คำ ใช้ emoji ได้เล็กน้อย"""

CLAUDE_SYSTEM = """คุณเป็นผู้เชี่ยวชาญตู้ทะเลฝั่ง Claude เน้นความถูกต้องทางเทคนิค
บริบทตู้: LPS-dominant ขนาด 24 นิ้ว (~140L) ระบบ KZ (ZeoStart3 + CV)
ปลา: Clownfish 2 ตัว (Black + Orange)
ปะการัง: Hammer, Octopus, Brain, Candy Cane, Acan, GSP, ลูกโป่ง, Zoa
Skimmer: Aqua Excel Nano 70D | PO4 ~0–0.03 | NO2 = 0
ตอบกระชับ ไม่เกิน 180 คำ"""

CLAUDE_CRITIQUE_TEMPLATE = """คุณเป็น Claude ผู้เชี่ยวชาญเทคนิคตู้ทะเล
บริบทตู้: LPS-dominant 24 นิ้ว (~140L) ระบบ KZ | Clownfish 2 ตัว | PO4 ~0–0.03 | Skimmer Aqua Excel Nano 70D

Gemini เพิ่งตอบคำถาม "{question}" ว่า:
---
{gemini}
---

จงวิจารณ์คำตอบของ Gemini แบบตรงๆ:
- ถ้า Gemini พูดผิด ให้แย้งทันที บอกว่าผิดตรงไหนและถูกต้องคือ?
- ถ้า Gemini พูดถูกแต่ไม่ครบ ให้เสริมในสิ่งที่ขาด
- ถ้า Gemini พูดถูกหมด ให้บอกว่าเห็นด้วย แต่เพิ่ม insight เชิงเทคนิค
ตอบกระชับ ไม่เกิน 150 คำ ไม่ต้องเกริ่น ตอบตรงๆ เลย"""

SYNTHESIS_TEMPLATE = """สรุปจากการถกกันระหว่าง Gemini และ Claude เรื่อง "{question}":

Gemini: {gemini}
Claude แย้ง/เสริม: {claude_critique}

จงสรุปคำตอบสุดท้ายแบบกระชับ ไม่เกิน 80 คำ
โดยใช้ข้อมูลที่ถูกต้องที่สุดจากทั้งสองฝั่ง
ไม่ต้องพูดว่า "สรุป" — ตอบตรงๆ เลย"""

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


def ask_claude_critique(question: str, gemini_ans: str) -> str:
    """ให้ Claude วิจารณ์คำตอบของ Gemini — fallback ไป Gemini ถ้า credits หมด"""
    # ใช้ replace แทน .format() เพื่อป้องกัน crash จาก { } ใน AI response
    prompt = CLAUDE_CRITIQUE_TEMPLATE.replace("{question}", question).replace("{gemini}", gemini_ans)
    try:
        resp = claude_client.messages.create(
            model="claude-opus-4-8",
            max_tokens=600,
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
        except Exception as e:
            return f"[error: {e}]"


def synthesize(question: str, gemini_ans: str, claude_critique: str) -> str:
    prompt = (SYNTHESIS_TEMPLATE
              .replace("{question}", question)
              .replace("{gemini}", gemini_ans)
              .replace("{claude_critique}", claude_critique))
    try:
        resp = claude_client.messages.create(
            model="claude-opus-4-8",
            max_tokens=400,
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
    """Gemini เสนอ → Claude แย้ง → สรุป แบบ debate format"""
    history = get_history(user_id)

    # Step 1: Gemini ตอบก่อน
    gemini_ans = ask_gemini(question, history)

    # Step 2: Claude วิจารณ์ Gemini (ทำหลัง Gemini เสร็จ)
    claude_critique = ask_claude_critique(question, gemini_ans)

    # Step 3: สรุปผล
    final = synthesize(question, gemini_ans, claude_critique)

    # รวมเป็น debate format
    answer = (
        f"🤖 Gemini:\n{gemini_ans}\n\n"
        f"🔬 Claude แย้ง:\n{claude_critique}\n\n"
        f"⚖️ ตัดสิน:\n{final}"
    )

    # บันทึกลง SQLite
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

    # แสดง loading animation ทันที
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        try:
            line_bot_api.show_loading_animation(
                ShowLoadingAnimationRequest(chatId=user_id, loadingSeconds=60)
            )
        except Exception:
            pass

    # ประมวลผลใน background thread — ใช้ push_message ไม่มี token หมดอายุ
    def process_and_push():
        try:
            answer = get_best_answer(user_message, user_id)
            if len(answer) > 4900:
                answer = answer[:4897] + "..."
        except Exception as e:
            answer = f"⚠️ เกิดข้อผิดพลาด: {e}"
        try:
            with ApiClient(configuration) as api_client:
                line_bot_api = MessagingApi(api_client)
                line_bot_api.push_message(
                    PushMessageRequest(to=user_id, messages=[TextMessage(text=answer)])
                )
        except Exception as e:
            print(f"push_message error: {e}")

    threading.Thread(target=process_and_push, daemon=True).start()


# ──────────────────────────────────────────
# Main
# ──────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"🐠 Reef Bot กำลังเริ่มต้น... port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
