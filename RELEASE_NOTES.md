## RelayRadio v0.1

First release.

- Turns YouTube playlists into on-demand MP3 radio stations (`/<name>.mp3`) for Synology Audio Station and DS audio.
- Real-time conversion with yt-dlp and ffmpeg. Nothing is written to disk.
- Stations run only while someone is listening, and every listener on a station hears the same stream.
- Each user has a personal `streaming-<username>.mp3` channel that the iOS Share shortcut switches.
- Web UI with multiple users, admin user management, `cookies.txt` upload, and yt-dlp updates.
- Resume, shuffle, skip, and song titles via ICY metadata.
- Docker image (Python 3.12, ffmpeg, Deno) and docker-compose file for Synology Container Manager.
