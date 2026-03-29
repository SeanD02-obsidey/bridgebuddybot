# <p align="center">BridgeBuddy Bot

## Introduction

This is an asynchronous one-way Telegram API to Google Sheets bridge implementation on Python using [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot) and native Google Sheets API. Currently hosted on [Render](render.com).

## Features

- Chat updates through webhooks
- Telegram attachment* support
- Exponential backoff
- Caching & flushing system to comply with usage limits (also flushes data in case of shutdown)
- Batch uploads thanks to the aforementioned feature
- Lightweight [uvicorn](https://github.com/Kludex/uvicorn)/[starlette](https://github.com/Kludex/starlette) server for handling HTTP requests
- Secrets obtained through at-deploy-time provided .env

## Setup

### Requirements

- [uv](https://github.com/astral-sh/uv) – lightweight python package manager
- Python – tested and built on `3.14.3`, other versions may or may not work

### Instructions

1) Clone repository

```
git clone https://github.com/SeanD02-obsidey/bridgebuddybot
cd bridgebuddybot
```

2) Install and sync dependencies with **uv**

```
uv install && uv sync
```

3) Run main.py

```
python bridgebuddybot/main.py
```

## Configuration

Currently, Bridgebuddy is managed through the following environment variables:

|        Key        | Type |     Default value    |                                                                                          Description                                                                                         |
|:-----------------:|:----:|:--------------------:|:--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------:|
| URL               |  str | $RENDER_EXTERNAL_URL | Website address that Bridgebuddy is running on                                                                                                                                               |
| PORT              |  int |         8443         | Telegram Bot API accepts ports 443, 80, 88, 8443                                                                                                                                             |
| TG_API_TOKEN      |  str |         None         | Import your own Telegram bot token obtained from @BotFather                                                                                                                                  |
| GS_SERVICE_JSON   |  str |  "credentials.json"  | Path/to/somecredentials.json for the Google Cloud service account obtained from [Google Cloud Dashboard](https://console.cloud.google.com/apis/)                                             |
| GS_SPREADSHEET_ID |  str |         None         | Unique Sheets spreadsheet ID obtainable from the address bar (create desired spreadsheet beforehand; ex: https://docs.google.com/spreadsheets/d/**01234abcdefghthatsyourIDgrabit**/edit?...) |

Additionally, the following in-code variables are available for fine-grained flush control, as well as for setting the maximum file upload size and maximum backoff ceiling:

|       Key      | Type | Default value |                                                               Description                                                              |
|:--------------:|:----:|:-------------:|:--------------------------------------------------------------------------------------------------------------------------------------:|
| MAX_FILE_SIZE  |  int |     50000     | Maximum file size in bytes for upload to the Telegram bot                                                                              |
| FLUSH_INTERVAL |  int |       30      | Seconds between cache flushes; consider that GSheets API limits read/write requests to 300/min. per user, 60/min. per user per project |
| MAX_BACKOFF    |  int |       64      | Seconds, ceiling for exponential backoff                                                                                               |
## References

- https://core.telegram.org/bots/api
- https://developers.google.com/workspace/sheets

## License

MIT License

Copyright 2026 Sean Doyer and other contributors

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
