# Vendor JavaScript

These third-party libraries are fetched at install time by `scripts/fetch_vendor.sh`.
They are not committed to git.

| File              | What it is        | Used on             |
| ----------------- | ----------------- | ------------------- |
| socket.io.min.js  | Socket.IO 4.x     | every page          |
| chart.min.js      | Chart.js 4.x      | dashboard trend     |
| leaflet.js        | Leaflet 1.9.x     | dashboard map       |
| leaflet.css       | Leaflet styles    | dashboard map       |

If a file is missing the page will still load (the relevant feature
will be disabled), so a fresh checkout is usable for development even
without an internet connection.
