import discord
from discord.ext import commands
import asyncio
import os
from dotenv import load_dotenv
from flask import Flask, request, jsonify
import threading
import hmac
import hashlib
from datetime import datetime

load_dotenv()

# ── Config ──────────────────────────────────────────────────────────────────
DISCORD_TOKEN        = os.getenv("DISCORD_TOKEN")
WHOP_WEBHOOK_SECRET  = os.getenv("WHOP_WEBHOOK_SECRET")
GUILD_ID             = int(os.getenv("GUILD_ID"))
TICKET_CATEGORY_ID   = int(os.getenv("TICKET_CATEGORY_ID"))
PREMIUM_ROLE_ID      = int(os.getenv("PREMIUM_ROLE_ID"))
PAST_DUE_ROLE_ID     = int(os.getenv("PAST_DUE_ROLE_ID"))
STAFF_ROLE_NAME      = os.getenv("STAFF_ROLE_NAME", "Owner")
CHECKOUT_URL         = "https://whop.com/joined/scaleresell/products/scaleresell/"
EMBED_COLOR          = 0xd7182a
PORT                 = int(os.getenv("PORT", 8080))
# ────────────────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)
app = Flask(__name__)


# ── Helpers ──────────────────────────────────────────────────────────────────
def verify_whop_signature(payload: bytes, sig_header: str) -> bool:
    if not WHOP_WEBHOOK_SECRET or not sig_header:
        return True  # skip verification if secret not set (dev mode)
    expected = hmac.new(
        WHOP_WEBHOOK_SECRET.encode(), payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", sig_header)


def format_date(ts) -> str:
    if not ts:
        return "Unknown"
    try:
        if isinstance(ts, (int, float)):
            return datetime.utcfromtimestamp(ts).strftime("%B %d, %Y")
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).strftime("%B %d, %Y")
    except Exception:
        return str(ts)


async def handle_membership_event(event_type: str, data: dict):
    """Core logic — runs in the bot's event loop."""
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        print("Guild not found")
        return

    # ── Pull user info from payload ──────────────────────────────────────────
    user_data      = data.get("user") or {}
    discord_id_str = user_data.get("discord_id") or user_data.get("id") or ""
    username       = user_data.get("username") or user_data.get("name") or "Unknown"
    joined_at_raw  = data.get("joined_at") or data.get("created_at")
    event_date_raw = data.get("canceled_at") or data.get("updated_at")
    manage_url     = data.get("manage_url") or CHECKOUT_URL

    joined_str     = format_date(joined_at_raw)
    event_date_str = format_date(event_date_raw)

    is_past_due    = event_type in ("invoice_past_due", "membership_deactivated_past_due")
    event_label    = "Past Due Date" if is_past_due else "Cancellation Date"
    ticket_title   = "Payment Failure" if is_past_due else "Cancellation"
    action_label   = "Update Payment" if is_past_due else "Resubscribe"
    action_url     = manage_url if is_past_due else CHECKOUT_URL
    description    = (
        f"We've noticed your subscription payment has failed. "
        f"Please update your payment method to restore access."
        if is_past_due else
        f"Your Scale Resell membership has been cancelled. "
        f"We'd love to have you back — click below to resubscribe."
    )

    # ── Find Discord member ──────────────────────────────────────────────────
    member = None
    if discord_id_str:
        try:
            member = guild.get_member(int(discord_id_str))
            if not member:
                member = await guild.fetch_member(int(discord_id_str))
        except Exception as e:
            print(f"Could not fetch member {discord_id_str}: {e}")

    # ── Role changes ─────────────────────────────────────────────────────────
    premium_role  = guild.get_role(PREMIUM_ROLE_ID)
    past_due_role = guild.get_role(PAST_DUE_ROLE_ID)

    if member:
        try:
            if premium_role and premium_role in member.roles:
                await member.remove_roles(premium_role, reason=f"Whop: {event_type}")
            if past_due_role and past_due_role not in member.roles:
                await member.add_roles(past_due_role, reason=f"Whop: {event_type}")
        except discord.Forbidden:
            print(f"Missing permissions to modify roles for {member}")
        except Exception as e:
            print(f"Role error: {e}")

    # ── Find ticket category ─────────────────────────────────────────────────
    category = guild.get_channel(TICKET_CATEGORY_ID)
    if not category or not isinstance(category, discord.CategoryChannel):
        print("Ticket category not found")
        return

    # ── Create thread inside #cancellation channel ───────────────────────────
    ticket_channel = None
    for ch in category.channels:
        if "cancellation" in ch.name.lower() or "ticket" in ch.name.lower():
            ticket_channel = ch
            break
    if not ticket_channel:
        # fallback: first text channel in category
        for ch in category.channels:
            if isinstance(ch, discord.TextChannel):
                ticket_channel = ch
                break
    if not ticket_channel:
        print("No suitable channel found in ticket category")
        return

    # ── Build embed ──────────────────────────────────────────────────────────
    embed = discord.Embed(
        title=ticket_title,
        description=description,
        color=EMBED_COLOR,
        timestamp=datetime.utcnow()
    )
    embed.add_field(name="Member",        value=username,       inline=True)
    embed.add_field(name="Join Date",     value=joined_str,     inline=True)
    embed.add_field(name=event_label,     value=event_date_str, inline=True)
    embed.add_field(name=action_label,    value=f"[Click Here]({action_url})", inline=False)
    embed.set_footer(text="Scale Resell | Support")

    # ── Close ticket button ──────────────────────────────────────────────────
    class CloseButton(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=None)

        @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.danger, custom_id="close_ticket")
        async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
            await interaction.response.send_message("Closing ticket...", ephemeral=True)
            await interaction.channel.delete()

    # ── Create thread ────────────────────────────────────────────────────────
    thread_name = username
    thread = await ticket_channel.create_thread(
        name=thread_name,
        type=discord.ChannelType.public_thread,
        reason=f"Whop {event_type}"
    )

    # Ping member + staff role
    staff_role = discord.utils.get(guild.roles, name=STAFF_ROLE_NAME)
    ping_parts = []
    if member:
        ping_parts.append(member.mention)
    if staff_role:
        ping_parts.append(staff_role.mention)
    ping_msg = " ".join(ping_parts) if ping_parts else ""

    await thread.send(content=ping_msg, embed=embed, view=CloseButton())
    print(f"Ticket created: {thread_name} ({event_type})")


# ── Manual !ticket command ────────────────────────────────────────────────────
@bot.command(name="ticket")
@commands.has_role("Owner")
async def manual_ticket(ctx, member: discord.Member, event_type: str = "cancelled"):
    """Usage: !ticket @user cancelled  OR  !ticket @user past_due"""
    await ctx.message.delete()
    is_past_due = event_type == "past_due"
    fake_data = {
        "user": {
            "discord_id": str(member.id),
            "username": member.name,
        },
        "joined_at": member.joined_at.timestamp() if member.joined_at else None,
        "canceled_at": datetime.utcnow().timestamp(),
        "manage_url": CHECKOUT_URL
    }
    event = "invoice_past_due" if is_past_due else "membership_deactivated"
    await handle_membership_event(event, fake_data)
    await ctx.send(f"Ticket created for {member.mention}", delete_after=5)


# ── Flask webhook endpoint ────────────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def whop_webhook():
    payload   = request.get_data()
    sig       = request.headers.get("X-Whop-Signature", "")

    if not verify_whop_signature(payload, sig):
        return jsonify({"error": "Invalid signature"}), 401

    body       = request.get_json(force=True) or {}
    event_type = body.get("event") or body.get("type") or ""
    data       = body.get("data") or {}

    print(f"Received event: {event_type}")

    if event_type in ("membership_deactivated", "invoice_past_due", "membership_cancel_at_period_end_changed"):
        asyncio.run_coroutine_threadsafe(
            handle_membership_event(event_type, data),
            bot.loop
        )

    return jsonify({"status": "ok"}), 200


# ── Bot events ────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    print(f"Bot ready: {bot.user} | Guild ID: {GUILD_ID}")
    bot.add_view(PersistentCloseView())


class PersistentCloseView(discord.ui.View):
    """Persistent view so Close Ticket button works after bot restarts."""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.danger, custom_id="close_ticket")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Closing ticket...", ephemeral=True)
        await interaction.channel.delete()


# ── Start Flask in background thread ─────────────────────────────────────────
def run_flask():
    app.run(host="0.0.0.0", port=PORT)


threading.Thread(target=run_flask, daemon=True).start()

# ── Run bot ───────────────────────────────────────────────────────────────────
bot.run(DISCORD_TOKEN)

