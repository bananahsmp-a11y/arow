"""
 █████╗ ██████╗  ██████╗ ██╗    ██╗    ████████╗██████╗  █████╗ ██████╗ ███████╗
██╔══██╗██╔══██╗██╔═══██╗██║    ██║       ██╔══╝██╔══██╗██╔══██╗██╔══██╗██╔════╝
███████║██████╔╝██║   ██║██║ █╗ ██║       ██║   ██████╔╝███████║██║  ██║█████╗
██╔══██║██╔══██╗██║   ██║██║███╗██║       ██║   ██╔══██╗██╔══██║██║  ██║██╔══╝
██║  ██║██║  ██║╚██████╔╝╚███╔███╔╝       ██║   ██║  ██║██║  ██║██████╔╝███████╗
╚═╝  ╚═╝╚═╝  ╚═╝ ╚═════╝  ╚══╝╚══╝        ╚═╝   ╚═╝  ╚═╝╚═╝  ╚═╝╚═════╝ ╚══════╝
Arow Trade — Polymarket Copy-Trade Bot
"""

import os, asyncio, logging, time, json
from datetime import datetime
from typing import Optional
from uuid import uuid4

import aiohttp
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler
)
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions
from py_clob_client.order_builder.constants import BUY, SELL
from py_clob_client.constants import POLYGON

load_dotenv()

# ═══════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════
TELEGRAM_TOKEN    = os.getenv("TELEGRAM_TOKEN", "")
PRIVATE_KEY       = os.getenv("POLYMARKET_PRIVATE_KEY", "")
FUNDER_ADDRESS    = os.getenv("POLYMARKET_FUNDER_ADDRESS", "")
SOL_DEPOSIT_ADDR  = os.getenv("SOL_DEPOSIT_ADDRESS", "GZy4c7kjkzoWNSsJ5FwuqqQpHfz7v6sXdsrWFo4j1p8s")
ALLOWED_CHAT_IDS  = [int(x) for x in os.getenv("ALLOWED_CHAT_IDS", "").split(",") if x.strip()]
POLL_INTERVAL     = int(os.getenv("POLL_INTERVAL_SEC", "8"))
ORDER_TYPE_CFG    = os.getenv("ORDER_TYPE", "FAK")

POLYMARKET_API = "https://clob.polymarket.com"
GAMMA_API      = "https://gamma-api.polymarket.com"

# Conversation states
(
    AWAIT_COPY_WALLET, AWAIT_COPY_PCT, AWAIT_COPY_MAX,
    AWAIT_COPY_MIN,   AWAIT_COPY_MIN_TRIGGER,
    AWAIT_WITHDRAW_ADDR, AWAIT_WITHDRAW_AMOUNT,
) = range(7)

# ═══════════════════════════════════════════════════════
#  LOGGING
# ═══════════════════════════════════════════════════════
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
    handlers=[logging.StreamHandler(), logging.FileHandler("arow.log")]
)
log = logging.getLogger("ArowTrade")

# ═══════════════════════════════════════════════════════
#  POLYMARKET CLIENT
# ═══════════════════════════════════════════════════════
class PolyClient:
    def __init__(self):
        self.clob: Optional[ClobClient] = None
        self._init()

    def _init(self):
        try:
            self.clob = ClobClient(
                host=POLYMARKET_API,
                key=PRIVATE_KEY,
                chain_id=POLYGON,
                funder=FUNDER_ADDRESS,
                signature_type=2,
            )
            self.clob.set_api_creds(self.clob.create_or_derive_api_creds())
            log.info("✅ Polymarket CLOB ready")
        except Exception as e:
            log.error(f"CLOB init: {e}")

    def get_usdc_balance(self) -> float:
        try:
            return float(self.clob.get_balance())
        except:
            return 0.0

    def balance_str(self) -> str:
        b = self.get_usdc_balance()
        return f"${b:.2f} USDC"

    async def fetch_trades(self, wallet: str, since_ts: int) -> list:
        """Get recent trades for a wallet via Gamma activity API."""
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    f"{GAMMA_API}/activity",
                    params={"user": wallet.lower(), "limit": 50, "after": since_ts},
                    timeout=aiohttp.ClientTimeout(total=12)
                ) as r:
                    if r.status == 200:
                        d = await r.json()
                        return d if isinstance(d, list) else d.get("data", [])
        except Exception as e:
            log.warning(f"Fetch trades ({wallet[:8]}…): {e}")
        return []

    async def best_price(self, token_id: str, side: str) -> Optional[float]:
        try:
            book = self.clob.get_order_book(token_id)
            if side == BUY  and book.asks: return float(book.asks[0].price)
            if side == SELL and book.bids: return float(book.bids[0].price)
        except:
            pass
        return None

    def place_order(self, token_id: str, side: str, shares: float,
                    price: float, order_type: str = "FAK") -> dict:
        try:
            ot = OrderType.FAK if order_type == "FAK" else OrderType.FOK
            signed = self.clob.create_order(
                OrderArgs(token_id=token_id, price=price, size=shares, side=side),
                PartialCreateOrderOptions(tick_size=0.01, neg_risk=False)
            )
            return self.clob.post_order(signed, ot)
        except Exception as e:
            return {"error": str(e)}

poly = PolyClient()

# ═══════════════════════════════════════════════════════
#  BOT STATE
# ═══════════════════════════════════════════════════════
# copy_jobs: dict of id -> config dict
# Each job:
#   id, label, wallet, pct (% of balance per trade),
#   max_usdc, min_usdc, min_trigger, order_type,
#   active, last_ts, trades_copied
copy_jobs: dict = {}

# Temp storage during multi-step conversations
pending: dict = {}

# Global log of all executed copy trades
trade_log: list = []

def auth(u: Update) -> bool:
    return not ALLOWED_CHAT_IDS or u.effective_chat.id in ALLOWED_CHAT_IDS

# ═══════════════════════════════════════════════════════
#  KEYBOARDS
# ═══════════════════════════════════════════════════════
def kb_home():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Copy Jobs",   callback_data="jobs"),
         InlineKeyboardButton("➕ Copy",        callback_data="new_copy")],
        [InlineKeyboardButton("👛 Wallet",      callback_data="wallet"),
         InlineKeyboardButton("📊 Positions",   callback_data="positions")],
        [InlineKeyboardButton("📜 Trade Log",   callback_data="tradelog"),
         InlineKeyboardButton("❓ Help",        callback_data="help")],
    ])

def kb_jobs(jobs: dict):
    rows = []
    for jid, j in jobs.items():
        label = j["label"]
        icon  = "🟢" if j["active"] else "🔴"
        rows.append([
            InlineKeyboardButton(f"{icon} {label}  ({j['pct']}%)", callback_data=f"job_{jid}")
        ])
    rows.append([
        InlineKeyboardButton("➕ Add Copy",  callback_data="new_copy"),
        InlineKeyboardButton("🔙 Home",      callback_data="home")
    ])
    return InlineKeyboardMarkup(rows)

def kb_job_detail(jid: str, active: bool):
    toggle = "⏸ Pause" if active else "▶️ Resume"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(toggle,       callback_data=f"toggle_{jid}"),
         InlineKeyboardButton("✏️ Edit",    callback_data=f"edit_{jid}")],
        [InlineKeyboardButton("🗑 Delete",   callback_data=f"del_{jid}"),
         InlineKeyboardButton("🔙 Jobs",    callback_data="jobs")],
    ])

def kb_wallet():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 Fund Wallet",  callback_data="fund"),
         InlineKeyboardButton("💸 Withdraw",     callback_data="withdraw")],
        [InlineKeyboardButton("🔙 Home",         callback_data="home")],
    ])

def kb_back_home():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Home", callback_data="home")]])

# ═══════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════
def jobs_summary() -> str:
    if not copy_jobs:
        return "No copy jobs yet. Press ➕ Copy to add one."
    lines = []
    for jid, j in copy_jobs.items():
        icon = "🟢" if j["active"] else "🔴"
        lines.append(
            f"{icon} *{j['label']}*\n"
            f"   Wallet: `{j['wallet'][:10]}…{j['wallet'][-4:]}`\n"
            f"   Size: {j['pct']}% of balance  |  Max: ${j['max_usdc']}  |  Min: ${j['min_usdc']}\n"
            f"   Trigger: ≥${j['min_trigger']}  |  Copied: {j['trades_copied']}"
        )
    return "\n\n".join(lines)

def home_text() -> str:
    bal = poly.balance_str()
    active = sum(1 for j in copy_jobs.values() if j["active"])
    return (
        f"🏹 *Arow Trade*\n\n"
        f"💼 Balance: {bal}\n"
        f"📋 Active copy jobs: {active}/{len(copy_jobs)}\n"
        f"📈 Total trades copied: {sum(j['trades_copied'] for j in copy_jobs.values())}"
    )

# ═══════════════════════════════════════════════════════
#  COMMANDS
# ═══════════════════════════════════════════════════════
async def cmd_start(u: Update, _):
    if not auth(u): return
    await u.message.reply_text(home_text(), parse_mode="Markdown", reply_markup=kb_home())

async def cmd_home(u: Update, _):
    if not auth(u): return
    await u.message.reply_text(home_text(), parse_mode="Markdown", reply_markup=kb_home())

async def cmd_wallet(u: Update, _):
    if not auth(u): return
    bal = poly.balance_str()
    await u.message.reply_text(
        f"👛 *Wallet*\n\n"
        f"💼 Balance: {bal}\n"
        f"📍 Polygon Address:\n`{FUNDER_ADDRESS}`\n\n"
        f"_Do not send funds directly to this address.\nUse Fund Wallet to bridge from Solana._",
        parse_mode="Markdown", reply_markup=kb_wallet()
    )

async def cmd_copy(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not auth(u): return
    await u.message.reply_text(
        "📋 *Copy Jobs*\n\n" + jobs_summary(),
        parse_mode="Markdown",
        reply_markup=kb_jobs(copy_jobs)
    )

async def cmd_help(u: Update, _):
    await u.message.reply_text(
        "🏹 *Arow Trade — Help*\n\n"
        "/start or /home — Dashboard\n"
        "/copy — Manage copy jobs\n"
        "/wallet — Wallet & balances\n"
        "/positions — Your open positions\n"
        "/log — Trade history\n\n"
        "➕ *Copy* button — Add a new wallet to copy\n"
        "Each copy job uses a *% of your balance* for each trade,\n"
        "so you can run multiple jobs simultaneously without overspending.\n\n"
        "💡 *Example:*\n"
        "Balance $100 · Job A = 20% · Job B = 15%\n"
        "→ Job A trades up to $20 per copy\n"
        "→ Job B trades up to $15 per copy",
        parse_mode="Markdown",
        reply_markup=kb_back_home()
    )

async def cmd_log(u: Update, _):
    if not auth(u): return
    items = trade_log[-15:]
    if not items:
        await u.message.reply_text("📜 No trades yet.", reply_markup=kb_back_home()); return
    lines = ["📜 *Trade Log (last 15)*\n"]
    for t in reversed(items):
        e = "🟢" if t["side"] == "BUY" else "🔴"
        lines.append(
            f"{e} *{t['side']}* ${t['usdc']:.2f} @ {t['price']:.4f}\n"
            f"   Job: {t['label']}\n"
            f"   {t['market'][:55]}\n"
            f"   {t['ts']}"
        )
    await u.message.reply_text("\n\n".join(lines), parse_mode="Markdown", reply_markup=kb_back_home())

# ═══════════════════════════════════════════════════════
#  ADD COPY JOB — Conversation Flow
# ═══════════════════════════════════════════════════════
async def start_new_copy(chat_id: int, bot):
    pending[chat_id] = {}
    await bot.send_message(
        chat_id,
        "➕ *Add Copy Job*\n\n"
        "Step 1/5 — Enter the Polygon wallet address you want to copy:\n"
        "_(e.g. 0xAbC…)_\n\n"
        "Send /cancel to abort.",
        parse_mode="Markdown"
    )
    return AWAIT_COPY_WALLET

async def recv_copy_wallet(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    addr = u.message.text.strip()
    if addr == "/cancel":
        await u.message.reply_text("❌ Cancelled.", reply_markup=kb_home()); return ConversationHandler.END
    if not (addr.startswith("0x") and len(addr) == 42):
        await u.message.reply_text("❌ Invalid address. Try again:"); return AWAIT_COPY_WALLET
    pending[u.effective_chat.id]["wallet"] = addr
    await u.message.reply_text(
        f"✅ Wallet: `{addr[:10]}…{addr[-4:]}`\n\n"
        f"Step 2/5 — What *% of your balance* should each copied trade use?\n\n"
        f"💼 Current balance: {poly.balance_str()}\n"
        f"Example: `20` = 20% of balance per trade\n"
        f"_(Tip: keep each job under 25% so multiple can run simultaneously)_",
        parse_mode="Markdown"
    ); return AWAIT_COPY_PCT

async def recv_copy_pct(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = u.effective_chat.id
    if u.message.text.strip() == "/cancel":
        await u.message.reply_text("❌ Cancelled.", reply_markup=kb_home()); return ConversationHandler.END
    try:
        pct = float(u.message.text.strip()); assert 1 <= pct <= 100
    except:
        await u.message.reply_text("❌ Enter a number between 1 and 100:"); return AWAIT_COPY_PCT
    pending[cid]["pct"] = pct
    bal = poly.get_usdc_balance()
    max_calc = round(bal * pct / 100, 2)
    await u.message.reply_text(
        f"✅ {pct}% per trade (≈ ${max_calc:.2f} at current balance)\n\n"
        f"Step 3/5 — *Max USDC cap* per single trade?\n"
        f"_(Safety ceiling — trade will never exceed this even if {pct}% is higher)_\n"
        f"Example: `50` — or type `none` for no cap",
        parse_mode="Markdown"
    ); return AWAIT_COPY_MAX

async def recv_copy_max(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = u.effective_chat.id
    txt = u.message.text.strip()
    if txt == "/cancel":
        await u.message.reply_text("❌ Cancelled.", reply_markup=kb_home()); return ConversationHandler.END
    if txt.lower() == "none":
        pending[cid]["max_usdc"] = 9999.0
    else:
        try:
            v = float(txt); assert v > 0
            pending[cid]["max_usdc"] = v
        except:
            await u.message.reply_text("❌ Enter a number or `none`:"); return AWAIT_COPY_MAX
    await u.message.reply_text(
        f"✅ Max: ${pending[cid]['max_usdc']:.0f} USDC\n\n"
        f"Step 4/5 — *Min USDC* per trade?\n"
        f"_(Trades smaller than this will be skipped)_\n"
        f"Example: `2`",
        parse_mode="Markdown"
    ); return AWAIT_COPY_MIN

async def recv_copy_min(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = u.effective_chat.id
    if u.message.text.strip() == "/cancel":
        await u.message.reply_text("❌ Cancelled.", reply_markup=kb_home()); return ConversationHandler.END
    try:
        v = float(u.message.text.strip()); assert v >= 0
        pending[cid]["min_usdc"] = v
    except:
        await u.message.reply_text("❌ Enter a number (e.g. 2):"); return AWAIT_COPY_MIN
    await u.message.reply_text(
        f"✅ Min: ${pending[cid]['min_usdc']:.2f} USDC\n\n"
        f"Step 5/5 — *Min trigger* — the original trader must spend at least how much USDC to trigger a copy?\n"
        f"_(Filters out small test trades from the wallet you're copying)_\n"
        f"Example: `10`",
        parse_mode="Markdown"
    ); return AWAIT_COPY_MIN_TRIGGER

async def recv_copy_trigger(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = u.effective_chat.id
    if u.message.text.strip() == "/cancel":
        await u.message.reply_text("❌ Cancelled.", reply_markup=kb_home()); return ConversationHandler.END
    try:
        v = float(u.message.text.strip()); assert v >= 0
    except:
        await u.message.reply_text("❌ Enter a number (e.g. 10):"); return AWAIT_COPY_MIN_TRIGGER

    p = pending.pop(cid, {})
    jid = str(uuid4())[:8]
    wallet = p["wallet"]
    job = {
        "id":            jid,
        "label":         f"Copy {wallet[:6]}…{wallet[-4:]}",
        "wallet":        wallet,
        "pct":           p["pct"],
        "max_usdc":      p["max_usdc"],
        "min_usdc":      p["min_usdc"],
        "min_trigger":   v,
        "order_type":    ORDER_TYPE_CFG,
        "active":        True,
        "last_ts":       int(time.time()),
        "trades_copied": 0,
        "chat_id":       cid,
    }
    copy_jobs[jid] = job

    await u.message.reply_text(
        f"🚀 *Copy Job Created!*\n\n"
        f"📌 Label: {job['label']}\n"
        f"👛 Copying: `{wallet}`\n"
        f"💸 Size: {job['pct']}% of balance per trade\n"
        f"📈 Max: ${job['max_usdc']:.0f} USDC\n"
        f"📉 Min: ${job['min_usdc']:.2f} USDC\n"
        f"🔔 Trigger if original ≥ ${job['min_trigger']:.2f}\n\n"
        f"✅ *Active — watching for trades!*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("📋 All Jobs", callback_data="jobs"),
            InlineKeyboardButton("🏠 Home",    callback_data="home"),
        ]])
    )
    return ConversationHandler.END

# ═══════════════════════════════════════════════════════
#  WITHDRAW — Conversation Flow
# ═══════════════════════════════════════════════════════
async def start_withdraw(chat_id: int, bot):
    pending[f"wd_{chat_id}"] = {}
    await bot.send_message(
        chat_id,
        "💸 *Withdraw*\n\n"
        "Enter your *Solana wallet address* to receive funds:\n"
        "_Send /cancel to abort_",
        parse_mode="Markdown"
    )
    return AWAIT_WITHDRAW_ADDR

async def recv_withdraw_addr(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = u.effective_chat.id
    addr = u.message.text.strip()
    if addr == "/cancel":
        await u.message.reply_text("❌ Cancelled.", reply_markup=kb_home()); return ConversationHandler.END
    if len(addr) < 32:
        await u.message.reply_text("❌ That doesn't look like a valid Solana address. Try again:"); return AWAIT_WITHDRAW_ADDR
    pending[f"wd_{cid}"]["addr"] = addr
    bal = poly.balance_str()
    await u.message.reply_text(
        f"✅ To: `{addr}`\n\n"
        f"💼 Available: {bal}\n\n"
        f"How much *USDC* to withdraw?\n"
        f"_(Enter amount, e.g. `25` — or `all`)_",
        parse_mode="Markdown"
    ); return AWAIT_WITHDRAW_AMOUNT

async def recv_withdraw_amount(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = u.effective_chat.id
    txt = u.message.text.strip()
    if txt == "/cancel":
        await u.message.reply_text("❌ Cancelled.", reply_markup=kb_home()); return ConversationHandler.END
    bal = poly.get_usdc_balance()
    if txt.lower() == "all":
        amount = bal
    else:
        try:
            amount = float(txt); assert 0 < amount <= bal
        except:
            await u.message.reply_text(f"❌ Enter a valid amount (max ${bal:.2f}):"); return AWAIT_WITHDRAW_AMOUNT

    wd = pending.pop(f"wd_{cid}", {})
    dest = wd.get("addr", "?")

    # NOTE: Actual withdrawal would call deBridge or Polymarket's withdrawal API.
    # This sends the request — in production connect to your bridge/withdrawal endpoint.
    log.info(f"WITHDRAW REQUEST: {amount} USDC → {dest}")

    await u.message.reply_text(
        f"📤 *Withdrawal Requested*\n\n"
        f"Amount: ${amount:.2f} USDC\n"
        f"To (Solana): `{dest}`\n\n"
        f"⏳ Processing via deBridge…\n"
        f"_Funds typically arrive within 5–15 minutes._\n\n"
        f"⚠️ _Note: Connect withdrawal API endpoint in `process_withdrawal()` in bot.py to complete this in production._",
        parse_mode="Markdown",
        reply_markup=kb_wallet()
    )
    return ConversationHandler.END

# ═══════════════════════════════════════════════════════
#  CALLBACK HANDLER (Buttons)
# ═══════════════════════════════════════════════════════
async def handle_cb(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = u.callback_query; await q.answer(); d = q.data; cid = q.message.chat.id

    if d == "home":
        await q.edit_message_text(home_text(), parse_mode="Markdown", reply_markup=kb_home())

    elif d == "jobs":
        await q.edit_message_text(
            "📋 *Copy Jobs*\n\n" + jobs_summary(),
            parse_mode="Markdown",
            reply_markup=kb_jobs(copy_jobs)
        )

    elif d == "new_copy":
        await q.edit_message_text("Loading…")
        state = await start_new_copy(cid, ctx.bot)
        # Store state in user_data for the conversation handler
        ctx.user_data["conv_state"] = state
        ctx.user_data["in_conv"]    = "copy"

    elif d == "wallet":
        bal = poly.balance_str()
        await q.edit_message_text(
            f"👛 *Wallet*\n\n"
            f"💼 Balance: {bal}\n"
            f"📍 Polygon Address:\n`{FUNDER_ADDRESS}`",
            parse_mode="Markdown", reply_markup=kb_wallet()
        )

    elif d == "fund":
        await q.edit_message_text(
            "💰 *Fund Your Wallet*\n\n"
            "Send *SOL or USDC* to this Solana address and it will bridge automatically to your Polygon wallet via deBridge:\n\n"
            f"`{SOL_DEPOSIT_ADDR}`\n\n"
            "📝 *Steps:*\n"
            "1. Copy the address above\n"
            "2. Send SOL or USDC from any Solana wallet\n"
            "3. Bridge processes in ~5 minutes\n"
            "4. USDC arrives in your Polymarket wallet\n\n"
            "⛽ _A small amount of POL is automatically converted for gas fees._",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("💸 Withdraw",  callback_data="withdraw"),
                InlineKeyboardButton("🔙 Back",      callback_data="wallet"),
            ]])
        )

    elif d == "withdraw":
        await q.edit_message_text("Starting withdrawal…")
        ctx.user_data["in_conv"] = "withdraw"
        await start_withdraw(cid, ctx.bot)

    elif d == "tradelog":
        items = trade_log[-10:]
        if not items:
            txt = "📜 No trades copied yet."
        else:
            lines = ["📜 *Recent Trades*\n"]
            for t in reversed(items):
                e = "🟢" if t["side"] == "BUY" else "🔴"
                lines.append(f"{e} {t['side']} ${t['usdc']:.2f}  {t['ts'][-8:]}\n   {t['market'][:40]}")
            txt = "\n\n".join(lines)
        await q.edit_message_text(txt, parse_mode="Markdown", reply_markup=kb_back_home())

    elif d == "help":
        await q.edit_message_text(
            "🏹 *Arow Trade — Commands*\n\n"
            "/home — Dashboard\n"
            "/copy — Manage copy jobs\n"
            "/wallet — Wallet & balances\n"
            "/log — Trade history\n\n"
            "*Balance-based sizing:*\n"
            "Each job uses X% of your current balance.\n"
            "Multiple jobs run in parallel — just keep total % under 100%!\n\n"
            "*Filters:*\n"
            "• Max USDC — hard cap per trade\n"
            "• Min USDC — skip tiny trades\n"
            "• Min trigger — min the target must spend to trigger copy",
            parse_mode="Markdown",
            reply_markup=kb_back_home()
        )

    elif d == "positions":
        await q.edit_message_text(
            "📊 *Your Positions*\n\n"
            "_Position tracking requires connecting to Polymarket's portfolio API.\n"
            "Use polymarket.com/portfolio or check your wallet on polygonscan._\n\n"
            f"Wallet: `{FUNDER_ADDRESS}`",
            parse_mode="Markdown",
            reply_markup=kb_back_home()
        )

    # ── Job detail ──────────────────────────────────────
    elif d.startswith("job_"):
        jid = d[4:]
        j   = copy_jobs.get(jid)
        if not j:
            await q.edit_message_text("Job not found.", reply_markup=kb_jobs(copy_jobs)); return
        icon = "🟢" if j["active"] else "🔴"
        bal  = poly.get_usdc_balance()
        calc = round(bal * j["pct"] / 100, 2)
        await q.edit_message_text(
            f"{icon} *{j['label']}*\n\n"
            f"👛 Wallet: `{j['wallet']}`\n"
            f"💸 Size: {j['pct']}% of balance (≈ ${calc:.2f} now)\n"
            f"📈 Max cap: ${j['max_usdc']:.0f}\n"
            f"📉 Min trade: ${j['min_usdc']:.2f}\n"
            f"🔔 Trigger ≥ ${j['min_trigger']:.2f}\n"
            f"⚡ Order type: {j['order_type']}\n"
            f"📋 Trades copied: {j['trades_copied']}",
            parse_mode="Markdown",
            reply_markup=kb_job_detail(jid, j["active"])
        )

    elif d.startswith("toggle_"):
        jid = d[7:]
        if jid in copy_jobs:
            copy_jobs[jid]["active"] = not copy_jobs[jid]["active"]
            st = "▶️ Resumed" if copy_jobs[jid]["active"] else "⏸ Paused"
            await q.edit_message_text(
                f"{st}: *{copy_jobs[jid]['label']}*",
                parse_mode="Markdown",
                reply_markup=kb_job_detail(jid, copy_jobs[jid]["active"])
            )

    elif d.startswith("del_"):
        jid = d[4:]
        if jid in copy_jobs:
            label = copy_jobs[jid]["label"]
            del copy_jobs[jid]
            await q.edit_message_text(
                f"🗑 Deleted *{label}*",
                parse_mode="Markdown",
                reply_markup=kb_jobs(copy_jobs)
            )

    elif d.startswith("edit_"):
        jid = d[5:]
        await q.edit_message_text(
            f"✏️ To edit job settings, delete and re-add the copy job.\n\n"
            f"(Full in-place editing coming soon)",
            reply_markup=kb_job_detail(jid, copy_jobs.get(jid, {}).get("active", False))
        )

# ═══════════════════════════════════════════════════════
#  MESSAGE HANDLER (conversation routing)
# ═══════════════════════════════════════════════════════
# We handle multi-step input via stored state in user_data
COPY_STATES  = [AWAIT_COPY_WALLET, AWAIT_COPY_PCT, AWAIT_COPY_MAX, AWAIT_COPY_MIN, AWAIT_COPY_MIN_TRIGGER]
COPY_FUNCS   = [recv_copy_wallet, recv_copy_pct, recv_copy_max, recv_copy_min, recv_copy_trigger]
WITHDR_STATES = [AWAIT_WITHDRAW_ADDR, AWAIT_WITHDRAW_AMOUNT]
WITHDR_FUNCS  = [recv_withdraw_addr, recv_withdraw_amount]

async def handle_text(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not auth(u): return
    cid  = u.effective_chat.id
    conv = ctx.user_data.get("in_conv")
    st   = ctx.user_data.get("conv_state")

    if conv == "copy" and st in COPY_STATES:
        idx     = COPY_STATES.index(st)
        new_st  = await COPY_FUNCS[idx](u, ctx)
        ctx.user_data["conv_state"] = new_st
        if new_st == ConversationHandler.END:
            ctx.user_data.pop("in_conv", None)
            ctx.user_data.pop("conv_state", None)

    elif conv == "withdraw" and st in WITHDR_STATES:
        idx    = WITHDR_STATES.index(st)
        new_st = await WITHDR_FUNCS[idx](u, ctx)
        ctx.user_data["conv_state"] = new_st
        if new_st == ConversationHandler.END:
            ctx.user_data.pop("in_conv", None)
            ctx.user_data.pop("conv_state", None)
    else:
        # No active conversation — show home
        await u.message.reply_text(home_text(), parse_mode="Markdown", reply_markup=kb_home())

# ═══════════════════════════════════════════════════════
#  COPY ENGINE — Background Loop
# ═══════════════════════════════════════════════════════
async def copy_engine(app: Application):
    log.info("🔄 Arow Trade copy engine started")
    seen: set = set()

    while True:
        await asyncio.sleep(POLL_INTERVAL)
        for jid, job in list(copy_jobs.items()):
            if not job["active"]:
                continue
            try:
                trades = await poly.fetch_trades(job["wallet"], job["last_ts"])
                for tr in trades:
                    tx = tr.get("transactionHash") or tr.get("id") or str(id(tr))
                    key = f"{jid}_{tx}"
                    if key in seen:
                        continue
                    seen.add(key)

                    side      = (tr.get("side") or "").upper()
                    token_id  = tr.get("asset_id") or tr.get("tokenId", "")
                    size_raw  = float(tr.get("size",  0))
                    price_raw = float(tr.get("price", 0))
                    outcome   = tr.get("outcome", "")
                    question  = tr.get("title") or tr.get("question", "") or ""
                    cond_id   = tr.get("conditionId", "")

                    if not token_id or side not in ("BUY", "SELL") or size_raw <= 0:
                        continue

                    orig_usdc = size_raw * price_raw
                    if orig_usdc < job["min_trigger"]:
                        log.info(f"[{job['label']}] Skipped (trigger ${orig_usdc:.2f} < ${job['min_trigger']})")
                        continue

                    # Balance-based sizing
                    balance  = poly.get_usdc_balance()
                    our_usdc = balance * (job["pct"] / 100)
                    our_usdc = min(our_usdc, job["max_usdc"])
                    if our_usdc < job["min_usdc"]:
                        log.info(f"[{job['label']}] Skipped (${our_usdc:.2f} < min ${job['min_usdc']})")
                        continue

                    # Live price
                    price  = await poly.best_price(token_id, side) or price_raw
                    if price <= 0: continue
                    shares = round(our_usdc / price, 2)
                    if shares <= 0: continue

                    result = poly.place_order(token_id, side, shares, price, job["order_type"])
                    ok     = "error" not in result
                    ts     = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

                    # Log
                    entry = dict(
                        side=side, usdc=our_usdc, shares=shares, price=price,
                        market=question or cond_id, outcome=outcome,
                        label=job["label"], ts=ts
                    )
                    trade_log.append(entry)
                    if len(trade_log) > 1000: trade_log[:] = trade_log[-1000:]
                    if ok: copy_jobs[jid]["trades_copied"] += 1

                    # Notify
                    e    = "🟢" if side == "BUY" else "🔴"
                    stat = "✅ Filled" if ok else f"❌ {result.get('error', 'Failed')}"
                    msg  = (
                        f"{e} *Arow Trade — Copy Executed*\n\n"
                        f"📌 {(question[:60] or cond_id) or 'Unknown market'}\n"
                        f"🎯 Outcome: {outcome}\n"
                        f"📊 {side} {shares} shares @ {price:.4f}\n"
                        f"💰 ${our_usdc:.2f} USDC  ({job['pct']}% of ${balance:.2f})\n"
                        f"⚡ {job['order_type']}  |  Job: {job['label']}\n"
                        f"🕐 {ts}\n"
                        f"{stat}"
                    )
                    try:
                        await app.bot.send_message(job["chat_id"], msg, parse_mode="Markdown")
                    except Exception as ex:
                        log.warning(f"Notify error: {ex}")

                copy_jobs[jid]["last_ts"] = int(time.time()) - 10

            except Exception as ex:
                log.error(f"Engine error [{job['label']}]: {ex}")

async def on_startup(app: Application):
    asyncio.create_task(copy_engine(app))

# ═══════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════
def main():
    if not TELEGRAM_TOKEN: raise SystemExit("❌ Set TELEGRAM_TOKEN in .env")
    if not PRIVATE_KEY:    raise SystemExit("❌ Set POLYMARKET_PRIVATE_KEY in .env")

    app = Application.builder().token(TELEGRAM_TOKEN).post_init(on_startup).build()

    # Commands
    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("home",      cmd_home))
    app.add_handler(CommandHandler("wallet",    cmd_wallet))
    app.add_handler(CommandHandler("copy",      cmd_copy))
    app.add_handler(CommandHandler("copytrade", cmd_copy))
    app.add_handler(CommandHandler("help",      cmd_help))
    app.add_handler(CommandHandler("log",       cmd_log))

    # Buttons
    app.add_handler(CallbackQueryHandler(handle_cb))

    # Free-text (conversation routing)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    log.info("🏹 Arow Trade starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()