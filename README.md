# Edit Guard Bot

Telegram groups ke liye automated protection aur moderation bot.

- **Edit protection** — edited message hata deta hai (turant ya delay ke baad) aur user ko batata hai
- **Media timer** — photo, video, sticker, GIF, file, voice tay samay baad gayab
- **Explicit-media filter** — adult sticker / photo / video / GIF apne aap hatata hai
- **Content locks** — members ke links, forwards, files, videos, audio aur channel posts band
- **Blocklist** — chune hue words/phrases wale messages, captions, file names aur forward sources hatao
- **Moderation** — ban, kick, mute, warn (limit par automatic penalty), delete-with-action variants
- **Cleanup** — purge, per-user purge, join/leave notices aur bot ke apne notices auto-hide
- **Reports** — member `/report` kare to admins ko action buttons milte hain
- **Control panel** — `/settings` se sab kuch inline buttons se
- **"/" menu** — admins ko poori command list, members ko chhoti list

## Commands

Jahan user chunna ho: uske message par reply karo, ya user ID / `@username` likho.

### Protection

| Command | Kaam |
| --- | --- |
| `/settings` | Control panel |
| `/editdelay 2m` | Edited message itni der baad hatega (`off` = turant) |
| `/mediadelay 10m` | Media itni der baad hatega (`off` = band) |
| `/nsfw on` / `off` | Explicit-media filter |
| `/allowedit`, `/revokeedit` | Member ko edit ki chhoot do / wapas lo (locks aur blocklist se bhi chhoot) |
| `/allowmedia`, `/revokemedia` | Member ka media kabhi na hate (timer aur filter dono se chhoot) |
| `/allowlist` | Chhoot wale members ki list |

### Content control

Group ko copyright / policy strikes se bachane ke liye.

| Command | Kaam |
| --- | --- |
| `/lock files videos` | Content type band karo. Types: `links`, `forwards`, `files`, `videos`, `audio`, `channels`, `all` |
| `/unlock files` | Wapas chalu |
| `/locks` | Kya-kya locked hai |
| `/block word, phrase` | Blocklist mein jodo (comma se alag) |
| `/unblock word` | Blocklist se hatao |
| `/blocklist` | Poori blocklist |
| `/del` | Reply kiya hua message delete |
| `/purge` | Reply kiye message se neeche tak sab delete; ya `/purge 50` = aakhri 50 (max 500) |
| `/purgeuser` | Member ke pichhle 48 ghante ke messages delete |

Blocklist yahan match karti hai: message text, caption, file ka naam, aur jis channel se forward hua uska naam. Admins aur `/allowedit` wale members par locks/blocklist lagu nahi hote.

### Moderation

| Command | Kaam |
| --- | --- |
| `/ban [time] [reason]` | Ban (jaise `/ban 1d spam`) |
| `/dban` | Ban + reply kiya hua message delete |
| `/unban` | Ban hatao |
| `/kick`, `/dkick` | Chat se nikalo (wapas aa sakta hai) |
| `/mute [time] [reason]`, `/dmute` | Chup karao |
| `/unmute` | Mute hatao |
| `/warn [reason]`, `/dwarn` | Warning do; limit par mute / remove / ban apne aap |
| `/unwarn`, `/clearwarns` | Ek warning wapas lo / record saaf karo |
| `/warns` | Warning record |
| `/whois` | Member ki profile |

### Chat tools

| Command | Kaam |
| --- | --- |
| `/pin [loud]`, `/unpin` | Pin / unpin |
| `/setwelcome <text>` | Naye members ke liye greeting (`{name}`, `{group}`) |
| `/welcome on` / `off` | Greeting chalu / band |
| `/setrules <text>` | Chat rules publish karo |

### Sabke liye

`/rules`, `/report`, `/warns`, `/id`, `/help`

Time format: `30s`, `10m`, `1h 30m`, `1d`.

## Setup

1. [@BotFather](https://t.me/BotFather) se bot banao aur token lo.
2. BotFather mein `/setprivacy` → **Disable** karo taaki bot sab messages dekh sake.
3. Bot ko group mein **admin** banao: **Delete messages**, **Ban users** aur **Pin messages** ke saath.
4. Server par Docker se chalao:

```bash
docker build -t edit-guard-bot .
```

```bash
docker run -d --name editguard --restart unless-stopped -e BOT_TOKEN=123456:ABC... -v editguard:/data edit-guard-bot
```

Naya code aane par update:

```bash
./update.sh
```

## Environment variables

| Variable | Default | Matlab |
| --- | --- | --- |
| `BOT_TOKEN` | — | BotFather ka token (zaroori) |
| `DB_PATH` | `bot.db` | SQLite file ka path |
| `NSFW_THRESHOLD` | `0.5` | Filter ki sensitivity (kam = zyada strict) |

## Notes

- Settings, warnings, blocklist aur pending deletions SQLite mein save hote hain; restart ke baad bhi timers chalte rehte hain.
- `/purgeuser` sirf wahi messages hata sakta hai jo bot ne pichhle 48 ghante mein dekhe hain.
- Explicit-media detection [NudeNet](https://github.com/notAI-tech/NudeNet) se locally hota hai. Video / GIF / animated sticker ka thumbnail scan hota hai, isliye detection 100% perfect nahi hai.
- Bot copyrighted content ko khud nahi pehchaan sakta; locks aur blocklist se aap tay karte ho kya rokna hai.
