<p align="center"><img src="assets/logo.png" alt="HodeWerner logo" width="256"></p>

# HodeWerner

HodeWerner is a small Discord bot that plays audio from YouTube in a voice channel. It is built for one private server: it only answers commands there, and it leaves any other server it gets added to.

It runs in a Proxmox LXC container at home. The bot is written in Python with discord.py, yt-dlp and FFmpeg.

## What it does

- **Plays from YouTube:** a video link, a playlist link (up to 100 songs), or just search words. `/search` shows the top 5 results so you can pick the right one.
- **Now playing card:** shows the thumbnail, length, who asked for it and what's next, with buttons for Pause/Resume, Skip, Stop, Loop, Autoplay and Shuffle.
- **Autoplay:** when it's on and the queue runs out, the bot keeps going with related songs from YouTube's mix for the last song. As soon as someone adds a song, the autoplay picks step aside.
- **Saved playlists:** save what's queued under a name like "Roadtrip" and play it again any time. Everyone on the server shares the same playlists.
- **Clear messages:** for songs added to the queue (with an estimate of when they'll play), playlists added, skips, the queue finishing and the bot leaving.
- **Tidy behavior:** it leaves the voice channel when everyone else has, or after 5 minutes with nothing playing.

## Commands

**Adding music**

| Command | What it does |
|---|---|
| `/play <link or search words>` | Plays a song or playlist, or adds it to the queue |
| `/search <words>` | Shows the top 5 YouTube results to pick from |
| `/playlist play <name> [shuffle]` | Adds a saved playlist to the queue |

**Controlling playback**

| Command | What it does |
|---|---|
| `/pause` and `/resume` | Pause and continue |
| `/skip` | Skips to the next song |
| `/seek <time>` | Jumps to a time in the song, like `1:30` |
| `/replay` | Starts the current song again |
| `/stop` | Stops, clears the queue and leaves the voice channel |
| `/volume <0-100>` | Sets the volume |
| `/loop <off, this song, whole queue>` | Repeats the current song or the whole queue |
| `/autoplay <on, off>` | Keeps playing related songs when the queue runs out |

**The queue**

| Command | What it does |
|---|---|
| `/nowplaying` | Shows the current song with a progress bar |
| `/queue [page]` | Shows the queue, 10 songs per page |
| `/move <from> <to>` | Moves a song in the queue (`to` = 1 plays it next) |
| `/remove <position>` | Removes one song from the queue |
| `/shuffle` | Shuffles the queue |
| `/clear` | Clears the queue but keeps the current song playing |

**Saved playlists**

| Command | What it does |
|---|---|
| `/playlist save <name>` | Saves the current song and the queue (same name replaces it) |
| `/playlist play <name>` | Adds it to the queue, optionally shuffled |
| `/playlist list` | Shows all saved playlists |
| `/playlist show <name>` | Shows the songs in one |
| `/playlist delete <name>` | Deletes one |

Controls and buttons only work if you're in the same voice channel as the bot, so nobody can skip songs from outside.

## Setup at a glance

1. Create the bot in Discord and get its token
2. Run one command on the Proxmox host that creates the container and installs everything
3. Add your Discord token and server ID, and start the bot

---

## 1. Create the bot in Discord

1. Go to https://discord.com/developers/applications and click **New Application**. Name it "HodeWerner" and upload `assets/logo.png` as the app icon.
2. Open the **Bot** tab. Upload the same logo as the bot's avatar.
3. Click **Reset Token**, copy the token, and keep it private. Anyone with this token can control the bot.
4. On the same tab, turn **Public Bot** off, so only you can add it to servers.
5. Open **OAuth2 > URL Generator**. Tick the scopes `bot` and `applications.commands`. Under bot permissions tick **View Channels**, **Send Messages**, **Embed Links**, **Connect** and **Speak**.
6. Open the generated link and add HodeWerner to your server.
7. Get your server ID: in Discord go to **User Settings > Advanced** and turn on **Developer Mode**, then right-click your server icon and pick **Copy Server ID**.

Keep the token and server ID handy for step 3.

## 2. Create the container

Open a shell on the Proxmox host as root (in the web UI: click the node, then **Shell**) and run:

```
bash -c "$(curl -fsSL https://raw.githubusercontent.com/DBCooperX/HodeWerner/main/deploy/proxmox-create-lxc.sh)"
```

The script downloads the code from GitHub, then:

- downloads the newest Debian template if you don't have it yet
- creates an unprivileged container with 1 CPU core, 1 GB RAM and a 4 GB disk, which starts on boot and gets its IP by DHCP
- installs FFmpeg, Deno (yt-dlp needs it for YouTube), Python and the bot into `/opt/hodewerner`
- sets up the `hodewerner` systemd service, running as its own user
- adds a nightly job that updates yt-dlp and restarts the bot, because YouTube changes often and yt-dlp releases fixes quickly

You can change the defaults with environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `CTID` | next free ID | Container ID |
| `STORAGE` | `local-lvm` | Storage for the container disk |
| `TEMPLATE_STORAGE` | `local` | Storage that holds templates |
| `BRIDGE` | `vmbr0` | Network bridge |
| `CORES` | `1` | CPU cores |
| `MEMORY` | `1024` | RAM in MB |
| `DISK_GB` | `4` | Disk size in GB |

Put them in front of the command, for example `CTID=150 STORAGE=local-zfs MEMORY=512 bash -c "$(curl ...)"`.

## 3. Add your Discord token and start the bot

When the script finishes, it prints the container ID. Then:

```
pct enter <CTID>
nano /opt/hodewerner/.env
```

Fill in the two values from step 1:

```
DISCORD_TOKEN=your-bot-token
GUILD_ID=your-server-id
```

The other settings in that file are optional: `IDLE_TIMEOUT` (seconds before leaving when nothing plays, `0` to never leave), `MAX_PLAYLIST` (most songs taken from one playlist), `DEFAULT_VOLUME` (percent) and `AUTOPLAY` (`on` to start with autoplay turned on).

Save with Ctrl+O, Enter, then Ctrl+X. Start the bot and watch the log:

```
systemctl start hodewerner
journalctl -u hodewerner -f
```

When the log says `Logged in as HodeWerner`, join a voice channel in Discord and type `/play never gonna give you up`. The slash commands show up right away because the bot registers them for your server only.

---

## Day to day

Run these inside the container (`pct enter <CTID>`):

| Task | Command |
|---|---|
| See the live log | `journalctl -u hodewerner -f` |
| Restart the bot | `systemctl restart hodewerner` |
| Stop the bot | `systemctl stop hodewerner` |
| Update yt-dlp now | `systemctl start hodewerner-update` |
| See when the next update runs | `systemctl list-timers hodewerner-update.timer` |

**Saved playlists** live in `/opt/hodewerner/data/playlists.json` inside the container. Updates keep them. Proxmox backups of the container include them too.

## Updating the bot code

On the Proxmox host, run the same command as in step 2 with `UPDATE_CTID` set to your container's ID:

```
UPDATE_CTID=<CTID> bash -c "$(curl -fsSL https://raw.githubusercontent.com/DBCooperX/HodeWerner/main/deploy/proxmox-create-lxc.sh)"
```

It downloads the newest code from GitHub, reinstalls it in the container, keeps your `.env`, and restarts the bot.

If you have a copy of the repo on the host, you can also run `bash deploy/proxmox-create-lxc.sh` from that folder, and it uses the local code instead of downloading.

## Troubleshooting

- **Slash commands don't show up:** check `GUILD_ID` in `.env`, and make sure you added the bot with the `applications.commands` scope.
- **Messages show up as plain links with no cards:** give the bot the **Embed Links** permission in that text channel.
- **The bot crashes at start:** look at `journalctl -u hodewerner -n 50`. A `KeyError: 'DISCORD_TOKEN'` means `.env` is not filled in.
- **The bot joins but there is no sound:** check that FFmpeg works with `ffmpeg -version`.
- **Errors mentioning a JavaScript runtime or "n challenge":** check that Deno works with `deno --version`.
- **One song fails and the bot skips it:** the video may be age-restricted, private or blocked in your country. Cookies (below) help with age-restricted videos.
- **"Sign in to confirm you're not a bot":** YouTube is blocking requests. See the next section.

### If YouTube blocks the bot

This is rare on a home internet connection and common on cloud servers. Try these in order:

1. **Update yt-dlp now** with `systemctl start hodewerner-update`. Many errors are fixed this way.
2. **Use cookies.** In a browser where you are signed in to YouTube, export cookies with an extension such as "Get cookies.txt LOCALLY". Copy the file into the container as `/opt/hodewerner/cookies.txt`, run `chown hodewerner /opt/hodewerner/cookies.txt`, set `YTDLP_COOKIES=cookies.txt` in `.env`, and restart the bot. Use a spare Google account, not your main one, because YouTube can restrict accounts used this way. Cookies expire, so you may need to export them again now and then.
3. **Add a PO token provider.** YouTube sometimes asks for a "proof of origin" token. The `bgutil-ytdlp-pot-provider` plugin creates these automatically. Follow the setup at https://github.com/Brainicism/bgutil-ytdlp-pot-provider: it runs a small helper service, and you install the plugin into the bot with `/opt/hodewerner/.venv/bin/pip install bgutil-ytdlp-pot-provider`. yt-dlp picks it up on its own.

---

## Other ways to run it

### Any Debian 12 or 13 machine or VM

Copy the repo to the machine and run, as root, from the repo folder:

```
bash deploy/install.sh
```

This does the same install as inside the Proxmox container. Then continue with step 3.

### On your own computer (for testing)

You need Python 3.10 or newer, FFmpeg and Deno.

- **Windows:** `winget install Python.Python.3.12 Gyan.FFmpeg DenoLand.Deno`
- **macOS:** `brew install python ffmpeg deno`

Then, in the repo folder:

```
python3 -m venv .venv
source .venv/bin/activate        # on Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # on Windows: copy .env.example .env
```

Fill in `.env` and start the bot with `python bot.py`. Stop it with Ctrl+C. Here, update yt-dlp yourself now and then with `pip install -U "yt-dlp[default]"`.

## A note on YouTube's rules

Streaming YouTube audio this way goes against YouTube's Terms of Service. For a small private server the practical risk is low, but it is why the bot is locked to one server, and why it's best to use a spare Google account if you need cookies.
