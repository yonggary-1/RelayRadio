## RelayRadio v0.1 (한국어)

첫 릴리즈입니다.

- 유튜브 플레이리스트를 Audio Station과 DS audio에서 들을 수 있는 MP3 라디오 방송국(`/<이름>.mp3`)으로 만듭니다.
- yt-dlp와 ffmpeg로 실시간 변환하며, 파일을 디스크에 저장하지 않습니다.
- 누군가 들을 때만 재생하고, 같은 방송국의 청취자는 모두 같은 방송을 듣습니다.
- 사용자마다 iOS 공유 단축어로 바꾸는 개인 채널 `streaming-<아이디>.mp3`가 있습니다.
- 웹 화면에서 여러 사용자, 관리자의 사용자 관리, `cookies.txt` 업로드, yt-dlp 업데이트를 지원합니다.
- 이어 듣기, 셔플, 다음 곡, 곡 제목 표시(ICY 메타데이터)를 지원합니다.
- 시놀로지 Container Manager용 Docker 이미지(Python 3.12, ffmpeg, Deno)와 docker-compose 파일을 제공합니다.
