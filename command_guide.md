#  Fantasy CS Bot — Command Reference

A quick overview of all available slash commands and what they do.  
*(Useful for remembering how to refresh stats, teams, leaderboards, etc.)*

---

##  Account Commands
- **/account register `<steamid>`**  
  Links your Discord account to your Steam64 ID.  
  Creates a user record and player entry if one doesn’t exist.  
  Also stores your Discord handle (nickname, username, global name).  
  → Must be done before joining teams or earning points.

- **/account unlink** *(if implemented)*  
  Removes your linked Steam ID from your account.

---

##  Team Commands
- **/team create `<name>`**  
  Creates a new fantasy team for your Discord guild.

- **/team join `<team>`**  
  Adds you to an existing team if it isn’t full.

- **/team leave**  
  Removes you from your current team.

- **/team show `[user]`**  
  Displays your team (or another member’s team if specified).  
  Includes player roles, average ratings, and match stats.

- **/team delete** *(Admin)*  
  Deletes a team from the guild (use with caution).

---

##  Leaderboard Commands
- **/leaderboard show**  
  Displays the current leaderboard for your Discord guild, sorted by weekly points.

- **/leaderboard weekly** *(optional alias)*  
  Shows the leaderboard for this week’s matches only.

---

##  Player Commands
- **/player stats `<player>`**  
  Shows recent performance for a player (ratings, ADR, kills, etc.) from the database.

- **/player search `<steamid>`**  
  Looks up a Steam profile to check if it’s linked and has stats recorded.

---

##  Pricing Commands
- **/pricing update** *(Admin)*  
  Recomputes all player prices based on:
  - Long-term Leetify rating (last 100 games)
  - Premier rating
  - Faceit ELO (if available)

- **/pricing show `[player]`**  
  Displays the current price(s) of a player or the top priced players.

---

##  Scoring & Stats Commands
- **/scoring update_stats `[member]` `[fetch=True]`** *(Admin)*  
  Recomputes `PlayerStats` and updates weekly fantasy points.  
  - `fetch=True`: also pulls new match data from the Leetify API.  
  - `fetch=False`: recomputes existing stored matches only.  
  Use this nightly or after new matches are played.

- **/scoring breakdown `<player>`** *(optional)*  
  Shows how a player’s fantasy points are calculated (kills, ADR, entry kills, etc.).

---

##  Maintenance / Utility
- **/commands**  
  Lists all available commands grouped by category (like this file).

- **/admin reset_weekly** *(Admin)*  
  Resets or starts a new week for leaderboard tracking.

---

###  Typical Workflows

**When new matches are played:**
1. `/scoring update_stats fetch:true`  
   → Fetches new matches + updates weekly points.

**When testing locally:**
1. `/account register` with your SteamID.  
2. `/team create` and `/team join`.  
3. `/leaderboard show` to confirm updates.

**When prices need refreshing:**
1. `/pricing update`  
   → Updates player prices from latest stats.

---

###  Daily Update Routine (Recommended Automation)

Goal: ensure all registered users have fully updated match + stat data,  
so any new `/account register` users automatically have fresh history.

**Suggested schedule:** every night at **00:05 Europe/London**

**Tasks to run (in order):**
1. **Fetch new matches and update stats**
    - `/scoring update_stats fetch:true`
    - This pulls any new matches for all registered users
    - Updates `PlayerGame`, `PlayerStats` and `WeeklyPoints`.
2. ** Recalculate player prices**
    - `/pricing update`
    - Uses the latest leetify, faceit, and premier data to refresh player prices
    - Keeps the fantasy market current

   
