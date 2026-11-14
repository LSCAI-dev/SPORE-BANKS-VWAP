# SG Banks — Anchored VWAP · MACD (12,26,9) · Stochastic (14,3,3)

Daily charts for **DBS (D05)**, **OCBC (O39)** and **UOB (U11)** with:
- Anchored VWAP from the last dividend announcement (editable anchor date + **Update chart** button), with ±1σ / ±2σ bands
- MACD 12, 26, 9
- Stochastic 14, 3, 3 (80/20 lines)

It is hosted free on **GitHub Pages**. A **GitHub Action** fetches fresh prices from Yahoo Finance every weekday at **18:20 SGT** (after the SGX close), so the charts stay current. It installs like an app on iPhone and PC.

---

## Files

| File | Purpose |
|---|---|
| `index.html` | The app (charts, indicators, anchor-date box) |
| `data.json` | Price data (overwritten by the Action each weekday) |
| `scripts/update_data.py` | Pulls 1 year of daily OHLCV via `yfinance` |
| `.github/workflows/update-data.yml` | Schedules the data update and commits `data.json` |
| `manifest.webmanifest`, `icons/` | Lets the site install as a home-screen / desktop app |
| `.nojekyll` | Tells GitHub Pages to serve files as-is |

---

## Part 1 — Put it on GitHub (about 10 minutes, all in the browser)

1. **Create the repository**
   - Go to https://github.com/new
   - Repository name: `sg-banks` (any name works)
   - Set it to **Public**. GitHub Pages is free for public repositories; private repositories need a paid plan for Pages.
   - Tick **Add a README file**, then click **Create repository**.

2. **Upload the files**
   - Unzip `sg-banks-app.zip` on your PC.
   - In the new repository, click **Add file → Upload files**.
   - Drag in **everything inside** the unzipped folder: `index.html`, `data.json`, `manifest.webmanifest`, `README.md`, `.nojekyll`, and the `icons`, `scripts` and `.github` folders.
   - On Windows File Explorer: turn on **View → Show → Hidden items** first, or `.github` and `.nojekyll` won't show. On Mac Finder: press `Cmd + Shift + .` to show them.
   - Click **Commit changes**.
   - Check that the repository now shows a `.github/workflows/update-data.yml` file. If the folder didn't upload, create it with **Add file → Create new file**: type `.github/workflows/update-data.yml` as the name and paste in the file's contents.

3. **Let the Action save data**
   - Go to **Settings → Actions → General**.
   - Under **Workflow permissions**, choose **Read and write permissions** and click **Save**.

4. **Turn on GitHub Pages**
   - Go to **Settings → Pages**.
   - Set **Source** to **Deploy from a branch**, then **Branch** to `main` and folder `/ (root)`.
   - Click **Save**.
   - After about 1 minute, the page shows your link: `https://<your-username>.github.io/sg-banks/`

5. **Run the first data update**
   - Go to the **Actions** tab. If asked, click **I understand my workflows, go ahead and enable them**.
   - Click **Update bank data** in the left list, then **Run workflow → Run workflow**.
   - Wait about 1 minute for the green tick. `data.json` is now fresh, and Pages redeploys on its own about 1 minute later.

From then on, it updates by itself every weekday at 18:20 SGT. To refresh at another time, run step 5 again.

---

## Part 2 — Install on iPhone

1. Open **Safari** (it must be Safari for "Add to Home Screen" to work on iOS) and go to `https://<your-username>.github.io/sg-banks/`.
2. Tap the **Share** button (square with an up-arrow).
3. Scroll down and tap **Add to Home Screen**, then tap **Add**.
4. An **SG Banks** icon appears. It opens full-screen, without the Safari address bar.

Tips:
- Turn the phone to landscape for wider charts.
- Pinch or drag on a chart to zoom and scroll; all three panels move together.
- Tap a bank card at the top to switch between DBS, OCBC and UOB.
- Tap **↻ Reload data** after 18:20 SGT to pull in the day's bar.

## Part 3 — Install on PC (Windows or Mac)

**Chrome or Edge (recommended):**
1. Open the link.
2. Click the **install icon** at the right end of the address bar (a monitor with a down-arrow), or open the **⋮ menu**. In Chrome choose **Cast, save and share → Install page as app**; in Edge choose **Apps → Install this site as an app**.
3. It now has its own window, a Start menu / Dock entry, and can be pinned to the taskbar.

**Safari on Mac (Sonoma or newer):** choose **File → Add to Dock**.

---

## Using the anchor date box

- Each bank's box starts at its **default anchor**, the latest dividend announcement date.
- To change it, type a date as `YYYY-MM-DD` and press **Update chart** (or Enter). The box turns amber when you've typed a change that isn't applied yet, and red if the date is invalid or outside the data range.
- If the date is a weekend or holiday, the VWAP starts from the next trading day.
- **Reset to default** restores the announcement date.
- A custom anchor is remembered on that device and browser.

### Updating the default anchors after new results

When a bank announces a new dividend, edit `index.html` on GitHub: click the file, click the ✏️ pencil, find the `BANKS` list near the top of the script, change the `anchor:` date and `ev:` text, and click **Commit changes**. The site updates in about 1 minute.

```js
const BANKS = [
  {sym:'D05.SI', nm:'DBS',  anchor:'2026-11-05', ev:'3Q26 results — 81¢ ...'},
  {sym:'O39.SI', nm:'OCBC', anchor:'2026-08-07', ev:'1H26 results — 47¢ interim ...'},
  {sym:'U11.SI', nm:'UOB',  anchor:'2026-08-07', ev:'1H26 results — 88¢ interim ...'}
];
```

---

## Run it on your own PC (optional)

```bash
pip install yfinance pandas
python scripts/update_data.py          # refresh data.json
python -m http.server 8000             # then open http://localhost:8000
```

Opening `index.html` by double-clicking won't work, because browsers block `data.json` from loading off `file://`. Use the small server above.

## Troubleshooting

| Problem | Fix |
|---|---|
| Page shows 404 | Wait 1–2 minutes after enabling Pages. Check that **Settings → Pages** shows `main` / `(root)` and that `index.html` sits at the top level, not inside a sub-folder. |
| "Could not load data.json" | `data.json` must be at the top level next to `index.html`. |
| Action fails at "git push" (error 403) | Set **Settings → Actions → General → Workflow permissions** to **Read and write**. |
| Action fails at "Fetch latest prices" | Yahoo occasionally rate-limits. The old data stays in place; re-run the workflow later. |
| Scheduled runs stopped | GitHub pauses scheduled workflows after 60 days with no repository activity. Re-enable them from the **Actions** tab, or commit any small change. |
| iPhone still shows old data | Tap **↻ Reload data**, or close and reopen the app. |

Notes: prices are unadjusted (not adjusted for dividends). AVWAP uses typical price (H+L+C)/3 weighted by volume. For information only — verify against your broker terminal before acting.
