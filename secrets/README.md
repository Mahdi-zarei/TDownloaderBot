# secrets/

Mounted read-only at `/secrets` in the `bot` container. Everything here except this README is
git-ignored. Never use your main accounts: these cookies can get the account banned.

| File | Used by | What |
| --- | --- | --- |
| `youtube-cookies.txt` | bot (`YOUTUBE_COOKIES`) | Cookies of a burner Google account, Netscape format. YouTube retries once with them after a sign-in / bot-check error. |
| `instagram-cookies.txt` | bot (`INSTAGRAM_COOKIES`) | Cookies of a burner Instagram account, Netscape format. Always used for Instagram; photo posts and carousels need it. |

## Exporting cookies

- Netscape format means the `cookies.txt` layout whose first line is `# Netscape HTTP Cookie File`
  (for example the "Get cookies.txt LOCALLY" browser extension). Export only the site you need; avoid
  `yt-dlp --cookies-from-browser`, which writes every site's cookies into the file.
- YouTube rotates cookies of open sessions. Log in to the burner account in a private window, open
  `https://www.youtube.com/robots.txt`, export the youtube.com cookies, then close the window without
  logging out.
- Instagram: log in to the burner account, export the instagram.com cookies, close the window without
  logging out. The file must contain a `sessionid` line, or the export happened logged out.
- The directory is bind-mounted, so the bot's next job sees a replaced cookie file without a restart.
  `/selftest` shows whether each cookie file is present and logged in.

```sh
chmod 700 secrets && chmod 600 secrets/*
```
