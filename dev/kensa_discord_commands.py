"""Discord slash-commands voor Kensa alerts (imported door webshop-bot).

Usage in webshop-bot.py:
    import kensa_discord_commands
    kensa_discord_commands.register(bot)
"""

import sys
from pathlib import Path

import discord
from discord import app_commands

# Kensa modules — voeg pad toe zodat imports werken
_KENSA_DIR = Path("/home/pi/.openclaw/workspace/agents/kensa")
if str(_KENSA_DIR) not in sys.path:
    sys.path.insert(0, str(_KENSA_DIR))
import alerts as kensa_alerts  # noqa: E402


def register(bot: discord.Client) -> None:
    tree = bot.tree
    group = app_commands.Group(name="kensa", description="Kensa alerts voor Buyee-listings")

    @group.command(name="alert-add", description="Nieuwe alert: word gepingt bij matching listing")
    @app_commands.describe(
        query="Zoekterm (bv 'charizard 110') — alle woorden moeten in titel voorkomen",
        max_yen="Alleen items goedkoper dan dit (JPY, bv 170000)",
        min_yen="Optioneel: alleen items duurder dan dit (JPY, bv 5000)",
        alle_grades="False (default): titel moet PSA10 bevatten. True: laat alle grades door.",
    )
    async def alert_add(interaction: discord.Interaction, query: str, max_yen: int,
                        min_yen: int = 0, alle_grades: bool = False):
        try:
            aid = kensa_alerts.add_alert(
                query=query,
                max_yen=max_yen,
                min_yen=min_yen if min_yen > 0 else None,
                all_grades=alle_grades,
                created_by=str(interaction.user.id),
            )
            mn = f" · min ¥{min_yen:,}" if min_yen > 0 else ""
            grade_hint = " · PSA10 forced" if not alle_grades else " · alle grades"
            await interaction.response.send_message(
                f"✅ Alert **#{aid}** aangemaakt.\n"
                f"Query: `{query}` · max ¥{max_yen:,}{mn}{grade_hint}\n"
                f"Ping komt automatisch in #algemeen zodra er een matching listing binnenkomt.",
                ephemeral=True,
            )
        except Exception as e:
            await interaction.response.send_message(f"❌ Fout: {e}", ephemeral=True)

    @group.command(name="alerts", description="Toon alle actieve alerts")
    async def alerts_list(interaction: discord.Interaction):
        try:
            active = kensa_alerts.list_alerts(active_only=True)
            if not active:
                await interaction.response.send_message("Geen actieve alerts.", ephemeral=True)
                return
            lines = []
            for a in active:
                mn = f" · min ¥{a['min_yen']:,}" if a.get('min_yen') else ""
                gr = "" if a.get('all_grades') else " · PSA10"
                lines.append(f"**#{a['alert_id']}** `{a['query']}` — max ¥{a['max_yen']:,}{mn}{gr}")
            await interaction.response.send_message("\n".join(lines), ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ Fout: {e}", ephemeral=True)

    @group.command(name="alert-del", description="Wis (deactiveer) een alert op id")
    @app_commands.describe(alert_id="Het id-nummer van de alert (te zien met /kensa alerts)")
    async def alert_del(interaction: discord.Interaction, alert_id: int):
        ok = kensa_alerts.delete_alert(alert_id)
        if ok:
            await interaction.response.send_message(
                f"✅ Alert #{alert_id} gedeactiveerd.", ephemeral=True)
        else:
            await interaction.response.send_message(
                f"❌ Geen alert met id {alert_id} gevonden.", ephemeral=True)

    tree.add_command(group)
