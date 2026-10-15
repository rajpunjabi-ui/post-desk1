# Post Desk

A private website for scheduling and publishing to many Instagram accounts
(reels, images, carousels, stories) through Meta's official Instagram Graph API.

What it does:
- Connect Instagram accounts directly with Instagram login. No Facebook Page needed
- Logins renew automatically every 60 days
- Upload videos and images directly. The site hosts them so Instagram can fetch them
- Create one post with a separate version per page (own file, caption and time)
- Queue grouped by day, with Post now, Remove, and automatic retries (3 attempts)
- History of everything posted, with links to the live posts
- Bulk import from a CSV / Google Sheet export
- Pause a page, test all connections, and see each page's daily posting limit
- Password-protected, with a hashtag counter that warns above 5

---

## Step 1: Put the site online (Railway, about 10 min)

Instagram must be able to download your files, so the site needs a public https address.

1. Put this folder in a GitHub repository (private is fine).
2. Go to railway.app, then New Project > Deploy from GitHub repo, and pick it.
3. In the service, open Settings > Networking > **Generate Domain**. Copy the address
   (e.g. `https://post-desk-production.up.railway.app`).
4. Add a **Volume**, mounted at `/data`. This keeps your database and uploads when the site restarts.
5. Under **Variables**, add everything from `.env.example`:
   - `ADMIN_PASSWORD` is your login password
   - `SECRET_KEY` is any long random text
   - `PUBLIC_BASE_URL` is the domain from step 3
   - `DATA_DIR=/data`
   - `TIMEZONE=Asia/Kolkata`
   - `META_APP_ID` and `META_APP_SECRET` come from step 2 below
6. Railway redeploys. Open the domain and log in.

Render, a VPS, or any Docker host also work (`Procfile` and `Dockerfile` are included).
Keep it to **one** web worker, since the scheduler runs inside it.

## Step 2: Create the Meta app (about 15 min, one time)

No Facebook Page is needed. You only need a Facebook account to log in to the developer site.

1. Every Instagram account must be **Business or Creator**
   (Instagram > menu > Account type and tools > Switch to professional account).
2. Go to developers.facebook.com, then My Apps > Create App. Choose the use case
   **Manage messaging & content on Instagram** (if not shown, choose Other, then Business,
   and add the **Instagram** product).
3. Open **Instagram > API setup with Instagram login**. At the top, copy the
   **Instagram app ID** and **Instagram app secret** into INSTAGRAM_APP_ID and INSTAGRAM_APP_SECRET.
   These are different from the main Meta App ID.
4. In the section **Set up Instagram business login**, open Business login settings and add the
   OAuth redirect URI: `https://YOUR-DOMAIN/oauth/callback`. Save.
5. While the app is in Development mode, each Instagram account must be a tester:
   App roles > Roles > Add People > **Instagram Tester**, enter the username.
   Then in that Instagram account (on instagram.com): Settings > Apps and websites >
   **Tester invites** > Accept. Do this for every account.

## Step 3: Connect your accounts

Pages > **Connect an Instagram account**. Log in with one account and allow access.
Then log out of instagram.com in that browser and click Connect again for the next account.
Click **Test all connections** at the end.

Alternative: in the Meta app, under API setup > Generate access tokens, click Add account,
log in, copy the token, and paste it in Pages > "Connect with an access token".

---

## Daily use
- **New post**: pick a type, then for each page choose a file, caption and time.
  Use *Add another page* for more versions. Leave the time empty to post immediately.
- **Queue**: everything coming up. The site checks every 20 seconds and publishes
  posts whose time has passed.
- **Import**: upload a CSV (see the sample on that page) to schedule dozens of posts at once.

## Good to know
- Reels: MP4 or MOV, H.264, 9:16. Images are converted to JPEG automatically.
- Uploads are stored on the volume. Delete old files from `/data/media` now and then if space runs low.
- Instagram limits how many posts each account can publish through the API per 24 hours.
  *Test all connections* shows each page's usage.
- If a post fails 3 times it moves to History > Failed with the reason. Fix it and press *Retry now*.
- Instagram logins last 60 days. Post Desk renews them automatically. If one ever stops
  working (for example after a password change), click *Connect* and log in to that account again.
- Never share your `.env` or the `/data` folder. They give access to all your pages.

## Run on your own computer (for testing)
```
pip install -r requirements.txt
cp .env.example .env      # fill it in; DATA_DIR can be ./data
python app.py             # open http://localhost:8000
```
Uploads won't reach Instagram from localhost. Use media links, or a tunnel such as
`cloudflared tunnel --url http://localhost:8000` and set PUBLIC_BASE_URL to the tunnel address.
