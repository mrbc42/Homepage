# Home Base

A colourful start page for a homelab: live weather, Hacker News headlines, and grouped
tiles for your self-hosted apps with online/offline status dots.

- **Settings in the page.** Use the gear button to set your location, units, search
  engine and headlines, and to add, edit, reorder and group apps.
- **Saved on your server.** Everything is stored in a SQLite database in a Podman volume.
  None of your details go into this repository.
- **Status checks run on the server.** They work for `http://` and `https://` apps, including
  ones with self-signed certificates.
- **Small.** A single Python file using only the standard library, in an Alpine image.

## Run it with Podman Quadlet

These steps run it rootless as your own user. You need Podman 4.4 or newer.

```sh
mkdir -p ~/.config/containers/systemd
curl -fsSL -o ~/.config/containers/systemd/homepage.container \
  https://raw.githubusercontent.com/mrbc42/homepage/main/quadlet/homepage.container
curl -fsSL -o ~/.config/containers/systemd/homepage.volume \
  https://raw.githubusercontent.com/mrbc42/homepage/main/quadlet/homepage.volume

systemctl --user daemon-reload
systemctl --user start homepage

# Keep it running after you log out and start it at boot
loginctl enable-linger "$USER"
```

Open `http://<server-ip>:8080`, click the gear, and set your location.

To change the port, edit `PublishPort=8080:8080` in `homepage.container` (for example `PublishPort=80:8080`
when running as root), then run `systemctl --user daemon-reload && systemctl --user restart homepage`.

### Updates

The image is rebuilt on every push to `main` and published to `ghcr.io/mrbc42/homepage:latest`.
The unit has `AutoUpdate=registry`, so you can turn on automatic updates with:

```sh
systemctl --user enable --now podman-auto-update.timer
```

Or update by hand with `podman auto-update`.

### Protect settings with a password (optional)

Anyone on your network can change settings unless you set a password:

```sh
printf 'your-password' | podman secret create homepage-admin -
```

Then uncomment the `Secret=` line in `homepage.container`, reload and restart.
The page itself stays open to read; only changes need the password.

## Build the image yourself

```sh
git clone https://github.com/mrbc42/homepage.git && cd homepage
podman build -t localhost/homepage:latest -f Containerfile .
```

Then set `Image=localhost/homepage:latest` in `homepage.container` and remove the `AutoUpdate=` line.

## Backups

Settings → Backup downloads a JSON file with all your settings and apps, and can restore one.
The database itself is in the `homepage-data` volume:

```sh
podman volume inspect homepage-data --format '{{.Mountpoint}}'
```

## Tips

- **`.local` addresses** (mDNS, like `homeassistant.local`) usually don't resolve inside containers,
  so the status dot shows offline even though the link works in your browser. Use the IP address in
  **Address to check** in the app's settings.
- **Icons** come from [dashboardicons.com](https://dashboardicons.com). Use the name from that site
  (for example `jellyfin` or `home-assistant`) or paste a full image URL.
- **An app that redirects to a login page on another host** can be given a separate address to check.

## Run without a container

```sh
HOMEPAGE_DATA=./data python3 app/server.py
```

| Variable | Default | Meaning |
| --- | --- | --- |
| `HOMEPAGE_DATA` | `/data` | Folder for `homepage.db` |
| `HOMEPAGE_PORT` | `8080` | Port to listen on |
| `HOMEPAGE_ADMIN_PASSWORD` | unset | Password needed to change settings |
| `HOMEPAGE_ACCESS_LOG` | unset | Set to `1` to log every request |

## Where the data comes from

Weather, air quality, tides and place search: [Open-Meteo](https://open-meteo.com) (free, no API key).
Tides are modelled sea level at the nearest sea point and are not for navigation.
Headlines: [Hacker News API](https://github.com/HackerNews/API).
