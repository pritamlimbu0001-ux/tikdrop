# TikDrop — TikTok Video Downloader

Flask + yt-dlp downloader with a responsive neon/glass UI, detected quality selection (including 1440p/2K and 2160p/4K when actually available), and clearly labelled ad placements above and below the downloader.

## Run on Windows

Install Python 3.11 or newer, extract this folder, then open Command Prompt in the extracted `tiktok-downloader` folder and run: 

```bat
py -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
python app.py
```

Open http://127.0.0.1:5000. If yt-dlp needs to combine separate video/audio streams, install FFmpeg and ensure `ffmpeg` is on PATH.

## Ads: Google AdSense + direct sponsors

The page supports both AdSense units and direct sponsor cards in the top and bottom ad placements. **No ads earn money until you configure real ad IDs or real sponsor details.** AdSense requires your own approved account, publisher ID, slot IDs, and site approval. Do not put placeholder IDs in production. Approval and earnings are not guaranteed.

Set these environment variables in your hosting dashboard (recommended) or define them before starting the server:

- `ADSENSE_CLIENT_ID`: your real publisher ID, e.g. the `ca-pub-...` value supplied by AdSense.
- `ADSENSE_SLOT_TOP`: numeric ad slot ID for the top placement.
- `ADSENSE_SLOT_BOTTOM`: numeric ad slot ID for the bottom placement.
- `DIRECT_AD_TOP_TITLE`, `DIRECT_AD_TOP_TEXT`, `DIRECT_AD_TOP_URL`, optional `DIRECT_AD_TOP_IMAGE`.
- `DIRECT_AD_BOTTOM_TITLE`, `DIRECT_AD_BOTTOM_TEXT`, `DIRECT_AD_BOTTOM_URL`, optional `DIRECT_AD_BOTTOM_IMAGE`.

Direct sponsor URLs and image URLs must use HTTPS. Sponsor links open in a new tab and are marked as sponsored. If neither ad method is configured, clearly labelled placeholder areas are shown instead. Follow AdSense policies and local law, and publish a privacy policy/cookie disclosures where required. Never click your own ads or ask visitors to click ads.

### Quick local PowerShell setup for AdSense (optional)

Replace the sample values with the actual values from your AdSense account before starting Flask:

```powershell
$env:ADSENSE_CLIENT_ID = 'ca-pub-REPLACE_WITH_YOUR_ID'
$env:ADSENSE_SLOT_TOP = 'REPLACE_WITH_TOP_SLOT_ID'
$env:ADSENSE_SLOT_BOTTOM = 'REPLACE_WITH_BOTTOM_SLOT_ID'
python app.py
```

Do not use the placeholder strings as real IDs.

## Quality detection notes

The app lists only heights exposed by yt-dlp for the current public video. 1440p is labelled 2K and 2160p is labelled 4K; neither is shown unless detected. A source may offer only lower resolutions. Some high-quality downloads require FFmpeg. This does not upscale video or create a true 2K/4K source where none exists.

## Important before public deployment

- Use only videos you own or have permission to save; respect creator rights and TikTok's terms.
- This is a starter application. Public deployment needs rate limiting, HTTPS, monitoring, worker/job limits, request timeouts, and security review. The in-memory job tracker is for a single-process starter setup.
- Do not run Flask's development server in production. The included `Procfile` uses Waitress.
- Keep dependencies updated and check the platform rules before deploying a downloader publicly.
