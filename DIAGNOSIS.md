# Graphony: why the last 4 reels stalled at 119-147 views

Diagnosis pass, 2026-10-13. Read-only: nothing was published, edited or deleted on Instagram or Facebook,
and no rendering or publishing code was changed. Every Graph API call was a GET. Tokens are not in this
file or in `diagnosis_frames/`; the saved files were checked for them.

Evidence files:

- `diagnosis_frames/<run>_t{0.0,0.5,1.0,2.0}.png`: frames extracted with ffmpeg
- `diagnosis_frames/<run>_ig_cover.jpg`: the cover Instagram actually shows, downloaded from `thumbnail_url`
- `diagnosis_frames/_contact_sheet_first_2s.png` and `_ig_covers.png`: side-by-side views of those frames and covers
- `diagnosis_frames/frame_stats.json`: brightness and per-element pixel coverage for each frame
- `diagnosis_frames/graph_api_pull.json`: the raw Graph API responses (token redacted)

## Short answer

1. **The Instagram account has no audience, and its profile says "meme page".** The reels go out on
   `@dankdoodlesxd` (display name "New insaan", bio "Follow for more"). It has 1 follower. 29 of its 33
   posts are meme posts from May and June 2025. All 449 accounts reached since Oct 8 were non-followers.
   Each reel got the small first audience Instagram gives a new reel (104-124 accounts) and was never shown
   to more people.
2. **The first 3 seconds lose 30-53% of viewers.** Instagram's `reels_skip_rate` was 30.1%, 43.2%, 38.9%
   and 53.4%. For the first 0.6 s the video is 99.3% empty background. The video never shows a hook: the
   title was taken off the frame in fix pass 01. The first data note plays at 0.8 s.
3. **People leave long before the payoff.** Average watch time was 11.9-17.4 s on 40.6-44.9 s videos
   (29-39%). The comparison only appears on the end card, at 37-41 s. Across all 4 reels there were
   0 saves, 0 shares and 0 comments.

The blank cover (3 of 4 reels), the caption, posting time and audio are weaker factors or inconclusive.
The detail and evidence are below.

---

## 1. Pipeline map

**Data fetch**
- `pipeline/fetch.py`: World Bank (wbgapi) and Our World in Data loaders; series cached in `cache/series/`.
- `topics.yaml` + `pipeline/topics.py`: each topic's title, subtitle (the metric), unit format and start year; `topics-verify`.
- `pipeline/score.py`: interestingness score (0-100) for each country series.
- `pipeline/picker.py`: picks the 5-8 country set for one video.
- `pipeline/selector.py`: picks the next topic (70% by topic weight, 30% exploration).
- `pipeline/labels.py`: turning point found in code; Gemini supplies only a 2-4 word event tag.

**Render**
- `pipeline/timeline.py`: the only source of timing (0.4 s intro, about 4.5 s per country, 3.5 s end card, event slow-mo).
- `pipeline/audio.py`: numpy synthesis (pluck, pad and fx stems), mastered to -14 LUFS.
- `pipeline/render.py`: draws every frame (via `pipeline/canvas.py`, skia or pillow) into ffmpeg; `build_meta()` writes the post caption and hashtags into `meta.json`.
- `pipeline/verify.py`: verification gate (ffprobe, loudness, A/V sync, centring, contact sheet).
- `pipeline/orchestrator.py`: runs the stages fetch, pick, label, render, notify, plus `publish_run()`.

**Approval bot**
- `pipeline/review.py`: the one-at-a-time review queue and the card wording.
- `pipeline/approve_telegram.py`: the Telegram bot (long polling, single self-editing card, `/new`, `/ready`, `/status`).
- `pipeline/actions.py`: approve, reject and publish operations shared by the bot and the dashboard.
- `dashboard/app.py`: local Flask dashboard (Review, Library, Settings).

**Publish**
- `pipeline/publish_instagram.py`: IG Reels and FB Page Reels through the Graph API (resumable upload, `thumb_offset`).
- `pipeline/meta_api.py`: spaces every Meta request at least 3 s apart, across processes.
- `pipeline/posted.py`: records a reel that went live without the publisher noting it.
- `pipeline/publish_youtube.py`: YouTube Shorts (turned off).
- `pipeline/analytics.py`: `run.py stats` pulls insights into the `stats` table and recomputes topic weights.

## 2. The first seconds

Videos checked: the 4 published reels, plus 2 older rejected renders (made before fix pass 01) for comparison.

| Run (topic) | t (s) | Mean luma (0-255) | Non-background pixels | Header (metric) | Country + flag | Chart band | Line pixels | Flag |
|---|---|---|---|---|---|---|---|---|
| 6a24 nuclear_share | 0.0 | 12.6 | 0.69% | yes | no | empty | 0 | **blank-like** |
| | 0.5 | 12.6 | 0.68% | yes | no | empty | 0 | **blank-like** |
| | 1.0 | 17.7 | 3.80% | yes | yes | grid | 1,421 | line just started |
| | 2.0 | 18.6 | 4.73% | yes | yes | grid + line | 8,617 | ok |
| 9d50 air_passengers | 0.0 | 12.3 | 0.58% | yes | no | empty | 0 | **blank-like** |
| | 0.5 | 12.3 | 0.57% | yes | no | empty | 0 | **blank-like** |
| | 1.0 | 17.0 | 3.45% | yes | yes | grid | 1,838 | line just started |
| | 2.0 | 17.7 | 4.02% | yes | yes | grid + line | 5,243 | ok |
| d91b landlines | 0.0 | 12.8 | 0.76% | yes | no | empty | 0 | **blank-like** |
| | 0.5 | 12.8 | 0.75% | yes | no | empty | 0 | **blank-like** |
| | 1.0 | 16.2 | 2.87% | yes | yes | grid | 1,567 | line just started |
| | 2.0 | 17.3 | 3.81% | yes | yes | grid + line | 6,983 | ok |
| 82c0 homicide_rate | 0.0 | 12.5 | 0.64% | yes | no | empty | 0 | **blank-like** |
| | 0.5 | 12.5 | 0.62% | yes | no | empty | 0 | **blank-like** |
| | 1.0 | 17.0 | 3.46% | yes | yes | grid | 2,484 | line just started |
| | 2.0 | 18.1 | 4.36% | yes | yes | grid + line | 9,318 | ok |
| 8357 nuclear (old, rejected) | 0.0 | 18.5 | 3.19% | title pill | no | empty | 0 | blank-like, but has a title |
| 9435 landlines (old, rejected) | 0.0 | 18.0 | 2.99% | title pill | no | empty | 0 | blank-like, but has a title |

"Non-background" means a pixel more than 40 levels away from `#0b0d12`. "Line pixels" are saturated
pixels inside the chart band. The background alone measures about 12.5 luma, so 0.0 s and 0.5 s are, in
effect, the empty background.

**What the render code draws at frame 0** (`render.py` `Renderer.draw_frame` and `intro_layer`, with timing from `timeline.py`):

| Time | On screen |
|---|---|
| 0.0 to 0.4 s (intro) | Background, one line of metric text (`topic.subtitle`, e.g. "Nuclear, % of electricity generated", bold 40-54 px at y = 160-220 of 1920), and 8 hollow progress dots. **No title, no hook, no country, no axes, no line.** |
| 0.4 to 0.6 s | The same frame, dipping through the shared chrome. |
| 0.6 to 0.8 s | The first country's name, flag, empty grid, year and value fade in. |
| 0.8 s | The line starts drawing, one country at a time, each on its own y-scale. |
| Audio | Digital silence from 0.0 to 0.35 s, a whoosh around 0.4-0.6 s, first data note at 0.80 s (measured in 50 ms RMS windows). |

The topic title, which is the only hook ("The year flying stopped", "The sound of the landline dying"),
**is never drawn on the video**. Fix pass 01 removed the title pill; the `render.py` docstring says "No
title pill ... (the hook and the data credit live in the post)". The older renders above still had it.

**Plain answer:** yes. Every published reel opens on a near-blank dark frame for **0.6 s (18 frames)**:
one small line of metric text and 8 dots, with no chart, country or line. The first country appears at
0.6-0.8 s and the line starts at 0.8 s. The metric *is* named from frame 0, but in small type at the very
top. No question, stakes or hook is ever on screen.

## 3. Cover check

| Reel | Posted | `thumb_offset` sent? | Cover Instagram uses (checked against `thumbnail_url`) |
|---|---|---|---|
| nuclear_share | 2026-10-09 | **no** | Frame 0: metric line and dots on black (mean pixel difference to our frame 0: 1.34/255) |
| air_passengers | 2026-10-09 | **no** | Frame 0 (difference 1.34/255) |
| landlines | 2026-10-09 | **no** | Frame 0 (difference 1.34/255) |
| homicide_rate | 2026-10-12 | yes, 5395 ms | The finished Netherlands chart, 1995-2024, "2023 / 0.7" |

`cover_url` is never used. `thumb_offset` was added in `publish_instagram.py::_cover_ms()`: it points at the
middle of the first country's hold, after its line is drawn. That change is **uncommitted** (the file was
modified 2026-10-10 16:54 IST; commit `2d82572` does not have it), so the 3 reels posted on Oct 9 went out
without it. With no `thumb_offset`, Instagram uses offset 0, the near-blank intro frame. On the profile
grid, those 3 reels are black tiles with a thin line of text, next to 29 meme thumbnails.

## 4. Captions and hashtags as posted

Read back from the live media objects with `GET /{media-id}?fields=caption`. They match
`meta.json["description_instagram"]`, built by `render.build_meta()`. The same text went to the Facebook
Page reels. Every reel uses the same template, and the hashtags are the same 4 every time.

**nuclear_share** (`DeRZjS4DPNx`)
```
The rise and fall of nuclear power. Each note is one year of real data: the higher the pitch, the higher the value.
What the number is: Nuclear, % of electricity generated.
Countries: United States, France, Canada, South Africa, Mexico, Pakistan, Argentina, Brazil.
Data: Our World in Data (CC BY 4.0), "Share of electricity generated by nuclear power".
Original sources: Ember - Yearly Electricity Data Europe (2026); Ember - Yearly Electricity Data (2026); Energy Institute - Statistical Review of World Energy (2025).

#dataviz #sonification #data #reels
```

**air_passengers** (`DeRZnMJjA2x`)
```
The year flying stopped. Each note is one year of real data: the higher the pitch, the higher the value.
What the number is: Passengers flown by each country's airlines.
Countries: Germany, South Korea, Brazil, Greece, Chile, Italy, Israel, South Africa.
Data: World Bank WDI (CC BY 4.0), "Air transport, passengers carried" (IS.AIR.PSGR).

#dataviz #sonification #data #reels
```

**landlines** (`DeRZ21mDHKi`)
```
The sound of the landline dying. Each note is one year of real data: the higher the pitch, the higher the value.
What the number is: Fixed phone lines per 100 people.
Countries: Brazil, South Africa, Egypt, Vietnam, Indonesia, Philippines, Iraq, Thailand.
Data: World Bank WDI (CC BY 4.0), "Fixed telephone subscriptions (per 100 people)" (IT.MLT.MAIN.P2).

#dataviz #sonification #data #reels
```

**homicide_rate** (`DeYd8dbDIE3`)
```
Murder rates, played as music. Each note is one year of real data: the higher the pitch, the higher the value.
What the number is: Homicides per 100,000 people per year.
Countries: Netherlands, Ireland, New Zealand, Mexico, Norway, Venezuela, Morocco.
Data: Our World in Data (CC BY 4.0), "Homicide rate per 100,000 population".
Original sources: United Nations Office on Drugs and Crime (2025).

#dataviz #sonification #data #reels
```

What stands out:
- **The visible preview is a definition, not a hook.** The first ~125 characters (what shows before
  "more") are the title plus "Each note is one year of real data...".
- **Nothing in the caption invites a reply.** There is no question, and all 4 reels got 0 comments.
- **The hashtags are weak.** There are only 4, `#reels` is a generic filler tag, and none matches the
  topic (energy, aviation, crime) or a country.
- **Most of the caption is a source citation.** That keeps the credit honest, but it leaves no room for
  a hook.

## 5. Insights

Pulled 2026-10-13 09:21 UTC, Graph API v25.0. The token is a Page token that never expires and includes
`instagram_manage_insights`. Durations come from ffprobe on the published files. IST is UTC+5:30.

| Reel | Posted (IST) | Length | Views | Reach | Avg watch | Avg watch % | Total watch time | 3 s skip rate | Likes | Comments | Shares | Saves | Reposts | Total interactions | Cover |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| nuclear_share | Fri 09 Oct, 16:57 | 44.9 s | 119 | 104 | 17.4 s | 38.9% | 30.5 min | **30.1%** | 3 | 0 | 0 | 0 | 0 | 3 | frame 0 |
| air_passengers | Fri 09 Oct, 16:57 | 44.3 s | **147** | 124 | 14.6 s | 33.0% | 31.0 min | **43.2%** | 0 | 0 | 0 | 0 | 0 | 0 | frame 0 |
| landlines | Fri 09 Oct, 16:59 | 41.8 s | 139 | 115 | 13.5 s | 32.2% | 25.9 min | **38.9%** | 0 | 0 | 0 | 0 | 1 | 1 | frame 0 |
| homicide_rate | Mon 12 Oct, 10:50 | 40.6 s | 133 | 117 | 11.9 s | 29.3% | 23.6 min | **53.4%** | 1 | 0 | 0 | 0 | 0 | 1 | chart (5.4 s) |

Every reel: **audio** is original generated audio (synthesised pluck, pad and fx embedded in the MP4).
There is no Instagram library or trending track; the Content Publishing API cannot attach one.
`share_to_feed` is true.

**Account context**

| | |
|---|---|
| IG account | `@dankdoodlesxd`, name "New insaan", bio "Follow for more", **1 follower**, follows 1, 33 posts |
| Posts before Graphony | 29, all from 28 May-5 June 2025: 28 reels (meme captions such as `#Meme #Memes #Funny #DankMemes ...`, a "#TruthBomb" one, or no caption) and 1 feed post of skull emojis. Nothing posted for 16 months before 9 Oct 2026. |
| Account reach since 8 Oct | 449 accounts, 543 views. **100% NON_FOLLOWER**, 100% REEL. |
| Facebook Page | "Graphony", **0 followers**. FB reels: air_passengers 3 plays (1 viewer, gone by second 4), landlines 0, homicide 0. The nuclear FB post failed on 10 Oct with `(#200) Cannot call API for app ... on behalf of user`. |
| Local stats table | **0 rows.** `run.py stats` has never stored anything (no scheduled job exists: `health:schedule` shows `jobs: []`). The topic weighting in `selector.py` therefore has no data to learn from. |

**Metrics that failed and were dropped**

| Metric | Error |
|---|---|
| `plays`, `ig_reels_aggregated_all_plays_count`, `clips_replays_count` | (#100) not a valid metric in v25.0 |
| `profile_visits`, `follows`, `profile_activity`, `navigation`, `replies`, `impressions` | (#100) not supported for REELS |
| `facebook_views`, `crossposted_views` | "Fatal" |

The same error message listed metrics that do exist; `reels_skip_rate`, `reposts` and `total_views` came
from that list.

## 6. Verdict: likely causes, strongest evidence first

### 1. No audience, and a profile that doesn't match the content (strong evidence)
- **Measured:** 1 follower, 100% of reach from non-followers, and 104-124 accounts reached per reel,
  in a tight band no matter the topic, day, time or cover.
- **What that means:** the reels never got past the first group of non-followers Instagram shows a new
  reel to.
- **What anyone who taps the profile sees:** `@dankdoodlesxd` / "New insaan" / "Follow for more", 29 meme
  thumbnails, and 3 black tiles. There is no reason there to follow a data channel.
- **Inference, not provable from the API:** the 2025 meme history and the unrelated name may also make it
  harder for Instagram to work out which audience to test the reels on. Instagram doesn't expose that
  signal, so this part is a judgement.
- **Facebook:** the Page has 0 followers and its reels got 0-3 plays, so Facebook adds nothing yet.

### 2. The first 3 seconds give no reason to stay (strong evidence of the problem; medium on the exact cause)
- **Measured:** 30-53% of people who started each reel left within 3 seconds.
- **Frames 0.0-0.6 s:** 99.3% empty background. The only content is a 40-54 px metric line and dots,
  placed in the top strip where Instagram draws its own Reels header.
- **What follows:** the first data note at 0.8 s, after 0.35 s of silence, and then one country's line
  climbing slowly, e.g. "GERMANY 1996 40.1M".
- **What's missing:** a hook, a question or stakes. The title, the closest thing to a hook, is never
  drawn on the video.
- **The cover is not the explanation.** The homicide reel had the good cover but the worst skip rate
  (53.4%), so the problem is the opening of the video, not the cover.
- **Unclear:** with 4 reels I can't separate "0.6 s blank" from "no hook in the first 3 s". The blank
  part is short, so the missing hook is probably the bigger half.

### 3. Low retention and a late payoff (strong evidence)
- **Measured:** viewers watched 29-39% of each video on average (11.9-17.4 s), so a typical viewer left
  during the 3rd or 4th country.
- **The payoff comes too late.** Each country is drawn alone on its own y-scale, so the comparison (who
  rose, who fell, who passed whom) only appears on the end card at 37-41 s. The question "Which one
  surprised you?" is there too, and about 70% of viewers never get that far.
- **Zero saves and zero shares,** with 5 interactions in total, so Instagram had no engagement signal
  to show the reels to more people.

### 4. Caption and hashtags (medium-weak evidence)
- **What was posted:** the caption preview is a definition of sonification, there's no question in the
  caption (0 comments), and there are 4 generic hashtags including `#reels`.
- **How much it matters:** Instagram has said publicly that hashtags do little for distribution, while
  caption keywords help search and topic classification. This data can't separate the caption's effect
  (there's no per-surface breakdown for these reels).
- **Verdict:** a real weakness, but it doesn't explain the 119-147 ceiling alone.

### 5. Blank cover on 3 of 4 reels (weak evidence as a cause of these view counts)
- **Measured:** confirmed from `thumbnail_url`; the Oct 9 covers are frame 0. The Oct 12 reel had a
  proper chart cover and got 133 views, inside the same range, so the cover is not what is capping views
  in the Reels feed (the feed autoplays from frame 0).
- **Where it does hurt:** the profile grid and any Explore or hashtag tile.

### 6. Posting time and cadence (weak, inconclusive)
- **What happened:** three reels went out within 2 min 40 s (Fri 16:57-16:59 IST). The Monday 10:50 IST
  reel went out alone and did no better (133 views).
- **Why time of day barely matters here:** with 1 follower, it mostly changes follower distribution, and
  there is none.
- **Still worth fixing:** posting three in a row puts them in competition for the same test audience.

### 7. Audio (inconclusive)
- **What we use:** original synthesised audio, with no trending sound (the API can't attach one).
- **Why it can't be judged:** no metric here isolates the effect of audio.

### Not a cause: "metric not named on screen"
The metric is on screen from frame 0. What is missing is the hook, not the metric name.

### Overall honesty check
This is 4 reels. The account-level findings (1 follower, 100% non-follower reach, meme history) and the
retention numbers are measured facts. The ranking between causes 2 and 3 is a judgement, because both
point at the same symptom: Instagram tested each reel on about 120 people, they didn't stay or interact,
and it stopped there.

## Recommended fixes, by expected impact (none applied in this pass)

1. **Give Graphony its own Instagram identity.**
   - Either create a new professional account named Graphony, linked to the Graphony FB Page, or rename
     this one, write a bio that says what it is, and archive the 29 meme posts.
   - This is a manual step in the Instagram app; no code change.
   - Expected impact: highest. Every later fix works on top of an audience Instagram can classify.
2. **Rebuild the first 3 seconds** (render change, so not done here):
   - From frame 0, show a large hook line on screen built from a computed fact (Part 2's `captions.py`
     produces these, e.g. "Watch Venezuela after 2015").
   - Show the chart and the first line already moving at frame 0, with no 0.6 s empty intro.
   - Start the first note at 0 s instead of after 0.35 s of silence.
   - Move the metric header out of the top bar strip and make it larger.
3. **Bring the payoff forward:**
   - Make videos shorter (20-30 s) with 4-5 countries, or overlay the lines on one shared axis so the race,
     the crossover or the record is visible while watching.
   - Show the question as on-screen text early (by about 10 s), not on the end card at 40 s.
4. **Captions:**
   - A hook line, a plain metric/countries/years line, then a question; 5 relevant hashtags with no
     `#reels`.
   - Part 2 builds this generator, wired into approval as a preview (see the note below).
   - Keep the data credit, lower down.
5. **Commit `thumb_offset`** (already in the working tree) so every reel gets the chart cover. Better: a
   designed cover frame with the hook text.
6. **Cadence:** at most 1-2 reels a day, a few hours apart. Right now, approving several cards in a row
   publishes them back to back.
7. **Measure:** schedule `run.py stats` daily, and add `reels_skip_rate`, `shares`, `saved` and
   `ig_reels_video_view_total_time` to `analytics.IG_METRICS` so the skip rate is tracked per reel.
   Today the stats table is empty and topic weights are blind.
8. **Facebook:** low priority until the Page has followers. Investigate the `(#200) Cannot call API for
   app ... on behalf of user` failure from 10 Oct.

## Notes for Part 2 (hashtag search test)

- **The endpoint works with this token.** `GET /ig_hashtag_search?user_id=...&q=dataviz` returned
  hashtag id `17842353388064405`.
- **`top_media` returns data, but only at `limit` 10 or less.** It returned 10 posts; asking for 25 with
  `permalink` gave HTTP 500 "Please reduce the amount of data you're asking for".
- **The quota is small.** Meta allows 30 unique hashtags per account per rolling 7 days. This test used
  1 (`dataviz`, 2026-10-13).
- **The current top posts for `#dataviz` are mostly carousels** from large accounts (1.7k-125k likes).
- **Missing from the brief:** the Part 2 brief was cut off after "If it works, add a". The trending check
  was built as an opt-in, cached and budgeted re-ranking of topic hashtags, and is off by default.
