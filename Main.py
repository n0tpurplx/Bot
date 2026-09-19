import os
import re
import random
import sqlite3
import asyncio
from datetime import datetime, timezone, timedelta
import discord
from discord import app_commands
from discord.ext import commands, tasks
# ============================================================
# CONFIG
# ============================================================
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("Set the DISCORD_TOKEN environment variable first.")
DB_FILE = "giveaways.db"
# ============================================================
# DATABASE
# ============================================================
db = sqlite3.connect(DB_FILE)
db.row_factory = sqlite3.Row
db.execute("""
CREATE TABLE IF NOT EXISTS giveaways (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    host_id INTEGER NOT NULL,
    prize TEXT NOT NULL,
    winners INTEGER NOT NULL,
    end_time REAL NOT NULL,
    ended INTEGER DEFAULT 0,
    rigged_user_id INTEGER
)
""")
db.execute("""
CREATE TABLE IF NOT EXISTS entries (
    giveaway_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    PRIMARY KEY (giveaway_id, user_id)
)
""")
db.commit()
# ============================================================
# BOT
# ============================================================
intents = discord.Intents.default()
intents.guilds = True
intents.members = True
bot = commands.Bot(
    command_prefix="!",
    intents=intents
)
# ============================================================
# HELPERS
# ============================================================
def parse_duration(value: str) -> int:
    """
    Converts:
        10s
        10m
        2h
        3d
    into seconds.
    """
    match = re.fullmatch(
        r"\s*(\d+)\s*([smhd])\s*",
        value.lower()
    )
    if not match:
        raise ValueError(
            "Invalid duration. Use something like `10m`, `2h`, or `3d`."
        )
    amount = int(match.group(1))
    unit = match.group(2)
    multipliers = {
        "s": 1,
        "m": 60,
        "h": 60 * 60,
        "d": 60 * 60 * 24
    }
    seconds = amount * multipliers[unit]
    if seconds < 5:
        raise ValueError("Duration must be at least 5 seconds.")
    if seconds > 365 * 24 * 60 * 60:
        raise ValueError("Duration cannot exceed one year.")
    return seconds
def discord_timestamp(timestamp: float) -> str:
    return f"<t:{int(timestamp)}:R>"
def giveaway_embed(row, ended=False, winners=None):
    if ended:
        description = (
            f"**{row['prize']}**\n\n"
            f"**Winners:** "
        )
        if winners:
            description += ", ".join(winners)
        else:
            description += "No valid entries."
        embed = discord.Embed(
            title="Giveaway Ended",
            description=description,
            color=discord.Color.dark_purple()
        )
    else:
        description = (
            f"**{row['prize']}**\n\n"
            f"**Winners:** `{row['winners']}`\n"
            f"**Ends:** {discord_timestamp(row['end_time'])}\n\n"
            f"Click the button below to enter."
        )
        embed = discord.Embed(
            title="Giveaway",
            description=description,
            color=discord.Color.purple()
        )
    embed.set_footer(
        text=f"Giveaway #{row['id']} | Hosted by <@{row['host_id']}>"
    )
    return embed
# ============================================================
# GIVEAWAY VIEW
# ============================================================
class GiveawayView(discord.ui.View):
    def __init__(self, giveaway_id: int):
        super().__init__(timeout=None)
        self.giveaway_id = giveaway_id
    @discord.ui.button(
        label="Enter Giveaway",
        style=discord.ButtonStyle.primary,
        custom_id="giveaway_enter"
    )
    async def enter(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        row = db.execute(
            "SELECT * FROM giveaways WHERE id = ?",
            (self.giveaway_id,)
        ).fetchone()
        if not row:
            await interaction.response.send_message(
                "This giveaway no longer exists.",
                ephemeral=True
            )
            return
        if row["ended"]:
            await interaction.response.send_message(
                "This giveaway has already ended.",
                ephemeral=True
            )
            return
        if datetime.now(timezone.utc).timestamp() >= row["end_time"]:
            await interaction.response.send_message(
                "This giveaway has already ended.",
                ephemeral=True
            )
            return
        existing = db.execute(
            """
            SELECT 1
            FROM entries
            WHERE giveaway_id = ?
            AND user_id = ?
            """,
            (self.giveaway_id, interaction.user.id)
        ).fetchone()
        if existing:
            await interaction.response.send_message(
                "You are already entered.",
                ephemeral=True
            )
            return
        db.execute(
            """
            INSERT INTO entries (
                giveaway_id,
                user_id
            )
            VALUES (?, ?)
            """,
            (self.giveaway_id, interaction.user.id)
        )
        db.commit()
        await interaction.response.send_message(
            "You have been entered into the giveaway.",
            ephemeral=True
        )
# ============================================================
# END GIVEAWAY
# ============================================================
async def end_giveaway(giveaway_id: int):
    row = db.execute(
        "SELECT * FROM giveaways WHERE id = ?",
        (giveaway_id,)
    ).fetchone()
    if not row or row["ended"]:
        return
    db.execute(
        """
        UPDATE giveaways
        SET ended = 1
        WHERE id = ?
        """,
        (giveaway_id,)
    )
    db.commit()
    entries = db.execute(
        """
        SELECT user_id
        FROM entries
        WHERE giveaway_id = ?
        """,
        (giveaway_id,)
    ).fetchall()
    user_ids = [
        entry["user_id"]
        for entry in entries
    ]
    winners = []
    # ========================================================
    # RIGGED MODE
    # ========================================================
    if row["rigged_user_id"]:
        rigged_id = row["rigged_user_id"]
        if rigged_id in user_ids:
            winners.append(rigged_id)
        remaining = [
            uid
            for uid in user_ids
            if uid not in winners
        ]
        remaining_count = max(
            0,
            row["winners"] - len(winners)
        )
        if remaining_count:
            winners.extend(
                random.sample(
                    remaining,
                    min(
                        remaining_count,
                        len(remaining)
                    )
                )
            )
    else:
        if user_ids:
            winners = random.sample(
                user_ids,
                min(
                    row["winners"],
                    len(user_ids)
                )
            )
    channel = bot.get_channel(
        row["channel_id"]
    )
    if not channel:
        try:
            channel = await bot.fetch_channel(
                row["channel_id"]
            )
        except Exception:
            return
    try:
        message = await channel.fetch_message(
            row["message_id"]
        )
    except Exception:
        message = None
    winner_mentions = [
        f"<@{user_id}>"
        for user_id in winners
    ]
    if winners:
        winner_text = ", ".join(
            winner_mentions
        )
        content = (
            f"Congratulations {winner_text}.\n"
            f"You won **{row['prize']}**."
        )
    else:
        winner_mentions = []
        content = (
            f"The giveaway for **{row['prize']}** "
            f"ended without any valid entries."
        )
    if message:
        embed = giveaway_embed(
            row,
            ended=True,
            winners=winner_mentions
        )
        view = GiveawayView(
            row["id"]
        )
        for child in view.children:
            child.disabled = True
        await message.edit(
            embed=embed,
            view=view
        )
    await channel.send(content)
# ============================================================
# GIVEAWAY CHECKER
# ============================================================
@tasks.loop(seconds=5)
async def giveaway_checker():
    now = datetime.now(
        timezone.utc
    ).timestamp()
    rows = db.execute(
        """
        SELECT id
        FROM giveaways
        WHERE ended = 0
        AND end_time <= ?
        """,
        (now,)
    ).fetchall()
    for row in rows:
        try:
            await end_giveaway(
                row["id"]
            )
        except Exception as exc:
            print(
                f"[Giveaway #{row['id']}] "
                f"Failed to end: {exc}"
            )
# ============================================================
# CREATE GIVEAWAY
# ============================================================
@bot.tree.command(
    name="giveaway",
    description="Create a giveaway."
)
@app_commands.describe(
    prize="What is being given away",
    duration="Duration, for example 10m, 2h, or 3d",
    winners="Number of winners",
    rigged="Explicitly select a winner"
)
@app_commands.default_permissions(
    manage_guild=True
)
async def giveaway(
    interaction: discord.Interaction,
    prize: str,
    duration: str,
    winners: app_commands.Range[int, 1, 100],
    rigged: discord.Member | None = None
):
    try:
        seconds = parse_duration(
            duration
        )
    except ValueError as exc:
        await interaction.response.send_message(
            str(exc),
            ephemeral=True
        )
        return
    end_time = (
        datetime.now(timezone.utc)
        + timedelta(seconds=seconds)
    ).timestamp()
    cursor = db.execute(
        """
        INSERT INTO giveaways (
            guild_id,
            channel_id,
            message_id,
            host_id,
            prize,
            winners,
            end_time,
            ended,
            rigged_user_id
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)
        """,
        (
            interaction.guild.id,
            interaction.channel.id,
            0,
            interaction.user.id,
            prize,
            winners,
            end_time,
            rigged.id if rigged else None
        )
    )
    giveaway_id = cursor.lastrowid
    db.commit()
    row = db.execute(
        """
        SELECT *
        FROM giveaways
        WHERE id = ?
        """,
        (giveaway_id,)
    ).fetchone()
    embed = giveaway_embed(row)
    view = GiveawayView(
        giveaway_id
    )
    await interaction.response.send_message(
        embed=embed,
        view=view
    )
    message = await interaction.original_response()
    db.execute(
        """
        UPDATE giveaways
        SET message_id = ?
        WHERE id = ?
        """,
        (
            message.id,
            giveaway_id
        )
    )
    db.commit()
    if rigged:
        print(
            f"[Giveaway #{giveaway_id}] "
            f"Configured winner: "
            f"{rigged} ({rigged.id})"
        )
# ============================================================
# END GIVEAWAY
# ============================================================
@bot.tree.command(
    name="giveaway-end",
    description="End a giveaway immediately."
)
@app_commands.describe(
    giveaway_id="Giveaway ID"
)
@app_commands.default_permissions(
    manage_guild=True
)
async def giveaway_end(
    interaction: discord.Interaction,
    giveaway_id: int
):
    row = db.execute(
        """
        SELECT *
        FROM giveaways
        WHERE id = ?
        """,
        (giveaway_id,)
    ).fetchone()
    if not row:
        await interaction.response.send_message(
            "Giveaway not found.",
            ephemeral=True
        )
        return
    if row["ended"]:
        await interaction.response.send_message(
            "That giveaway has already ended.",
            ephemeral=True
        )
        return
    await interaction.response.defer(
        ephemeral=True
    )
    await end_giveaway(
        giveaway_id
    )
    await interaction.followup.send(
        f"Giveaway #{giveaway_id} ended.",
        ephemeral=True
    )
# ============================================================
# REROLL
# ============================================================
@bot.tree.command(
    name="giveaway-reroll",
    description="Reroll the winners of an ended giveaway."
)
@app_commands.describe(
    giveaway_id="Giveaway ID"
)
@app_commands.default_permissions(
    manage_guild=True
)
async def giveaway_reroll(
    interaction: discord.Interaction,
    giveaway_id: int
):
    row = db.execute(
        """
        SELECT *
        FROM giveaways
        WHERE id = ?
        """,
        (giveaway_id,)
    ).fetchone()
    if not row:
        await interaction.response.send_message(
            "Giveaway not found.",
            ephemeral=True
        )
        return
    if not row["ended"]:
        await interaction.response.send_message(
            "That giveaway has not ended yet.",
            ephemeral=True
        )
        return
    entries = db.execute(
        """
        SELECT user_id
        FROM entries
        WHERE giveaway_id = ?
        """,
        (giveaway_id,)
    ).fetchall()
    user_ids = [
        entry["user_id"]
        for entry in entries
    ]
    if not user_ids:
        await interaction.response.send_message(
            "There are no entries to reroll.",
            ephemeral=True
        )
        return
    selected = random.sample(
        user_ids,
        min(
            row["winners"],
            len(user_ids)
        )
    )
    mentions = ", ".join(
        f"<@{uid}>"
        for uid in selected
    )
    await interaction.response.send_message(
        f"New winner(s): {mentions}\n"
        f"Prize: **{row['prize']}**"
    )
# ============================================================
# GIVEAWAY INFO
# ============================================================
@bot.tree.command(
    name="giveaway-info",
    description="Show information about a giveaway."
)
@app_commands.describe(
    giveaway_id="Giveaway ID"
)
async def giveaway_info(
    interaction: discord.Interaction,
    giveaway_id: int
):
    row = db.execute(
        """
        SELECT *
        FROM giveaways
        WHERE id = ?
        """,
        (giveaway_id,)
    ).fetchone()
    if not row:
        await interaction.response.send_message(
            "Giveaway not found.",
            ephemeral=True
        )
        return
    entries = db.execute(
        """
        SELECT COUNT(*) AS count
        FROM entries
        WHERE giveaway_id = ?
        """,
        (giveaway_id,)
    ).fetchone()["count"]
    status = (
        "Ended"
        if row["ended"]
        else "Active"
    )
    embed = discord.Embed(
        title=f"Giveaway #{giveaway_id}",
        color=discord.Color.purple()
    )
    embed.add_field(
        name="Prize",
        value=row["prize"],
        inline=False
    )
    embed.add_field(
        name="Status",
        value=status
    )
    embed.add_field(
        name="Winners",
        value=str(row["winners"])
    )
    embed.add_field(
        name="Entries",
        value=str(entries)
    )
    embed.add_field(
        name="Host",
        value=f"<@{row['host_id']}>"
    )
    if row["rigged_user_id"]:
        embed.add_field(
            name="Configured Winner",
            value=f"<@{row['rigged_user_id']}",
            inline=False
        )
    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
    )
# ============================================================
# READY
# ============================================================
@bot.event
async def on_ready():
    print(
        f"Logged in as {bot.user}"
    )
    print(
        f"Bot ID: {bot.user.id}"
    )
    try:
        synced = await bot.tree.sync()
        print(
            f"Synced {len(synced)} slash commands."
        )
    except Exception as exc:
        print(
            f"Command sync failed: {exc}"
        )
    if not giveaway_checker.is_running():
        giveaway_checker.start()
# ============================================================
# RUN
# ============================================================
bot.run(TOKEN)
