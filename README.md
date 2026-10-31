# Oura (Wizard connector)

A Wizard OS connector app for one Oura account. It holds the Oura sign-in, keeps a private synced copy of daily readiness, daily sleep, sleep periods, daily activity, workouts and daily SpO2, and offers that copy read-only to other apps on the same box through the `health-data` spot, contract [`wizard.health-data/1`](docs/health-data-contract.md). The [Health](https://github.com/oscarhenrycollins/wizard-health) app is the screen for the data; this app is only the connection.

**You need your own Oura developer application and its credentials (Client ID and Client Secret).** It is free to create with any Oura account. Oura ended personal access tokens in December 2025, so OAuth with your own application is the only way in. Oura shares data only while the Oura Membership is active.

## Setup (all on the app's own screen)

1. An Oura Ring set up in the Oura app, with an active Oura Membership.
2. Open https://developer.ouraring.com/applications (older accounts: https://cloud.ouraring.com/oauth/applications) and sign in with your Oura email (Oura sends a one-time code).
3. Create New with the values the screen shows, each with a Copy button: display name, description, your contact email, this page's address for Website / Privacy policy / Terms of service, the Redirect URI, and the scopes `email daily workout spo2`. Skip webhooks. Agree to the Oura API Agreement, Create, then View details.
4. Paste the Client ID and Client Secret (stored server-side, never echoed). The app tests them by exchanging a made-up code at Oura's token endpoints (`invalid_grant` = keys valid, `invalid_client` everywhere = wrong).
5. Connect with Oura: the consent page opens in a new tab. With the box's own https Redirect URI (`https://<box>/wizard-oura/callback`, the default when Wizard is opened over https) Oura brings the browser straight back and the app finishes by itself. With `http://localhost:3000/callback` the page won't load; paste its address back into the app. The history downloads (90-day windows back until Oura has nothing older), then the app syncs every 20 minutes with a 4-day overlap.

## Privacy

This app runs on your own Wizard box. Your Oura data goes from Oura to your box only; nothing is sent to Humanity Labs or anyone else. The Client Secret and sign-in tokens are stored on the box in files only the app can read, and are never shown again or returned by any API. Disconnect revokes the sign-in at Oura and deletes the local copy; you can also delete the application on Oura's developer site at any time.

## Terms

Provided under GPL-3.0-only, with no warranty. You use Oura's API under your own Oura account and the Oura API Agreement you accepted when creating your application. Not affiliated with or endorsed by Oura.

## How Oura maps onto the contract

- Readiness = Oura daily readiness score (0-100), banded 67/34. HRV = main night's `average_hrv`; resting HR = its `lowest_heart_rate`; skin temperature delta = readiness `temperature_deviation`; SpO2 = daily average.
- Sleep = the day's longest `long_sleep` period with the daily sleep score; other sleeps are naps, `rest` periods are ignored. Oura gives no sleep need (`need_h` is `null`), so Health judges Oura sleep by hours.
- Load = **activity effort on 0-21** (`scale_max: 21`) estimated from Oura's MET minutes, because Oura's Activity score (1-100) grades balance, not effort. Formula and anchors: `docs/health-data-contract.md`, "Provider notes". Oura's Activity score is kept in `extras.oura`.
- Workouts: activity, times, calories, distance; intensity and source in `extras.oura` (Oura gives no workout heart rate or load).

## Interfaces

- Screen: `/wizard-oura/` (status, connected account, Sync now, Disconnect, guided setup).
- Contract (box-only: refused when it arrives through the private-access proxy): `GET /api/status`, `GET /api/days?from&to&limit&page&changed_since`.
- Setup API used by the screen: `GET /api/setup`, `POST /api/keys`, `/api/keys/test`, `/api/keys/forget`, `/api/oauth/start`, `/api/oauth/finish`, `/api/sync`, `/api/disconnect`; `GET /callback` for the https Redirect URI.
- MCP (`POST /mcp`, box-only): `health_data_status`, `health_data_sync`, `health_data_days`.

## Development

`python3 -m unittest discover -s tests` runs the tests against a fake Oura built from the API v2 docs and Oura's sandbox payloads; `OURA_LIVE_TEST=1` also checks that Oura's real token endpoints refuse made-up credentials. `python3 tools/package.py` refreshes the inline Runtipi payload; `python3 tools/package.py --check` verifies it.

No live Oura account has been used to test this app yet: the sign-in, token refresh and data reads are built from Oura's documentation and checked against the fake.
