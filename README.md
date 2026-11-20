# Clipper

Clipper finds the moments in a stream where chat lost it, cuts them out in the best quality available, and turns them into vertical Shorts.

It works on **Twitch, YouTube and Kick**, both on **past streams** (VODs) and on **streams that are live right now**.

You choose how clips are found, one way or several at once:

- **Chat spikes:** chat runs at 2× the stream's average or more. The score weighs how far chat jumped, how many different people were typing, how long the spike lasted and what kind of reaction it was (laughing, hype, shock, "clip it").
- **What's said:** reaction phrases ("no way", "oh my god", "let's go"…) and any words you give it. Past streams use YouTube's own auto-generated subtitles, so nothing is listened to or transcribed. Twitch and Kick VODs have no subtitles, so this option isn't available for them. Live streams are transcribed on your computer (needs the speech-to-text model).
- **Streamer gets loud** (live streams only): the streamer's voice goes at least 6 dB above their usual level over the last 10 minutes.

Each moment gets a score out of 100, and moments where several signals agree rank higher. Only clips at or above the **minimum score** (85 by default) are kept, so you don't get weak ones; if nothing makes it, Clipper tells you the best score it found. Every clip shows which combination of signals found it (for example *Chat spikes + What's said*).

The dashboard runs on your own computer:

- a library of your streams on the left: open one, or tick several and delete them;
- a chart of chat activity (and the streamer's voice, if you picked it) across the stream, with each clip marked;
- a ranked list of clips with what chat reacted to and what the streamer said; tick clips to delete several at once;
- a read-only **Runtime health** panel for worker state, candidate counts, errors, CPU, memory and disk;
- a ranked list of candidates with the signals that selected each one;
- an optional **Antigravity quality review** after extraction: it checks a low-resolution contact sheet plus bounded transcript/chat context and returns KEEP, REJECT or REVIEW; only completed REJECT results are removed from the active library and moved to the system Trash / Recycle Bin where available;
- a Short editor: crop the face cam and gameplay, choose the layout and subtitle style, then render manually;
- every delete asks first, and deleted files go to the Trash.

## Install

macOS or Linux:

```sh
curl -fsSL https://raw.githubusercontent.com/guleriaakshit/clipper/main/install.sh | bash
```

Windows (PowerShell):

```powershell
irm https://raw.githubusercontent.com/guleriaakshit/clipper/main/install.ps1 | iex
```

The installer checks for git, ffmpeg and Python 3.11+ and offers to install anything missing (with Homebrew, apt, dnf or pacman on macOS and Linux, and winget on Windows). It sets up Clipper in `~/Clipper` (`%USERPROFILE%\Clipper` on Windows; you can pick another folder). Then it asks:

1. **Speech-to-text?** It downloads NVIDIA Parakeet (~490 MB), which runs offline. It's used for live streams and for the subtitles of Twitch and Kick clips (YouTube clips use YouTube's own subtitles). If you skip it, those get no subtitles and everything else works.
2. **The Short renderer?** It installs Remotion, which needs Node.js 18+ (~400 MB).
3. **A `clipper` terminal command?**
4. Optional local Antigravity CLI review; the CLI is configured separately and is not required to extract clips.

To install without questions, add `--yes` (`... | bash -s -- --yes`; on Windows set `$env:CLIPPER_YES = "1"` first).

### Update

```sh
clipper update
```

This pulls the latest Clipper from GitHub and refreshes whatever changed: the Python packages and the renderer's packages. Your streams, clips and settings aren't touched. Restart `clipper serve` afterwards if it was running. (Installed before `clipper update` existed? Run the install command once more, then use `clipper update` from then on.)

## Use

```sh
clipper serve                                   # dashboard at http://localhost:8765
clipper analyze https://www.twitch.tv/videos/…  # a past stream, found by chat spikes
clipper analyze <link> --by chat,speech --words "no way,clip that" --min-score 80   # chat + what's said, keep 80+
clipper live https://kick.com/somechannel      # a stream that's live now (Twitch, YouTube or Kick; `clipper stop <job>` to stop)
clipper resume <job>                           # safely recover an interrupted live session
clipper cleanup                                # preview old temporary caches and diagnostic logs
clipper cleanup --apply                        # apply the displayed cleanup plan
clipper render <job> <clip number>              # render a Short from the terminal
clipper list                                    # every stream you've clipped
clipper stt-download                            # add speech-to-text later
clipper update                                  # get the latest version from GitHub
```

You can also paste any of these links straight into the dashboard.

### Past streams

- **Twitch:** chat comes from Twitch's public web API. No login is needed.
- **YouTube:** chat comes from the video's chat replay, which must have been on during the stream.
- **Kick:** chat comes from Kick's chat replay, the same public endpoint kick.com uses for logged-out visitors. A long VOD is read in parallel ten-minute parts: a 9.6-hour stream with 27,000 messages took about 6 minutes. If Kick ever asks for a bot check, Clipper stops instead of trying to get around it.
- **Any platform:** you can also add an exported chat file with `--chat-file chat.json` (or under *More options* in the dashboard). This accepts TwitchDownloader JSON, chat-downloader JSON, or a CSV with `t,user,text` columns.
- **Members-only or age-gated videos:** use `--cookies-from-browser chrome` (or safari, firefox, brave, edge).

Clipper downloads only the clips themselves, in the best resolution and then the highest frame rate available.

### Live streams

Clipper watches the stream as it happens:

- It reads chat live: Twitch through its public chat server, read-only and without a login; YouTube through yt-dlp; Kick through Kick's realtime chat service with an anonymous token.
- It records the video into a rolling 10-minute buffer.
- The recorder retries transient HTTP and fragment failures with exponential backoff. If it exits, Clipper restarts it and waits for a fresh segment. For YouTube, recovery continues while the live-status check is live or inconclusive; the watcher ends only after two explicit post-live checks, the configured watch limit, or a manual stop. Twitch and Kick retain bounded recorder restart attempts. YouTube live chat reconnects with message-ID deduplication.
- One live source is allowed at a time. A paused source still occupies the slot; stop it before starting another. When a worker crashes, saved chat and buffer timestamps remain tied to the original session clock. Use **Recover & resume** on the dashboard (or `clipper resume <job>`) to continue. Clipper validates every interrupted candidate against the saved segment timeline and only re-extracts moments whose complete footage still exists; it will not join segments across a recording gap.
- It cuts each spike into a clip once chat calms down.

Times count from when Clipper started watching. It stops when you press **Stop watching**, when the stream ends, or after the number of hours set under *More options* (6 by default). For live jobs, use **Pause** and **Resume** to suspend and continue the watcher plus its media/chat subprocesses. **Stop watching** resumes a paused worker before requesting a clean stop.

### Storage safety

Clipper keeps a configurable free-space reserve (2 GiB by default). It checks before live capture, while a live watcher runs, before each VOD clip download and before each Short render. If a live session crosses the reserve, Clipper stops capture cleanly and removes its temporary rolling buffer; already extracted clips are preserved. The Runtime health panel warns when free space is below the reserve or when the volume cannot be inspected. Set `CLIPPER_MIN_FREE_GB=3` before launching Clipper to reserve 3 GiB instead; set it to `0` only to deliberately disable the guard. This does not automatically delete old jobs or finished media.

### Review and make a Short

When automatic Antigravity review is enabled under **More options**, Clipper reviews each extracted candidate before you decide what to produce:

- **KEEP:** the candidate remains available for editing.
- **REJECT:** the candidate is removed from the active library and its folder is moved to the system Trash / Recycle Bin where available.
- **REVIEW:** the candidate stays available for a human decision.

The **Review with Antigravity** button can also review an already extracted clip. While it runs, the button and clip selector are disabled and a progress message is shown. Review uses a low-resolution contact sheet with up to nine sampled frames plus bounded transcript/chat context; the original clip and full stream are not sent. The CLI runs in a temporary workspace with the configured sandbox and permission-bypass flags. If the CLI is unavailable or its response cannot be validated, the result is REVIEW and the clip is retained.

A passing candidate is not automatically rendered. Open **Make the Short**, frame the face cam and gameplay, choose the layout and subtitle style, then press **Render Short**. You get `final.mp4` (1080×1920, at the source frame rate up to 60 fps) and `final.srt`.

Effects live in `clips/<n>/fx.json` next to the dashboard's `edit.json`; the full list of fields is in `clipper/render.py` (`normalize_fx`). Memes for an edit come from `clipper meme search <words> [--kind videos|sounds|images]` and `clipper meme get <job> <clip> <id>`.

## How it works

| Step | Code |
| --- | --- |
| Chat → messages per 5 s → average → spikes → score | `clipper/spikes.py` |
| Loud voice and what's said, mixed with chat | `clipper/signals.py` |
| YouTube's own subtitles for past streams | `clipper/captions.py` |
| Clip window: starts 20 s before chat reacts and ends after chat calms down (20–60 s) | `clipper/spikes.py` |
| Download only that part (`yt-dlp --download-sections`) | `clipper/download.py` |
| Chat for each platform (Twitch, YouTube, Kick, chat files) | `clipper/chat/` |
| Live: chat readers, rolling recorder, cutting | `clipper/live.py` |
| Hottest moment: the loudest 3 s just before chat peaks | `clipper/hotspot.py` |
| Word timings (Parakeet through sherpa-onnx) | `clipper/stt.py`, `clipper/subs.py` |
| Face cam guess (OpenCV) | `clipper/face.py` |
| Render (Remotion), effects from `fx.json` | `clipper/render.py`, `remotion/src/` |
| Memes for edits (YouTube, myinstants, memegen/Imgflip) | `clipper/memes.py` |
| Candidate quality review (sampled frames + bounded context) | `clipper/ai_review.py` |
| `clipper update` | `clipper/update.py` |
| Dashboard (Gradio) | `clipper/app.py` |

Everything Clipper downloads or makes stays in the install folder: `jobs/` for streams and clips, `tmp/` for caches.
### Budgets, feedback, notifications and cleanup

Antigravity candidate review is capped at 20 attempts per local calendar day and 10 per stream by default. Configure `CLIPPER_AI_REVIEW_DAILY_LIMIT` and `CLIPPER_AI_REVIEW_JOB_LIMIT` before launching Clipper. A review attempt is counted before invocation so failures cannot create an unbounded retry loop.

After opening a clip, label it **Good clip**, **Poor clip** or **Not sure**. Clipper stores only the label, score and minimal candidate identifiers. After at least 20 good/poor labels, Runtime health may show an observed minimum-score suggestion. Clipper never changes the threshold automatically.

Desktop notifications are disabled by default. Set `CLIPPER_DESKTOP_NOTIFICATIONS=1` before launch to enable supported native notifications (macOS via osascript, Linux via notify-send, and Windows when BurntToast is available).

`clipper cleanup` is a dry-run preview for temporary cache files older than 14 days and diagnostic logs older than 30 days. Use `clipper cleanup --apply` only after reviewing the listed paths. This cleanup never removes source clips, finished Shorts, job metadata or rolling-buffer footage.

## Notes

- Clipper works with other people's streams and memes. Check what you're allowed to post where you post it.
- The dashboard font is Instrument Sans (SIL Open Font License, `clipper/assets/OFL.txt`).
- Meme sources: YouTube for short video memes (fetched with yt-dlp), [myinstants.com](https://www.myinstants.com) for sounds, and [memegen.link](https://memegen.link) and [Imgflip](https://imgflip.com) for images.

MIT licence.
