import asyncio
import logging
import mimetypes
import os
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
import discord
from discord import app_commands


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("kuro")

ASSET_DELIVERY_URL = "https://assetdelivery.roblox.com/v2/assetId/{asset_id}"
DEFAULT_UPLOAD_LIMIT = 8 * 1024 * 1024
UPLOAD_SAFETY_MARGIN = 256 * 1024
ALLOWED_DOWNLOAD_HOSTS = ("roblox.com", "rbxcdn.com")


class AssetUnavailable(Exception):
    pass


class AssetTooLarge(Exception):
    pass


def is_allowed_download_url(url: str) -> bool:
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and any(
        hostname == domain or hostname.endswith(f".{domain}")
        for domain in ALLOWED_DOWNLOAD_HOSTS
    )


def detect_extension(header: bytes, content_type: str) -> str:
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if header.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return ".webp"
    if header.startswith(b"RIFF") and header[8:12] == b"WAVE":
        return ".wav"
    if header.startswith(b"OggS"):
        return ".ogg"
    if header.startswith(b"ID3") or header[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return ".mp3"
    if len(header) >= 12 and header[4:8] == b"ftyp":
        return ".mp4"
    if header.startswith(b"<roblox!"):
        return ".rbxm"
    if header.lstrip().startswith(b"<roblox"):
        return ".rbxmx"

    normalized_type = content_type.partition(";")[0].strip().lower()
    known_types = {
        "application/octet-stream": ".rbxasset",
        "application/xml": ".rbxmx",
        "text/xml": ".rbxmx",
        "audio/mpeg": ".mp3",
        "audio/ogg": ".ogg",
        "video/mp4": ".mp4",
    }
    return known_types.get(normalized_type) or mimetypes.guess_extension(normalized_type) or ".rbxasset"


async def get_delivery_url(session: aiohttp.ClientSession, asset_id: str) -> str:
    async with session.get(ASSET_DELIVERY_URL.format(asset_id=asset_id)) as response:
        if response.status == 404:
            raise AssetUnavailable("That Roblox asset does not exist or is unavailable.")
        if response.status in (401, 403):
            raise AssetUnavailable("That Roblox asset is private or restricted.")
        if response.status == 429:
            raise AssetUnavailable("Roblox is rate-limiting requests. Try again shortly.")
        if response.status != 200:
            raise AssetUnavailable(f"Roblox returned HTTP {response.status} for that asset.")

        try:
            payload = await response.json(content_type=None)
        except (aiohttp.ContentTypeError, ValueError) as exc:
            raise AssetUnavailable("Roblox returned an invalid asset response.") from exc

    locations = payload.get("locations") if isinstance(payload, dict) else None
    if not locations or not isinstance(locations, list):
        raise AssetUnavailable("Roblox did not provide a downloadable file for that asset.")

    location = next(
        (
            item.get("location")
            for item in locations
            if isinstance(item, dict)
            and isinstance(item.get("location"), str)
            and is_allowed_download_url(item["location"])
        ),
        None,
    )
    if location is None:
        raise AssetUnavailable("Roblox returned an invalid download location.")

    return location


async def download_asset(
    session: aiohttp.ClientSession,
    download_url: str,
    destination: Path,
    max_bytes: int,
) -> tuple[str, int]:
    async with session.get(download_url) as response:
        if response.status != 200:
            raise AssetUnavailable(f"The Roblox download returned HTTP {response.status}.")

        declared_size = response.content_length
        if declared_size is not None and declared_size > max_bytes:
            raise AssetTooLarge

        content_type = response.headers.get("Content-Type", "application/octet-stream")
        size = 0
        header = bytearray()

        with destination.open("wb") as output:
            async for chunk in response.content.iter_chunked(64 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise AssetTooLarge
                if len(header) < 64:
                    header.extend(chunk[: 64 - len(header)])
                output.write(chunk)

    if size == 0:
        raise AssetUnavailable("Roblox returned an empty asset file.")

    return detect_extension(bytes(header), content_type), size


class RobloxCommands(app_commands.Group):
    def __init__(self, app: "KuroApp") -> None:
        super().__init__(name="roblox", description="Roblox asset utilities")
        self.app = app

    @app_commands.command(name="asset", description="Download a public Roblox asset by its ID")
    @app_commands.describe(asset_id="The numeric Roblox asset ID")
    @app_commands.rename(asset_id="id")
    async def asset(self, interaction: discord.Interaction, asset_id: str) -> None:
        asset_id = asset_id.strip()
        if not asset_id.isascii() or not asset_id.isdigit() or not (1 <= len(asset_id) <= 20):
            await interaction.response.send_message(
                "Enter a valid numeric Roblox asset ID.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True)

        guild_limit = interaction.guild.filesize_limit if interaction.guild else DEFAULT_UPLOAD_LIMIT
        max_bytes = max(1, guild_limit - UPLOAD_SAFETY_MARGIN)

        try:
            delivery_url = await get_delivery_url(self.app.http_session, asset_id)

            with tempfile.TemporaryDirectory(prefix="kuro-roblox-") as temp_dir:
                temporary_file = Path(temp_dir) / "asset.download"
                extension, _ = await download_asset(
                    self.app.http_session,
                    delivery_url,
                    temporary_file,
                    max_bytes,
                )
                filename = f"roblox_asset_{asset_id}{extension}"
                await interaction.followup.send(file=discord.File(temporary_file, filename=filename))
        except AssetTooLarge:
            await interaction.followup.send(
                "That asset is too large for Discord's upload limit in this chat.",
                ephemeral=True,
            )
        except AssetUnavailable as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
        except asyncio.TimeoutError:
            await interaction.followup.send("Roblox took too long to respond.", ephemeral=True)
        except aiohttp.ClientError:
            logger.exception("Roblox request failed for asset %s", asset_id)
            await interaction.followup.send("Could not reach Roblox right now.", ephemeral=True)
        except Exception:
            logger.exception("Unexpected failure while downloading Roblox asset %s", asset_id)
            await interaction.followup.send("The asset could not be downloaded.", ephemeral=True)


class KuroApp(discord.Client):
    def __init__(self) -> None:
        super().__init__(intents=discord.Intents.none())
        self.tree = app_commands.CommandTree(
            self,
            allowed_contexts=app_commands.AppCommandContext(
                guild=True,
                dm_channel=True,
                private_channel=True,
            ),
            allowed_installs=app_commands.AppInstallationType(guild=True, user=True),
        )
        self.http_session: aiohttp.ClientSession

    async def setup_hook(self) -> None:
        timeout = aiohttp.ClientTimeout(total=30)
        self.http_session = aiohttp.ClientSession(
            timeout=timeout,
            headers={"User-Agent": "Kuro Discord App"},
        )
        self.tree.add_command(RobloxCommands(self))
        synced = await self.tree.sync()
        logger.info("Synced %s global application command(s)", len(synced))

    async def close(self) -> None:
        if hasattr(self, "http_session") and not self.http_session.closed:
            await self.http_session.close()
        await super().close()

    async def on_ready(self) -> None:
        if self.user is None:
            return

        logger.info("Connected to Discord as %s (%s)", self.user, self.user.id)


def main() -> None:
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        raise RuntimeError(
            "DISCORD_BOT_TOKEN is missing. Add it to Railway Variables or your local .env."
        )

    app = KuroApp()
    app.run(token, log_handler=None)


if __name__ == "__main__":
    main()
