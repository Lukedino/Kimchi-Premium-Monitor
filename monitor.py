name: Kimchi Premium Monitor

on:
  schedule:
    - cron: '*/15 * * * *'
  workflow_dispatch:

jobs:
  monitor:
    runs-on: ubuntu-latest
    timeout-minutes: 5

    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Setup Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: Install dependencies
        run: pip install -r requirements.txt

      - name: Run monitor
        env:
          RUN_MODE: ${{ github.event_name }}
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
          GIST_TOKEN: ${{ secrets.GIST_TOKEN }}
          GIST_ID: ${{ secrets.GIST_ID }}
          USDT_KIMP_LOW: ${{ vars.USDT_KIMP_LOW }}
          USDT_KIMP_HIGH: ${{ vars.USDT_KIMP_HIGH }}
          GOLD_KIMP_LOW: ${{ vars.GOLD_KIMP_LOW }}
          GOLD_KIMP_HIGH: ${{ vars.GOLD_KIMP_HIGH }}
          ALERT_GAP: ${{ vars.ALERT_GAP }}
        run: python monitor.py
