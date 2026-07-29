# Telegram Forwarding Bot (Private Admin-Only)

## Features
- Private bot (only admin IDs can use it)
- Video batching (albums of 10)
- Auto-flush after 60 seconds inactivity
- Auto-detect batch end
- Photo approval mode
- Progress messages
- JobQueue background watcher
- Railway-ready Docker deployment

## Commands
/start  
/help  
/settings  
/admin_commands  
/toggle_photo_mode  
/approve  
/reject  
/flush  

## Deployment
1. Add BOT_TOKEN in Railway → Variables  
2. Push this repo to GitHub  
3. Railway auto-builds using Dockerfile  
4. Deploy and enjoy
