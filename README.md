# mvp-fantasy-cs
A discord based fantasy CS league utilising Leetify.


# Idea

Server based fantasy leagues where player price is affected by their overall elo movement (weighted to prefer faceit > renown > premier > matchmaking). Users can buy and sell players within their budget with a minimum amount of 5 and maximum of 6.

Users can register through the bot by linking their steam account which then allows the bot to gather leetify data and constantly update if more than 6 hours have passed since last refresh.

Leagues run weekly where points are given to players based on their averages for matches that week. Players who play more games are rewarded with bonus points every x amount of wins to encourage more playing.

All stats are held in a local database which allows the seemless addition or removal of players to teams (if they meet the criteria). 

Fun game to play within small and large discord servers. Small discord servers is currently the aim as its all discord based. However, in the future I'd like to expand to a web platform which makes it easier to search players and add them compared to discord embeds.

