import logging
import os

import discord


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("kuro")


class KuroApp(discord.Client):
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

    # Slash commands do not require Message Content or other privileged intents.
    app = KuroApp(intents=discord.Intents.none())
    app.run(token, log_handler=None)


if __name__ == "__main__":
    main()
