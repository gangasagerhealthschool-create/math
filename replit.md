# Discord Invite Tracking Bot

## Overview

This is a Discord bot built with Python and discord.py that provides comprehensive invite tracking, giveaway management, and server administration tools for Discord servers. The bot tracks member invitations, manages server events, and provides detailed analytics through Discord slash commands. It's designed to run on cloud hosting platforms like Replit with persistent SQLite data storage.

## User Preferences

Preferred communication style: Simple, everyday language.

## System Architecture

### Backend Architecture
- **Language**: Python 3.11 with discord.py framework
- **Architecture Pattern**: Event-driven bot with modular command structure using Discord slash commands
- **Bot Framework**: Discord.py with full intents for comprehensive server monitoring and member tracking
- **Database Layer**: Custom SQLite wrapper class with threading locks for concurrent access safety
- **Deployment**: Flask-based keep-alive server designed for hosting on platforms like Replit

### Core Design Decisions

**Event-Driven Invite Tracking**: The bot implements sophisticated invite tracking by maintaining invite caches and comparing Discord invite statistics before and after member joins. This approach handles Discord's invite system limitations and provides accurate attribution of new members to their inviters.

**Threaded Database Access**: The Database class uses threading locks to ensure data consistency when multiple operations access SQLite concurrently. This prevents race conditions during high-activity periods when multiple Discord events trigger database operations simultaneously.

**Configuration-Driven UI**: Colors, emojis, and message templates are centralized in configuration dictionaries (COLORS and EMOJIS), allowing easy theming and consistent visual presentation across all bot responses and embeds.

**Stateless Command Design**: Each command operates independently without maintaining session state, making the bot resilient to restarts and scaling horizontally if needed.

### Database Schema
- **user_invites**: Tracks invite statistics per user per guild (total, left, fake, bonus, claimed invites)
- **invite_codes**: Stores Discord invite codes with metadata (creator, usage counts, creation timestamps)
- **invite_relationships**: Records who invited whom with join timestamps for detailed analytics
- **giveaways**: Manages giveaway events with participant tracking and winner selection
- **guild_settings**: Stores per-server configuration (welcome channel, mod log channel, staff log channel)
- **role_permissions**: Manages command permissions per role

### Bot Event System
- **Member Join Events**: Automatically tracks invite usage and updates statistics
- **Invite Create/Delete Events**: Maintains synchronized invite cache for accurate tracking
- **Message Events**: Handles command processing and automated responses
- **Error Handling**: Global error handler prevents bot crashes and logs issues for debugging

## Key Commands

### Ping Command
- `/ping` - Ping specific alert roles with custom messages
  - **Quickdrop Ping**: For quickdrop announcements
  - **Giveaway Ping**: For giveaway announcements
  - **Server Booster**: For booster-exclusive events
- Features autocomplete for role selection

### Invite Tracking
- `/invites` - Check invite stats for a user
- `/leaderboard` - View server invite leaderboard
- `/syncinvites` - Sync historical invite data

### Giveaway Management
- `/gcreate` - Create a new giveaway (results show after 1 second when ended)
- `/glist` - List active giveaways
- `/gend` - End a giveaway early
- `/greroll` - Reroll giveaway winners

### Staff Management
- `/promote` - Promote a user with logging (uses server custom :arrowup: emoji)
- `/demote` - Demote a user with logging (uses server custom :arrowdown: emoji)
- `/addclaims` / `/removeclaims` - Manage user claims

### Server Configuration
- `/setwelcome` - Set welcome channel
- `/setstafflog` - Set staff log channel
- `/setmodlogs` - Set mod log channel
- `/addcmdperm` / `/removecmdperm` - Manage command permissions
- `/listcmdperm` - List all command permissions

### Moderation Commands
- `/purge` - Delete up to 100 messages from a channel
- `/lock` - Lock the current channel for non-staff
- `/unlock` - Unlock the current channel for everyone
- `/membercount` - Show server member statistics (total, humans, bots)

### Games
- `/split_steal` - Start a Split or Steal game between two players

## External Dependencies

### Core Dependencies
- **Discord.py**: Primary bot framework for Discord API interaction
- **SQLite3**: Built-in Python database for local data persistence
- **Flask**: Lightweight web server for keep-alive functionality on hosting platforms
- **Threading**: Python standard library for concurrent database access management

### Environment Configuration
- **DISCORD_TOKEN**: Environment variable containing the Discord bot token for API authentication
- **Replit Hosting**: Configured with Flask keep-alive server to maintain bot uptime on Replit's hosting platform

### Discord API Integration
- **Full Discord Intents**: Bot requires all intents for comprehensive member tracking and invite monitoring
- **Slash Commands**: Modern Discord command interface using app_commands for better user experience
- **Event Webhooks**: Real-time Discord event processing for invite tracking and member management

## Recent Changes

- **Updated /ping command**: Role-based ping system supporting Quickdrop Ping, Giveaway Ping, and Server Booster roles with custom messages and autocomplete
- **Updated /promote and /demote**: Now use server custom emojis :arrowup: and :arrowdown:
- **Faster giveaway results**: Giveaways now check every 1 second instead of 1 minute for near-instant results
- **Added /purge command**: Delete messages from channels with mod log integration
- **Added /membercount command**: Display server member statistics
- **Added /lock and /unlock commands**: Channel moderation tools
- **Added /split_steal command**: Split or Steal game for two players
