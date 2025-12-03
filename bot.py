# bot.py — Full working Discord slots + manual payout workflow
# Requires: Python 3.10+, pip install discord.py aiosqlite
# Set environment variables or edit DEFAULT_* values below.

import os
import discord
from discord.ext import commands
import random
import aiosqlite
import asyncio
from typing import Optional

# -------------------------
# ENV / CONFIG
# -------------------------
# You can either set these via environment variables or edit here:
DEFAULT_TOKEN = None                    # or put token string here (not recommended)
DEFAULT_ANNOUNCE_CHANNEL = None         # channel ID to announce wins publicly (optional)
DEFAULT_ADMIN_IDS = ""                  # comma-separated admin IDs, e.g. "123,456"

# Economy
PENCE_TO_COINS = 10     # 1 pence = 10 coins
COINS_PER_SPIN = 10     # 10 coins = 1 spin
SPIN_COST = COINS_PER_SPIN

DB_FILE = "casino.db"

TOKEN = os.getenv("DISCORD_TOKEN") or DEFAULT_TOKEN
ANNOUNCE_CHANNEL_ID = int(os.getenv("ANNOUNCE_CHANNEL_ID")) if os.getenv("ANNOUNCE_CHANNEL_ID") else DEFAULT_ANNOUNCE_CHANNEL
ADMIN_IDS = set()
admins_env = os.getenv("ADMIN_IDS") or DEFAULT_ADMIN_IDS
if admins_env:
    for a in admins_env.split(","):
        a = a.strip()
        if a.isdigit():
            ADMIN_IDS.add(int(a))

# -------------------------
# Slot / Prize configuration
# -------------------------
SYMBOLS = ["7", "BAR", "🍒", "🍋"]  # adjust or weight if desired

# Prize table (coins)
# Note: coins -> pence = coins / PENCE_TO_COINS
PRIZES = {
    ("7", "7", "7"): 1000,   # e.g. 1000 coins -> 100 pence = £1.00 (since 1p=10coins)
    ("BAR", "BAR", "BAR"): 200,
    ("🍒", "🍒", "🍒"): 100,
    ("🍋", "🍋", "🍋"): 50
}

# -------------------------
# Bot setup
# -------------------------
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# -------------------------
# Database initialization
# -------------------------
async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                discord_id INTEGER PRIMARY KEY,
                balance INTEGER DEFAULT 0,
                kyc INTEGER DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_id INTEGER,
                type TEXT,    -- deposit, bet, win, payout_request, payout_approved, payout_sent, admin_credit, refund
                amount INTEGER, -- coins (+/-)
                status TEXT,
                metadata TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS spins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_id INTEGER,
                s1 TEXT, s2 TEXT, s3 TEXT,
                won INTEGER,
                ledger_id INTEGER,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS cashouts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_id INTEGER,
                paypal_email TEXT,
                amount_coins INTEGER,
                status TEXT DEFAULT 'queued', -- queued, approved, paid, rejected
                ledger_request_id INTEGER,   -- ledger id for the reservation debit
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.commit()

# -------------------------
# DB helper functions
# -------------------------
async def ensure_user(discord_id: int):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("INSERT OR IGNORE INTO users (discord_id,balance) VALUES (?, ?)", (discord_id, 0))
        await db.commit()

async def get_balance(discord_id: int) -> int:
    await ensure_user(discord_id)
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute("SELECT balance FROM users WHERE discord_id = ?", (discord_id,))
        row = await cur.fetchone()
        return row[0] if row else 0

async def change_balance_with_ledger(discord_id: int, amount_coins: int, ltype: str, metadata: Optional[str] = None):
    """
    Adds ledger row and updates balance (coins). amount_coins can be negative.
    Returns ledger id.
    """
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute("INSERT INTO ledger (discord_id,type,amount,status,metadata) VALUES (?,?,?,?,?)",
                               (discord_id, ltype, amount_coins, "completed", metadata or ""))
        await db.execute("UPDATE users SET balance = balance + ? WHERE discord_id = ?", (amount_coins, discord_id))
        await db.commit()
        lid = (await db.execute("SELECT last_insert_rowid()")).fetchone
        # sqlite's aiosqlite cursor handling: fetch last_insert_rowid by query
        cur2 = await db.execute("SELECT last_insert_rowid()")
        row = await cur2.fetchone()
        return row[0] if row else None

async def add_ledger_only(discord_id: int, amount_coins: int, ltype: str, metadata: Optional[str] = None):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("INSERT INTO ledger (discord_id,type,amount,status,metadata) VALUES (?,?,?,?,?)",
                         (discord_id, ltype, amount_coins, "completed", metadata or ""))
        await db.commit()
        cur = await db.execute("SELECT last_insert_rowid()")
        row = await cur.fetchone()
        return row[0] if row else None

# -------------------------
# Utility
# -------------------------
def coins_to_pounds(coins: int) -> str:
    # coins -> pence -> pounds
    pence = coins // PENCE_TO_COINS
    pounds = pence / 100
    return f"£{pounds:.2f}"

def is_admin_user(user: discord.User) -> bool:
    return user.guild_permissions.administrator or (user.id in ADMIN_IDS)

# -------------------------
# User commands
# -------------------------
@bot.command(name="prizes")
async def cmd_prizes(ctx):
    lines = [
        "🎰 **PRIZE TABLE**",
        f"Economy: 1p = {PENCE_TO_COINS} coins | {COINS_PER_SPIN} coins = 1 spin",
        f"Spin cost: **{SPIN_COST} coins**",
        ""
    ]
    for combo, reward in PRIZES.items():
        lines.append(f"{combo[0]} {combo[1]} {combo[2]} → **{reward} coins** ({coins_to_pounds(reward)})")
    await ctx.send("\n".join(lines))

@bot.command(name="balance")
async def cmd_balance(ctx):
    bal = await get_balance(ctx.author.id)
    await ctx.send(f"{ctx.author.mention} Balance: **{bal} coins** ({coins_to_pounds(bal)})")

@bot.command(name="spin")
async def cmd_spin(ctx):
    await ensure_user(ctx.author.id)
    bal = await get_balance(ctx.author.id)
    if bal < SPIN_COST:
        return await ctx.send(f"❌ Not enough coins to spin. You need {SPIN_COST} coins. Use `!topup` and ask an admin to credit you.")
    # Deduct cost and record bet
    # create ledger for bet
    ledger_id_bet = await add_ledger_only(ctx.author.id, -SPIN_COST, "bet", "spin_cost")
    # update balance
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("UPDATE users SET balance = balance - ? WHERE discord_id = ?", (SPIN_COST, ctx.author.id))
        await db.commit()

    # spin reels
    s1 = random.choice(SYMBOLS)
    s2 = random.choice(SYMBOLS)
    s3 = random.choice(SYMBOLS)
    combo = (s1, s2, s3)
    won = PRIZES.get(combo, 0)

    win_ledger_id = None
    if won > 0:
        # credit winnings and ledger
        win_ledger_id = await change_balance_with_ledger(ctx.author.id, won, "win", f"symbols:{s1},{s2},{s3}")
    else:
        # create a spin_result ledger row for audit (0 win)
        win_ledger_id = await add_ledger_only(ctx.author.id, 0, "spin_result", f"symbols:{s1},{s2},{s3}")

    # log the spin
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("INSERT INTO spins (discord_id,s1,s2,s3,won,ledger_id) VALUES (?,?,?,?,?,?)",
                         (ctx.author.id, s1, s2, s3, won, win_ledger_id))
        await db.commit()

    new_bal = await get_balance(ctx.author.id)
    lines = [f"🎰 **SPIN RESULT** — {ctx.author.mention}", f"[{s1}] [{s2}] [{s3}]"]
    if won > 0:
        lines.append(f"🎉 You won **{won} coins** ({coins_to_pounds(won)}) — ledger id: {win_ledger_id}")
    else:
        lines.append("😢 No win this spin.")
    lines.append(f"💰 New balance: **{new_bal} coins** ({coins_to_pounds(new_bal)})")

    await ctx.send("\n".join(lines))

    # announce publicly (if configured)
    if ANNOUNCE_CHANNEL_ID:
        try:
            ch = bot.get_channel(ANNOUNCE_CHANNEL_ID) or await bot.fetch_channel(ANNOUNCE_CHANNEL_ID)
            if ch:
                if won > 0:
                    await ch.send(f"🎉 WIN: <@{ctx.author.id}> won **{won} coins** ({coins_to_pounds(won)}) — symbols: {s1} {s2} {s3} — ledger {win_ledger_id}")
        except Exception:
            pass

@bot.command(name="topup")
async def cmd_topup(ctx, pounds: float):
    """
    User requests a top-up. This bot does not process payments automatically.
    Admin must credit manually using !credit (pence -> coins) or !addcoins.
    Usage: !topup 1.00  (requests £1.00 -> admin credits later)
    """
    if pounds <= 0:
        return await ctx.send("Enter a positive amount, e.g. `!topup 1.00` for £1.00")
    pence = round(pounds * 100)
    coins = pence * PENCE_TO_COINS
    await ctx.send(
        f"{ctx.author.mention} Top-up requested: **£{pounds:.2f}** → **{coins} coins**.\n"
        "Please pay the admin's PayPal. After payment, an admin should run:\n"
        f"`!credit @{ctx.author.display_name} {pence}`  OR  `!addcoins @{ctx.author.display_name} {coins}`"
    )

@bot.command(name="cashout")
async def cmd_cashout(ctx, paypal_email: str, amount_coins: Optional[int] = None):
    """
    Request a cashout to a PayPal email.
    Usage: !cashout user@example.com            -> cash out FULL balance
           !cashout user@example.com 300        -> cash out 300 coins
    """
    await ensure_user(ctx.author.id)
    bal = await get_balance(ctx.author.id)
    if bal <= 0:
        return await ctx.send("❌ You have no coins to cash out.")
    if amount_coins is None:
        amount_coins = bal
    if amount_coins <= 0:
        return await ctx.send("Enter a positive number of coins to cash out.")
    if amount_coins > bal:
        return await ctx.send(f"❌ You only have {bal} coins.")
    # Reserve coins: create ledger payout_request and reduce balance
    ledger_request_id = await add_ledger_only(ctx.author.id, -amount_coins, "payout_request", f"paypal:{paypal_email}")
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("UPDATE users SET balance = balance - ? WHERE discord_id = ?", (amount_coins, ctx.author.id))
        cur = await db.execute("INSERT INTO cashouts (discord_id,paypal_email,amount_coins,status,ledger_request_id) VALUES (?,?,?,?,?)",
                               (ctx.author.id, paypal_email, amount_coins, "queued", ledger_request_id))
        await db.commit()
        # get created id
        row = await cur.fetchone()  # this is None for aiosqlite after insert; fetch last_insert_rowid
        cur2 = await db.execute("SELECT last_insert_rowid()")
        r2 = await cur2.fetchone()
        cashout_id = r2[0] if r2 else None

    await ctx.send(
        f"💳 Cashout requested: **{amount_coins} coins** ({coins_to_pounds(amount_coins)}) to `{paypal_email}`.\n"
        f"Request ID: {cashout_id}. An admin will review and pay you manually. Use `!status` to check status."
    )

@bot.command(name="status")
async def cmd_status(ctx):
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute("SELECT id,paypal_email,amount_coins,status,created_at FROM cashouts WHERE discord_id = ? ORDER BY created_at DESC LIMIT 10", (ctx.author.id,))
        rows = await cur.fetchall()
    if not rows:
        return await ctx.send("You have no cashout requests.")
    lines = ["Your recent cashout requests:"]
    for r in rows:
        lines.append(f"ID {r[0]} — {r[2]} coins ({coins_to_pounds(r[2])}) → {r[1]} — status: {r[3]} (requested: {r[4]})")
    await ctx.send("\n".join(lines))

# -------------------------
# Admin commands
# -------------------------
def admin_check():
    async def predicate(ctx):
        if is_admin_user(ctx.author):
            return True
        raise commands.MissingPermissions(["administrator"])
    return commands.check(predicate)

@bot.command(name="list_requests")
@admin_check()
async def cmd_list_requests(ctx):
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute("SELECT id,discord_id,paypal_email,amount_coins,status,created_at FROM cashouts WHERE status = 'queued' ORDER BY created_at ASC")
        rows = await cur.fetchall()
    if not rows:
        return await ctx.send("No queued cashout requests.")
    lines = ["Queued cashout requests (ID — user — email — coins — created):"]
    for r in rows:
        lines.append(f"{r[0]} — <@{r[1]}> — {r[2]} — {r[3]} coins — {r[5]}")
    await ctx.send("\n".join(lines))

@bot.command(name="approve")
@admin_check()
async def cmd_approve(ctx, request_id: int):
    """
    Admin approves a queued cashout after sending the PayPal payment manually.
    Mark approved and create ledger entry.
    Then admin should run !markpaid <id> when the payment clears.
    """
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute("SELECT id,discord_id,paypal_email,amount_coins,status FROM cashouts WHERE id = ?", (request_id,))
        row = await cur.fetchone()
        if not row:
            return await ctx.send("Request not found.")
        if row[4] != "queued":
            return await ctx.send(f"Request not queued (status {row[4]}).")
        await db.execute("UPDATE cashouts SET status = 'approved' WHERE id = ?", (request_id,))
        await db.execute("INSERT INTO ledger (discord_id,type,amount,status,metadata) VALUES (?,?,?,?,?)",
                         (row[1], "payout_approved", -row[3], "completed", f"admin:{ctx.author.id};paypal:{row[2]};request:{request_id}"))
        await db.commit()
    await ctx.send(f"✅ Request {request_id} approved. After you have sent the PayPal payment manually, run `!markpaid {request_id}` to finalize.")

@bot.command(name="markpaid")
@admin_check()
async def cmd_markpaid(ctx, request_id: int):
    """
    Mark a previously approved cashout as paid (finalize audit trail).
    """
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute("SELECT id,discord_id,amount_coins,status FROM cashouts WHERE id = ?", (request_id,))
        row = await cur.fetchone()
        if not row:
            return await ctx.send("Request not found.")
        if row[3] == "paid":
            return await ctx.send("Request already marked as paid.")
        # Allow markpaid from approved or queued (but ideally approved)
        await db.execute("UPDATE cashouts SET status = 'paid' WHERE id = ?", (request_id,))
        await db.execute("INSERT INTO ledger (discord_id,type,amount,status,metadata) VALUES (?,?,?,?,?)",
                         (row[1], "payout_sent", -row[2], "completed", f"admin:{ctx.author.id};request:{request_id}"))
        await db.commit()
    await ctx.send(f"✅ Request {request_id} marked as PAID. Please notify the user.")

@bot.command(name="reject")
@admin_check()
async def cmd_reject(ctx, request_id: int, *, reason: str = "rejected by admin"):
    """
    Reject a cashout request and refund coins back to user.
    """
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute("SELECT id,discord_id,amount_coins,status FROM cashouts WHERE id = ?", (request_id,))
        row = await cur.fetchone()
        if not row:
            return await ctx.send("Request not found.")
        if row[3] != "queued":
            return await ctx.send(f"Cannot reject request with status {row[3]}.")
        # refund coins
        await db.execute("UPDATE users SET balance = balance + ? WHERE discord_id = ?", (row[2], row[1]))
        await db.execute("INSERT INTO ledger (discord_id,type,amount,status,metadata) VALUES (?,?,?,?,?)",
                         (row[1], "payout_rejected_refund", row[2], "completed", f"request:{request_id};reason:{reason}"))
        await db.execute("UPDATE cashouts SET status = 'rejected' WHERE id = ?", (request_id,))
        await db.commit()
    await ctx.send(f"❌ Request {request_id} rejected. {row[2]} coins refunded to <@{row[1]}>. Reason: {reason}")

@bot.command(name="credit")
@admin_check()
async def cmd_credit(ctx, member: discord.Member, pence: int):
    """
    Admin credits a user by pence. Conversion uses PENCE_TO_COINS: 1p -> PENCE_TO_COINS coins.
    Example: !credit @user 100  -> credits £1.00 -> 100 * PENCE_TO_COINS coins
    """
    if pence <= 0:
        return await ctx.send("Enter a positive pence amount (integer). Example: `!credit @user 100` for £1.00")
    coins = pence * PENCE_TO_COINS
    ledger_id = await change_balance_with_ledger(member.id, coins, "admin_credit", f"credited_by:{ctx.author.id};pence:{pence}")
    await ctx.send(f"✅ Credited {coins} coins to {member.mention} (ledger id {ledger_id}).")

@bot.command(name="addcoins")
@admin_check()
async def cmd_addcoins(ctx, member: discord.Member, coins: int):
    """
    Admin adds coins directly (no pence conversion).
    """
    if coins == 0:
        return await ctx.send("Specify a non-zero coin amount.")
    ledger_id = await change_balance_with_ledger(member.id, coins, "admin_credit", f"credited_by:{ctx.author.id};manual_coins:{coins}")
    await ctx.send(f"✅ Added {coins} coins to {member.mention} (ledger id {ledger_id}).")

@bot.command(name="ledger")
@admin_check()
async def cmd_ledger(ctx, limit: int = 20):
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute("SELECT id,discord_id,type,amount,status,metadata,created_at FROM ledger ORDER BY created_at DESC LIMIT ?", (limit,))
        rows = await cur.fetchall()
    if not rows:
        return await ctx.send("Ledger is empty.")
    lines = ["Recent ledger rows (id — user — type — amount — status — metadata — time):"]
    for r in rows:
        lines.append(f"{r[0]} — <@{r[1]}> — {r[2]} — {r[3]} coins — {r[4]} — {r[5]} — {r[6]}")
    # split into multiple messages if too long
    chunk_size = 1800
    message = "\n".join(lines)
    for i in range(0, len(message), chunk_size):
        await ctx.send(message[i:i+chunk_size])

# -------------------------
# Utility commands
# -------------------------
@bot.command(name="lastspins")
async def cmd_lastspins(ctx, member: Optional[discord.Member] = None, limit: int = 5):
    target = member or ctx.author
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute("SELECT s1,s2,s3,won,created_at FROM spins WHERE discord_id = ? ORDER BY created_at DESC LIMIT ?", (target.id, limit))
        rows = await cur.fetchall()
    if not rows:
        return await ctx.send("No spins found.")
    lines = [f"Last {len(rows)} spins for {target.display_name}:"]
    for r in rows:
        lines.append(f"[{r[0]}] [{r[1]}] [{r[2]}] → {r[3]} coins ({r[4]})")
    await ctx.send("\n".join(lines))

# -------------------------
# Bot startup and checks
# -------------------------
@bot.event
async def on_ready():
    if not TOKEN:
        print("ERROR: No Discord token provided. Set DISCORD_TOKEN env var or edit DEFAULT_TOKEN.")
        await bot.close()
        return
    await init_db()
    print(f"Bot ready: {bot.user} (ID {bot.user.id})")
    if ANNOUNCE_CHANNEL_ID:
        print(f"Announce channel id set to {ANNOUNCE_CHANNEL_ID}")
    if ADMIN_IDS:
        print(f"Admin IDs loaded: {ADMIN_IDS}")

# Helpful friendly error for admin permission missing
@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ You do not have permission to use that command.")
    elif isinstance(error, commands.BadArgument):
        await ctx.send("❌ Bad argument. Check usage.")
    else:
        # print for debugging but don't spam users
        print("Command error:", error)
        await ctx.send("❌ An error occurred while processing the command.")

# -------------------------
# Run
# -------------------------
if __name__ == "__main__":
    # Optionally load env defaults if not provided above
    if not TOKEN:
        TOKEN = os.getenv("DISCORD_TOKEN")
    try:
        bot.run(TOKEN)
    except Exception as e:

        print("Failed to start bot:", e)
