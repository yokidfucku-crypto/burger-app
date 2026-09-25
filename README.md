# Kuro

A clean foundation for a user-installable Discord quality-of-life app. This is
a new app and contains none of the commands or services from the older bot.

## Current state

Kuro connects to Discord and requests no privileged gateway intents.

Available command:

- `/roblox asset id:<asset ID>` shows the asset's public catalog details and
  thumbnail. It also attaches the original file when Roblox permits public
  delivery for that asset. If `ROBLOX_OPEN_CLOUD_API_KEY` is configured, Kuro
  also tries Roblox's authenticated Open Cloud Asset Delivery API.

## Create the Discord application

1. Open the [Discord Developer Portal](https://discord.com/developers/applications).
2. Select **New Application**, enter **Kuro**, and create it.
3. Open **Bot**, create the bot user if Discord asks, and reset/copy its token.
4. Never commit or paste that token into the source code.
5. Open **Installation** and enable **User Install**. You can also enable
   **Guild Install** so servers can install Kuro.
6. For User Install, add the `applications.commands` scope.
7. For Guild Install, add the `applications.commands` and `bot` scopes. Basic
   future permissions are **Send Messages**, **Embed Links**, and **Attach Files**.
8. Copy the install link from the Installation page and open it to add Kuro to
   your Discord account or a test server.

Global commands are synchronized when Kuro starts. Discord may take a little
time to display a newly synchronized global command everywhere.

## Deploy on Railway

1. Put this folder in its own GitHub repository.
2. Create a Railway project from that repository.
3. In Railway **Variables**, add `DISCORD_BOT_TOKEN` with the bot token from the
   Discord Developer Portal.
4. To request authenticated asset downloads, create a Roblox Open Cloud API key
   with asset read access and add it as `ROBLOX_OPEN_CLOUD_API_KEY`. The key can
   only download assets that Roblox allows its owner to access.
5. Deploy. Railway detects the Dockerfile and starts `python app.py`.

A successful deployment logs `Connected to Discord as ...`.

## Run locally

Create a virtual environment, install `requirements.txt`, and set
`DISCORD_BOT_TOKEN` in your terminal before running:

```powershell
python app.py
```

