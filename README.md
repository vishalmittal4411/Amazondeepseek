# Amazon Stock Tracker Bot 🚀

Telegram bot to track Amazon India product availability.

## Features
- Add products via Amazon links
- Check stock status
- List all tracked products
- Remove products
- 24/7 running on Render
- Auto-restart on crash

## Commands
- `/start` - Activate bot
- `/add` - Add product
- `/list` - Show products
- `/status` - Check stock status
- `/remove` - Remove product

## Deployed on Render
Bot runs 24/7 with auto-restart and health checks.

## Environment Variables
- `BOT_TOKEN` - Your Telegram bot token
- `DATABASE_URL` - PostgreSQL database URL
- `PYTHON_VERSION` - 3.11.8
