// Copy this file to config.local.js and put your own details there.
// config.local.js is listed in .gitignore, so it never gets committed.
// Anything you leave out keeps the default from index.html.
window.HOMEPAGE_CONFIG = {
  location: { name: "Your town", latitude: 0, longitude: 0 },
  units: { temperature: "celsius", wind: "kmh" },
  openInNewTab: false,

  // colour: tangerine · berry · teal · lime · sky · sun · violet · coral
  // icon:   a name from https://dashboardicons.com, a full image URL, or leave it out
  apps: [
    { name: "Home Assistant", url: "http://homeassistant.local:8123", group: "Home",  colour: "sky",    icon: "home-assistant" },
    { name: "Jellyfin",       url: "http://192.168.1.10:8096",       group: "Media", colour: "violet", icon: "jellyfin" },
    { name: "Proxmox",        url: "https://192.168.1.2:8006",       group: "System", colour: "tangerine", icon: "proxmox" },
  ],
};
