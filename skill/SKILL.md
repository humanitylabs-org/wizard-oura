---
name: wizard-oura
description: Use when the owner asks about his Oura connection, Oura setup or sync. Health questions go to Health.
version: 0.1.1
author: Humanity Labs
license: GPL-3.0-only
metadata:
  hermes:
    tags: [oura, health-data, connector]
    related_skills: [wizard-health]
---
# Oura (connector)

The box's Oura Ring connection (`health-data` spot, contract `wizard.health-data/1`). It holds the Oura sign-in and a synced copy; the **Health** app (`/wizard-health/`) shows the data and answers health questions. Tools: `mcp__wizard__wizard_oura__<tool>`.

- Questions about readiness, sleep, HRV, activity, workouts or trends: use the Health app's tools, not these.
- "Is my Oura connected / when did it last sync": `health_data_status`. `connected`, `syncing` or `rate_limited` = data is flowing; `not_configured` or `needs_login` = the owner must finish or redo setup. `setup.not_shared` lists data the owner did not tick at Oura's consent page (blood oxygen, workouts).
- Setup, reconnecting or replacing keys: send the owner the app link `[Oura](/wizard-oura/)` and say its screen walks him through it (five steps: ring and membership, Oura's developer site, create an application with the copy buttons, paste Client ID and Secret, Connect). He needs his own free Oura developer application; Oura ended personal access tokens in December 2025, so there is no token-paste shortcut. Never walk him through Oura's site in chat, and never ask for or accept a Client ID, Client Secret, sign-in code or callback address in chat; those go only into the app's own screen.
- Oura has no strain. The connector's `load` is an activity effort on a 0-21 scale estimated from Oura's MET minutes (formula in `docs/health-data-contract.md`); Oura's own Activity score is in `extras.oura.activity_score`. Oura gives no sleep need, so Health judges Oura sleep by hours.
- `health_data_sync` asks for fresh Oura data now (the app also syncs every 20 minutes); it returns at once. Sleep reaches Oura only after the owner opens the Oura phone app.
- `health_data_days` is the raw contract read (normalized days, paged, `changed_since` cursor). Use it only to check what the connector holds, e.g. when Health looks wrong.
- Scripts on the box can read the same contract at `http://127.0.0.1:8828/api/status` and `/api/days`.
