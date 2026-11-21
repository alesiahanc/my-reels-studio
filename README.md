# Reels Studio

A small app that runs on your Mac and turns your art-process photos and videos into 1080×1920 Instagram reels. Everything stays on your computer.

## One-time setup (about 5 minutes)

1. **Install Homebrew** if you don't have it. Open the **Terminal** app, paste the install command from <https://brew.sh>, and press Enter.
2. **Install FFmpeg** (the video engine). In Terminal, run:
   ```
   brew install ffmpeg
   ```
3. **Unzip** `reels_studio.zip` and put the `reels_studio` folder anywhere you like, for example in Documents.

## Starting it

Double-click **`Start Reels Studio.command`**. A Terminal window opens and the app opens in your browser at <http://127.0.0.1:8765>. Keep that Terminal window open while you work, and close it when you're done.

> **First time only:** macOS may block the launcher because it was downloaded from the internet. If that happens, go to **System Settings → Privacy & Security**, scroll down, and click **Open Anyway**.
> You can also open Terminal, type `bash ` (with a space), drag the `.command` file into the window and press Enter.

## Making a reel

1. **Add media.** Drag files into the app, or (better for big videos) drop them into the **Library folder**: click *Open Library folder* in the app. It's `~/ReelsStudio/Library`, and subfolders are fine. Click *Refresh*.
2. **Build the order.** Click items on the left to add them. Drag ⋮⋮ to reorder. Click a thumbnail to choose what stays inside the 9:16 frame. Use *Whole + blur* for wide shots where the whole canvas must be visible. For videos, you can set *Use from / to* to skip setup or cleanup footage.
3. **Choose a template and length:**
   - **Reveal**: finished piece first (hook), fast process, finished piece again. Put the finished artwork **last**. This is the best default for reach.
   - **Timelapse**: the whole process compressed. Ends on the last item.
   - **Progression**: quick cuts with slow zooms. Good for work-in-progress photos.
   - **Details**: close-up clips at real speed, then the full piece.
   - **Custom**: items play in your order with their own settings. Photos show for 2.5 s and videos play their trimmed part at real speed, unless you change them. The Length buttons are ignored.
4. **Add hook text** (optional), plus optional ending text and a signature or logo. The preview shows where Instagram's buttons and caption will cover the frame.
5. Click **Make reel**, then **Download MP4** and **Download cover**, and post from your phone. Every reel is also saved in `~/ReelsStudio/Reels/`.

## Settings for each item (work with every template)

- **Length**: how many seconds this item stays on screen. Leave it empty to let the template decide.
- **Speed** (videos): ¼× or ½× slow-motion, 1× real, or 2×/4×/8×. Leave it on *auto* to let the template fit the clip.
- **Motion** (photos): zoom in, zoom out, still, or **pan** →, ←, ↓, ↑. Pans slide across wide paintings. Use **Closeness** 2× or more to get in tight, click the thumbnail to choose which band to follow, and set *Pan from/to* (in %) to stop before the desk shows.
- **Stabilise** (videos): smooths out hand shake, using FFmpeg's vidstab. The top bar says *Stabilise ok* when it's available. It crops about 5% at the edges. The first render of a clip takes about a minute longer; after that the result is cached in `~/ReelsStudio/.cache/stabilised`. It works best on slow pans and close-ups. Leave it off for fast deliberate camera moves and for clips where hands move through the frame. Clips with more than 3 minutes of used footage aren't stabilised, because a timelapse hides shake anyway.
- **Text on this item**: a caption shown only while that item is on screen. Choose where it goes under *Text on items goes*.

## Good to know

- **Huge videos are fine.** There's no 30-minute limit. Long recordings become a "jump-cut timelapse" built from short sped-up moments spread across the whole video, so a 3-hour clip renders about as fast as a 3-minute one.
- **Sound:** *Original*, *Quieter*, or *Silent*. With brush and pencil sounds, Original often works well. You can also add a trending sound inside Instagram when you post.
- **iPhone photos (HEIC) and iPhone HDR video** are handled automatically. If HDR clips look washed out, the app shows a warning. Turning off *Settings → Camera → Formats → HDR Video* on the iPhone avoids the problem.
- **Removing media:** delete or move files in the Library folder in Finder.
- **Starting over:** your current reel order and text settings are remembered in the browser. Use *Clear* to empty the order.

## Files

| File | What it is |
|---|---|
| `Start Reels Studio.command` | Launcher (double-click) |
| `server.py` | Local web server (Python standard library only, no packages to install) |
| `render.py` | Video engine: templates, cropping, timelapse, text, built on FFmpeg |
| `static/index.html` | The app's page |

Advanced: you can set the `REELS_STUDIO_HOME` environment variable to move the Library/Reels folders, and `REELS_STUDIO_PORT` to change the port.

## Spot the details reels

Choose the **Spot the details** template, add **one painting** (a photo) to the list, then click the painting to mark up to 6 things to find. Name each one; the name appears in the checklist. Drag a circle to move it, and use *size* and the zoom (e.g. 2.2×) to fit each detail. Press **▶ Play preview** to watch it, then **Make reel**.

The reel follows your approved v5 format: the title and 2×3 checklist sit above a large painting (about 900 px wide). The painting starts foggy and gets clearer with each item, then "answers revealed in 3... 2... 1..." builds up on the painting. The answers pop up magnified in white circles, one at a time, and tick their boxes. The title flips to "How many did you find?", followed by an end card asking for numbers in the comments. The title, end text and pace can be changed. The reel is silent, so add music in Instagram.
