# Siloquy

Live server stats for [Silo](https://github.com/Silo-Server/silo-server) as Discord voice-channel counters, in the style of Tauticord.

Siloquy is an independent project. It is not affiliated with or endorsed by the Silo project. Made by Departed69.

```
📡 Silo Stats
   🟢 Silo: Online
   ▶️ Streams: 4 (1 ⏸)
   🔥 Transcodes: 1
   ⚡ Direct Play: 3
   🌐 Remote: 52 Mbps
📊 Performance
   👥 Users: 48
   🧠 Silo CPU: 15%
   🐏 Silo RAM: 2.5 GB
   💾 Storage: 98/130 TB
```

- **Silo:** 🟢 Online, 🟡 Degraded (Silo is up but an API call failed or the key was refused) or 🔴 Offline.
- **Streams:** total streams, with paused ones in brackets. **Transcodes** counts video transcodes. **Direct Play** counts direct play, remux and direct stream.
- **Remote:** combined bitrate of remote streams that aren't paused.
- **CPU / RAM:** by default, what **Silo itself** uses, including its ffmpeg transcoders. This is comparable to Tauticord's Plex-only figures. CPU is a share of the whole machine, rounded to the nearest 5%. RAM is shown in GB. Set `RESOURCE_SCOPE=host` to show the whole machine instead (`Host CPU` / `Host RAM`), which includes everything else running on it.
- **Storage:** adds up your library drives.

Optionally, Siloquy can also give members an **@Watching role while they're playing something on Silo**, so you can warn just those people before a restart. See [@Watching role](#optional-watching-role).

### How fast the counters update
Discord lets a bot rename a channel only **twice every 10 minutes**, and Tauticord has the same limit. Siloquy checks Silo every 30 seconds:
- A change like a stream starting usually shows within 30–60 seconds.
- If a counter has already changed twice in the last 10 minutes, the next change waits, and the log says how long.
- Short blips are skipped, so they don't use up a channel's renames.

---

## Security

| Area | What Siloquy does |
|---|---|
| Silo access | Uses an API key limited to **`admin:monitor:read`**, which is read-only. At startup it checks the key and **refuses to run if the key has full admin access**. |
| Network | Plain `http` is only allowed to local addresses: a Docker container name, `localhost`, `host.docker.internal`, or a private LAN IP. Anything else must use `https`. Redirects are never followed and proxy settings are ignored, so the key only ever goes to your Silo. |
| Requests | GET requests only, to 5 fixed paths (plus an account's profile list when an admin uses `/watching`). The health check is sent without the key. |
| Personal data | Titles, IPs and device names are discarded as soon as a response is read. Channel names contain **only counts and percentages**. Nothing personal is logged. The optional watching role keeps only Discord user IDs and Silo's account/profile ID numbers, never names. |
| Secrets | Read from Docker secret files. The bot refuses files anyone on the server can read, never prints secrets, and scrubs them from every log line and error. |
| Discord | Asks Discord for the bare minimum (no message reading, no member list). Has no commands unless the watching role is on, and then only admin-only `/watching` commands whose answers only the admin sees. Works in **one** server and leaves any other it's added to. Only renames or deletes channels it created. Nobody can join its voice channels. |
| Container | Runs as a non-root user with a read-only filesystem, no extra privileges, no published ports and capped memory. It has no access to the Docker socket. |
| Dependencies | `discord.py`, `aiohttp` and their dependencies, pinned to exact versions and installed from prebuilt packages only. |

**What's left if something leaks:**
- **Silo key:** someone could *read* your Silo monitoring data (usernames, IPs, devices, watch history), but not change anything or stop streams. This is the narrowest Silo permission that includes transcode and user counts.
- **Discord token:** someone could do whatever the bot's role allows. Step 7 limits that to Siloquy's own two categories. With the watching role on, the bot also has Manage Roles, so they could hand out roles that sit **below** Siloquy's role. Keeping Siloquy's role directly above @Watching limits that to @Watching.

---

## Setup

You need Docker with Compose v2, a Silo server, and admin rights on Silo and on your Discord server.

### 1. Create the Discord bot
1. Go to <https://discord.com/developers/applications>, click **New Application**, and name it.
2. On the **Bot** tab:
   - Click **Reset Token** and copy the token. You'll need it in step 4.
   - Turn **Public Bot** **off**.
   - Leave all three **Privileged Gateway Intents** **off**.
3. On the **General Information** tab, copy the **Application ID**.

### 2. Invite the bot to your server
Open this link with your Application ID in place of `APP_ID`:

```
https://discord.com/oauth2/authorize?client_id=APP_ID&scope=bot+applications.commands&permissions=1049616
```

That permission number covers **View Channels**, **Manage Channels** and **Connect**, and nothing else. `applications.commands` lets the optional `/watching` commands appear; Siloquy registers none unless you turn that feature on.

To find your **server ID**, turn on Discord **Settings → Advanced → Developer Mode**, then right-click your server's icon and choose **Copy Server ID**.

### 3. Create a read-only Silo API key
Silo's **Create API Key** dialog doesn't offer a scope choice yet, so keys made there have **full admin access**. Siloquy refuses to run with those. Instead, create the key from your browser's developer console:

1. Open Silo in Chrome, signed in as an **admin**, and open DevTools (**⌘⌥I** on Mac or **F12** elsewhere).
2. **Network** tab: filter by `/api/v2/`, click **Fetch/XHR**, and refresh the page. Click any request with status 200, then under **Request Headers** copy:
   - the value of `authorization` (starts with `Bearer eyJ…`). Chrome shows it in lowercase.
   - the value of `x-profile-id`, if there is one.
3. **Console** tab: paste the code below. If Chrome blocks pasting, type `allow pasting` and press Enter first.

```js
const auth = prompt("Paste the authorization value (starts with Bearer)");
const profile = prompt("Paste the x-profile-id value (blank if none)");
const headers = { "Authorization": auth.trim(), "Content-Type": "application/json" };
if (profile && profile.trim()) headers["X-Profile-Id"] = profile.trim();
const res = await fetch("/api/v2/admin/api-keys", {
  method: "POST", headers,
  body: JSON.stringify({ label: "Siloquy", scopes: ["admin:monitor:read"] }),
});
const out = await res.json();
console.log(res.status, out.scopes, out.key ? `key is ${out.key.length} chars` : out);
```

4. You should see `201 ['admin:monitor:read'] key is 67 chars`. Copy the key with this:

```js
prompt("Press Cmd/Ctrl+A, then Cmd/Ctrl+C", out.key)
```

Select all, copy, and click **Cancel**. Silo never shows the key again, and a partial copy won't work. When you're done, close the tab.

### 4. Install
```bash
git clone https://github.com/BEisem/siloquy.git /opt/siloquy
cd /opt/siloquy
umask 077
mkdir -p secrets data
read -rsp "Discord bot token: " T && printf '%s' "$T" > secrets/discord_token && unset T; echo
read -rsp "Silo API key: "      K && printf '%s' "$K" > secrets/silo_api_key  && unset K; echo
wc -c secrets/silo_api_key        # must print 67
sudo chown -R 10001:10001 secrets data
sudo chmod 700 secrets data
sudo chmod 400 secrets/discord_token secrets/silo_api_key
```

`read -s` keeps both secrets out of your shell history and off the screen.

### 5. Configure
```bash
cp .env.example .env
nano .env
```

Set `DISCORD_GUILD_ID` to your server ID, then choose how Siloquy reaches Silo:

| Your setup | In `.env` |
|---|---|
| Silo on **this machine**, port published (default 8090) | `SILO_URL=http://host.docker.internal:8090` |
| Silo on **another machine on your LAN** | `SILO_URL=http://192.168.1.50:8090` (use its IP) |
| Silo behind **HTTPS** | `SILO_URL=https://silo.example.com` |
| **Same Docker network** as Silo (most private) | `SILO_URL=http://silo:8080`<br>`COMPOSE_FILE=docker-compose.yml:compose.network.yml`<br>`SILO_NETWORK=<name from docker network ls>` |
| **Saltbox** | `SILO_URL=http://silo:8080`<br>`COMPOSE_FILE=docker-compose.yml:compose.saltbox.yml` |

The Saltbox preset joins the `saltbox` network and adds the label Saltbox backups use to stop and start containers.

Your `.env` is never part of an update, so your settings stay put.

### 6. Start it
```bash
docker compose up -d
docker logs -f siloquy
```

Look for `Silo API key scope verified`. Two categories with nine channels then appear in Discord, and you can drag them wherever you like.

### 7. Lock down the bot (do this)
Once the channels exist, the bot no longer needs Manage Channels across your whole server. Go to **Server Settings → Roles → [your bot's role]** and turn **Manage Channels** **off**.

Siloquy's categories and channels already give the bot the access it needs, so it keeps working, and a leaked token can only touch those two categories.

### Updating
```bash
cd /opt/siloquy && git pull
docker compose pull && docker compose up -d
```

`git pull` updates the compose files and docs. `docker compose pull` fetches the new image. To stay on one version, set `SILOQUY_TAG` in `.env`, for example `SILOQUY_TAG=1.2` (gets 1.2.x fixes only) or `SILOQUY_TAG=1.2.0`.

### Prebuilt image or build it yourself
By default Siloquy runs the image GitHub builds from this repo (`ghcr.io/beisem/siloquy`), for both Intel/AMD and ARM. Each image is built only after the tests pass, and comes with a signed record of how it was built. You can check that record yourself:

```bash
gh attestation verify oci://ghcr.io/beisem/siloquy:latest --owner BEisem
```

If you'd rather build from source, add `compose.build.yml` to `COMPOSE_FILE` in `.env` (see `.env.example`). Then use `docker compose up -d --build` instead of `pull`.

### Removing it
1. Run `docker compose down`.
2. Delete Siloquy's two categories in Discord.
3. **Revoke the API key in Silo.**

---

## Optional: @Watching role

Gives linked members a role while they're playing something on Silo, and takes it away shortly after they stop. Use it to warn only the people who'd notice: `@Watching restarting Silo in 5 minutes`.

It uses the same read-only Silo key. It doesn't need any extra Discord intents.

### Set it up
1. **Create the role.** In **Server Settings → Roles → Create Role**, name it (for example `Watching`). Don't give it any moderator or admin permissions; Siloquy refuses to use a role that has them. Turn on **Allow anyone to @mention this role** only if you want everyone to be able to ping it.
2. **Let Siloquy manage it.** On **Siloquy's role**, turn **Manage Roles** **on**. Then drag Siloquy's role so it sits **directly above `Watching`** and below every role with moderator or admin powers. A bot can hand out any role below its own, so keeping it low means a leaked token can only give out `Watching`. Siloquy's log warns if its role sits above any such roles. (Step 7's Manage Channels change is separate; leave that off.)
3. **Turn it on.** Right-click the role, choose **Copy Role ID**, and add it to `.env`:
   ```
   WATCHING_ROLE_ID=123456789012345678
   ```
   Then run `docker compose up -d`. The log should show `Watching role enabled`.
4. **Link people** with `/watching link`. Pick the member and their Silo account; both lists fill in as you type.
   - `profile` is optional. Leave it empty to count any profile on the account. Pick one when an account is shared, so that only that person's profile gives them the role.
   - `/watching list` shows who's linked (🟢 = has the role right now). `/watching unlink` removes someone and takes the role away.
   - Only members with **Manage Roles** can see or use these commands, and only you see the answers.

### How it behaves
- The role is added within one check of Silo (30 seconds by default; set `POLL_SECONDS=15` for quicker).
- Paused playback still counts as watching.
- The role is removed after `WATCHING_GRACE_SECONDS` (60 by default) with nothing playing, so it doesn't flicker between episodes.
- If Silo can't be reached, roles stay as they are rather than being removed from everyone.
- Siloquy only removes the role from people it gave it to. If you give someone the role by hand, it stays.
- Changing or removing `WATCHING_ROLE_ID` takes the old role back from everyone Siloquy gave it to. Links are kept.
- Links are stored in `data/watching.json` as Discord user IDs and Silo ID numbers only.

---

## Troubleshooting

| Log message | Fix |
|---|---|
| `API key was rejected (HTTP 401)` | The key file is wrong. `sudo wc -c secrets/silo_api_key` must print **67** and `sudo head -c 3 secrets/silo_api_key` must print `sa_`. If not, create a new key (step 3), rewrite the file, and revoke the bad key in Silo. Then run `docker compose up -d --force-recreate`, because the key is only read at startup. |
| `Refusing to run: the Silo API key is NOT scoped` | The key has full admin access. Create one with step 3, then revoke the old one. |
| `The bot is not in server …` | Invite it again with the link from step 2. |
| `No permission to rename the … channel` | On each Siloquy category, give the bot's role **View Channel**, **Connect** and **Manage Channels**, then sync the channels. |
| `SILO_URL uses plain http to a non-internal host` | Use `https`, a LAN IP, `host.docker.internal`, or the shared Docker network. |
| `waiting on Discord's rename limit` | This is normal: Discord's two-renames-per-10-minutes limit. The new value shows when the wait ends. |
| `Watching role paused: …` | The message says what to fix: usually Manage Roles is off on Siloquy's role, Siloquy's role is below `Watching`, or `Watching` has moderator permissions. It resumes on its own once fixed. |
| `/watching` doesn't appear | Restart Discord. If it still isn't there, the log has a re-invite link that adds slash commands. Re-inviting keeps the bot's channels and settings. |
| A linked member never gets the role | Check `/watching list` shows the member linked to the right account, and that the profile (if set) is the one they're playing on. |
| 🔴 **Offline** but Silo is running | Siloquy can't reach `SILO_URL`. From the server, check `curl <SILO_URL>/api/v1/health`. |

`chmod: cannot access 'secrets/*'` during install isn't an error in the files. It happens because the folder is already locked to the bot's user, so your shell can't expand the `*`. Name the files directly, as step 4 does.

---

## Options

| Variable | Default | Notes |
|---|---|---|
| `DISCORD_GUILD_ID` | (none) | **Required.** Your server ID. |
| `SILO_URL` | (none) | **Required.** See step 5. |
| `STATS` | all | Comma-separated list from `status,streams,transcodes,direct,bandwidth,users,cpu,memory,storage`. Leaving one out deletes its channel. |
| `POLL_SECONDS` | `30` | 15–240. |
| `USERS_POLL_SECONDS` | `900` | How often to recount users. |
| `STATS_CATEGORY` / `PERFORMANCE_CATEGORY` | `📡 Silo Stats` / `📊 Performance` | Only used when the categories are first created. Rename them in Discord any time. |
| `SILOQUY_TAG` | `latest` | Image version to run, e.g. `1.2` or `1.2.0`. |
| `RESOURCE_SCOPE` | `silo` | `silo` shows Silo's own CPU and RAM, including transcoders. `host` shows the whole machine. |
| `WATCHING_ROLE_ID` | (none) | Turns on the [@Watching role](#optional-watching-role). |
| `WATCHING_GRACE_SECONDS` | `60` | 0–900. How long after playback stops before the role is removed. |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR`. |
| `ALLOW_UNSCOPED_KEY` | `false` | Leave it `false`. |

Exit codes: `1` = configuration error, `2` = the API key has full admin access, `3` = Silo keeps rejecting the key.

## Development
```bash
pip install -r requirements.txt
python -m unittest discover -s tests -v
```

Dependencies are pinned to exact versions. For stricter installs, you can also lock them to checksums: run `pip-compile --generate-hashes`, then change `--no-deps` to `--require-hashes` in the `Dockerfile`.

Silo is pre-release software, and its admin API may change. If an update breaks a counter, that counter shows `—`, the status shows 🟡 Degraded, and the log names the endpoint that failed. Please open an issue.

## License
MIT. See [LICENSE](LICENSE).
