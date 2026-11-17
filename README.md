# Edit Guard Bot

Telegram group bot: edited messages delete karta hai, media auto-delete karta hai, NSFW media block karta hai, aur admins ko poora moderation toolkit deta hai.

> ⚠️ [Shibi](https://t.me/), your message was deleted because you edited it.

Naam clickable hota hai — tap karne par user ki poori profile khulti hai.

## Features

- **Edit Guard** — edited message delete (turant ya delay ke baad) + notice
- **Media auto-delete** — photo, video, sticker, GIF, file, voice ek tay samay baad delete
- **NSFW protection** — adult sticker / photo / video / GIF hatao (text par lagu nahi)
- **Locks** — members ke links aur forwarded messages block
- **Moderation** — ban, kick, mute, warn (limit par automatic action), purge, pin
- **Reports** — member `/report` kare to admins ko Delete / Warn / Mute / Ban buttons milte hain
- **Welcome & rules** — naye member ko welcome message, rules ka button
- **Cleanup** — join/leave messages aur bot ke apne notices auto-delete
- **Inline settings panel** — `/settings` se sab kuch buttons se set hota hai
- **"/" command menu** — admins ko group mein poori command list dikhti hai, members ko chhoti list

## Commands

Admin commands mein user chunne ke teen tareeke: uske message par reply, user ID, ya `@username`.

### Protection (admins)

| Command | Kaam |
| --- | --- |
| `/settings` | Inline settings panel |
| `/editdelay 2m` | Edited message itni der baad delete (`0` = instant) |
| `/mediadelay 10m` | Saara media itni der baad delete (`off` = band) |
| `/nsfw on` / `off` | NSFW protection |
| `/auth`, `/unauth` | User ko edit karne ki permission do / hatao (links/forwards lock se bhi chhoot) |
| `/authmedia`, `/unauthmedia` | User ka media kabhi delete na ho (auto-delete aur NSFW dono se chhoot) |
| `/authlist` | Authorized users ki list |

Purane naam bhi chalte hain: `/set_delay`, `/set_delaymedia`, `/auth_media`, `/unauth_media`.

### Moderation (admins)

| Command | Kaam |
| --- | --- |
| `/ban [time] [reason]` | Ban (time optional, jaise `/ban 1d spam`) |
| `/unban` | Ban hatao |
| `/kick [reason]` | Group se nikalo (wapas join kar sakta hai) |
| `/mute [time] [reason]` | Message bhejne se roko |
| `/unmute` | Mute hatao |
| `/warn [reason]` | Warning do; limit par mute / kick / ban apne aap |
| `/unwarn`, `/resetwarns` | Ek warning hatao / sab saaf karo |
| `/warns` | User ki warnings dekho |
| `/info` | User ki details |

### Tools (admins)

| Command | Kaam |
| --- | --- |
| `/del` | Reply kiya hua message delete |
| `/purge` | Reply kiye message se neeche tak sab delete (max 500) |
| `/pin [loud]`, `/unpin` | Pin / unpin |
| `/setwelcome <text>` | Welcome message (`{name}`, `{group}` use kar sakte ho) |
| `/welcome on` / `off` | Welcome chalu / band |
| `/setrules <text>` | Group rules set karo |

### Sabke liye

| Command | Kaam |
| --- | --- |
| `/rules` | Group rules |
| `/report` | Reply kiye message ko admins ko report karo |
| `/warns` | Apni warnings dekho |
| `/id` | Chat aur user ID |
| `/help` | Command menu |

Time format: `30 sec`, `2 minute`, `10m`, `1 hour`, `1h 30m`, `1d`.

## Setup

1. [@BotFather](https://t.me/BotFather) se bot banao aur token lo.
2. BotFather mein `/setprivacy` → **Disable** karo taaki bot sab messages dekh sake.
3. Bot ko group mein **admin** banao: **Delete messages**, **Ban users** aur **Pin messages** permission ke saath.
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

Docker ke bina:

```bash
pip install -r requirements.txt
```

```bash
BOT_TOKEN=123456:ABC... python bot.py
```

## Environment variables

| Variable | Default | Matlab |
| --- | --- | --- |
| `BOT_TOKEN` | — | BotFather ka token (zaroori) |
| `DB_PATH` | `bot.db` | SQLite file ka path |
| `NSFW_THRESHOLD` | `0.5` | NSFW sensitivity (kam = zyada strict) |

## Notes

- Settings, warnings aur pending deletions SQLite mein save hote hain; restart ke baad bhi timers chalte rehte hain.
- NSFW detection [NudeNet](https://github.com/notAI-tech/NudeNet) se locally hota hai (koi API key nahi). Video / GIF / animated sticker ka thumbnail scan hota hai, isliye detection 100% perfect nahi hai.
- Default: edit aur NSFW rules sirf members par (`User`); media auto-delete sab par (`Admin | User`). `/settings` se badal sakte ho.
