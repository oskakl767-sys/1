# Agent APK Directory

This directory contains `agent.apk` — the hidden MDM agent APK.

The file is automatically uploaded here by the build workflow in repo
`oskakl767-sys/11` after each build. The Flask server serves it via
the `/agent.apk` route.

Do NOT commit `agent.apk` manually — let the build workflow handle it.
