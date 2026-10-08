"""How to connect each service, in plain words for the owner (the README's Credentials section).

Text in `backticks` is shown as code. {folder} becomes this project's folder.
"""
from pipeline.config import ROOT

FOLDER = str(ROOT)
ENV_FILE = str(ROOT / ".env")

SERVICES = {
    "youtube": {
        "fills": ["YT_CLIENT_SECRET_FILE"],
        "steps": [
            "Open https://console.cloud.google.com and sign in with the Google account that owns your YouTube "
            "channel. Create a new project (project picker at the top → New project), for example \"Graphony\".",
            "Open APIs & Services → Library. Find and enable \"YouTube Data API v3\", then \"YouTube Analytics API\".",
            "Open Google Auth Platform (also called \"OAuth consent screen\") → Get started. Enter an app name and "
            "your email, choose the audience External, and finish.",
            "Still in Google Auth Platform, open Audience and click Publish app, so the status reads "
            "\"In production\". Do not leave it in \"Testing\": in Testing, Google disconnects YouTube after 7 days. "
            "You do not need Google's app verification for your own channel.",
            "Open Clients → Create client, choose the type Desktop app, create it and click Download JSON. "
            "Save the file as `client_secret.json` in the `tokens` folder inside {folder}.",
            "Open `.env` and set `YT_CLIENT_SECRET_FILE=tokens/client_secret.json`. Save the file.",
            "Open a terminal window of its own: press Start, type `cmd` and press Enter. Type "
            "`cd /d \"{folder}\"` and press Enter, then type "
            "`.venv\\Scripts\\python.exe -m pipeline.publish_youtube --auth` and press Enter "
            "(this is `python -m pipeline.publish_youtube --auth` with Graphony's own Python).",
            "A browser opens. Sign in with the channel's Google account. When Google says \"Google hasn't verified "
            "this app\", click Advanced → Go to [your app name] (unsafe), then allow access. When the terminal says \"YouTube token "
            "saved\", close that terminal window. You only do this once.",
            "Apply for Google's YouTube API audit: fill in the \"YouTube API Services – Audit and Quota Extension\" "
            "form at https://support.google.com/youtube/contact/yt_api_form for this project. Until Google approves "
            "it, YouTube keeps every upload private, and Graphony tells you when that happens. This review is "
            "separate from step 4 and can take a few weeks; you can post in the meantime.",
            "Restart Dashboard.bat and Bot.bat, then press Test here.",
        ],
    },
    "instagram": {
        "fills": ["IG_USER_ID", "IG_ACCESS_TOKEN"],
        "steps": [
            "Make your Instagram account a professional account: in the Instagram app open Settings → "
            "Account type and tools → Switch to professional account (Business or Creator).",
            "Link it to a Facebook Page you manage (create a Page first if you have none): in Instagram open "
            "Edit profile → Page, or use Meta Business Suite → Settings.",
            "Go to https://developers.facebook.com and log in with the Facebook account that manages the Page. "
            "Open My Apps → Create app. When asked what the app is for, choose managing content on Instagram "
            "(\"Instagram API with Facebook Login\").",
            "Open the Graph API Explorer at https://developers.facebook.com/tools/explorer and pick your app at the "
            "top right. Under Permissions add `instagram_basic`, `instagram_content_publish`, "
            "`instagram_manage_insights`, `pages_read_engagement` and `pages_show_list` (also `pages_manage_posts` "
            "if you will post to Facebook). Click Generate Access Token and allow everything for your Page and "
            "Instagram account.",
            "Make the token long-lived: click the blue \"i\" next to the token → Open in Access Token Tool → "
            "Extend Access Token. Copy the new long-lived token.",
            "Back in the Graph API Explorer, paste the long-lived token, type `me/accounts` in the query box and "
            "click Submit. Next to your Page, copy its \"access_token\": this long-lived Page token is your "
            "`IG_ACCESS_TOKEN` (it does not expire). Also note the Page's \"id\".",
            "Query `<your Page id>?fields=instagram_business_account` and click Submit. The \"id\" inside "
            "instagram_business_account is your `IG_USER_ID`.",
            "Open `.env`, fill in `IG_USER_ID=` and `IG_ACCESS_TOKEN=`, and save. Restart Dashboard.bat and "
            "Bot.bat, then press Test here.",
        ],
    },
    "facebook": {
        "fills": ["FB_PAGE_ID"],
        "uses": ["IG_ACCESS_TOKEN"],
        "steps": [
            "Do the Instagram steps first: Facebook uses the same Meta app and the same long-lived Page token "
            "(`IG_ACCESS_TOKEN`).",
            "That token needs `pages_show_list` and `pages_manage_posts`. If you made it without them, repeat "
            "Instagram steps 4–6 with them ticked and replace `IG_ACCESS_TOKEN` in `.env`.",
            "Your Page ID is the \"id\" next to your Page in the `me/accounts` result (Instagram step 6). "
            "You can also find it on the Page under About → Page transparency.",
            "Open `.env`, set `FB_PAGE_ID=` to that number, and save.",
            "Turn Facebook on under Posting below. Restart Dashboard.bat and Bot.bat, then press Test here.",
        ],
    },
    "telegram": {
        "fills": ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"],
        "steps": [
            "In Telegram, open a chat with @BotFather and send `/newbot`. Choose a display name, then a username "
            "that ends in \"bot\".",
            "BotFather replies with a token that looks like `123456789:AAH...`. That is your `TELEGRAM_BOT_TOKEN`.",
            "Open a chat with your new bot and send it any message, for example \"hi\".",
            "In your browser open `https://api.telegram.org/bot<TOKEN>/getUpdates`, with your token in place of "
            "`<TOKEN>`. Find `\"chat\":{\"id\":` followed by a number: that number is your `TELEGRAM_CHAT_ID`. "
            "If the page shows an empty list, send the bot another message and reload.",
            "Open `.env`, fill in `TELEGRAM_BOT_TOKEN=` and `TELEGRAM_CHAT_ID=`, and save. Only this chat can "
            "approve videos.",
            "Restart Dashboard.bat and double-click Bot.bat (keep its window open), then press Test here.",
        ],
    },
    "gemini": {
        "fills": ["GEMINI_API_KEY"],
        "steps": [
            "Open https://aistudio.google.com/apikey and sign in with a Google account.",
            "Click Create API key and copy the key.",
            "Open `.env`, paste it after `GEMINI_API_KEY=`, and save.",
            "Restart Dashboard.bat and Bot.bat, then press Test here. Gemini only writes the short event labels; "
            "videos work without it.",
        ],
    },
}


def steps(service: str) -> list[str]:
    return [s.replace("{folder}", FOLDER) for s in SERVICES[service]["steps"]]
