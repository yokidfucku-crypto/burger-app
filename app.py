import asyncio
from datetime import datetime
import logging
import mimetypes
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urljoin, urlparse

import aiohttp
import discord
from discord import app_commands


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("kuro")

ASSET_DELIVERY_URL = "https://assetdelivery.roblox.com/v2/assetId/{asset_id}"
OPEN_CLOUD_DELIVERY_URL = (
    "https://apis.roblox.com/asset-delivery-api/v1/assetId/{asset_id}"
)
ASSET_DETAILS_URL = "https://economy.roblox.com/v2/assets/{asset_id}/details"
ASSET_THUMBNAIL_URL = (
    "https://thumbnails.roblox.com/v1/assets"
    "?assetIds={asset_id}&size=420x420&format=Png&isCircular=false"
)
DEFAULT_UPLOAD_LIMIT = 8 * 1024 * 1024
UPLOAD_SAFETY_MARGIN = 256 * 1024
ALLOWED_DOWNLOAD_HOSTS = ("roblox.com", "rbxcdn.com")

ASSET_TYPE_NAMES = {
    1: "Image",
    2: "T-Shirt",
    3: "Audio",
    4: "Mesh",
    5: "Lua",
    8: "Hat",
    9: "Place",
    10: "Model",
    11: "Shirt",
    12: "Pants",
    13: "Decal",
    17: "Head",
    18: "Face",
    19: "Gear",
    24: "Animation",
    27: "Torso",
    28: "Right Arm",
    29: "Left Arm",
    30: "Left Leg",
    31: "Right Leg",
    32: "Package",
    41: "Hair Accessory",
    42: "Face Accessory",
    43: "Neck Accessory",
    44: "Shoulder Accessory",
    45: "Front Accessory",
    46: "Back Accessory",
    47: "Waist Accessory",
    48: "Climb Animation",
    49: "Death Animation",
    50: "Fall Animation",
    51: "Idle Animation",
    52: "Jump Animation",
    53: "Run Animation",
    54: "Swim Animation",
    55: "Walk Animation",
    56: "Pose Animation",
    61: "Emote Animation",
    64: "T-Shirt Accessory",
    65: "Shirt Accessory",
    66: "Pants Accessory",
    67: "Jacket Accessory",
    68: "Sweater Accessory",
    69: "Shorts Accessory",
    70: "Left Shoe Accessory",
    71: "Right Shoe Accessory",
    72: "Dress/Skirt Accessory",
}


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


def format_date(value: object) -> str:
    if not isinstance(value, str) or not value:
        return "Unknown"

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value[:100]
    return f"<t:{int(parsed.timestamp())}:D>"


def safe_filename(value: object, fallback: str) -> str:
    name = str(value or fallback)
    name = re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip(" ._")
    return (name[:80] or fallback).replace(" ", "_")


async def get_asset_details(
    session: aiohttp.ClientSession,
    asset_id: str,
) -> dict[str, object] | None:
    async with session.get(ASSET_DETAILS_URL.format(asset_id=asset_id)) as response:
        if response.status != 200:
            return None
        try:
            payload = await response.json(content_type=None)
        except (aiohttp.ContentTypeError, ValueError):
            return None
    return payload if isinstance(payload, dict) and payload.get("Name") else None


async def get_asset_thumbnail(
    session: aiohttp.ClientSession,
    asset_id: str,
) -> str | None:
    async with session.get(ASSET_THUMBNAIL_URL.format(asset_id=asset_id)) as response:
        if response.status != 200:
            return None
        try:
            payload = await response.json(content_type=None)
        except (aiohttp.ContentTypeError, ValueError):
            return None

    items = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return None
    for item in items:
        if not isinstance(item, dict) or item.get("state") != "Completed":
            continue
        image_url = item.get("imageUrl")
        if isinstance(image_url, str) and is_allowed_download_url(image_url):
            return image_url
    return None


def build_asset_embed(
    asset_id: str,
    details: dict[str, object],
    thumbnail_url: str | None,
) -> discord.Embed:
    name = str(details.get("Name") or f"Roblox asset {asset_id}")[:256]
    description = str(details.get("Description") or "").strip()
    embed = discord.Embed(
        title=name,
        url=f"https://www.roblox.com/catalog/{asset_id}",
        description=description[:500] or None,
        colour=discord.Colour.from_rgb(88, 101, 242),
    )

    asset_type_id = details.get("AssetTypeId")
    asset_type = ASSET_TYPE_NAMES.get(asset_type_id, f"Asset type {asset_type_id}")
    embed.add_field(name="ID", value=asset_id, inline=True)
    embed.add_field(name="Type", value=asset_type, inline=True)

    creator = details.get("Creator")
    if isinstance(creator, dict):
        creator_name = discord.utils.escape_markdown(str(creator.get("Name") or "Unknown"))
        creator_id = creator.get("CreatorTargetId") or creator.get("Id")
        creator_type = str(creator.get("CreatorType") or "").lower()
        if creator_id and creator_type == "group":
            creator_value = f"[{creator_name}](https://www.roblox.com/groups/{creator_id})"
        elif creator_id:
            creator_value = f"[{creator_name}](https://www.roblox.com/users/{creator_id}/profile)"
        else:
            creator_value = creator_name
    else:
        creator_value = "Unknown"
    embed.add_field(name="Creator", value=creator_value, inline=True)

    price = details.get("PriceInRobux")
    if price is None:
        price = details.get("Price")
    price_value = f"{price} R$" if isinstance(price, (int, float)) else "Off sale"
    embed.add_field(name="Price", value=price_value, inline=True)
    embed.add_field(name="Created", value=format_date(details.get("Created")), inline=True)
    embed.add_field(name="Updated", value=format_date(details.get("Updated")), inline=True)

    if thumbnail_url:
        embed.set_thumbnail(url=thumbnail_url)
    return embed


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

    errors = payload.get("errors") if isinstance(payload, dict) else None
    if isinstance(errors, list) and errors:
        first_error = errors[0] if isinstance(errors[0], dict) else {}
        if first_error.get("code") == 401:
            raise AssetUnavailable(
                "Roblox restricts the original file for this catalog item, "
                "so only its public details are available."
            )

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
    headers: dict[str, str] | None = None,
) -> tuple[str, int]:
    request_url = download_url
    request_headers = headers

    for _ in range(4):
        async with session.get(
            request_url,
            headers=request_headers,
            allow_redirects=False,
        ) as response:
            if response.status in (301, 302, 303, 307, 308):
                redirect = response.headers.get("Location")
                redirect_url = urljoin(request_url, redirect) if redirect else ""
                if not redirect_url or not is_allowed_download_url(redirect_url):
                    raise AssetUnavailable("Roblox returned an invalid download location.")
                request_url = redirect_url
                # Never forward the Open Cloud key to a redirected CDN host.
                request_headers = None
                continue

            return await save_asset_response(response, destination, max_bytes, bool(headers))

    raise AssetUnavailable("Roblox returned too many download redirects.")


async def save_asset_response(
    response: aiohttp.ClientResponse,
    destination: Path,
    max_bytes: int,
    authenticated: bool,
) -> tuple[str, int]:
    if response.status in (401, 403) and authenticated:
        raise AssetUnavailable(
            "The configured Roblox Open Cloud key cannot access this asset."
        )
    if response.status != 200:
        raise AssetUnavailable(f"The Roblox download returned HTTP {response.status}.")

    content_type = response.headers.get("Content-Type", "application/octet-stream")
    normalized_type = content_type.partition(";")[0].strip().lower()
    if normalized_type in ("application/json", "text/json"):
        raise AssetUnavailable("Roblox did not return an asset file for this item.")

    declared_size = response.content_length
    if declared_size is not None and declared_size > max_bytes:
        raise AssetTooLarge

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

        details_result, thumbnail_result = await asyncio.gather(
            get_asset_details(self.app.http_session, asset_id),
            get_asset_thumbnail(self.app.http_session, asset_id),
            return_exceptions=True,
        )
        details = details_result if isinstance(details_result, dict) else None
        thumbnail_url = thumbnail_result if isinstance(thumbnail_result, str) else None
        embed = build_asset_embed(asset_id, details, thumbnail_url) if details else None

        try:
            delivery_headers = None
            try:
                delivery_url = await get_delivery_url(self.app.http_session, asset_id)
            except AssetUnavailable:
                if not self.app.roblox_open_cloud_api_key:
                    raise
                delivery_url = OPEN_CLOUD_DELIVERY_URL.format(asset_id=asset_id)
                delivery_headers = {"x-api-key": self.app.roblox_open_cloud_api_key}

            with tempfile.TemporaryDirectory(prefix="kuro-roblox-") as temp_dir:
                temporary_file = Path(temp_dir) / "asset.download"
                extension, _ = await download_asset(
                    self.app.http_session,
                    delivery_url,
                    temporary_file,
                    max_bytes,
                    headers=delivery_headers,
                )
                base_name = safe_filename(details.get("Name") if details else None, f"roblox_asset_{asset_id}")
                filename = f"{base_name}{extension}"
                await interaction.followup.send(
                    file=discord.File(temporary_file, filename=filename),
                    embed=embed,
                )
        except AssetTooLarge:
            if embed:
                await interaction.followup.send(
                    "The original file is too large for Discord's upload limit in this chat.",
                    embed=embed,
                )
            else:
                await interaction.followup.send(
                    "That asset is too large for Discord's upload limit in this chat.",
                    ephemeral=True,
                )
        except AssetUnavailable as exc:
            if embed:
                await interaction.followup.send(str(exc), embed=embed)
            else:
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
        self.roblox_open_cloud_api_key = os.getenv("ROBLOX_OPEN_CLOUD_API_KEY")

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

