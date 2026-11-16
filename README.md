# Discord Music Bot

Slash-command music bot (discord.py + yt-dlp + FFmpeg). Separate queue per server.

| Command | What it does |
|---|---|
| `/play <url or search>` | Play/queue a song or a whole playlist (YouTube, Spotify, SoundCloud, etc.) |

**Spotify links** (tracks, albums, public playlists, artists) work without any Spotify account or API key. The bot reads the song list from Spotify's public embed page and plays each song by searching YouTube. Playlists are capped at about 100 tracks, and the occasional song may match a different version (live, cover).
| `/skip` `/pause` `/resume` `/stop` | Playback controls (`/stop` clears the queue) |
| `/queue` `/nowplaying` | Show what's queued / playing |
| `/volume <0-150>` | Set volume |
| `/shuffle` `/remove <#>` `/loop off/track/queue` | Queue management |
| `/leave` | Disconnect |

**DJ role:** only members with the `DJ` role (or server admins) can use the control commands. Anyone can use `/queue` and `/nowplaying`. Create a role named `DJ` in Server Settings → Roles, or set `DJ_ROLE` to a different role name or role ID.

The bot leaves on its own after 5 idle minutes or when everyone leaves the channel.

## 1. Create the bot in Discord
1. https://discord.com/developers/applications → **New Application** → **Bot** → **Reset Token**, copy it.
2. **OAuth2 → URL Generator**: scopes `bot` + `applications.commands`; permissions **Connect**, **Speak**, **Send Messages**, **Embed Links**. Open the URL and add it to your server.
3. No privileged intents are needed.

## 2. Run locally (Windows)
```powershell
winget install Gyan.FFmpeg
winget install DenoLand.Deno      # yt-dlp needs a JS runtime for YouTube
# restart the terminal so both are on PATH

py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env            # then paste your token into .env
python bot.py
```
Slash commands are registered globally, so they work in every server the bot is added to.

## 3. Host 24/7 on Railway
Push to GitHub, create a Railway service from the repo — it builds from the `Dockerfile` (FFmpeg + Deno included). Add `DISCORD_TOKEN` under **Variables**.

### If YouTube says "Sign in to confirm you're not a bot"
YouTube often blocks cloud server IPs. Give the bot cookies from a **spare** Google account:
1. Open a private/incognito window, sign in to YouTube with the spare account, and export cookies with a "Get cookies.txt LOCALLY" extension (allow it in incognito). Then close the private window, which keeps those cookies from changing.
2. In PowerShell, copy the file as base64:
   `[Convert]::ToBase64String([IO.File]::ReadAllBytes("$HOME\Downloads\cookies.txt")) | Set-Clipboard`
3. In Railway → Variables, add `YTDLP_COOKIES_TXT` and paste. Delete the cookies.txt file afterwards, and never commit it.

SoundCloud links are usually unaffected.

## Keeping it working
YouTube changes things often; when playback suddenly breaks, update yt-dlp first:
`pip install -U "yt-dlp[default]"` (on Railway, just redeploy).

Note: streaming from YouTube is against YouTube's terms of service — fine for a private friends' server, but don't run it as a public bot.
