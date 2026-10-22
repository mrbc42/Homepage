# Home Base

A colourful start page for a homelab: live weather (Open-Meteo), Hacker News headlines,
and grouped links to self-hosted apps with up/down status dots. One file, no build step,
no API keys.

## Customise

Open `index.html` and edit the `CONFIG` block near the bottom:

- `location`: name, latitude and longitude for the weather
- `units`: `celsius`/`fahrenheit` and `kmh`/`mph`
- `apps`: one line per app

```js
{ name: "Jellyfin", url: "http://192.168.1.10:8096", group: "Media", colour: "violet", icon: "jellyfin" },
```

`colour` is one of: tangerine, berry, teal, lime, sky, sun, violet, coral.
`icon` is a name from https://dashboardicons.com, a full image URL, or left out to show the first letter.

Apps added with the **+ Add app** button are saved in that browser only. Use **Copy config**
to get lines you can paste into `CONFIG`.

## Host it

Any static web server works. With Docker:

```sh
docker run -d --name homepage --restart unless-stopped \
  -p 8080:80 -v "$PWD/index.html:/usr/share/nginx/html/index.html:ro" nginx:alpine
```

Then open http://<server-ip>:8080.

Status dots work best when the page is served over plain `http://` on your network.
Browsers block checks from an `https://` page to `http://` apps, which makes them show as down.
