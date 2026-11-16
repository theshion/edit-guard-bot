# Edit Guard Bot

Telegram group bot jo edited messages delete karta hai, media auto-delete karta hai aur NSFW media block karta hai.

> ⚠️ [Shibi](https://t.me/), your message was deleted because you edited it.

Naam clickable hota hai — tap karne par user ki poori profile khulti hai.

## Commands (sirf admins)

| Command | Kaam |
| --- | --- |
| `/settings` | Current settings + buttons: rule sirf `User` par lage ya `Admin \| User` dono par |
| `/set_delay 2 minute` | Edited message turant nahi, 2 minute baad delete hoga (`0` = instant) |
| `/set_delaymedia 10 minute` | Saara media (photo, video, sticker, GIF, file, voice...) 10 minute baad delete; text nahi (`off` = band) |
| `/auth` | Is user ko message edit karne ki permission |
| `/unauth` | Edit permission hatao |
| `/auth_media` | Is user ka media bot kabhi delete nahi karega (auto-delete aur NSFW dono se chhoot) |
| `/unauth_media` | Media permission hatao |
| `/authlist` | Authorized users ki list |
| `/nsfw on` / `/nsfw off` | Adult sticker / photo / video / GIF delete (text par lagu nahi) |

`/auth` wale commands: user ke message par reply karke bhejo, ya `/auth 123456789`, ya `/auth @username`.

Time format: `30 sec`, `2 minute`, `10m`, `1 hour`, `1h 30m` (max 48h).

## Setup

1. [@BotFather](https://t.me/BotFather) se bot banao aur token lo.
2. BotFather mein `/setprivacy` → **Disable** karo (ya bot ko group admin banao) taaki bot sab messages dekh sake.
3. Bot ko group mein **admin** banao, **Delete messages** permission ke saath.
4. Run karo:

```bash
pip install -r requirements.txt
```

```bash
BOT_TOKEN=123456:ABC... python bot.py
```

Ya Docker se:

```bash
docker build -t edit-guard-bot .
```

```bash
docker run -d --restart unless-stopped -e BOT_TOKEN=123456:ABC... -v editguard:/data edit-guard-bot
```

## Environment variables

| Variable | Default | Matlab |
| --- | --- | --- |
| `BOT_TOKEN` | — | BotFather ka token (zaroori) |
| `DB_PATH` | `bot.db` | SQLite file ka path |
| `NSFW_THRESHOLD` | `0.5` | NSFW sensitivity (kam = zyada strict) |

## Notes

- Settings aur pending deletions SQLite mein save hote hain, restart ke baad bhi timers chalte rehte hain.
- NSFW detection [NudeNet](https://github.com/notAI-tech/NudeNet) se locally hota hai (koi API key nahi). Video / GIF / animated sticker ka thumbnail scan hota hai, isliye detection 100% perfect nahi hai.
- Default: edit aur NSFW rules sirf members par (`User`); media auto-delete sab par (`Admin | User`). `/settings` se badal sakte ho.
