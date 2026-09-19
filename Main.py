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
    raise RuntimeError(
        "Set the DISCORD_TOKEN environment variable first."
    )
DB_FILE = "giveaways.db"
VOUCH_CHANNELS = {
    1550793812954185763,
    1550793812954185759,
    1550793812555595865,
}
VOUCH_GENERATOR_ROLE = 1550793811469279268
MIN_RATING = 1
MAX_RATING = 5
# ============================================================
# DATABASE
# ============================================================
db = sqlite3.connect(
    DB_FILE,
    check_same_thread=False
)
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
intents.message_content = True
bot = commands.Bot(
    command_prefix="!",
    intents=intents
)
# ============================================================
# HELPERS
# ============================================================
def parse_duration(value: str) -> int:
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
        "d": 60 * 60 * 24,
    }
    seconds = amount * multipliers[unit]
    if seconds < 5:
        raise ValueError(
            "Duration must be at least 5 seconds."
        )
    if seconds > 365 * 24 * 60 * 60:
        raise ValueError(
            "Duration cannot exceed one year."
        )
    return seconds
def parse_vouch_rating(value: str) -> int:
    try:
        rating = int(value)
    except ValueError:
        raise ValueError(
            "Rating must be a number from 1 to 5."
        )
    if rating < MIN_RATING or rating > MAX_RATING:
        raise ValueError(
            "Rating must be between 1 and 5."
        )
    return rating
def stars(rating: int) -> str:
    return "⭐" * rating
def discord_timestamp(timestamp: float) -> str:
    return f"<t:{int(timestamp)}:R>"
def has_generator_role(
    member: discord.Member
) -> bool:
    return any(
        role.id == VOUCH_GENERATOR_ROLE
        for role in member.roles
    )
async def dm_user(
    user: discord.User | discord.Member,
    content: str
):
    try:
        await user.send(content)
        return True
    except discord.DiscordException as exc:
        print(
            f"Could not DM {user}: {exc}"
        )
        return False
# ============================================================
# GIVEAWAY EMBED
# ============================================================
def giveaway_embed(
    row,
    ended=False,
    winners=None
):
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
# REAL VOUCH EMBED
# ============================================================
def create_real_vouch_embed(
    user: discord.User,
    rating: int,
    reason: str | None
):
    embed = discord.Embed(
        title="New Vouch",
        color=discord.Color.purple(),
        timestamp=datetime.now(timezone.utc)
    )
    embed.add_field(
        name="Rating",
        value=stars(rating),
        inline=False
    )
    if reason:
        embed.add_field(
            name="Reason",
            value=reason,
            inline=False
        )
    embed.set_author(
        name=str(user),
        icon_url=user.display_avatar.url
    )
    embed.set_footer(
        text="Verified user vouch"
    )
    return embed
# ============================================================
# SIMULATED VOUCH EMBED
# ============================================================
def create_demo_vouch_embed(
    rating: int,
    reason: str | None
):
    
    embed = discord.Embed(
        title="New Vouch",
        color=discord.Color.purple(),
        timestamp=datetime.now(timezone.utc)
    )
    embed.add_field(
        name="Rating",
        value=stars(rating),
        inline=False
    )
    if reason:
        embed.add_field(
            name="Reason",
            value=reason,
            inline=False
        )
    embed.set_author(
        name=str(user),
        icon_url=user.display_avatar.url
    )
    embed.set_footer(
        text="Verified user vouch"
    )
    return embed
# ============================================================
# GIVEAWAY VIEW
# ============================================================
class GiveawayView(discord.ui.View):
    def __init__(self, giveaway_id: int):
        super().__init__(timeout=None)
        self.giveaway_id = giveaway_id
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.custom_id = (
                    f"giveaway_enter:{giveaway_id}"
                )
    @discord.ui.button(
        label="Enter Giveaway",
        style=discord.ButtonStyle.primary
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
        if datetime.now(
            timezone.utc
        ).timestamp() >= row["end_time"]:
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
            (
                self.giveaway_id,
                interaction.user.id
            )
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
            (
                self.giveaway_id,
                interaction.user.id
            )
        )
        db.commit()
        await interaction.response.send_message(
            "You have been entered into the giveaway.",
            ephemeral=True
        )
# ============================================================
# GIVEAWAY END
# ============================================================
async def end_giveaway(
    giveaway_id: int
):
    row = db.execute(
        "SELECT * FROM giveaways WHERE id = ?",
        (giveaway_id,)
    ).fetchone()
    if not row or row["ended"]:
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
    winners = []
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
        except discord.DiscordException as exc:
            print(
                f"Could not fetch giveaway channel: {exc}"
            )
            return
    try:
        message = await channel.fetch_message(
            row["message_id"]
        )
    except discord.DiscordException:
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
        try:
            await message.edit(
                embed=embed,
                view=view
            )
        except discord.DiscordException as exc:
            print(
                f"Could not edit giveaway message: {exc}"
            )
            return
    try:
        await channel.send(content)
    except discord.DiscordException as exc:
        print(
            f"Could not announce giveaway winners: {exc}"
        )
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
# PING
# ============================================================
@bot.tree.command(
    name="ping",
    description="Check the bot's latency"
)
async def ping(
    interaction: discord.Interaction
):
    latency_ms = round(
        bot.latency * 1000
    )
    await interaction.response.send_message(
        f"Pong! `{latency_ms}ms`"
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
    if interaction.guild is None:
        await interaction.response.send_message(
            "This command can only be used in a server.",
            ephemeral=True
        )
        return
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
            value=f"<@{row['rigged_user_id']}>",
            inline=False
        )
    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
    )
# ============================================================
# REGULAR VOUCH MESSAGE SYSTEM
# ============================================================
async def handle_vouch_message(
    message: discord.Message
):
    if message.author.bot:
        return
    if message.channel.id not in VOUCH_CHANNELS:
        return
    # Generator users using -vouches are handled separately.
    if (
        message.content.strip().lower().startswith("-vouches")
        and isinstance(message.author, discord.Member)
        and has_generator_role(message.author)
    ):
        return
    try:
        await message.delete()
    except discord.DiscordException:
        pass
    try:
        await message.channel.send(
            f"{message.author.mention} "
            f"please do `vouch {{rating}} (optional) {{reason}}`"
        )
    except discord.DiscordException as exc:
        print(
            f"Failed to send vouch instructions: {exc}"
        )
# ============================================================
# SIMULATED VOUCH GENERATOR
# ============================================================
async def generate_demo_vouches(
    channel: discord.TextChannel,
    amount: int,
    min_rating: int,
    max_rating: int,
    duration_minutes: int
):
    interval = (
        (duration_minutes * 60) / amount
        if amount > 0
        else 0
    )
    for index in range(amount):
        rating = random.randint(
            min_rating,
            max_rating
        )
        reason = random.choice([
            "Great service!",
            "Fast and easy.",
            "Everything worked perfectly.",
            "Really good experience.",
            "Would use again.",
            "Quick and reliable."
        ])
        embed = create_demo_vouch_embed(
            rating,
            reason
        )
        try:
            await channel.send(
                embed=embed
            )
        except discord.DiscordException as exc:
            print(
                f"Failed to send simulated vouch: {exc}"
            )
            return
        if index < amount - 1:
            await asyncio.sleep(interval)
# ============================================================
# -VOUCHES GENERATOR
# ============================================================
async def handle_vouches_command(
    message: discord.Message
):
    if message.author.bot:
        return
    if message.channel.id not in VOUCH_CHANNELS:
        return
    if not isinstance(message.author, discord.Member):
        return
    content = message.content.strip()
    if not content.lower().startswith("-vouches"):
        return
    # Only users with the generator role get the generator.
    if not has_generator_role(message.author):
        return
    parts = content.split()
    # Delete the original command.
    try:
        await message.delete()
    except discord.DiscordException:
        pass
    if len(parts) != 5:
        await dm_user(
            message.author,
            "Invalid format.\n\n"
            "Use:\n"
            "`-vouches {amount} {min. rating} "
            "{max. rating} {time}`"
        )
        return
    try:
        amount = int(parts[1])
        min_rating = int(parts[2])
        max_rating = int(parts[3])
        duration = int(parts[4])
    except ValueError:
        await dm_user(
            message.author,
            "Amount, ratings, and time must all be numbers."
        )
        return
    if amount < 1:
        await dm_user(
            message.author,
            "Amount must be at least 1."
        )
        return
    if amount > 1000:
        await dm_user(
            message.author,
            "Amount cannot exceed 1000."
        )
        return
    if min_rating < 1 or min_rating > 5:
        await dm_user(
            message.author,
            "Minimum rating must be between 1 and 5."
        )
        return
    if max_rating < 1 or max_rating > 5:
        await dm_user(
            message.author,
            "Maximum rating must be between 1 and 5."
        )
        return
    if min_rating > max_rating:
        await dm_user(
            message.author,
            "Minimum rating cannot be greater than "
            "maximum rating."
        )
        return
    if duration < 1:
        await dm_user(
            message.author,
            "Time must be at least 1 minute."
        )
        return
    # No public confirmation message.
    # Send the information privately instead.
    dm_sent = await dm_user(
        message.author,
        f"**Vouch simulation started.**\n\n"
        f"Amount: `{amount}`\n"
        f"Rating range: `{min_rating}-{max_rating}`\n"
        f"Duration: `{duration} minute(s)`\n"
        f"Channel: <#{message.channel.id}>\n\n"
        f"All generated messages are explicitly marked "
        f"as **SIMULATED / TEST DATA**."
    )
    if not dm_sent:
        print(
            f"Could not send generator status DM "
            f"to {message.author}."
        )
    asyncio.create_task(
        generate_demo_vouches(
            message.channel,
            amount,
            min_rating,
            max_rating,
            duration
        )
    )
# ============================================================
# MESSAGE LISTENER
# ============================================================
@bot.event
async def on_message(
    message: discord.Message
):
    if message.author.bot:
        return
    # --------------------------------------------------------
    # First: check whether this is an authorized -vouches
    # generator command.
    # --------------------------------------------------------
    if (
        message.channel.id in VOUCH_CHANNELS
        and message.content.strip().lower().startswith("-vouches")
        and isinstance(message.author, discord.Member)
        and has_generator_role(message.author)
    ):
        await handle_vouches_command(
            message
        )
        return
    # --------------------------------------------------------
    # Otherwise, regular vouch-channel messages are handled
    # normally.
    # --------------------------------------------------------
    if message.channel.id in VOUCH_CHANNELS:
        await handle_vouch_message(
            message
        )
        return
    # --------------------------------------------------------
    # Normal bot commands outside the vouch system.
    # --------------------------------------------------------
    await bot.process_commands(
        message
    )
# ============================================================
# STARTUP
# ============================================================
@bot.event
async def on_ready():
    print(
        f"Logged in as {bot.user}"
    )
    print(
        f"Bot ID: {bot.user.id}"
    )
    if not giveaway_checker.is_running():
        giveaway_checker.start()
# ============================================================
# SETUP HOOK
# ============================================================
async def setup_hook():
    synced = await bot.tree.sync()
    print(
        f"Synced {len(synced)} slash commands."
    )
    rows = db.execute(
        """
        SELECT id
        FROM giveaways
        WHERE ended = 0
        """
    ).fetchall()
    for row in rows:
        giveaway_id = row["id"]
        bot.add_view(
            GiveawayView(giveaway_id)
        )
    print(
        f"Restored {len(rows)} active giveaway views."
    )
bot.setup_hook = setup_hook
# ============================================================
# RUN
# ============================================================
bot.run(TOKEN)

The important bit is now the dispatch in on_message: authorized -vouches goes exclusively to the generator; everything else in those channels goes through the regular listener.
