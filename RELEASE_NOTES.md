## RelayRadio v0.1.1

Bug fixes for shared playlists.

- **Fixed**: YouTube mix links (`list=RD…`) failed with "This playlist type is unviewable". Mixes now load as `watch?v=…&list=RD…`, 100 songs per round.
- **Changed**: sharing a single video plays the YouTube mix that starts with it, instead of repeating that one song.
- **Fixed**: a song that appears more than once in a playlist plays only once per round.
- **Fixed**: the repository now contains an empty `data/` folder, so the Synology Container Manager build no longer fails with a missing folder.
- **Docs**: the README explains the required `data/` folder and how mixes and single videos play. `ROADMAP.md` lists the next goals.

To update, replace the files on the NAS and press **Build** on the project in Container Manager. The app code is built into the image, so a restart alone is not enough.
