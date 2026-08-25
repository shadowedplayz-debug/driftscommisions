"""
Graphic design commission Discord bot.

Required environment variable:
    DISCORD_TOKEN=your_bot_token

Install:
    pip install -U discord.py
"""

from __future__ import annotations

import logging
import os
from keep_alive import keep_alive
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands


# ----------------------------- Configuration ----------------------------- #

DASHBOARD_CHANNEL_NAME = "order-status"
PUBLIC_STATUS_CHANNEL_NAME = "open-closed"
STAFF_STATUS_CHANNEL_NAME = "open-closed-staff"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("commission-bot")


# ------------------------------- Embed helpers --------------------------- #

STATUS_DETAILS: dict[str, tuple[str, discord.Color]] = {
    "Sketch": ("✏️ Sketch", discord.Color.blurple()),
    "Color": ("🎨 Color", discord.Color.orange()),
    "Revision": ("🔁 Revision", discord.Color.magenta()),
    "Review": ("👀 Review", discord.Color.gold()),
    "Final Files": ("📦 Final Files", discord.Color.purple()),
    "Done": ("✅ Done", discord.Color.green()),
}
STATUS_ORDER = ("Sketch", "Color", "Revision", "Review", "Final Files", "Done")


def is_staff_member(member: discord.Member | discord.User) -> bool:
    """Use Discord's Manage Server permission as the staff permission."""
    return isinstance(member, discord.Member) and (
        member.guild_permissions.administrator or member.guild_permissions.manage_guild
    )


async def require_staff(interaction: discord.Interaction) -> bool:
    if interaction.user and is_staff_member(interaction.user):
        return True
    message = "Only staff members with **Manage Server** permission can use this."
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)
    return False


def order_progress(status: str) -> str:
    """Render a compact visual progress indicator for an order card."""
    try:
        index = STATUS_ORDER.index(status)
    except ValueError:
        index = 0
    filled = index + 1
    return "●" * filled + "○" * (len(STATUS_ORDER) - filled)


def make_open_closed_embed(status: str) -> discord.Embed:
    is_open = status == "Open"
    embed = discord.Embed(
        title="Commission Status",
        description=(
            "Commissions are currently **OPEN**.\n"
            "You may submit a new commission request."
            if is_open
            else "Commissions are currently **CLOSED**.\n"
            "New commission requests are temporarily paused."
        ),
        color=discord.Color.green() if is_open else discord.Color.red(),
        timestamp=utc_now(),
    )
    embed.add_field(
        name="Current Status",
        value="🟢 OPEN" if is_open else "🔴 CLOSED",
        inline=False,
    )
    embed.set_footer(text="Commission status • Updated automatically")
    return embed


def open_closed_status_from_embed(embed: discord.Embed) -> str:
    for field in embed.fields:
        if field.name == "Current Status":
            return "Closed" if "CLOSED" in field.value else "Open"
    return "Open"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def make_order_embed(
    client: discord.Member,
    item: str,
    status: str,
    *,
    dashboard: bool,
    ticket_channel: discord.TextChannel,
    ticket_message_id: Optional[int] = None,
    expected_by: str | None = None,
) -> discord.Embed:
    status_label, color = STATUS_DETAILS.get(
        status, (f"⏳ {status}", discord.Color.light_grey())
    )

    if dashboard:
        embed = discord.Embed(
            title=f"✦ Order Control • {client.display_name}",
            description=(
                f"**{item}**\n"
                f"`{order_progress(status)}`  **{status_label}**\n\n"
                "Use the stage controls below to keep the client ticket in sync."
            ),
            color=color,
            timestamp=utc_now(),
        )
        embed.add_field(name="Client", value=client.mention, inline=True)
        embed.add_field(name="Current stage", value=status_label, inline=True)
        embed.add_field(
            name="Expected to be done by",
            value=expected_by or "Not set",
            inline=False,
        )
        embed.add_field(
            name="Pipeline",
            value=" → ".join(STATUS_ORDER),
            inline=False,
        )

        # The persistent button callback uses this metadata to update the
        # matching order embed in the customer's ticket channel after a restart.
        if ticket_message_id is not None:
            embed.set_footer(
                text=f"ticket_channel_id={ticket_channel.id};"
                f"ticket_message_id={ticket_message_id}"
            )
    else:
        embed = discord.Embed(
            title=f"✦ Commission Order • {item}",
            description=(
                "Your commission is moving through our creative process.\n\n"
                f"`{order_progress(status)}`  **{status_label}**"
            ),
            color=color,
            timestamp=utc_now(),
        )
        embed.add_field(name="Client", value=client.mention, inline=True)
        embed.add_field(name="Stage", value=status_label, inline=True)
        embed.add_field(
            name="What happens next",
            value="Our team will post an update here as your design progresses.",
            inline=False,
        )
        embed.set_footer(text="Commission status • Live updates")

    return embed


def parse_ticket_reference(embed: discord.Embed) -> tuple[int, int] | None:
    """Read the ticket channel/message IDs stored in a dashboard embed footer."""
    footer = embed.footer.text or ""
    values: dict[str, str] = {}

    for part in footer.split(";"):
        if "=" in part:
            key, value = part.split("=", 1)
            values[key.strip()] = value.strip()

    try:
        return int(values["ticket_channel_id"]), int(values["ticket_message_id"])
    except (KeyError, TypeError, ValueError):
        return None


# ------------------------------- Bot and view ----------------------------- #

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True


class CommissionBot(commands.Bot):
    def __init__(self) -> None:
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self) -> None:
        # Register a generic persistent view. Its callbacks read the target
        # ticket message IDs from the dashboard embed footer.
        self.add_view(OrderStatusView())
        self.add_view(OpenClosedView())
        synced = await self.tree.sync()
        logger.info("Synced %d application command(s).", len(synced))

    async def on_ready(self) -> None:
        if self.user:
            logger.info("Logged in as %s (%s)", self.user, self.user.id)
        for guild in self.guilds:
            try:
                await refresh_order_control_messages(
                    guild,
                    self.user.id if self.user else 0,
                )
                await ensure_open_closed_messages(
                    guild, self.user.id if self.user else 0
                )
            except discord.DiscordException:
                logger.exception(
                    "Could not initialize open/closed status in guild %s", guild.id
                )


class OrderStatusView(discord.ui.View):
    """Persistent order status controls."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    async def update_order(
        self,
        interaction: discord.Interaction,
        status: str,
    ) -> None:
        if (
            interaction.channel is None
            or interaction.channel.name != order-status
        ):
            await interaction.response.send_message(
                f"These controls only work in `#{order-status}`.",
                ephemeral=True,
            )
            return
        if not await require_staff(interaction):
            return

        if interaction.message is None or not interaction.message.embeds:
            await interaction.response.send_message(
                "This order card is missing its order data.",
                ephemeral=True,
            )
            return

        dashboard_embed = interaction.message.embeds[0]
        reference = parse_ticket_reference(dashboard_embed)
        if reference is None:
            await interaction.response.send_message(
                "This order card does not contain a valid ticket reference.",
                ephemeral=True,
            )
            return

        ticket_channel_id, ticket_message_id = reference
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "This action can only be used inside a server.",
                ephemeral=True,
            )
            return

        ticket_channel = guild.get_channel(ticket_channel_id)
        if not isinstance(ticket_channel, discord.TextChannel):
            try:
                fetched_channel = await guild.fetch_channel(ticket_channel_id)
            except discord.DiscordException:
                fetched_channel = None
            ticket_channel = (
                fetched_channel
                if isinstance(fetched_channel, discord.TextChannel)
                else None
            )

        if ticket_channel is None:
            await interaction.response.send_message(
                "I could not find the customer's ticket channel.",
                ephemeral=True,
            )
            return

        try:
            ticket_message = await ticket_channel.fetch_message(ticket_message_id)
        except discord.NotFound:
            await interaction.response.send_message(
                "The main order status message no longer exists in the ticket.",
                ephemeral=True,
            )
            return
        except discord.DiscordException:
            logger.exception("Could not fetch ticket message %s", ticket_message_id)
            await interaction.response.send_message(
                "Discord rejected the ticket update. Check my channel permissions.",
                ephemeral=True,
            )
            return

        # Preserve the existing ticket embed and only replace its status.
        old_embed = ticket_message.embeds[0] if ticket_message.embeds else None
        item = "Commission"

        if old_embed:
            item_value = next(
                (field.value for field in old_embed.fields if field.name == "Item"),
                None,
            )
            if item_value:
                item = item_value
            elif old_embed.title and "•" in old_embed.title:
                item = old_embed.title.split("•", 1)[1].strip()

        # Editing the existing ticket embed keeps one canonical status card in
        # the customer's ticket while the message below provides a clear alert.
        updated_ticket_embed = old_embed.copy() if old_embed else discord.Embed()
        status_label, color = STATUS_DETAILS[status]
        updated_ticket_embed.color = color
        updated_ticket_embed.timestamp = utc_now()
        ticket_status_index = next(
            (
                index
                for index, field in enumerate(updated_ticket_embed.fields)
                if field.name in {"Stage", "Status"}
            ),
            None,
        )
        if ticket_status_index is None:
            updated_ticket_embed.add_field(
                name="Stage",
                value=status_label,
                inline=True,
            )
        else:
            updated_ticket_embed.set_field_at(
                ticket_status_index,
                name="Stage",
                value=status_label,
                inline=True,
            )
        if (
            updated_ticket_embed.title
            and "Commission Order" in updated_ticket_embed.title
        ):
            updated_ticket_embed.description = (
                "Your commission is moving through our creative process.\n\n"
                f"`{order_progress(status)}`  **{status_label}**"
            )

        await ticket_message.edit(embed=updated_ticket_embed)

        # Update the dashboard card as well.
        updated_dashboard = dashboard_embed.copy()
        updated_dashboard.color = color
        updated_dashboard.timestamp = utc_now()
        updated_dashboard.description = (
            f"**{item}**\n"
            f"`{order_progress(status)}`  **{status_label}**\n\n"
            "Use the stage controls below to keep the client ticket in sync."
        )
        status_index = next(
            (
                index
                for index, field in enumerate(updated_dashboard.fields)
                if field.name in {"Current stage", "Status"}
            ),
            None,
        )
        if status_index is not None:
            updated_dashboard.set_field_at(
                status_index,
                name="Current stage",
                value=status_label,
                inline=True,
            )
        await interaction.message.edit(embed=updated_dashboard, view=self)

        if status == "Done":
            await ticket_channel.send(
                f"📢 **Commission complete:** {status_label}\n"
                f"Your order for **{item}** is done and ready for delivery."
            )
        await interaction.response.send_message(
            f"Order status updated to **{status_label}**.",
            ephemeral=True,
        )

    @discord.ui.button(
        label="Sketch",
        emoji="✏️",
        style=discord.ButtonStyle.primary,
        row=0,
        custom_id="commission_status:sketch",
    )
    async def sketch(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self.update_order(interaction, "Sketch")

    @discord.ui.button(
        label="Color",
        emoji="🎨",
        style=discord.ButtonStyle.primary,
        row=0,
        custom_id="commission_status:color",
    )
    async def color(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self.update_order(interaction, "Color")

    @discord.ui.button(
        label="Revision",
        emoji="🔁",
        style=discord.ButtonStyle.primary,
        custom_id="commission_status:revision",
        row=1,
    )
    async def revision(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self.update_order(interaction, "Revision")

    @discord.ui.button(
        label="Review",
        emoji="👀",
        style=discord.ButtonStyle.secondary,
        row=1,
        custom_id="commission_status:review",
    )
    async def review(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self.update_order(interaction, "Review")

    @discord.ui.button(
        label="Final Files",
        emoji="📦",
        style=discord.ButtonStyle.primary,
        custom_id="commission_status:final-files",
        row=1,
    )
    async def final_files(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self.update_order(interaction, "Final Files")

    @discord.ui.button(
        label="Done",
        emoji="✅",
        style=discord.ButtonStyle.success,
        row=1,
        custom_id="commission_status:done",
    )
    async def done(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self.update_order(interaction, "Done")

    @discord.ui.button(
        label="Update deadline",
        emoji="📅",
        style=discord.ButtonStyle.secondary,
        row=2,
        custom_id="commission_status:update-deadline",
    )
    async def update_deadline(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if (
            interaction.channel is None
            or interaction.channel.name != DASHBOARD_CHANNEL_NAME
        ):
            await interaction.response.send_message(
                f"This control only works in `#{DASHBOARD_CHANNEL_NAME}`.",
                ephemeral=True,
            )
            return
        if not await require_staff(interaction):
            return

        current = "Not set"
        if interaction.message and interaction.message.embeds:
            current = next(
                (
                    field.value
                    for field in interaction.message.embeds[0].fields
                    if field.name == "Expected to be done by"
                ),
                current,
            )
        await interaction.response.send_modal(
            DeadlineModal(interaction.message.id if interaction.message else 0, current)
        )


class DeadlineModal(discord.ui.Modal, title="Update expected completion"):
    deadline = discord.ui.TextInput(
        label="Expected to be done by",
        placeholder="Example: Friday, September 12 at 6 PM",
        required=True,
        max_length=100,
    )

    def __init__(self, message_id: int, current: str) -> None:
        super().__init__()
        self.message_id = message_id
        self.deadline.default = "" if current == "Not set" else current

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if (
            interaction.channel is None
            or interaction.channel.name != DASHBOARD_CHANNEL_NAME
        ):
            await interaction.response.send_message(
                f"This control only works in `#{DASHBOARD_CHANNEL_NAME}`.",
                ephemeral=True,
            )
            return
        if not await require_staff(interaction):
            return

        try:
            message = await interaction.channel.fetch_message(self.message_id)
        except discord.DiscordException:
            await interaction.response.send_message(
                "I could not find that order card.",
                ephemeral=True,
            )
            return

        if not message.embeds:
            await interaction.response.send_message(
                "That order card is missing its order data.",
                ephemeral=True,
            )
            return

        embed = message.embeds[0].copy()
        deadline = self.deadline.value.strip()
        field_index = next(
            (
                index
                for index, field in enumerate(embed.fields)
                if field.name == "Expected to be done by"
            ),
            None,
        )
        if field_index is None:
            embed.add_field(
                name="Expected to be done by",
                value=deadline,
                inline=False,
            )
        else:
            embed.set_field_at(
                field_index,
                name="Expected to be done by",
                value=deadline,
                inline=False,
            )

        await message.edit(embed=embed, view=OrderStatusView())

        # Keep the deadline visible in the customer-facing ticket without
        # sending a separate message for this staff-only update.
        reference = parse_ticket_reference(embed)
        if reference and interaction.guild:
            ticket_channel_id, ticket_message_id = reference
            ticket_channel = interaction.guild.get_channel(ticket_channel_id)
            if not isinstance(ticket_channel, discord.TextChannel):
                try:
                    fetched_channel = await interaction.guild.fetch_channel(
                        ticket_channel_id
                    )
                except discord.DiscordException:
                    fetched_channel = None
                ticket_channel = (
                    fetched_channel
                    if isinstance(fetched_channel, discord.TextChannel)
                    else None
                )

            if ticket_channel is not None:
                try:
                    ticket_message = await ticket_channel.fetch_message(
                        ticket_message_id
                    )
                    ticket_embed = (
                        ticket_message.embeds[0].copy()
                        if ticket_message.embeds
                        else discord.Embed(title="Commission Order")
                    )
                    ticket_field_index = next(
                        (
                            index
                            for index, field in enumerate(ticket_embed.fields)
                            if field.name == "Expected to be done by"
                        ),
                        None,
                    )
                    if ticket_field_index is None:
                        ticket_embed.add_field(
                            name="Expected to be done by",
                            value=deadline,
                            inline=False,
                        )
                    else:
                        ticket_embed.set_field_at(
                            ticket_field_index,
                            name="Expected to be done by",
                            value=deadline,
                            inline=False,
                        )
                    await ticket_message.edit(embed=ticket_embed)
                except discord.DiscordException:
                    logger.exception(
                        "Could not sync deadline to ticket %s", ticket_message_id
                    )

        await interaction.response.send_message(
            f"Expected completion updated to **{deadline}**.",
            ephemeral=True,
        )


class OpenClosedView(discord.ui.View):
    """Persistent controls for the staff-only open/closed status dashboard."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    async def set_status(
        self,
        interaction: discord.Interaction,
        status: str,
    ) -> None:
        if (
            interaction.channel is None
            or interaction.channel.name != STAFF_STATUS_CHANNEL_NAME
        ):
            await interaction.response.send_message(
                f"These controls only work in `#{STAFF_STATUS_CHANNEL_NAME}`.",
                ephemeral=True,
            )
            return
        if not await require_staff(interaction):
            return

        if interaction.guild is None:
            await interaction.response.send_message(
                "This control can only be used inside a server.",
                ephemeral=True,
            )
            return

        public_channel = discord.utils.get(
            interaction.guild.text_channels,
            name=PUBLIC_STATUS_CHANNEL_NAME,
        )
        staff_channel = discord.utils.get(
            interaction.guild.text_channels,
            name=STAFF_STATUS_CHANNEL_NAME,
        )
        if public_channel is None or staff_channel is None:
            await interaction.response.send_message(
                f"Please create `#{PUBLIC_STATUS_CHANNEL_NAME}` and "
                f"`#{STAFF_STATUS_CHANNEL_NAME}` first.",
                ephemeral=True,
            )
            return

        public_message = await find_open_closed_message(public_channel, bot.user.id)
        staff_message = await find_open_closed_message(staff_channel, bot.user.id)
        embed = make_open_closed_embed(status)

        if public_message is None:
            public_message = await public_channel.send(embed=embed)
        else:
            await public_message.edit(embed=embed, view=None)

        if staff_message is None:
            staff_message = await staff_channel.send(
                embed=embed,
                view=OpenClosedView(),
            )
        else:
            await staff_message.edit(embed=embed, view=self)

        await interaction.response.send_message(
            f"Commission status changed to **{status.upper()}** in both channels.",
            ephemeral=True,
        )

    @discord.ui.button(
        label="Open",
        emoji="🟢",
        style=discord.ButtonStyle.success,
        custom_id="commission_open_closed:open",
    )
    async def open_status(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self.set_status(interaction, "Open")

    @discord.ui.button(
        label="Closed",
        emoji="🔴",
        style=discord.ButtonStyle.danger,
        custom_id="commission_open_closed:closed",
    )
    async def closed_status(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self.set_status(interaction, "Closed")


async def find_open_closed_message(
    channel: discord.TextChannel,
    bot_user_id: int,
) -> discord.Message | None:
    """Find the bot's existing status message without creating duplicates."""
    async for message in channel.history(limit=100):
        if (
            message.author.id == bot_user_id
            and message.embeds
            and message.embeds[0].title == "Commission Status"
        ):
            return message
    return None


async def refresh_order_control_messages(
    guild: discord.Guild,
    bot_user_id: int,
) -> None:
    """Replace old order controls so removed stages disappear from existing cards."""
    dashboard_channel = discord.utils.get(
        guild.text_channels,
        name=DASHBOARD_CHANNEL_NAME,
    )
    if dashboard_channel is None:
        return

    async for message in dashboard_channel.history(limit=100):
        if (
            message.author.id == bot_user_id
            and message.embeds
            and (
                (message.embeds[0].title or "").startswith("✦ Order Control")
                or (message.embeds[0].title or "").startswith("Order Control:")
            )
        ):
            embed = message.embeds[0].copy()
            if not any(
                field.name == "Expected to be done by" for field in embed.fields
            ):
                embed.add_field(
                    name="Expected to be done by",
                    value="Not set",
                    inline=False,
                )
            await message.edit(embed=embed, view=OrderStatusView())


async def ensure_open_closed_messages(guild: discord.Guild, bot_user_id: int) -> None:
    """Create or synchronize the public and staff status messages on startup."""
    public_channel = discord.utils.get(
        guild.text_channels,
        name=PUBLIC_STATUS_CHANNEL_NAME,
    )
    staff_channel = discord.utils.get(
        guild.text_channels,
        name=STAFF_STATUS_CHANNEL_NAME,
    )
    if public_channel is None or staff_channel is None:
        logger.warning(
            "Guild %s needs #%s and #%s for the open/closed system.",
            guild.id,
            PUBLIC_STATUS_CHANNEL_NAME,
            STAFF_STATUS_CHANNEL_NAME,
        )
        return

    public_message = await find_open_closed_message(public_channel, bot_user_id)
    staff_message = await find_open_closed_message(staff_channel, bot_user_id)

    status = "Open"
    if staff_message and staff_message.embeds:
        status = open_closed_status_from_embed(staff_message.embeds[0])
    elif public_message and public_message.embeds:
        status = open_closed_status_from_embed(public_message.embeds[0])

    embed = make_open_closed_embed(status)
    if public_message is None:
        await public_channel.send(embed=embed)
    else:
        await public_message.edit(embed=embed, view=None)

    if staff_message is None:
        await staff_channel.send(embed=embed, view=OpenClosedView())
    else:
        await staff_message.edit(embed=embed, view=OpenClosedView())

    logger.info("Open/closed status initialized in guild %s as %s.", guild.id, status)


# -------------------------------- Commands -------------------------------- #

bot = CommissionBot()


@bot.tree.command(name="neworder", description="Create a new design commission order.")
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(
    client="The customer who placed the commission.",
    item="What is being designed.",
    ticket_channel="The customer's ticket channel.",
)
@app_commands.guild_only()
async def neworder(
    interaction: discord.Interaction,
    client: discord.Member,
    item: str,
    ticket_channel: discord.TextChannel,
) -> None:
    if not await require_staff(interaction):
        return

    dashboard_channel = discord.utils.get(
        interaction.guild.text_channels,
        name=DASHBOARD_CHANNEL_NAME,
    )
    if dashboard_channel is None:
        await interaction.response.send_message(
            f"I could not find a text channel named `#{DASHBOARD_CHANNEL_NAME}`.",
            ephemeral=True,
        )
        return

    # Create the canonical customer-facing status embed first so its message
    # ID can be stored on the dashboard card for persistent button callbacks.
    ticket_embed = make_order_embed(
        client,
        item,
        "Sketch",
        dashboard=False,
        ticket_channel=ticket_channel,
    )
    ticket_message = await ticket_channel.send(embed=ticket_embed)

    dashboard_embed = make_order_embed(
        client,
        item,
        "Sketch",
        dashboard=True,
        ticket_channel=ticket_channel,
        ticket_message_id=ticket_message.id,
    )
    await dashboard_channel.send(embed=dashboard_embed, view=OrderStatusView())

    await interaction.response.send_message(
        f"✅ Order created for {client.mention} in "
        f"{ticket_channel.mention}. Dashboard card posted in "
        f"{dashboard_channel.mention}.",
        ephemeral=True,
    )


@bot.tree.command(name="receipt", description="Generate a paid commission receipt.")
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(
    client="The customer who paid.",
    item="What was purchased.",
    price="The total amount paid, including currency.",
)
@app_commands.guild_only()
async def receipt(
    interaction: discord.Interaction,
    client: discord.Member,
    item: str,
    price: str,
) -> None:
    if not await require_staff(interaction):
        return

    embed = discord.Embed(
        title="DESIGN COMMISSION RECEIPT",
        description="Thank you for your commission purchase.",
        color=discord.Color.green(),
        timestamp=utc_now(),
    )
    embed.add_field(name="Client", value=client.mention, inline=False)
    embed.add_field(name="Item Purchased", value=item, inline=False)
    embed.add_field(name="Total Paid", value=price, inline=False)
    embed.add_field(name="Status", value="✅ Paid & Delivered", inline=False)
    embed.set_footer(text="Graphic Design Commission")

    await interaction.response.send_message(embed=embed)


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
) -> None:
    logger.error("Application command error: %s", error)
    message = "Something went wrong while running that command."
    if isinstance(error, app_commands.errors.MissingPermissions):
        message = "You do not have permission to use that command."
    elif isinstance(error, app_commands.errors.CommandInvokeError):
        message = "Discord rejected the request. Check my channel permissions."

    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


def main() -> None:
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError(
            "DISCORD_TOKEN is not set. Add it as a Replit Secret or environment variable."
        )
        keep_alive() 
    bot.run(token)


if __name__ == "__main__":
    main()
