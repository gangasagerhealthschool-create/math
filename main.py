import discord
from discord import app_commands, Interaction
from discord.ext import commands
from discord.utils import utcnow
from datetime import datetime, timedelta, timezone
import asyncio
import os
import json
import re
import time
import random
import aiohttp

# Load .env file if present (used when self-hosting outside Replit)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ============================================================
# GLOBAL CONSTANTS / DIRECTORIES
# ============================================================

SETTINGS_DIR = "server_data"
SPLITSTEAL_DIR = "splitsteal_data"
GIVEAWAY_DIR = "giveaway"

os.makedirs(SETTINGS_DIR, exist_ok=True)
os.makedirs(SPLITSTEAL_DIR, exist_ok=True)
os.makedirs(GIVEAWAY_DIR, exist_ok=True)

PINGABLE_ROLES = ["Quickdrop Ping", "Giveaway Ping", "Server Booster"]


# ============================================================
# SHARED HELPERS
# ============================================================

def parse_giveaway_duration(duration: str):
    """Parse '1d2h30m' style durations into total seconds (giveaway-style)."""
    total = 0
    num = ""
    units = {"d": 86400, "h": 3600, "m": 60, "s": 1}
    for c in duration:
        if c.isdigit():
            num += c
        elif c in units and num:
            total += int(num) * units[c]
            num = ""
        else:
            return None
    return total if total > 0 else None


def parse_mod_duration(duration_str: str) -> timedelta:
    """Parse '1d2h30m' style durations into a timedelta (moderation-style)."""
    pattern = r'((?P<days>\d+)d)?((?P<hours>\d+)h)?((?P<minutes>\d+)m)?((?P<seconds>\d+)s)?'
    match = re.fullmatch(pattern, duration_str)
    if not match:
        raise ValueError("Invalid duration format. Use '1d2h30m'.")
    time_params = {k: int(v) for k, v in match.groupdict(default='0').items()}
    return timedelta(**time_params)


def get_moderation_log_channel(guild: discord.Guild):
    path = f"{SETTINGS_DIR}/{guild.id}_settings.json"
    if not os.path.exists(path):
        return None
    with open(path, "r") as f:
        data = json.load(f)
    return guild.get_channel(data.get("modlog_channel"))


def load_punishments(guild_id: int) -> list:
    path = f"{SETTINGS_DIR}/{guild_id}_punishments.json"
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return []


def save_punishment(guild_id: int, record: dict):
    path = f"{SETTINGS_DIR}/{guild_id}_punishments.json"
    records = load_punishments(guild_id)
    records.append(record)
    with open(path, "w") as f:
        json.dump(records, f, indent=4)


# ============================================================
# GIVEAWAY
# ============================================================

def create_giveaway_embed(prize, end_time, host, entries, winners):
    ts = int(end_time.timestamp())
    embed = discord.Embed(
        title=f"{prize}",
        description=(
            f"Hosted by: {host.mention}\n"
            f"Entries: {entries}\n"
            f"Winners: {winners}\n"
            f"Time: <t:{ts}:R>"
        ),
        color=discord.Color.green()
    )
    return embed


class GiveawayView(discord.ui.View):
    def __init__(self, cog, giveaway_id):
        super().__init__(timeout=None)
        self.cog = cog
        self.gid = giveaway_id

    @discord.ui.button(label="🎉 Enter Giveaway", style=discord.ButtonStyle.primary)
    async def enter_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        g = self.cog.giveaways.get(self.gid)
        if not g:
            return await interaction.response.send_message("⚠️ This giveaway has ended.", ephemeral=True)

        uid = interaction.user.id
        if uid in g["entries"]:
            return await interaction.response.send_message("❌ You've already entered!", ephemeral=True)

        g["entries"].add(uid)
        self.cog.save_giveaways(interaction.guild.id)
        await interaction.response.send_message("✅ You have entered!", ephemeral=True)

        host_user = interaction.guild.get_member(g["host"]) or self.cog.bot.get_user(g["host"])
        await g["message"].edit(
            embed=create_giveaway_embed(
                g["prize"], g["end_time"], host_user, len(g["entries"]), g["winners"]
            )
        )


class GiveawayModal(discord.ui.Modal, title="Giveaway Setup"):
    duration = discord.ui.TextInput(label="Duration", placeholder="e.g. 1d2h30m", required=True)
    winners = discord.ui.TextInput(label="Number of Winners", placeholder="e.g. 1", required=True)
    prize = discord.ui.TextInput(label="Prize", placeholder="e.g. Nitro, $10", required=True)

    def __init__(self, cog, interaction):
        super().__init__()
        self.cog = cog
        self.interaction = interaction

    async def on_submit(self, interaction: discord.Interaction):
        sec = parse_giveaway_duration(self.duration.value)
        if sec is None:
            return await interaction.response.send_message("❌ Invalid duration format!", ephemeral=True)
        if sec < 5:
            return await interaction.response.send_message("❌ Minimum giveaway duration is **5 seconds**.", ephemeral=True)

        try:
            win_count = int(self.winners.value)
        except ValueError:
            return await interaction.response.send_message("❌ Number of winners must be a number!", ephemeral=True)

        end_time = datetime.now(timezone.utc) + timedelta(seconds=sec)
        host_id = self.interaction.user.id
        embed = create_giveaway_embed(self.prize.value, end_time, self.interaction.user, 0, win_count)
        view = GiveawayView(self.cog, None)
        message = await interaction.channel.send(embed=embed, view=view)
        gid = message.id
        view.gid = gid

        self.cog.giveaways[gid] = {
            "prize": self.prize.value,
            "winners": win_count,
            "end_time": end_time,
            "host": host_id,
            "channel": interaction.channel.id,
            "message": message,
            "entries": set()
        }
        self.cog.save_giveaways(interaction.guild.id)
        self.cog.bot.loop.create_task(self.cog.update_giveaway_message(gid))

        await interaction.response.send_message(f"✅ Giveaway started in {interaction.channel.mention}!", ephemeral=True)


class Giveaway(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.giveaways = {}
        self.bot.loop.create_task(self.load_giveaways())

    async def load_giveaways(self):
        await self.bot.wait_until_ready()
        for fn in os.listdir(GIVEAWAY_DIR):
            if not fn.endswith(".json"):
                continue
            guild_id = int(fn[:-5])
            path = os.path.join(GIVEAWAY_DIR, fn)
            with open(path) as f:
                data = json.load(f)
            guild = self.bot.get_guild(guild_id)
            if not guild:
                continue
            for gid_str, g in data.items():
                gid = int(gid_str)
                channel = guild.get_channel(g["channel"])
                if not channel:
                    continue
                try:
                    message = await channel.fetch_message(g["message"])
                except discord.NotFound:
                    continue

                end_time = datetime.fromisoformat(g["end_time"])
                entries = set(g.get("entries", []))

                self.giveaways[gid] = {
                    "prize": g["prize"],
                    "winners": g["winners"],
                    "end_time": end_time,
                    "host": g["host"],
                    "channel": g["channel"],
                    "message": message,
                    "entries": entries
                }
                view = GiveawayView(self, gid)
                await message.edit(view=view)
                self.bot.loop.create_task(self.update_giveaway_message(gid))

    def save_giveaways(self, guild_id):
        path = os.path.join(GIVEAWAY_DIR, f"{guild_id}.json")
        to_save = {}
        for gid, g in self.giveaways.items():
            if self.bot.get_channel(g["channel"]):
                to_save[gid] = {
                    "prize": g["prize"],
                    "winners": g["winners"],
                    "end_time": g["end_time"].isoformat(),
                    "host": g["host"],
                    "channel": g["channel"],
                    "message": g["message"].id,
                    "entries": list(g["entries"])
                }
        with open(path, "w") as f:
            json.dump(to_save, f, indent=4)

    async def update_giveaway_message(self, gid: int):
        while gid in self.giveaways:
            g = self.giveaways[gid]
            now = datetime.now(timezone.utc)
            if now >= g["end_time"]:
                return await self.end_giveaway(gid)
            host_member = (
                g["message"].guild.get_member(g["host"]) or self.bot.get_user(g["host"])
            )
            embed = create_giveaway_embed(
                g["prize"], g["end_time"], host_member,
                len(g["entries"]), g["winners"]
            )
            try:
                await g["message"].edit(embed=embed)
            except Exception:
                pass
            await asyncio.sleep(10)

    async def end_giveaway(self, gid: int):
        g = self.giveaways.pop(gid, None)
        if not g:
            return

        channel = self.bot.get_channel(g["channel"])
        participants = list(g["entries"])
        host_user = channel.guild.get_member(g["host"]) or self.bot.get_user(g["host"])

        settings_path = f"{SETTINGS_DIR}/{channel.guild.id}_settings.json"
        ticket_channel_mention = None
        if os.path.exists(settings_path):
            try:
                with open(settings_path) as sf:
                    settings_data = json.load(sf)
                ch_id = settings_data.get("giveaway_claim_channel")
                if ch_id:
                    ticket_channel_mention = f"<#{ch_id}>"
            except Exception:
                pass
        ticket_channel_mention = ticket_channel_mention or "a ticket channel"

        win_embed = discord.Embed(title=" 🎉 Congratulations!", color=discord.Color.green())
        desc_lines = [f"**Prize:** {g['prize']}"]
        winner_mentions = []

        if participants:
            winners = random.sample(participants, min(len(participants), g['winners']))
            for uid in winners:
                user = channel.guild.get_member(uid) or await self.bot.fetch_user(uid)
                if user:
                    winner_mentions.append(user.mention)
            desc_lines.append(f"**Winner(s):** {', '.join(winner_mentions)}")
        else:
            desc_lines.append("**Winner(s):** No valid entries.")

        desc_lines.append(f"**Hosted by:** {host_user.mention}")
        desc_lines.append("\n--------------------------")
        desc_lines.append(f"- Open a ticket in {ticket_channel_mention}")
        desc_lines.append("- Please take a screenshot of this message and send it in your claim ticket!")

        win_embed.description = "\n".join(desc_lines)

        if winner_mentions:
            await channel.send(content="🎉" + " ".join(winner_mentions), embed=win_embed)
        else:
            await channel.send(embed=win_embed)

        self.save_giveaways(channel.guild.id)

    def has_staff_or_above(self, member: discord.Member):
        staff_role = discord.utils.get(member.guild.roles, name="Staff Team")
        if not staff_role:
            return member.guild_permissions.administrator
        return member.top_role >= staff_role

    @app_commands.command(name="gcreate", description="Create a giveaway via form")
    async def gcreate(self, interaction: discord.Interaction):
        member = interaction.user
        if not isinstance(member, discord.Member):
            return await interaction.response.send_message("❌ You must be in a server to use this command.", ephemeral=True)
        if not self.has_staff_or_above(member):
            return await interaction.response.send_message("❌ You do not have permission to use this command.", ephemeral=True)
        await interaction.response.send_modal(GiveawayModal(self, interaction))

    @app_commands.command(name="select_ticket_channel",
                          description="Set the ticket channel that giveaway winners should open a claim ticket in")
    @app_commands.describe(channel="The ticket channel to tag in giveaway results")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def select_ticket_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        settings_path = f"{SETTINGS_DIR}/{interaction.guild.id}_settings.json"
        data = {}
        if os.path.exists(settings_path):
            try:
                with open(settings_path, "r") as f:
                    data = json.load(f)
            except json.JSONDecodeError:
                data = {}
        data["giveaway_claim_channel"] = channel.id
        with open(settings_path, "w") as f:
            json.dump(data, f, indent=4)
        embed = discord.Embed(
            title="✅ Ticket Channel Set",
            description=f"Giveaway winners will now be directed to open a ticket in {channel.mention}.",
            color=discord.Color.green()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


# ============================================================
# PURGE
# ============================================================

class Purge(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def get_moderation_log_channel(self, guild: discord.Guild):
        path = f"{SETTINGS_DIR}/{guild.id}_settings.json"
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r") as f:
                data = json.load(f)
                log_channel_id = data.get("modlog_channel")
                if log_channel_id:
                    return guild.get_channel(log_channel_id)
        except Exception as e:
            print(f"[ERROR] Failed to load moderation log channel: {e}")
        return None

    @app_commands.command(name="purge", description="Delete a number of messages from the channel.")
    @app_commands.describe(amount="Number of messages to delete (max 100)")
    async def purge(self, interaction: discord.Interaction, amount: int):
        if not interaction.user.guild_permissions.manage_messages:
            await interaction.response.send_message(":x: You don't have permission to manage messages.", ephemeral=True)
            return

        if amount < 1 or amount > 100:
            await interaction.response.send_message("⚠ You can only purge between 1 and 100 messages.", ephemeral=True)
            return

        await interaction.response.defer(thinking=True)

        cutoff = interaction.created_at - timedelta(seconds=1)

        def is_eligible(msg: discord.Message):
            return msg.created_at < cutoff

        try:
            purged = await interaction.channel.purge(limit=amount + 1, check=is_eligible)
            purged = purged[:amount]
        except Exception as e:
            print(f"[ERROR] Error while purging messages: {e}")
            await interaction.followup.send(":x: Failed to purge messages.", ephemeral=True)
            return

        if not purged:
            await interaction.followup.send(":x: No messages found to purge.", ephemeral=True)
            return

        embed = discord.Embed(
            title="🧹 Messages Purged",
            description=f"**{len(purged)}** messages were purged by {interaction.user.mention} in {interaction.channel.mention}.",
            color=discord.Color.orange(),
            timestamp=datetime.utcnow()
        )
        embed.set_footer(text=f"User ID: {interaction.user.id}")
        if interaction.guild.icon:
            embed.set_thumbnail(url=interaction.guild.icon.url)

        log_channel = await self.get_moderation_log_channel(interaction.guild)
        if log_channel:
            try:
                await log_channel.send(embed=embed)
            except Exception as e:
                print(f"[ERROR] Failed to send embed to log channel: {e}")

        await interaction.followup.send(embed=embed, ephemeral=True)


# ============================================================
# MEMBER COUNT
# ============================================================

class MemberCount(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="membercount", description="Shows the number of members in the server.")
    async def membercount(self, interaction: discord.Interaction):
        guild = interaction.guild
        total_members = guild.member_count
        humans = len([m for m in guild.members if not m.bot])
        bots = total_members - humans

        embed = discord.Embed(title="📊 Member Count", color=discord.Color.blue())
        embed.add_field(name="Total Members", value=str(total_members), inline=False)
        embed.add_field(name="Humans", value=str(humans), inline=True)
        embed.add_field(name="Bots", value=str(bots), inline=True)
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
            embed.set_footer(text=f"Server: {guild.name}", icon_url=guild.icon.url)
        else:
            embed.set_footer(text=f"Server: {guild.name}")

        await interaction.response.send_message(embed=embed)


# ============================================================
# TICKET SYSTEM (panel, modals, claim/close, AI summary)
# ============================================================

def build_fallback_summary(category: str, children, server_name: str = "the server") -> str:
    ign = children[0].value.strip() if children else "Unknown"
    if category == "general":
        question = children[1].value.strip() if len(children) > 1 else ""
        return (
            f"**{ign}** has opened a General Support ticket on {server_name}. "
            f"They are seeking help with the following: {question}"
        )
    elif category == "report":
        reported = children[1].value.strip() if len(children) > 1 else "Unknown"
        evidence = children[2].value.strip() if len(children) > 2 else "None provided"
        return (
            f"**{ign}** has submitted a Player Report against **{reported}**. "
            f"They have provided the following evidence: {evidence}"
        )
    elif category == "bug":
        desc = children[1].value.strip() if len(children) > 1 else ""
        return (
            f"**{ign}** has reported a bug on the {server_name} server. "
            f"The issue they encountered is: {desc}"
        )
    elif category == "appeal":
        reason = children[1].value.strip() if len(children) > 1 else ""
        return (
            f"**{ign}** is appealing a punishment on {server_name}. "
            f"Their reason for the appeal is: {reason}"
        )
    return f"**{ign}** has opened a new support ticket under the **{category.upper()}** category."


async def get_ai_summary(category: str, fields_text: str, children=None, server_name: str = "the server") -> str:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return build_fallback_summary(category, children, server_name) if children else fields_text[:500]
    try:
        async with aiohttp.ClientSession() as session:
            prompt = (
                f"You are a senior support manager for a Minecraft server called {server_name}. "
                f"A player just opened a support ticket. Using the details below, write a clear "
                f"3-5 sentence paragraph in your own words explaining exactly what this ticket is about, "
                f"what the player needs, and any important details staff should be aware of. "
                f"Do NOT just repeat the questions and answers — explain the situation naturally "
                f"as if you are briefing a staff member who has not seen the ticket.\n\n"
                f"Category: {category.upper()}\n{fields_text}"
            )
            async with session.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": "gpt-3.5-turbo",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 250
                },
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data["choices"][0]["message"]["content"].strip()
                return build_fallback_summary(category, children) if children else fields_text[:500]
    except Exception:
        return build_fallback_summary(category, children) if children else fields_text[:500]


class TicketSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="General Support", emoji="📝",
                                 description="Ask a question, report someone, or general help"),
            discord.SelectOption(label="Player Report", emoji="⚔️",
                                 description="Report a hacker or rule breaker"),
            discord.SelectOption(label="Bug Reports", emoji="🛠️",
                                 description="Report a server issue or bug"),
            discord.SelectOption(label="Punishment Appeal", emoji="🔮",
                                 description="Appeal a ban or mute"),
        ]
        super().__init__(placeholder="Select a category...", min_values=1, max_values=1,
                         options=options, custom_id="radiummc_ticket_select")

    async def callback(self, interaction: discord.Interaction):
        selection = self.values[0]
        if selection == "General Support":
            await interaction.response.send_modal(TicketModal(title="GENERAL Ticket", category="general"))
        elif selection == "Player Report":
            await interaction.response.send_modal(TicketModal(title="REPORT Ticket", category="report"))
        elif selection == "Bug Reports":
            await interaction.response.send_modal(TicketModal(title="BUG Ticket", category="bug"))
        elif selection == "Punishment Appeal":
            await interaction.response.send_modal(TicketModal(title="APPEAL Ticket", category="appeal"))


class TicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(TicketSelect())


class TicketModal(discord.ui.Modal):
    def __init__(self, title, category):
        super().__init__(title=title)
        self.category = category

        self.add_item(discord.ui.TextInput(
            label="Minecraft IGN", required=True,
            placeholder="Your Minecraft username", max_length=16
        ))

        if category == "general":
            self.add_item(discord.ui.TextInput(label="How can we help?",
                                               style=discord.TextStyle.paragraph, required=True))
        elif category == "report":
            self.add_item(discord.ui.TextInput(label="Player you are reporting",
                                               required=True,
                                               placeholder="Their Minecraft username", max_length=16))
            self.add_item(discord.ui.TextInput(label="Evidence",
                                               style=discord.TextStyle.paragraph, required=True))
        elif category == "bug":
            self.add_item(discord.ui.TextInput(label="Describe the bug",
                                               style=discord.TextStyle.paragraph, required=True))
        elif category == "appeal":
            self.add_item(discord.ui.TextInput(label="Why should we unban you?",
                                               style=discord.TextStyle.paragraph, required=True))

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        user = interaction.user

        ign = self.children[0].value.strip()
        mc_face_url = f"https://mc-heads.net/avatar/{ign}/64"

        staff_role_id = None
        settings_path = f"{SETTINGS_DIR}/{guild.id}_settings.json"
        if os.path.exists(settings_path):
            with open(settings_path, "r") as f:
                data = json.load(f)
                staff_role_id = data.get("ticket_staff_role")

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            user: discord.PermissionOverwrite(read_messages=True, send_messages=True, attach_files=True)
        }

        staff_role = None
        if staff_role_id:
            staff_role = guild.get_role(staff_role_id)
            if staff_role:
                overwrites[staff_role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
                for role in guild.roles:
                    if role.position > staff_role.position:
                        overwrites[role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

        category_name = f"{self.category}-{user.name}"
        category_name = "".join(c for c in category_name if c.isalnum() or c in "-_").lower()

        try:
            category_channel = discord.utils.get(guild.categories, name="Tickets")
            if not category_channel:
                category_channel = await guild.create_category("Tickets")

            ticket_channel = await guild.create_text_channel(
                category_name, category=category_channel, overwrites=overwrites)
        except Exception as e:
            await interaction.followup.send(f"❌ Failed to create ticket: {e}", ephemeral=True)
            return

        embed = discord.Embed(title=f"New Ticket: {self.category.upper()}",
                              color=discord.Color.blue(), timestamp=discord.utils.utcnow())
        description = ""
        for child in self.children:
            description += f"**{child.label}**\n{child.value}\n\n"
        embed.description = description.strip()
        embed.set_thumbnail(url=mc_face_url)
        embed.set_footer(text=f"Ticket opened by {user.display_name}")

        ping_msg = f"Welcome {user.mention}!"
        if staff_role:
            ping_msg += f" {staff_role.mention}"

        cog = interaction.client.get_cog("Ticket")
        if cog:
            cog.save_active_ticket(ticket_channel.id, user.id, int(time.time()))

        view = TicketControlView()
        await ticket_channel.send(content=ping_msg, embed=embed, view=view)

        fields_text = "\n".join(f"{child.label}: {child.value}" for child in self.children)
        summary_text = await get_ai_summary(self.category, fields_text, self.children, server_name=guild.name)
        summary_embed = discord.Embed(
            title="🤖 AI Ticket Summary", description=summary_text,
            color=discord.Color.purple(), timestamp=discord.utils.utcnow()
        )
        summary_embed.set_footer(text=f"Powered by {guild.name} AI")
        await ticket_channel.send(embed=summary_embed)

        await ticket_channel.send(
            f"Hey {user.mention} pls be patient a staff will assist you soon!"
        )

        await interaction.followup.send(f"✅ Ticket created at {ticket_channel.mention}", ephemeral=True)


class CloseRequestView(discord.ui.View):
    def __init__(self, closer: discord.Member, reason: str, close_delay: int, cog):
        super().__init__(timeout=300)
        self.closer = closer
        self.reason = reason
        self.close_delay = close_delay
        self.cog = cog

    @discord.ui.button(label="✅ Confirm Close", style=discord.ButtonStyle.danger)
    async def confirm_close(self, interaction: discord.Interaction, button: discord.ui.Button):
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)

        if self.close_delay and self.close_delay > 0:
            await interaction.followup.send(
                f"🔒 Ticket will be closed in **{self.close_delay} hour(s)** if there is no response."
            )
            await asyncio.sleep(self.close_delay * 3600)

        if self.cog:
            await self.cog.log_ticket_close(interaction.channel, self.closer)

        await interaction.channel.send("🔒 Closing ticket in 5 seconds...")
        await asyncio.sleep(5)
        await interaction.channel.delete()

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_close(self, interaction: discord.Interaction, button: discord.ui.Button):
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)
        await interaction.followup.send("✅ Close request cancelled.", ephemeral=True)


class TicketControlView(discord.ui.View):
    """Persistent ticket control view — opener_id is read from disk at click time."""

    def __init__(self):
        super().__init__(timeout=None)

    def _get_opener_id(self, channel_id: int):
        path = f"{SETTINGS_DIR}/active_tickets.json"
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r") as f:
                data = json.load(f)
        except json.JSONDecodeError:
            return None
        info = data.get(str(channel_id))
        return info.get("opener_id") if info else None

    @discord.ui.button(label="Claim", style=discord.ButtonStyle.success,
                       emoji="🙋‍♂️", custom_id="radiummc_ticket_claim")
    async def claim_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        opener_id = self._get_opener_id(interaction.channel.id)
        if opener_id and interaction.user.id == opener_id:
            await interaction.response.send_message("❌ You cannot claim your own ticket!", ephemeral=True)
            return

        embed = discord.Embed(
            title="Claimed Ticket",
            description=f"Your ticket will be handled by {interaction.user.mention}",
            color=discord.Color.green())
        embed.set_footer(text=f"Powered by {interaction.guild.name}",
                         icon_url=interaction.guild.icon.url if interaction.guild.icon else None)

        button.disabled = True
        button.label = "Claimed"
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(embed=embed)

        cog = interaction.client.get_cog("Ticket")
        if cog:
            cog.update_ticket_claim(interaction.channel.id, interaction.user.id)
            cog.increment_claims(interaction.guild.id, interaction.user.id)

    @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.danger,
                       emoji="🔒", custom_id="radiummc_ticket_close")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("🔒 Ticket will close in 5 seconds...")

        cog = interaction.client.get_cog("Ticket")
        if cog:
            await cog.log_ticket_close(interaction.channel, interaction.user)

        await asyncio.sleep(5)
        try:
            await interaction.channel.delete()
        except (discord.NotFound, discord.Forbidden):
            pass


class Ticket(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.claims_file = f"{SETTINGS_DIR}/claims.json"
        self.active_tickets_file = f"{SETTINGS_DIR}/active_tickets.json"

        if not os.path.exists(self.claims_file):
            with open(self.claims_file, "w") as f:
                json.dump({}, f)
        if not os.path.exists(self.active_tickets_file):
            with open(self.active_tickets_file, "w") as f:
                json.dump({}, f)

        self.bot.add_view(TicketView())
        self.bot.add_view(TicketControlView())

    def load_json(self, path):
        try:
            with open(path, "r") as f:
                return json.load(f)
        except Exception:
            return {}

    def save_json(self, path, data):
        with open(path, "w") as f:
            json.dump(data, f, indent=4)

    def save_active_ticket(self, channel_id, opener_id, open_time):
        data = self.load_json(self.active_tickets_file)
        data[str(channel_id)] = {"opener_id": opener_id, "open_time": open_time, "claimed_by": None}
        self.save_json(self.active_tickets_file, data)

    def update_ticket_claim(self, channel_id, claimer_id):
        data = self.load_json(self.active_tickets_file)
        if str(channel_id) in data:
            data[str(channel_id)]["claimed_by"] = claimer_id
            self.save_json(self.active_tickets_file, data)

    def increment_claims(self, guild_id, user_id):
        data = self.load_json(self.claims_file)
        guild_key = str(guild_id)
        if guild_key not in data:
            data[guild_key] = {}
        user_key = str(user_id)
        if user_key not in data[guild_key]:
            data[guild_key][user_key] = 0
        data[guild_key][user_key] += 1
        self.save_json(self.claims_file, data)

    async def log_ticket_close(self, channel, closer):
        data = self.load_json(self.active_tickets_file)
        ticket_data = data.get(str(channel.id))

        opener = None
        open_time = datetime.now()
        claimer = None

        if ticket_data:
            opener = channel.guild.get_member(ticket_data.get("opener_id"))
            open_time = datetime.fromtimestamp(ticket_data.get("open_time"))
            claimer_id = ticket_data.get("claimed_by")
            if claimer_id:
                claimer = channel.guild.get_member(claimer_id)

        if str(channel.id) in data:
            del data[str(channel.id)]
            self.save_json(self.active_tickets_file, data)

        settings_path = f"{SETTINGS_DIR}/{channel.guild.id}_settings.json"
        if not os.path.exists(settings_path):
            return
        with open(settings_path, "r") as f:
            settings = json.load(f)
            log_channel_id = settings.get("ticket_log_channel")
        if not log_channel_id:
            return
        log_channel = channel.guild.get_channel(log_channel_id)
        if not log_channel:
            return

        embed = discord.Embed(title="Ticket Closed", color=discord.Color.green(),
                              timestamp=discord.utils.utcnow())
        ticket_id = str(channel.id)[-4:]
        embed.add_field(name="# Ticket ID", value=ticket_id, inline=True)
        opener_mention = opener.mention if opener else "Unknown"
        embed.add_field(name="✅ Opened By", value=opener_mention, inline=True)
        embed.add_field(name="🔒 Closed By", value=closer.mention, inline=True)
        time_str = open_time.strftime("%B %d, %Y at %I:%M %p")
        embed.add_field(name="🕒 Open Time", value=time_str, inline=True)
        claimed_text = claimer.mention if claimer else "Not claimed"
        embed.add_field(name="👤 Claimed By", value=claimed_text, inline=True)
        embed.add_field(name="❓ Reason", value="done", inline=True)

        await log_channel.send(embed=embed)

    @app_commands.command(name="sendpanel", description="Send the ticket panel to the current channel")
    async def sendpanel(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ You need Administrator permission.", ephemeral=True)
            return

        server_name = interaction.guild.name
        embed = discord.Embed(title=f"{server_name} Support Center", description="",
                              color=discord.Color.from_rgb(87, 198, 120))
        desc = f"""
📝 **General Support**
General Support is intended for all non-urgent and general inquiries. This includes questions about server features, payouts, rules, gameplay mechanics, or anything you're unsure about. If you're not certain which category applies to your situation, this is usually the best place to start.

⚔️ **Player Reports**
If you suspect a player of cheating, hacking, exploiting, or breaking server rules, you may open a Player Report ticket. To proceed with a report, you must provide clear and valid evidence, such as:
• A video clip
• A screenshot clearly showing the player's username

🛠️ **Bug Reports**
Have you discovered a bug, glitch, or unintended behavior on the server? Bug reports help us improve {server_name} and maintain a fair experience for everyone. When submitting a bug report, please include:
• A detailed explanation of the issue
• Steps to reproduce the bug (if possible)
• Screenshots or video evidence (recommended)

🔧 Our team will review the report and work on a fix as quickly as possible.

🔮 **Punishment Appeals**
If you believe a punishment was issued incorrectly or unfairly, you may submit a Punishment Appeal. Appeals must include strong and clear evidence proving that you did not violate the rules.

📌 **Appeal Guidelines:**
• Record the punishment as soon as it happens
• Only bans longer than 7 days are appealable (unless the punishment was false)
Please note that submitting an appeal does not guarantee removal of the punishment.

Thank you for playing on {server_name}! We appreciate your cooperation and aim to provide fair, fast, and reliable support for all players.
"""
        embed.description = desc.strip()
        if interaction.guild.icon:
            embed.set_thumbnail(url=interaction.guild.icon.url)

        await interaction.channel.send(embed=embed, view=TicketView())
        await interaction.response.send_message("✅ Panel sent!", ephemeral=True)

    @app_commands.command(name="select_ticket_role", description="Select the staff role for tickets")
    @app_commands.describe(role="The role that should handle tickets")
    async def select_ticket_role(self, interaction: discord.Interaction, role: discord.Role):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ You need Administrator permission.", ephemeral=True)
            return
        data = {}
        settings_path = f"{SETTINGS_DIR}/{interaction.guild.id}_settings.json"
        if os.path.exists(settings_path):
            with open(settings_path, "r") as f:
                data = json.load(f)
        data["ticket_staff_role"] = role.id
        with open(settings_path, "w") as f:
            json.dump(data, f, indent=4)
        await interaction.response.send_message(
            f"✅ Ticket staff role set to {role.mention}. This role and roles above it will see tickets.",
            ephemeral=True)

    @app_commands.command(name="ticketlogs", description="Set the channel for ticket logs")
    @app_commands.describe(channel="The channel where ticket logs should be sent")
    async def ticketlogs(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ You need Administrator permission.", ephemeral=True)
            return
        settings_path = f"{SETTINGS_DIR}/{interaction.guild.id}_settings.json"
        data = {}
        if os.path.exists(settings_path):
            with open(settings_path, "r") as f:
                data = json.load(f)
        data["ticket_log_channel"] = channel.id
        with open(settings_path, "w") as f:
            json.dump(data, f, indent=4)
        await interaction.response.send_message(
            f"✅ Ticket logs will be sent to {channel.mention}", ephemeral=True)

    @app_commands.command(name="tclaimscheck", description="Check how many tickets a user has claimed")
    @app_commands.describe(user="The user to check")
    async def tclaimscheck(self, interaction: discord.Interaction, user: discord.Member):
        data = self.load_json(self.claims_file)
        guild_data = data.get(str(interaction.guild.id), {})
        count = guild_data.get(str(user.id), 0)
        embed = discord.Embed(title="Claim Check", color=discord.Color.blurple())
        embed.description = f"{user.mention} has claimed **{count}** tickets."
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="claimsleaderboard", description="Show the top 10 ticket claimers")
    async def claimsleaderboard(self, interaction: discord.Interaction):
        data = self.load_json(self.claims_file)
        guild_data = data.get(str(interaction.guild.id), {})
        if not guild_data:
            await interaction.response.send_message("❌ No claims data found for this server.", ephemeral=True)
            return
        sorted_users = sorted(guild_data.items(), key=lambda x: x[1], reverse=True)[:10]
        embed = discord.Embed(title="🏆 Ticket Claims Leaderboard", color=discord.Color.gold())
        desc = ""
        for idx, (user_id, count) in enumerate(sorted_users, 1):
            user = interaction.guild.get_member(int(user_id))
            user_str = user.mention if user else f"<@{user_id}>"
            desc += f"**{idx}.** {user_str} → **{count}**\n"
        embed.description = desc
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="resetserverdata", description="Reset all server data for a fresh start")
    async def resetserverdata(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ You need Administrator permission.", ephemeral=True)
            return
        guild_id = str(interaction.guild.id)
        deleted = []
        settings_path = f"{SETTINGS_DIR}/{guild_id}_settings.json"
        if os.path.exists(settings_path):
            os.remove(settings_path)
            deleted.append("Server settings")
        claims_data = self.load_json(self.claims_file)
        if guild_id in claims_data:
            del claims_data[guild_id]
            self.save_json(self.claims_file, claims_data)
            deleted.append("Claims data")
        tickets_data = self.load_json(self.active_tickets_file)
        guild_channel_ids = [str(ch.id) for ch in interaction.guild.channels]
        removed_tickets = [k for k in tickets_data if k in guild_channel_ids]
        for k in removed_tickets:
            del tickets_data[k]
        if removed_tickets:
            self.save_json(self.active_tickets_file, tickets_data)
            deleted.append("Active ticket records")
        summary = "\n".join(f"✅ {item}" for item in deleted) if deleted else "ℹ️ No data found to reset."
        embed = discord.Embed(
            title="🗑️ Server Data Reset",
            description=f"The following data has been cleared:\n\n{summary}",
            color=discord.Color.red(), timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text=f"Reset by {interaction.user.name}")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="adduser", description="Add a user to the current ticket channel")
    @app_commands.describe(member="The member to add to this ticket")
    async def adduser(self, interaction: discord.Interaction, member: discord.Member):
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("❌ Use this inside a ticket channel.", ephemeral=True)
            return
        await channel.set_permissions(member, read_messages=True, send_messages=True, attach_files=True)
        await interaction.response.send_message(
            f"✅ {member.mention} has been added to this ticket.", ephemeral=False)

    @app_commands.command(name="removeuser", description="Remove a user from the current ticket channel")
    @app_commands.describe(member="The member to remove from this ticket")
    async def removeuser(self, interaction: discord.Interaction, member: discord.Member):
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("❌ Use this inside a ticket channel.", ephemeral=True)
            return
        await channel.set_permissions(member, overwrite=None)
        await interaction.response.send_message(
            f"✅ {member.mention} has been removed from this ticket.", ephemeral=False)

    @app_commands.command(name="closerequest",
                          description="Send a message asking the user to confirm the ticket can be closed")
    @app_commands.describe(reason="The reason the ticket was closed",
                           close_delay="Hours to close the ticket if the user does not respond")
    async def closerequest(self, interaction: discord.Interaction,
                           reason: str = "No reason provided", close_delay: int = 24):
        embed = discord.Embed(
            title="🔒 Close Request",
            description=(
                f"**{interaction.user.mention}** has requested to close this ticket.\n\n"
                f"**Reason:** {reason}\n"
                f"**Auto-close in:** {close_delay} hour(s) if no response."
            ),
            color=discord.Color.orange(), timestamp=discord.utils.utcnow()
        )
        view = CloseRequestView(closer=interaction.user, reason=reason,
                                close_delay=close_delay, cog=self)
        await interaction.response.send_message(embed=embed, view=view)

    @app_commands.command(name="escalate", description="Escalate this ticket to a specific staff role")
    @app_commands.describe(target="The staff role to escalate to", reason="Why you are escalating this ticket")
    async def escalate(self, interaction: discord.Interaction,
                       target: discord.Role, reason: str = "No reason provided"):
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("❌ Use this inside a ticket channel.", ephemeral=True)
            return
        await channel.set_permissions(target, read_messages=True, send_messages=True)
        embed = discord.Embed(
            title="⚠️ Ticket Escalated",
            description=(
                f"This ticket has been escalated to {target.mention} by {interaction.user.mention}.\n\n"
                f"**Reason:** {reason}"
            ),
            color=discord.Color.yellow(), timestamp=discord.utils.utcnow()
        )
        await interaction.response.send_message(content=target.mention, embed=embed)


# ============================================================
# AUTO-JOIN ROLE / WELCOME / INVITE ASSIGNMENT
# ============================================================

class AutoJoinRole(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.invites = {}

    def get_settings_path(self, guild_id):
        return f"{SETTINGS_DIR}/{guild_id}_settings.json"

    def get_invite_data_path(self, guild_id):
        return f"{SETTINGS_DIR}/{guild_id}_invites.json"

    def get_members_data_path(self, guild_id):
        return f"{SETTINGS_DIR}/{guild_id}_members.json"

    def save_setting(self, guild_id, key, value):
        path = self.get_settings_path(guild_id)
        data = {}
        if os.path.exists(path):
            with open(path, "r") as f:
                try:
                    data = json.load(f)
                except json.JSONDecodeError:
                    data = {}
        data[key] = value
        with open(path, "w") as f:
            json.dump(data, f, indent=4)

    def load_setting(self, guild_id, key):
        path = self.get_settings_path(guild_id)
        if os.path.exists(path):
            with open(path, "r") as f:
                try:
                    data = json.load(f)
                    return data.get(key)
                except json.JSONDecodeError:
                    return None
        return None

    def load_invite_counts(self, guild_id):
        path = self.get_invite_data_path(guild_id)
        if os.path.exists(path):
            with open(path, "r") as f:
                try:
                    return json.load(f)
                except json.JSONDecodeError:
                    return {}
        return {}

    def save_invite_counts(self, guild_id, data):
        path = self.get_invite_data_path(guild_id)
        with open(path, "w") as f:
            json.dump(data, f, indent=4)

    def load_members_data(self, guild_id):
        path = self.get_members_data_path(guild_id)
        if os.path.exists(path):
            with open(path, "r") as f:
                try:
                    return json.load(f)
                except json.JSONDecodeError:
                    return {}
        return {}

    def save_members_data(self, guild_id, data):
        path = self.get_members_data_path(guild_id)
        with open(path, "w") as f:
            json.dump(data, f, indent=4)

    @app_commands.command(name="join_role", description="Set a role to automatically give when someone joins")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def join_role(self, interaction: discord.Interaction, role: discord.Role):
        if role >= interaction.guild.me.top_role:
            await interaction.response.send_message(
                "That role is higher than my top role. Please choose a lower role.", ephemeral=True)
            return
        self.save_setting(interaction.guild.id, "join_role", role.id)
        await interaction.response.send_message(
            f"✅ Members who join will now receive the **{role.name}** role.", ephemeral=True)

    @app_commands.command(name="testwelcome",
                          description="Send a test welcome message to the configured welcome channel (Admin only)")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def testwelcome(self, interaction: discord.Interaction):
        guild = interaction.guild
        channel_id = self.load_setting(guild.id, "welcome_channel")
        if not channel_id:
            await interaction.response.send_message(
                "❌ No welcome channel set. Use `/logs type:welcome` first.", ephemeral=True)
            return
        channel = guild.get_channel(channel_id)
        if not channel:
            await interaction.response.send_message(
                "❌ Welcome channel not found. Please set a new one with `/logs type:welcome`.",
                ephemeral=True)
            return

        member = interaction.user
        description = f"Welcome to **{guild.name}**, {member.mention}!\nInvited by: Test User (this is a test)"
        embed = discord.Embed(title="🎉 Welcome!", description=description, color=discord.Color.green())
        embed.set_thumbnail(url=member.avatar.url if member.avatar else member.default_avatar.url)
        embed.set_footer(text=f"Member #{len(guild.members)} • Powered by {guild.name}",
                         icon_url=guild.icon.url if guild.icon else None)
        try:
            await channel.send(embed=embed)
            await interaction.response.send_message(
                f"✅ Test welcome message sent to {channel.mention}!", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message(
                f"❌ I don't have permission to send messages in {channel.mention}.", ephemeral=True)

    @app_commands.command(name="invites", description="Check how many people a user has invited")
    async def invites(self, interaction: discord.Interaction, user: discord.User = None):
        user = user or interaction.user
        invite_data = self.load_invite_counts(interaction.guild.id)
        joined = invite_data.get(str(user.id), {}).get("joined", 0)
        left = invite_data.get(str(user.id), {}).get("left", 0)
        fake = invite_data.get(str(user.id), {}).get("fake", 0)
        net_invites = joined - left - fake
        embed = discord.Embed(
            title=f"Invite Stats for {user.name}",
            description=(
                f"**Joined:** {joined}\n**Left:** {left}\n"
                f"**Fake Invites (accounts < 7 days):** {fake}\n**Net Invites:** {net_invites}"
            ),
            color=discord.Color.green()
        )
        embed.set_thumbnail(url=user.avatar.url if user.avatar else user.default_avatar.url)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="invite_leaderboard", description="See the top inviters in the server.")
    async def invite_leaderboard(self, interaction: discord.Interaction):
        invite_data = self.load_invite_counts(interaction.guild.id)
        leaderboard = []
        for user_id, stats in invite_data.items():
            joined = stats.get("joined", 0)
            left = stats.get("left", 0)
            net = joined - left
            leaderboard.append((user_id, joined, left, net))
        leaderboard.sort(key=lambda x: x[3], reverse=True)
        top_entries = leaderboard[:10]
        description = ""
        for i, (user_id, joined, left, net) in enumerate(top_entries, start=1):
            user = interaction.guild.get_member(int(user_id))
            name = user.mention if user else f"<@{user_id}>"
            description += f"**{i}.** {name} → **{net}** (joined: {joined}, left: {left})\n"
        if not description:
            description = "No invite data found."
        embed = discord.Embed(title="🏆 Invite Leaderboard", description=description, color=discord.Color.gold())
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="invitesreset",
                          description="Reset invite counts. Leave empty to reset all, or mention a user to reset one.")
    @app_commands.describe(user="(Optional) The user whose invites to reset.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def invitesreset(self, interaction: discord.Interaction, user: discord.User = None):
        guild_id = interaction.guild.id
        invite_data = self.load_invite_counts(guild_id)
        claims_path = f"invites_{guild_id}.json"

        if user is None:
            for uid in invite_data:
                invite_data[uid] = {"joined": 0, "left": 0, "fake": 0}
            self.save_invite_counts(guild_id, invite_data)
            if os.path.exists(claims_path):
                with open(claims_path, "w") as f:
                    json.dump({}, f, indent=4)
            await interaction.response.send_message("✅ All invite stats and claims have been reset.", ephemeral=True)
        else:
            uid = str(user.id)
            if uid in invite_data:
                invite_data[uid] = {"joined": 0, "left": 0, "fake": 0}
                self.save_invite_counts(guild_id, invite_data)
            if os.path.exists(claims_path):
                try:
                    with open(claims_path, "r") as f:
                        claims = json.load(f)
                    if uid in claims:
                        del claims[uid]
                        with open(claims_path, "w") as f:
                            json.dump(claims, f, indent=4)
                except json.JSONDecodeError:
                    pass
            await interaction.response.send_message(
                f"✅ Invite stats and claims reset for {user.mention}.", ephemeral=True)

    async def update_invites(self, guild: discord.Guild):
        try:
            self.invites[guild.id] = await guild.invites()
        except discord.Forbidden:
            self.invites[guild.id] = []

    @commands.Cog.listener()
    async def on_ready(self):
        for guild in self.bot.guilds:
            await self.update_invites(guild)

    @commands.Cog.listener()
    async def on_guild_join(self, guild):
        await self.update_invites(guild)

    @commands.Cog.listener()
    async def on_invite_create(self, invite):
        await self.update_invites(invite.guild)

    @commands.Cog.listener()
    async def on_invite_delete(self, invite):
        await self.update_invites(invite.guild)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        guild = member.guild
        inviter = "Unknown"
        inviter_id = None

        try:
            before = self.invites.get(guild.id, [])
            after = await guild.invites()
            for old in before:
                for new in after:
                    if old.code == new.code and new.uses > old.uses:
                        inviter = new.inviter.mention
                        inviter_id = new.inviter.id
                        break
        except Exception as e:
            print(f"Error checking invites: {e}")

        await self.update_invites(guild)

        members_data = self.load_members_data(guild.id)
        invite_data = self.load_invite_counts(guild.id)

        now = datetime.now(timezone.utc)
        account_age = now - member.created_at
        is_fake = account_age < timedelta(days=7)

        if inviter_id and str(member.id) not in members_data:
            if str(inviter_id) not in invite_data:
                invite_data[str(inviter_id)] = {"joined": 0, "left": 0, "fake": 0}
            if is_fake:
                invite_data[str(inviter_id)]["fake"] = invite_data[str(inviter_id)].get("fake", 0) + 1
            else:
                invite_data[str(inviter_id)]["joined"] += 1
            members_data[str(member.id)] = {"inviter_id": inviter_id, "fake": is_fake}
            self.save_invite_counts(guild.id, invite_data)

        role_id = self.load_setting(guild.id, "join_role")
        role = guild.get_role(role_id) if role_id else None
        if role:
            try:
                await member.add_roles(role, reason="Auto-assigned join role")
            except discord.Forbidden:
                print(f"Missing permissions to assign role {role.name} in {guild.name}")
            if str(member.id) in members_data:
                members_data[str(member.id)]["role"] = role.name

        self.save_members_data(guild.id, members_data)

        channel_id = self.load_setting(guild.id, "welcome_channel")
        if channel_id:
            channel = guild.get_channel(channel_id)
            if channel:
                description = f"Welcome to **{guild.name}**, {member.mention}!\nInvited by: {inviter}"
                if is_fake:
                    description += "\n⚠️ Account is new (< 7 days old) — counted as a fake invite."
                embed = discord.Embed(title="🎉 Welcome!", description=description, color=discord.Color.green())
                embed.set_thumbnail(url=member.avatar.url if member.avatar else member.default_avatar.url)
                embed.set_footer(text=f"Member #{len(guild.members)}")
                try:
                    await channel.send(embed=embed)
                except discord.Forbidden:
                    print(f"Missing permissions to send welcome message in {channel.name}")

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        guild = member.guild
        members_data = self.load_members_data(guild.id)
        invite_data = self.load_invite_counts(guild.id)

        member_info = members_data.pop(str(member.id), None)
        if member_info and "inviter_id" in member_info:
            inviter_id = member_info["inviter_id"]
            if str(inviter_id) not in invite_data:
                invite_data[str(inviter_id)] = {"joined": 0, "left": 0, "fake": 0}
            invite_data[str(inviter_id)]["left"] += 1
            self.save_invite_counts(guild.id, invite_data)

        self.save_members_data(guild.id, members_data)


# ============================================================
# INVITE TRACKER (sync + claim management)
# ============================================================

class InviteTracker(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def get_invite_file(self, guild_id: int) -> str:
        return f"invites_{guild_id}.json"

    def load_invite_data(self, guild_id: int):
        file = self.get_invite_file(guild_id)
        if os.path.exists(file):
            with open(file, "r") as f:
                return {int(k): v for k, v in json.load(f).items()}
        return {}

    def save_invite_data(self, guild_id: int, data: dict):
        file = self.get_invite_file(guild_id)
        with open(file, "w") as f:
            json.dump(data, f)

    def get_invites_data_path(self, guild_id):
        return f"{SETTINGS_DIR}/{guild_id}_invites.json"

    def load_server_invite_counts(self, guild_id):
        path = self.get_invites_data_path(guild_id)
        if os.path.exists(path):
            with open(path, "r") as f:
                try:
                    return json.load(f)
                except json.JSONDecodeError:
                    return {}
        return {}

    def save_server_invite_counts(self, guild_id, data):
        path = self.get_invites_data_path(guild_id)
        with open(path, "w") as f:
            json.dump(data, f, indent=4)

    @app_commands.command(name="syncinvites",
                          description="Sync historical invite data from current server invites (Admin only)")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def syncinvites(self, interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            invites = await interaction.guild.invites()
        except discord.Forbidden:
            await interaction.followup.send("❌ I don't have permission to view server invites.", ephemeral=True)
            return

        invite_data = self.load_server_invite_counts(interaction.guild.id)
        synced_users = set()

        for invite in invites:
            if not invite.inviter:
                continue
            uid = str(invite.inviter.id)
            if uid not in invite_data:
                invite_data[uid] = {"joined": 0, "left": 0, "fake": 0}
            invite_data[uid]["joined"] = max(invite_data[uid].get("joined", 0), invite.uses)
            synced_users.add(uid)

        self.save_server_invite_counts(interaction.guild.id, invite_data)

        try:
            cog = self.bot.get_cog("AutoJoinRole")
            if cog:
                await cog.update_invites(interaction.guild)
        except Exception as e:
            print(f"Error refreshing invite cache: {e}")

        embed = discord.Embed(
            title="✅ Invites Synced",
            description=f"Successfully synced historical invite data for **{len(synced_users)}** user(s) in **{interaction.guild.name}**.",
            color=discord.Color.green()
        )
        if interaction.guild.icon:
            embed.set_footer(text=f"Powered by {interaction.guild.name}", icon_url=interaction.guild.icon.url)
        else:
            embed.set_footer(text=f"Powered by {interaction.guild.name}")
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="claim_add", description="Add claims to a user")
    @app_commands.describe(user="Select the user", number="Number of claims to add")
    async def invite_add(self, interaction: discord.Interaction, user: discord.Member, number: int):
        guild_id = interaction.guild.id
        invite_data = self.load_invite_data(guild_id)
        invite_data[user.id] = invite_data.get(user.id, 0) + number
        self.save_invite_data(guild_id, invite_data)
        embed = discord.Embed(
            title="✅ Claims Added",
            description=f"{number} claimed invites added to {user.mention}.",
            color=discord.Color.green()
        )
        embed.add_field(name="Total Claims", value=str(invite_data[user.id]), inline=False)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="claim_remove", description="Remove claims from a user")
    @app_commands.describe(user="Select the user", number="Number of claims to remove")
    async def invite_remove(self, interaction: discord.Interaction, user: discord.Member, number: int):
        guild_id = interaction.guild.id
        invite_data = self.load_invite_data(guild_id)
        invite_data[user.id] = max(0, invite_data.get(user.id, 0) - number)
        self.save_invite_data(guild_id, invite_data)
        embed = discord.Embed(
            title="❌ Claims Removed",
            description=f"{number} claimed invites removed from {user.mention}.",
            color=discord.Color.red()
        )
        embed.add_field(name="Remaining claims", value=str(invite_data[user.id]), inline=False)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="claims_check", description="Check how many claimed invites a user has")
    @app_commands.describe(user="Select the user")
    async def invite_check(self, interaction: discord.Interaction, user: discord.Member):
        guild_id = interaction.guild.id
        invite_data = self.load_invite_data(guild_id)
        current = invite_data.get(user.id, 0)
        embed = discord.Embed(
            title="📊 Claim Check",
            description=f"{user.mention} has claimed **{current}** invites.",
            color=discord.Color.blurple()
        )
        await interaction.response.send_message(embed=embed)


# ============================================================
# LOCK / UNLOCK
# ============================================================

class LockUnlock(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def _get_staff_role(self, guild: discord.Guild):
        for role in guild.roles:
            if role.name.lower() == "staff team":
                return role
        return None

    @app_commands.command(name="lock", description="Lock the current channel for non-staff.")
    async def lock(self, interaction: Interaction):
        await interaction.response.defer(ephemeral=True)
        channel = interaction.channel
        guild = interaction.guild
        staff_role = await self._get_staff_role(guild)
        if not staff_role:
            await interaction.followup.send("❌ Couldn't find a role named **Staff Team**.")
            return
        overwrite = channel.overwrites_for(guild.default_role)
        overwrite.send_messages = False
        await channel.set_permissions(guild.default_role, overwrite=overwrite)
        await interaction.followup.send(f"🔒 Locked {channel.mention} for everyone except **{staff_role.name}**.")
        await channel.send("🔒 This channel has been locked by staff.")

    @app_commands.command(name="unlock", description="Unlock the current channel for everyone.")
    async def unlock(self, interaction: Interaction):
        await interaction.response.defer(ephemeral=True)
        channel = interaction.channel
        guild = interaction.guild
        overwrite = channel.overwrites_for(guild.default_role)
        overwrite.send_messages = None
        await channel.set_permissions(guild.default_role, overwrite=overwrite)
        await interaction.followup.send(f"🔓 Unlocked {channel.mention}.")
        await channel.send("🔓 This channel has been unlocked by staff.")


# ============================================================
# LOGS — sets log channels for moderation/welcome/staff/ticket
# ============================================================

class Logs(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="logs", description="Set the log channel for moderation, welcome, staff, tickets")
    @app_commands.describe(
        type="The type of log to set (moderation, welcome, staff, ticket)",
        channel="The channel where logs should be sent"
    )
    async def logs(self, interaction: Interaction, type: str, channel: discord.TextChannel):
        log_types = {
            "moderation": "modlog_channel",
            "welcome": "welcome_channel",
            "staff": "stafflog_channel",
            "ticket": "giveaway_log"
        }
        type = type.lower()
        if type not in log_types:
            await interaction.response.send_message(
                "❌ Invalid type. Choose from: moderation, welcome, staff, ticket.", ephemeral=True)
            return

        file_path = f"{SETTINGS_DIR}/{interaction.guild.id}_settings.json"
        if os.path.exists(file_path):
            with open(file_path, "r") as f:
                data = json.load(f)
        else:
            data = {}

        data[log_types[type]] = channel.id

        with open(file_path, "w") as f:
            json.dump(data, f, indent=4)

        await interaction.response.send_message(
            f"✅ {type.capitalize()} log channel set to {channel.mention}.", ephemeral=True)


# ============================================================
# MODERATION (mute/ban/unmute/unban)
# ============================================================

MUTE_REASONS = [
    app_commands.Choice(name='Spamming', value='spamming'),
    app_commands.Choice(name='Toxicity', value='toxicity'),
    app_commands.Choice(name='Racism', value='racism'),
    app_commands.Choice(name='Threatening', value='threatening'),
    app_commands.Choice(name='Advertising', value='advertising'),
]

BAN_REASONS = [
    app_commands.Choice(name='Ban Evading', value='ban_evading'),
    app_commands.Choice(name='Doxxing', value='doxxing'),
    app_commands.Choice(name='DDoS Attack', value='ddos_attack'),
    app_commands.Choice(name='Inappropriate Profile', value='inappropriate_profile'),
    app_commands.Choice(name='NSFW', value='nsfw'),
]

MUTE_DURATIONS = {
    'spamming': timedelta(minutes=15),
    'racism': timedelta(days=3),
    'toxicity': timedelta(minutes=30),
    'threatening': timedelta(days=7),
    'advertising': timedelta(days=1),
}


class Moderation(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def send_log(self, interaction, title, user, reason, proof=None):
        channel = get_moderation_log_channel(interaction.guild)
        if not channel:
            return
        embed = discord.Embed(title=title, color=discord.Color.red())
        embed.set_thumbnail(url=interaction.guild.icon.url if interaction.guild.icon else None)
        embed.add_field(name="User", value=f"{user.mention} ({user.id})", inline=False)
        embed.add_field(name="Moderator", value=interaction.user.mention, inline=False)
        embed.add_field(name="Reason", value=reason, inline=False)
        if proof:
            embed.set_image(url=proof.url)
        await channel.send(embed=embed)

    def record_punishment(self, guild_id, p_type, target_user, moderator, reason, duration=None):
        record = {
            "type": p_type,
            "discord_id": str(target_user.id),
            "discord_name": str(target_user),
            "display_name": getattr(target_user, "display_name", str(target_user)),
            "moderator_id": str(moderator.id),
            "moderator_name": str(moderator),
            "reason": reason,
            "duration": duration,
            "timestamp": int(time.time())
        }
        save_punishment(guild_id, record)

    @app_commands.command(name="mute", description="Mute a specific member")
    @app_commands.describe(member='Member to mute', reason='Reason for mute', proof='Proof attachment')
    @app_commands.choices(reason=MUTE_REASONS)
    async def mute(self, interaction: discord.Interaction, member: discord.Member,
                   reason: app_commands.Choice[str], proof: discord.Attachment):
        duration = MUTE_DURATIONS.get(reason.value)
        if not duration:
            await interaction.response.send_message("No duration configured for this reason.", ephemeral=True)
            return
        if member.top_role > interaction.user.top_role:
            await interaction.response.send_message(
                "You can't moderate members with higher roles than yours.", ephemeral=True)
            return
        if member.top_role >= interaction.guild.me.top_role:
            await interaction.response.send_message(
                "I can't mute this member because their top role is higher or equal to mine.", ephemeral=True)
            return
        try:
            await member.edit(timed_out_until=utcnow() + duration, reason=reason.name)
        except Exception as e:
            await interaction.response.send_message(f"Failed to mute member: {e}", ephemeral=True)
            return
        dur_str = str(duration)
        self.record_punishment(interaction.guild.id, "mute", member, interaction.user, reason.name, dur_str)
        await self.send_log(interaction, "🔇 Member Timed Out", member, reason.name, proof)
        await interaction.response.send_message(
            f"✅ {member.mention} has been muted for {dur_str} due to **{reason.name}**.", ephemeral=True)

    @app_commands.command(name="ban", description="Ban a specific user")
    @app_commands.describe(user='User to ban', reason='Reason for ban', proof='Proof attachment')
    @app_commands.choices(reason=BAN_REASONS)
    async def ban(self, interaction: discord.Interaction, user: discord.User,
                  reason: app_commands.Choice[str], proof: discord.Attachment):
        member = interaction.guild.get_member(user.id)
        if member:
            if member.top_role > interaction.user.top_role:
                await interaction.response.send_message(
                    "You can't ban members with higher roles than yours.", ephemeral=True)
                return
            if member.top_role >= interaction.guild.me.top_role:
                await interaction.response.send_message(
                    "I can't ban this member because their top role is higher or equal to mine.", ephemeral=True)
                return
        try:
            await interaction.guild.ban(user, reason=reason.name)
        except Exception as e:
            await interaction.response.send_message(f"Failed to ban user: {e}", ephemeral=True)
            return
        self.record_punishment(interaction.guild.id, "ban", user, interaction.user, reason.name)
        await self.send_log(interaction, "🔨 Member Banned", user, reason.name, proof)
        await interaction.response.send_message(
            f"✅ {user.mention} has been banned for **{reason.name}**.", ephemeral=True)

    @app_commands.command(name="unmute", description="Unmute a member currently muted")
    @app_commands.describe(member="Member to unmute", reason="Reason for unmute")
    async def unmute(self, interaction: discord.Interaction, member: discord.Member, reason: str):
        if not member.timed_out_until or member.timed_out_until < utcnow():
            await interaction.response.send_message("This member is not currently muted.", ephemeral=True)
            return
        if member.top_role > interaction.user.top_role:
            await interaction.response.send_message(
                "You can't unmute members with higher roles than yours.", ephemeral=True)
            return
        if member.top_role >= interaction.guild.me.top_role:
            await interaction.response.send_message(
                "I can't unmute this member because their top role is higher or equal to mine.", ephemeral=True)
            return
        try:
            await member.edit(timed_out_until=None, reason=reason)
        except Exception as e:
            await interaction.response.send_message(f"Failed to unmute member: {e}", ephemeral=True)
            return
        self.record_punishment(interaction.guild.id, "unmute", member, interaction.user, reason)
        await self.send_log(interaction, "🔊 Member Unmuted", member, reason)
        await interaction.response.send_message(f"✅ {member.mention} has been unmuted.", ephemeral=True)

    @app_commands.command(name="unban", description="Unban a user")
    @app_commands.describe(user="User to unban", reason="Reason for unban")
    async def unban(self, interaction: discord.Interaction, user: discord.User, reason: str):
        try:
            bans = await interaction.guild.bans()
            if discord.utils.find(lambda b: b.user.id == user.id, bans):
                await interaction.guild.unban(user, reason=reason)
            else:
                await interaction.response.send_message("❌ This user is not banned.", ephemeral=True)
                return
        except Exception as e:
            await interaction.response.send_message(f"Failed to unban user: {e}", ephemeral=True)
            return
        self.record_punishment(interaction.guild.id, "unban", user, interaction.user, reason)
        await self.send_log(interaction, "♻️ Member Unbanned", user, reason)
        await interaction.response.send_message(f"✅ {user.mention} has been unbanned.", ephemeral=True)


# ============================================================
# PING ROLE
# ============================================================

class PingRole(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="ping", description="Ping a specific alert role")
    @app_commands.describe(role="The role to ping")
    async def ping(self, interaction: discord.Interaction, role: str):
        if role not in PINGABLE_ROLES:
            return await interaction.response.send_message(
                embed=discord.Embed(description="❌ Invalid role selected.", color=discord.Color.red()),
                ephemeral=True
            )
        role_obj = discord.utils.get(interaction.guild.roles, name=role)
        if not role_obj:
            return await interaction.response.send_message(
                embed=discord.Embed(description=f"❌ Could not find the **{role}** role.", color=discord.Color.red()),
                ephemeral=True
            )

        if role == "Quickdrop Ping":
            content = (f"{role_obj.mention} Join up quick! "
                       f"{interaction.user.mention} is hosting a quickdrop! 🤑")
        elif role == "Giveaway Ping":
            content = (f"{role_obj.mention} Make sure to join! "
                       f"W {interaction.user.mention} for hosting a giveaway! 🎉")
        elif role == "Server Booster":
            content = (f"{role_obj.mention} Special giveaway! "
                       f"W {interaction.user.mention} for hosting a booster quickdrop! 🎉")

        await interaction.response.send_message(
            content=content, allowed_mentions=discord.AllowedMentions(roles=True))

    @ping.autocomplete('role')
    async def ping_autocomplete(self, interaction: discord.Interaction, current: str):
        return [app_commands.Choice(name=r, value=r)
                for r in PINGABLE_ROLES if r.lower().startswith(current.lower())]


# ============================================================
# SPLIT / STEAL
# ============================================================

class SplitStealView(discord.ui.View):
    def __init__(self, user1, user2, prize, host, guild_id):
        super().__init__(timeout=None)
        self.user1 = user1
        self.user2 = user2
        self.host = host
        self.choices = {user1.id: None, user2.id: None}
        self.prize = prize
        self.message = None
        self.guild_id = guild_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id not in [self.user1.id, self.user2.id]:
            await interaction.response.send_message("You aren't part of this game.", ephemeral=True)
            return False
        return True

    async def make_choice(self, interaction: discord.Interaction, choice: str):
        if self.choices[interaction.user.id] is not None:
            await interaction.response.send_message("You already decided!", ephemeral=True)
            return
        self.choices[interaction.user.id] = choice
        await interaction.response.send_message(f"You chose **{choice}**.", ephemeral=True)
        if all(self.choices.values()):
            await self.reveal_results()
        else:
            embed = self.create_waiting_embed()
            await self.message.edit(embed=embed, view=self)

    def create_waiting_embed(self):
        if self.choices[self.user1.id] is None and self.choices[self.user2.id] is None:
            waiting_text = "Waiting for both players to choose!"
        elif self.choices[self.user1.id] is None:
            waiting_text = f"Waiting for {self.user1.mention} to choose!"
        elif self.choices[self.user2.id] is None:
            waiting_text = f"Waiting for {self.user2.mention} to choose!"
        else:
            waiting_text = "Processing..."
        embed = discord.Embed(title="⚠️ Split or Steal ⚠️", color=discord.Color.orange())
        embed.description = (f"{waiting_text}\n--------------------------\n"
                             f"💰 **Prize:** {self.prize}")
        return embed

    async def reveal_results(self):
        u1c = self.choices[self.user1.id]
        u2c = self.choices[self.user2.id]
        embed = discord.Embed(title="⚠️ Results ⚠️", color=discord.Color.yellow())
        embed.add_field(name="", value=f"{self.user1.mention} chose **{u1c}**\n"
                                       f"{self.user2.mention} chose **{u2c}**", inline=False)
        embed.add_field(name="", value="--------------------------", inline=False)

        if u1c == "Split" and u2c == "Split":
            result = f"Both split {self.prize}"
            embed.add_field(name="", value=f"Both split **{self.prize}**!", inline=False)
        elif u1c == "Steal" and u2c == "Split":
            result = f"{self.user1.name} stole {self.prize}"
            embed.add_field(name="", value=f"{self.user1.mention} wins **{self.prize}**!", inline=False)
        elif u1c == "Split" and u2c == "Steal":
            result = f"{self.user2.name} stole {self.prize}"
            embed.add_field(name="", value=f"{self.user2.mention} wins **{self.prize}**!", inline=False)
        else:
            result = "Nobody won"
            embed.add_field(name="", value=f"No one wins **{self.prize}**!", inline=False)

        embed.set_footer(text=f"Giveaway hosted by @{self.host.name}")
        await self.message.channel.send(f"{self.user1.mention} and {self.user2.mention}, the game results are in!")
        await self.message.edit(embed=embed, view=self)
        await self.save_game(u1c, u2c, result)

    async def save_game(self, user1_choice, user2_choice, result):
        filepath = os.path.join(SPLITSTEAL_DIR, f"splitsteal_{self.guild_id}.json")
        game_data = {
            "user1_id": self.user1.id, "user2_id": self.user2.id,
            "host_id": self.host.id, "prize": self.prize,
            "user1_choice": user1_choice, "user2_choice": user2_choice,
            "result": result, "timestamp": datetime.utcnow().isoformat()
        }
        try:
            if os.path.exists(filepath):
                with open(filepath, 'r') as f:
                    data = json.load(f)
            else:
                data = []
            data.append(game_data)
            with open(filepath, 'w') as f:
                json.dump(data, f, indent=4)
        except Exception as e:
            print(f"[ERROR] Failed to save splitsteal game: {e}")

    @discord.ui.button(label="Split", style=discord.ButtonStyle.success)
    async def split_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.make_choice(interaction, "Split")

    @discord.ui.button(label="Steal", style=discord.ButtonStyle.danger)
    async def steal_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.make_choice(interaction, "Steal")


class SplitStealCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="split_steal", description="Start a Split or Steal game")
    @app_commands.describe(user1="First player", user2="Second player", prize="Prize")
    async def split_steal(self, interaction: discord.Interaction,
                          user1: discord.User, user2: discord.User, prize: str):
        view = SplitStealView(user1, user2, prize, host=interaction.user, guild_id=interaction.guild.id)
        embed = view.create_waiting_embed()
        await interaction.response.send_message(
            f"{user1.mention} and {user2.mention}, the game is starting!",
            embed=embed, view=view
        )
        view.message = await interaction.original_response()


# ============================================================
# STAFF ROLES — promote / demote
# ============================================================

PROMOTE_GREEN = 0x00FF7F
DEMOTE_RED = 0xFF0000


class StaffRoles(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def get_settings(self, guild_id: int) -> dict:
        path = f"{SETTINGS_DIR}/{guild_id}_settings.json"
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r") as f:
                return json.load(f)
        except json.JSONDecodeError:
            return {}

    async def send_stafflog(self, guild: discord.Guild, embed: discord.Embed):
        settings = self.get_settings(guild.id)
        chan_id = settings.get("stafflog_channel")
        if not chan_id:
            return
        channel = guild.get_channel(int(chan_id))
        if channel:
            try:
                await channel.send(embed=embed)
            except (discord.Forbidden, discord.HTTPException):
                pass

    @app_commands.command(name="promote", description="Promote a user and log to staff channel")
    @app_commands.describe(user="The user to promote", role="The role to give", reason="Reason for promotion")
    async def promote(self, interaction: discord.Interaction,
                      user: discord.Member, role: discord.Role, reason: str = "No reason provided"):
        try:
            await user.add_roles(role)
            arrow_up = discord.utils.get(interaction.guild.emojis, name="arrowup")
            arrow_up_str = str(arrow_up) if arrow_up else "⬆️"

            embed = discord.Embed(
                title=f"{arrow_up_str} User Promoted",
                description=f"{user.mention} has been promoted to {role.mention}",
                color=PROMOTE_GREEN
            )
            await interaction.response.send_message(embed=embed)

            log_embed = discord.Embed(
                title=f"{arrow_up_str} Staff Promotion {arrow_up_str}",
                description=(
                    f"{user.mention} Has Been **PROMOTED** to {role.mention}\n\n"
                    f"Promoted By: {interaction.user.mention}"
                ),
                color=PROMOTE_GREEN, timestamp=datetime.now(timezone.utc)
            )
            log_embed.set_thumbnail(url=user.display_avatar.url)
            log_embed.set_footer(
                text=f"Updated by {interaction.user.display_name} • {datetime.now().strftime('%m/%d/%y, %I:%M %p')}",
                icon_url=interaction.user.display_avatar.url
            )
            await self.send_stafflog(interaction.guild, log_embed)
        except Exception as e:
            print(f"Promote error: {e}")
            try:
                await interaction.response.send_message("An error occurred during promotion.", ephemeral=True)
            except discord.InteractionResponded:
                pass

    @app_commands.command(name="demote", description="Demote a user and log to staff channel")
    @app_commands.describe(user="The user to demote", role="The role to demote to", reason="Reason for demotion")
    async def demote(self, interaction: discord.Interaction,
                     user: discord.Member, role: discord.Role, reason: str = "No reason provided"):
        try:
            user_roles = [r for r in user.roles if not r.is_default()]
            target_pos = role.position
            roles_to_remove = [r for r in user_roles if r.position > target_pos]
            if roles_to_remove:
                await user.remove_roles(*roles_to_remove)
            if role not in user.roles:
                await user.add_roles(role)

            arrow_down = discord.utils.get(interaction.guild.emojis, name="arrowdown")
            arrow_down_str = str(arrow_down) if arrow_down else "⬇️"

            embed = discord.Embed(
                title=f"{arrow_down_str} User Demoted",
                description=f"{user.mention} has been demoted to {role.mention}",
                color=DEMOTE_RED
            )
            await interaction.response.send_message(embed=embed)

            log_embed = discord.Embed(
                title=f"{arrow_down_str} Staff Demotion {arrow_down_str}",
                description=(
                    f"{user.mention} Has Been **DEMOTED** to {role.mention}\n\n"
                    f"Demoted By: {interaction.user.mention}"
                ),
                color=DEMOTE_RED, timestamp=datetime.now(timezone.utc)
            )
            log_embed.set_thumbnail(url=user.display_avatar.url)
            log_embed.set_footer(
                text=f"Updated by {interaction.user.display_name} • {datetime.now().strftime('%m/%d/%y, %I:%M %p')}",
                icon_url=interaction.user.display_avatar.url
            )
            await self.send_stafflog(interaction.guild, log_embed)
        except Exception as e:
            print(f"Demote error: {e}")
            try:
                await interaction.response.send_message("An error occurred during demotion.", ephemeral=True)
            except discord.InteractionResponded:
                pass


# ============================================================
# STAFF (linkstaff, stafflist, activity, history, sendall, etc.)
# ============================================================

class SendAllModal(discord.ui.Modal, title="Send DM to All Members"):
    message = discord.ui.TextInput(
        label="Message to send", style=discord.TextStyle.paragraph,
        required=True, placeholder="Type the announcement message here..."
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        success = 0
        failed = 0
        for member in guild.members:
            if member.bot:
                continue
            try:
                await member.send(self.message.value)
                success += 1
            except Exception:
                failed += 1
        embed = discord.Embed(
            title="📨 DM Blast Complete",
            description=(
                f"✅ Sent to **{success}** members\n"
                f"❌ Failed for **{failed}** members (DMs closed)"
            ),
            color=discord.Color.green(), timestamp=discord.utils.utcnow()
        )
        await interaction.followup.send(embed=embed, ephemeral=True)


class Staff(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.staff_links_file = f"{SETTINGS_DIR}/staff_links.json"
        self.activity_file = f"{SETTINGS_DIR}/activity_log.json"
        if not os.path.exists(self.staff_links_file):
            with open(self.staff_links_file, "w") as f:
                json.dump({}, f)
        if not os.path.exists(self.activity_file):
            with open(self.activity_file, "w") as f:
                json.dump({}, f)

    def load_json(self, path):
        try:
            with open(path, "r") as f:
                return json.load(f)
        except Exception:
            return {}

    def save_json(self, path, data):
        with open(path, "w") as f:
            json.dump(data, f, indent=4)

    def get_guild_links(self, guild_id):
        data = self.load_json(self.staff_links_file)
        return data.get(str(guild_id), {})

    def save_guild_links(self, guild_id, links):
        data = self.load_json(self.staff_links_file)
        data[str(guild_id)] = links
        self.save_json(self.staff_links_file, data)

    def log_activity(self, guild_id, user_id, action: str):
        data = self.load_json(self.activity_file)
        gid = str(guild_id)
        uid = str(user_id)
        if gid not in data:
            data[gid] = {}
        if uid not in data[gid]:
            data[gid][uid] = []
        data[gid][uid].append({"action": action, "ts": int(time.time())})
        data[gid][uid] = data[gid][uid][-200:]
        self.save_json(self.activity_file, data)

    @app_commands.command(name="linkstaff",
                          description="Link a staff member's Discord account to their Minecraft IGN")
    @app_commands.describe(member="The staff member to link", ign="Their Minecraft IGN")
    async def linkstaff(self, interaction: discord.Interaction, member: discord.Member, ign: str):
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("❌ You need Manage Server permission.", ephemeral=True)
            return
        links = self.get_guild_links(interaction.guild.id)
        links[str(member.id)] = ign
        self.save_guild_links(interaction.guild.id, links)
        embed = discord.Embed(
            title="🔗 Staff Linked",
            description=f"{member.mention} has been linked to Minecraft IGN **{ign}**.",
            color=discord.Color.green(), timestamp=discord.utils.utcnow()
        )
        embed.set_thumbnail(url=f"https://mc-heads.net/avatar/{ign}/64")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="unlinkstaff", description="Remove a staff member's Minecraft IGN link")
    @app_commands.describe(
        member="The Discord member to unlink (if they're still in the server)",
        user_id="The Discord user ID to unlink (if they've left the server)")
    async def unlinkstaff(self, interaction: discord.Interaction,
                          member: discord.Member = None, user_id: str = None):
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("❌ You need Manage Server permission.", ephemeral=True)
            return
        if not member and not user_id:
            await interaction.response.send_message("❌ Provide either a member or a user ID.", ephemeral=True)
            return
        target_id = str(member.id) if member else user_id
        links = self.get_guild_links(interaction.guild.id)
        if target_id not in links:
            await interaction.response.send_message("❌ That user has no linked IGN.", ephemeral=True)
            return
        removed_ign = links.pop(target_id)
        self.save_guild_links(interaction.guild.id, links)
        tag = member.mention if member else f"<@{target_id}>"
        embed = discord.Embed(
            title="🔓 Staff Unlinked",
            description=f"{tag} has been unlinked from IGN **{removed_ign}**.",
            color=discord.Color.orange(), timestamp=discord.utils.utcnow()
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="stafflist",
                          description="Show all linked staff members and their Minecraft IGNs")
    async def stafflist(self, interaction: discord.Interaction):
        links = self.get_guild_links(interaction.guild.id)
        if not links:
            await interaction.response.send_message(
                "❌ No staff IGN links found for this server.", ephemeral=True)
            return
        await interaction.response.defer()
        items = list(links.items())
        total = len(items)

        header = discord.Embed(
            title="👥 Staff List — Linked IGNs",
            description=f"**{total}** staff member(s) linked",
            color=discord.Color.green(), timestamp=discord.utils.utcnow()
        )

        batch_size = 9
        batches = [items[i:i + batch_size] for i in range(0, len(items), batch_size)]
        for batch_index, batch in enumerate(batches):
            embeds = [header] if batch_index == 0 else []
            for idx, (uid, ign) in enumerate(batch, start=batch_index * batch_size + 1):
                member = interaction.guild.get_member(int(uid))
                tag = member.mention if member else f"<@{uid}>"
                e = discord.Embed(
                    description=f"**{idx}.** {tag} → **{ign}**",
                    color=discord.Color.green()
                )
                e.set_thumbnail(url=f"https://mc-heads.net/avatar/{ign}/64")
                embeds.append(e)
            await interaction.followup.send(embeds=embeds)

    @app_commands.command(name="activity",
                          description="Show staff activity stats for the last 7 days")
    @app_commands.describe(user="(Optional) Filter to a specific staff member")
    async def activity(self, interaction: discord.Interaction, user: discord.Member = None):
        data = self.load_json(self.activity_file)
        guild_data = data.get(str(interaction.guild.id), {})
        claims_data = {}
        claims_path = f"{SETTINGS_DIR}/claims.json"
        if os.path.exists(claims_path):
            try:
                with open(claims_path, "r") as f:
                    all_claims = json.load(f)
                claims_data = all_claims.get(str(interaction.guild.id), {})
            except Exception:
                pass
        cutoff = time.time() - (7 * 24 * 3600)
        embed = discord.Embed(
            title="📊 Staff Activity — Last 7 Days",
            color=discord.Color.green(), timestamp=discord.utils.utcnow()
        )
        if user:
            uid = str(user.id)
            actions = guild_data.get(uid, [])
            recent = [a for a in actions if a.get("ts", 0) >= cutoff]
            ticket_claims = claims_data.get(uid, 0)
            embed.description = (
                f"**{user.mention}**\n"
                f"🎫 Tickets Claimed: **{ticket_claims}**\n"
                f"⚡ Actions (7d): **{len(recent)}**"
            )
            embed.set_thumbnail(url=user.display_avatar.url)
        else:
            lines = []
            all_ids = set(guild_data.keys()) | set(claims_data.keys())
            for uid in all_ids:
                actions = guild_data.get(uid, [])
                recent = [a for a in actions if a.get("ts", 0) >= cutoff]
                claims = claims_data.get(uid, 0)
                if claims == 0 and len(recent) == 0:
                    continue
                member = interaction.guild.get_member(int(uid))
                tag = member.mention if member else f"<@{uid}>"
                lines.append((claims, tag, len(recent)))
            lines.sort(key=lambda x: x[0], reverse=True)
            if lines:
                desc = ""
                for claims, tag, actions_count in lines[:15]:
                    desc += f"{tag} → 🎫 **{claims}** claims · ⚡ **{actions_count}** actions\n"
                embed.description = desc
            else:
                embed.description = "No staff activity found in the last 7 days."
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="quotacheck",
                          description="Check ticket and activity stats for a staff member")
    @app_commands.describe(user="The staff member to check")
    async def quotacheck(self, interaction: discord.Interaction, user: discord.Member):
        claims_path = f"{SETTINGS_DIR}/claims.json"
        claims = 0
        if os.path.exists(claims_path):
            try:
                with open(claims_path, "r") as f:
                    all_claims = json.load(f)
                claims = all_claims.get(str(interaction.guild.id), {}).get(str(user.id), 0)
            except Exception:
                pass
        activity_data = self.load_json(self.activity_file)
        user_actions = activity_data.get(str(interaction.guild.id), {}).get(str(user.id), [])
        cutoff = time.time() - (7 * 24 * 3600)
        recent_actions = [a for a in user_actions if a.get("ts", 0) >= cutoff]
        links = self.get_guild_links(interaction.guild.id)
        ign = links.get(str(user.id), "Not linked")
        embed = discord.Embed(
            title=f"📋 Quota Check — {user.display_name}",
            color=discord.Color.green(), timestamp=discord.utils.utcnow()
        )
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.add_field(name="🎫 Tickets Claimed (all time)", value=str(claims), inline=True)
        embed.add_field(name="⚡ Actions (last 7d)", value=str(len(recent_actions)), inline=True)
        embed.add_field(name="🎮 Minecraft IGN", value=ign, inline=True)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="history", description="View a player's punishment history")
    @app_commands.describe(ign="The Minecraft IGN or Discord username to look up")
    async def history(self, interaction: discord.Interaction, ign: str):
        guild_id = interaction.guild.id
        ign_lower = ign.lower()
        links = self.get_guild_links(guild_id)
        matched_discord_id = None
        for uid, linked_ign in links.items():
            if linked_ign.lower() == ign_lower:
                matched_discord_id = uid
                break

        punishments_path = f"{SETTINGS_DIR}/{guild_id}_punishments.json"
        all_records = []
        if os.path.exists(punishments_path):
            try:
                with open(punishments_path, "r") as f:
                    all_records = json.load(f)
            except Exception:
                all_records = []

        matched = []
        for rec in all_records:
            if matched_discord_id and rec.get("discord_id") == matched_discord_id:
                matched.append(rec)
            elif not matched_discord_id:
                d_name = rec.get("discord_name", "").lower()
                d_display = rec.get("display_name", "").lower()
                if ign_lower in d_name or ign_lower in d_display:
                    matched.append(rec)

        embed = discord.Embed(
            title=f"📜 Punishment History — {ign}",
            color=discord.Color.red() if matched else discord.Color.green(),
            timestamp=discord.utils.utcnow()
        )
        embed.set_thumbnail(url=f"https://mc-heads.net/avatar/{ign}/64")

        if not matched:
            embed.description = f"✅ No punishment records found for **{ign}**."
        else:
            type_emoji = {"mute": "🔇", "ban": "🔨", "unmute": "🔊", "unban": "♻️"}
            lines = []
            for rec in reversed(matched[-20:]):
                ts = rec.get("timestamp", 0)
                date_str = datetime.fromtimestamp(ts).strftime("%d %b %Y") if ts else "Unknown"
                p_type = rec.get("type", "?").upper()
                emoji = type_emoji.get(rec.get("type", ""), "⚠️")
                reason = rec.get("reason", "No reason")
                duration = rec.get("duration")
                mod = rec.get("moderator_name", "Unknown")
                dur_text = f" `({duration})`" if duration else ""
                lines.append(
                    f"{emoji} **{p_type}**{dur_text} — {reason}\n"
                    f"  `By {mod} on {date_str}`"
                )
            embed.description = "\n\n".join(lines)
            embed.set_footer(text=f"Total records: {len(matched)}")

        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="sendall",
                          description="Send an announcement to all server members via DM")
    async def sendall(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ You need Administrator permission.", ephemeral=True)
            return
        await interaction.response.send_modal(SendAllModal())


# ============================================================
# MOD LOGS — green-embed listeners
# ============================================================

class ModLogs(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def get_settings_path(self, guild_id: int) -> str:
        return f"{SETTINGS_DIR}/{guild_id}_settings.json"

    def get_modlog_channel(self, guild: discord.Guild):
        if guild is None:
            return None
        path = self.get_settings_path(guild.id)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r") as f:
                data = json.load(f)
        except json.JSONDecodeError:
            return None
        chan_id = data.get("modlog_channel")
        if not chan_id:
            return None
        return guild.get_channel(int(chan_id))

    def base_embed(self, guild, title, description):
        embed = discord.Embed(
            title=title, description=description,
            color=discord.Color.green(), timestamp=datetime.now(timezone.utc)
        )
        if guild and guild.icon:
            embed.set_footer(text=f"Powered by {guild.name}", icon_url=guild.icon.url)
        elif guild:
            embed.set_footer(text=f"Powered by {guild.name}")
        return embed

    async def safe_send(self, channel, embed):
        if not channel:
            return
        try:
            await channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException) as e:
            print(f"[modlogs] Failed to send: {e}")

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        try:
            channel = self.get_modlog_channel(member.guild)
            if not channel:
                return
            account_age = datetime.now(timezone.utc) - member.created_at
            embed = self.base_embed(
                member.guild, "📥 Member Joined",
                f"{member.mention} **{member.display_name}**\n\n"
                f"**Account Age:** {account_age.days} days\n**ID:** `{member.id}`",
            )
            embed.set_author(name=member.display_name, icon_url=member.display_avatar.url)
            embed.set_thumbnail(url=member.display_avatar.url)
            await self.safe_send(channel, embed)
        except Exception as e:
            print(f"[modlogs] on_member_join error: {e}")

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        try:
            channel = self.get_modlog_channel(member.guild)
            if not channel:
                return
            embed = self.base_embed(
                member.guild, "📤 Member Left",
                f"{member.mention} **{member.display_name}**\n\n**ID:** `{member.id}`",
            )
            embed.set_author(name=member.display_name, icon_url=member.display_avatar.url)
            embed.set_thumbnail(url=member.display_avatar.url)
            await self.safe_send(channel, embed)
        except Exception as e:
            print(f"[modlogs] on_member_remove error: {e}")

    @commands.Cog.listener()
    async def on_member_update(self, before, after):
        try:
            if before.roles == after.roles:
                return
            channel = self.get_modlog_channel(after.guild)
            if not channel:
                return
            added = [r for r in after.roles if r not in before.roles]
            removed = [r for r in before.roles if r not in after.roles]
            desc = f"{after.mention} **{after.display_name}**\n\n"
            if added:
                desc += f"**Roles Added:** {', '.join(r.mention for r in added)}\n"
            if removed:
                desc += f"**Roles Removed:** {', '.join(r.mention for r in removed)}\n"
            desc += f"\n**ID:** `{after.id}`"
            embed = self.base_embed(after.guild, "🔧 Member Updated", desc)
            embed.set_author(name=after.display_name, icon_url=after.display_avatar.url)
            await self.safe_send(channel, embed)
        except Exception as e:
            print(f"[modlogs] on_member_update error: {e}")

    @commands.Cog.listener()
    async def on_member_ban(self, guild, user):
        try:
            channel = self.get_modlog_channel(guild)
            if not channel:
                return
            embed = self.base_embed(
                guild, "🔨 Member Banned",
                f"{user.mention} **{user.name}**\n\n**ID:** `{user.id}`",
            )
            embed.set_author(name=user.name, icon_url=user.display_avatar.url)
            embed.set_thumbnail(url=user.display_avatar.url)
            await self.safe_send(channel, embed)
        except Exception as e:
            print(f"[modlogs] on_member_ban error: {e}")

    @commands.Cog.listener()
    async def on_member_unban(self, guild, user):
        try:
            channel = self.get_modlog_channel(guild)
            if not channel:
                return
            embed = self.base_embed(
                guild, "♻️ Member Unbanned",
                f"{user.mention} **{user.name}**\n\n**ID:** `{user.id}`",
            )
            embed.set_author(name=user.name, icon_url=user.display_avatar.url)
            embed.set_thumbnail(url=user.display_avatar.url)
            await self.safe_send(channel, embed)
        except Exception as e:
            print(f"[modlogs] on_member_unban error: {e}")

    @commands.Cog.listener()
    async def on_message_delete(self, message):
        try:
            if not message.guild or message.author.bot:
                return
            channel = self.get_modlog_channel(message.guild)
            if not channel:
                return
            content = message.content[:1000] if message.content else "*No text content*"
            embed = self.base_embed(
                message.guild, "🗑️ Message Deleted",
                f"**Author:** {message.author.mention} **{message.author.display_name}**\n"
                f"**Channel:** {message.channel.mention}\n\n"
                f"**Content:** {content}\n\n"
                f"**Message ID:** `{message.id}`",
            )
            embed.set_author(name=message.author.display_name, icon_url=message.author.display_avatar.url)
            await self.safe_send(channel, embed)
        except Exception as e:
            print(f"[modlogs] on_message_delete error: {e}")

    @commands.Cog.listener()
    async def on_message_edit(self, before, after):
        try:
            if not before.guild or before.author.bot:
                return
            if before.content == after.content:
                return
            channel = self.get_modlog_channel(before.guild)
            if not channel:
                return
            old = before.content[:500] if before.content else "*No text*"
            new = after.content[:500] if after.content else "*No text*"
            embed = self.base_embed(
                before.guild, "✏️ Message Edited",
                f"**Author:** {before.author.mention} **{before.author.display_name}**\n"
                f"**Channel:** {before.channel.mention}\n\n"
                f"**Before:** {old}\n**After:** {new}\n\n"
                f"[Jump to message]({after.jump_url})",
            )
            embed.set_author(name=before.author.display_name, icon_url=before.author.display_avatar.url)
            await self.safe_send(channel, embed)
        except Exception as e:
            print(f"[modlogs] on_message_edit error: {e}")

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel):
        try:
            log_chan = self.get_modlog_channel(channel.guild)
            if not log_chan:
                return
            embed = self.base_embed(
                channel.guild, "📺 Channel Created",
                f"**Channel:** {channel.mention}\n**Name:** `{channel.name}`\n"
                f"**Type:** `{channel.type}`\n\n**ID:** `{channel.id}`",
            )
            await self.safe_send(log_chan, embed)
        except Exception as e:
            print(f"[modlogs] on_guild_channel_create error: {e}")

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel):
        try:
            log_chan = self.get_modlog_channel(channel.guild)
            if not log_chan:
                return
            embed = self.base_embed(
                channel.guild, "📺 Channel Deleted",
                f"**Name:** `#{channel.name}`\n**Type:** `{channel.type}`\n\n**ID:** `{channel.id}`",
            )
            await self.safe_send(log_chan, embed)
        except Exception as e:
            print(f"[modlogs] on_guild_channel_delete error: {e}")

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before, after):
        try:
            log_chan = self.get_modlog_channel(after.guild)
            if not log_chan:
                return
            changes = []
            if before.name != after.name:
                changes.append(f"**Name:** `#{before.name}` → `#{after.name}`")
            if not changes:
                return
            embed = self.base_embed(
                after.guild, "📝 Channel Updated",
                f"**Channel:** {after.mention}\n\n" + "\n".join(changes) + f"\n\n**ID:** `{after.id}`",
            )
            await self.safe_send(log_chan, embed)
        except Exception as e:
            print(f"[modlogs] on_guild_channel_update error: {e}")

    @commands.Cog.listener()
    async def on_guild_role_create(self, role):
        try:
            log_chan = self.get_modlog_channel(role.guild)
            if not log_chan:
                return
            embed = self.base_embed(
                role.guild, "🆕 Role Created",
                f"**Role:** {role.mention}\n**Name:** `{role.name}`\n\n**ID:** `{role.id}`",
            )
            await self.safe_send(log_chan, embed)
        except Exception as e:
            print(f"[modlogs] on_guild_role_create error: {e}")

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role):
        try:
            log_chan = self.get_modlog_channel(role.guild)
            if not log_chan:
                return
            embed = self.base_embed(
                role.guild, "🗑️ Role Deleted",
                f"**Name:** `{role.name}`\n\n**ID:** `{role.id}`",
            )
            await self.safe_send(log_chan, embed)
        except Exception as e:
            print(f"[modlogs] on_guild_role_delete error: {e}")


# ============================================================
# SERVER IP — register / removeip / on_message auto-reply
# ============================================================

class ServerIP(commands.Cog):
    IP_PATTERN = re.compile(r"\bip\b", re.IGNORECASE)

    def __init__(self, bot):
        self.bot = bot

    def get_settings_path(self, guild_id: int) -> str:
        return f"{SETTINGS_DIR}/{guild_id}_settings.json"

    def save_setting(self, guild_id: int, key: str, value):
        path = self.get_settings_path(guild_id)
        data = {}
        if os.path.exists(path):
            try:
                with open(path, "r") as f:
                    data = json.load(f)
            except json.JSONDecodeError:
                data = {}
        data[key] = value
        with open(path, "w") as f:
            json.dump(data, f, indent=4)

    def load_setting(self, guild_id: int, key: str):
        path = self.get_settings_path(guild_id)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r") as f:
                data = json.load(f)
        except json.JSONDecodeError:
            return None
        return data.get(key)

    @app_commands.command(name="registerip",
                          description="Register the Minecraft server IP for auto-reply (Admin only)")
    @app_commands.describe(ip="The server IP address (e.g. play.radiummc.net)")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def registerip(self, interaction: discord.Interaction, ip: str):
        ip = ip.strip()
        if not ip:
            await interaction.response.send_message("❌ IP cannot be empty.", ephemeral=True)
            return
        self.save_setting(interaction.guild.id, "server_ip", ip)
        embed = discord.Embed(
            title="✅ Server IP Registered",
            description=f"The server IP has been set to **`{ip}`**.\n\nWhenever someone asks for the IP, I'll reply automatically.",
            color=discord.Color.green()
        )
        if interaction.guild.icon:
            embed.set_footer(text=f"Powered by {interaction.guild.name}", icon_url=interaction.guild.icon.url)
        else:
            embed.set_footer(text=f"Powered by {interaction.guild.name}")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="removeip", description="Remove the registered server IP (Admin only)")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def removeip(self, interaction: discord.Interaction):
        path = self.get_settings_path(interaction.guild.id)
        if os.path.exists(path):
            try:
                with open(path, "r") as f:
                    data = json.load(f)
                if "server_ip" in data:
                    del data["server_ip"]
                    with open(path, "w") as f:
                        json.dump(data, f, indent=4)
            except json.JSONDecodeError:
                pass
        await interaction.response.send_message("✅ Server IP removed.", ephemeral=True)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        content = message.content.strip()
        if not content:
            return
        if not self.IP_PATTERN.search(content):
            return
        ip = self.load_setting(message.guild.id, "server_ip")
        if not ip:
            return
        try:
            await message.reply(f"The IP Of The Server Is - `{ip}`", mention_author=False)
        except (discord.Forbidden, discord.HTTPException) as e:
            print(f"[serverip] Failed to reply in #{message.channel}: {e}")


# ============================================================
# BOT SETUP & ENTRY POINT
# ============================================================

intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

synced = False


@bot.event
async def on_ready():
    global synced
    if not synced:
        try:
            cmds = await bot.tree.sync()
            print(f"🌍 Synced {len(cmds)} global slash commands.")
        except Exception as e:
            print(f"❌ Failed to sync commands: {e}")
        synced = True
    print(f"🚀 Bot is ready. Logged in as {bot.user}")


@bot.event
async def on_error(event, *args, **kwargs):
    print(f"Bot error in {event}: {args}")


async def setup_cogs():
    cogs = [
        Giveaway, Purge, MemberCount, Ticket, AutoJoinRole, InviteTracker,
        LockUnlock, Logs, Moderation, PingRole, SplitStealCog, StaffRoles,
        Staff, ModLogs, ServerIP,
    ]
    for CogClass in cogs:
        try:
            await bot.add_cog(CogClass(bot))
            print(f"✅ Loaded {CogClass.__name__}")
        except Exception as e:
            print(f"❌ Error loading {CogClass.__name__}: {e}")


async def main():
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        print("❌ DISCORD_TOKEN environment variable not found. Please set it in Secrets.")
        return
    async with bot:
        await setup_cogs()
        await bot.start(token)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
