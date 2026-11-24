English | [한국어](README.ko.md)

# RelayRadio

RelayRadio is a small server that **turns YouTube playlists you choose into internet radio stations on your home NAS**. It was built to listen to YouTube playlists through the radio feature of Synology Audio Station and the iOS DS audio app. Nothing is downloaded to disk: audio is converted in real time, and only while someone is listening. One tap on the YouTube app's Share button switches what your personal channel plays.

> This is a personal-use project. Accessing YouTube through yt-dlp may violate YouTube's Terms of Service. Use it only for yourself or your household, and do not publish your stream URLs.

## Features

- **Stations**: every playlist gets a fixed stream URL such as `http://NAS:8000/jazz.mp3`.
- **On-demand playback**: nothing runs while nobody listens. A station stops 30 seconds after its last listener leaves.
- **Broadcast mode**: everyone on a station hears the same song. Ten listeners still cost one conversion.
- **Personal streaming channel**: each user has `streaming-<username>.mp3`, which plays the playlist they shared last.
- **iOS Share shortcut**: share from the YouTube or YouTube Music app and your channel switches immediately.
- **Resume, shuffle, skip**, plus song titles via ICY metadata.
- **Multiple users**: web login, user management for admins, and stations that all users manage together.
- **Private playlists**: upload a `cookies.txt` to play them.
- **yt-dlp auto-update**: on start-up and once a day, or on demand from the web UI.

## Requirements

- An x86-64 Linux host with Docker. RelayRadio targets the Synology **DS923+** (DSM 7.2, Container Manager).
- About 500 MB of RAM in total, so the stock 4 GB is plenty.
- Stream format: MP3 192 kbps, 44.1 kHz stereo.
- Components: Python 3.12, aiohttp, yt-dlp, ffmpeg, and Deno (yt-dlp needs it for YouTube).

## Installation (Synology Container Manager)

1. In **File Station**, create `docker/relayradio` and upload every file from this repository (`Dockerfile`, `docker-compose.yml`, `requirements.txt`, `app/`, …). **A `data` folder must exist inside it.** Create it if it is missing, because Container Manager cannot mount a folder that does not exist and the build fails.
2. Set `ADMIN_PASSWORD` in `docker-compose.yml`. It is only used on the first start.
3. **Container Manager → Project → Create**
   - Project name: `relayradio`
   - Path: `/docker/relayradio`
   - Source: **Use existing docker-compose.yml**
4. Finish the wizard. The image builds and starts, which takes a few minutes the first time.
5. Open `http://NAS-IP:8000` and sign in as `admin` with your password.

Data (users, stations, cookies) lives in `docker/relayradio/data`.

## Usage

### Create a station
Under **새 방송국 추가** (Add station), enter a playlist URL, a name, and a short URL name such as `jazz`. All of these URL forms work:
`youtube.com/playlist?list=…`, `youtube.com/watch?v=…&list=…`, `youtu.be/…`, `music.youtube.com/playlist?list=…`

- **YouTube mixes** (`list=RD…`) never end, so they are loaded 100 songs at a time. After those 100 songs, the mix is loaded again.
- **Sharing a single video** plays the YouTube mix that starts with that video. You hear related songs, like a radio station, instead of one song on repeat.

### Listen in DS audio
1. In **Audio Station → Radio → User defined**, add a radio station.
2. Give it any name, and set its URL to the station's **stream URL** from the web UI, for example `http://192.168.0.10:8000/jazz.mp3`.
3. The station appears under **Radio** in the iOS DS audio app.

### iOS Share shortcut
Build the shortcut by following the **iOS 공유 단축어** card in the web UI, which shows your share endpoint and token. From then on, choose **Share → (More) → RelayRadio로 보내기** in the YouTube app, and `streaming-<username>.mp3` switches to that playlist. To keep a playlist as a station, use **지금 플레이리스트를 방송국으로 저장** (save current playlist as a station).

### Private playlists
Export your YouTube cookies as a Netscape-format `cookies.txt`, for example with a browser extension, and upload it under **관리 → 유튜브 로그인** (Admin → YouTube login). Use a **secondary account** rather than your main account, because the account may be restricted.

### Listening away from home
Port 8000 on the NAS must be reachable from outside. Use a VPN, or put Synology's reverse proxy with HTTPS in front and set `PUBLIC_URL` and `STREAM_KEY`.

## Configuration (environment variables in docker-compose.yml)

| Variable | Default | Description |
|---|---|---|
| `ADMIN_USER` / `ADMIN_PASSWORD` | `admin` / (random) | Admin account created on first start. If the password is empty, a random one is printed in the log |
| `BITRATE` | `192` | MP3 bitrate (kbps) |
| `GRACE_SEC` | `30` | Seconds to keep a station running after its last listener leaves |
| `AUTO_UPDATE` | `1` | Update yt-dlp on start-up and once a day |
| `PUBLIC_URL` | (auto) | Base URL shown in the UI, for example `https://radio.example.com` |
| `STREAM_KEY` | (none) | If set, stream URLs require `?key=…` |

## Troubleshooting

- **Songs fail with "곡 재생 실패" (track failed)**: press **yt-dlp 지금 업데이트** (update yt-dlp now) in the admin section. yt-dlp has to keep up with YouTube changes.
- **A few seconds of silence on first connect**: the station sends silence while it prepares the first song (2–7 s). This is expected.
- **Logs**: Container Manager → Container → relayradio → Log.

## License

[MIT](LICENSE)
